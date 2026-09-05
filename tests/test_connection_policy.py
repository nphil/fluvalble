"""Tests for connection-lifecycle hardening: hold_connection, backoff, the
connect-time clock sync, async_ensure_mode, and async_read_state.

Frozen surface confirmed by FluvalConn over hub:
- `Device.hold_connection` (bool property) backed by `self._hold_connection`,
  default True from config_data["hold_connection"]. Setter=False parks the
  client (ping_time=0, cancels ping_future, blocks new connects via a
  `_connection_parked()` guard in `_async_ensure_client`). Setter=True wakes
  it back up and calls `client.ping()`. Both propagate to a mirrored
  `Client.hold_connection` bool (new ctor param, default False so existing
  direct-Client tests are untouched); a Client is persistent when
  `active_time == 0 OR hold_connection is True`.
- `register_connection_listener(cb)`: NOT fired at registration, only on
  future `set_connected()` transitions; additive to updates_connect /
  updates_component; returns an unsubscribe closure.
- The automatic clock-before-first-status-read sequence is NOT a call to the
  public `async_sync_clock()` (that's the manual/button wrapper around the
  same primitives) — it is Client's phased `ready_callback` /
  `state_ready_callback` invoking `device._async_on_client_ready` /
  `device._async_on_client_state_ready`, which already exists and already
  re-arms every physical reconnect via `set_connected(False)` resetting
  `_clock_synced` / `_clock_sync_started`.
- `async_ensure_mode(mode)`: raises ValueError for a mode outside MODES;
  fast no-op returning `mode` with zero BLE traffic when already in that
  mode; otherwise sends the one mode packet, re-reads, and returns the
  freshly confirmed mode (which may differ from what was requested).
- Backoff: pure `reconnect_backoff_seconds(attempt)` in core/client.py,
  `RECONNECT_BACKOFF_MIN=2.0`, `RECONNECT_BACKOFF_MAX=120.0`, formula
  `random.uniform(MIN, min(MAX, MIN * 2**(attempt-1)))`. attempt=1 collapses
  to exactly 2.0; the upper bound saturates at exactly 120.0 from attempt=7.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.fluvalble.core import client as client_module
from custom_components.fluvalble.core import protocol
from custom_components.fluvalble.core.client import Client
from custom_components.fluvalble.core.device import Device, FluvalState


def _make_device(name="AquaSky2.0_Test", model="AquaSky 2.0 Bluetooth LED", **config):
    return Device(
        name,
        config_data={
            "mac": "44:A6:E5:70:F1:8D",
            "model": model,
            "product_id": 328,
            **config,
        },
    )


def _classic_client_stub(**overrides):
    """A minimal device.client stand-in resolved to the classic/legacy profile."""
    fields = {
        "plant_pro_spp": False,
        "wifi_facebd": False,
        "command_write_uuid": "00001001-0000-1000-8000-00805f9b34fb",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _ble_device(source="esphome_proxy", address="44:A6:E5:70:F1:8D"):
    return SimpleNamespace(address=address, name="AquaSky", details={"source": source})


def _advertisement(rssi=-60):
    return SimpleNamespace(rssi=rssi, service_uuids=[], service_data={}, manufacturer_data={})


class _FakeTask:
    """Task-like object so constructing a bare Client never starts real BLE work."""

    def __init__(self, coroutine=None):
        if coroutine is not None:
            coroutine.close()

    def done(self):
        return False

    def cancel(self):
        pass

    def __await__(self):
        if False:
            yield None
        return None


def _make_client(*, active_time=120, ping_interval=10, hold_connection=False):
    ble_device = MagicMock()
    ble_device.address = "44:A6:E5:70:F1:8D"
    with patch("asyncio.create_task", side_effect=lambda coro: _FakeTask(coro)):
        return Client(
            ble_device,
            active_time=active_time,
            ping_interval=ping_interval,
            hold_connection=hold_connection,
        )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# hold_connection: defaults and options wiring
# ---------------------------------------------------------------------------


def test_hold_connection_defaults_true():
    device = _make_device()
    assert device.hold_connection is True


def test_hold_connection_reads_false_from_config_data():
    device = _make_device(hold_connection=False)
    assert device.hold_connection is False


def test_hold_connection_setter_delegates_to_the_client_when_one_exists():
    device = _make_device()
    client = SimpleNamespace(hold_connection=True)
    device.client = client

    device.hold_connection = False

    assert device.hold_connection is False
    assert client.hold_connection is False

    device.hold_connection = True

    assert device.hold_connection is True
    assert client.hold_connection is True


def test_hold_connection_setter_is_safe_with_no_client_yet():
    device = _make_device(hold_connection=True)
    assert device.client is None

    device.hold_connection = False

    assert device.hold_connection is False


# ---------------------------------------------------------------------------
# Client.hold_connection: the real persistent-reconnect supervisor toggle
# ---------------------------------------------------------------------------


def test_client_hold_connection_false_collapses_the_idle_deadline_and_wakes_the_heartbeat():
    client = _make_client(active_time=120, hold_connection=True)
    client.ping_future = MagicMock()
    client.ping_time = float("inf")

    client.hold_connection = False

    assert client.hold_connection is False
    assert client.ping_time <= 0
    client.ping_future.cancel.assert_called_once()


def test_client_hold_connection_true_rearms_and_starts_the_heartbeat():
    client = _make_client(active_time=120, hold_connection=False)
    client.connect_task = None
    client.ping_task = None

    with patch(
        "custom_components.fluvalble.core.client.asyncio.create_task",
        side_effect=lambda coro: _FakeTask(coro),
    ):
        client.hold_connection = True

    assert client.hold_connection is True
    assert client.ping_time == float("inf")
    assert client.ping_task is not None


def test_client_hold_connection_setter_is_a_noop_when_value_is_unchanged():
    client = _make_client(active_time=120, hold_connection=True)
    client.ping_time = float("inf")

    client.hold_connection = True

    assert client.ping_time == float("inf")


# ---------------------------------------------------------------------------
# hold_connection: blocks new connects while parked
# ---------------------------------------------------------------------------


def test_async_ensure_client_blocked_while_hold_connection_false():
    _run(_async_test_async_ensure_client_blocked_while_hold_connection_false())


async def _async_test_async_ensure_client_blocked_while_hold_connection_false():
    device = _make_device(hold_connection=False)
    device._async_find_device = AsyncMock(return_value=_ble_device())

    result = await device._async_ensure_client()

    assert result is False
    assert device.client is None
    device._async_find_device.assert_not_awaited()


def test_async_ensure_client_proceeds_normally_when_hold_connection_true():
    _run(_async_test_async_ensure_client_proceeds_normally_when_hold_connection_true())


async def _async_test_async_ensure_client_proceeds_normally_when_hold_connection_true():
    device = _make_device(hold_connection=True)
    device._async_find_device = AsyncMock(return_value=_ble_device())

    with patch("asyncio.create_task", side_effect=lambda coro: _FakeTask(coro)):
        result = await device._async_ensure_client()

    assert result is True
    assert device.client is not None


def test_hold_connection_false_prevents_client_creation_on_advertisement():
    device = _make_device(hold_connection=False)
    assert device.client is None

    device.update_ble(_ble_device(), _advertisement(), "esphome_proxy")

    assert device.client is None


def test_hold_connection_true_creates_exactly_one_client_across_repeated_advertisements():
    device = _make_device(hold_connection=True)

    with patch("asyncio.create_task", side_effect=lambda coro: _FakeTask(coro)) as create_task:
        device.update_ble(_ble_device(), _advertisement(), "esphome_proxy")
        first_client = device.client
        assert first_client is not None
        device.update_ble(_ble_device(), _advertisement(rssi=-70), "esphome_proxy")

    assert device.client is first_client
    create_task.assert_called_once()


# ---------------------------------------------------------------------------
# register_connection_listener
# ---------------------------------------------------------------------------


def test_register_connection_listener_is_not_fired_immediately():
    device = _make_device()
    listener = MagicMock()

    device.register_connection_listener(listener)

    listener.assert_not_called()


def test_register_connection_listener_fires_with_connected_bool_on_transitions():
    device = _make_device()
    listener = MagicMock()
    device.register_connection_listener(listener)

    device.set_connected(True)
    device.set_connected(False)

    assert listener.call_args_list == [((True,),), ((False,),)]


def test_register_connection_listener_unsubscribe_stops_future_notifications():
    device = _make_device()
    listener = MagicMock()
    unsubscribe = device.register_connection_listener(listener)

    device.set_connected(True)
    unsubscribe()
    device.set_connected(False)

    listener.assert_called_once_with(True)


def test_register_connection_listener_does_not_replace_existing_update_handlers():
    device = _make_device()
    legacy_handler = MagicMock()
    device.register_update("connection", legacy_handler)
    listener = MagicMock()
    device.register_connection_listener(listener)

    device.set_connected(True)

    legacy_handler.assert_called_once_with()
    listener.assert_called_once_with(True)


# ---------------------------------------------------------------------------
# Client-level persistence: hold_connection overrides a finite active_time
# ---------------------------------------------------------------------------


def test_hold_connection_true_reconnects_after_disconnect_despite_finite_active_time():
    client = _make_client(active_time=120, hold_connection=True)
    connected = MagicMock()
    client.client = connected
    client.ping_future = MagicMock()

    with patch(
        "custom_components.fluvalble.core.client.asyncio.create_task",
        side_effect=lambda coro: _FakeTask(coro),
    ) as create_task:
        client._on_disconnected(connected)

    assert client.client is None
    create_task.assert_called_once()


def test_hold_connection_false_matches_existing_finite_no_reconnect_behavior():
    """Default (False) must not change today's finite-active_time behavior."""
    client = _make_client(active_time=120, hold_connection=False)
    client.connect_task = None
    connected = MagicMock()
    client.client = connected

    with patch("custom_components.fluvalble.core.client.asyncio.create_task") as create_task:
        client._on_disconnected(connected)

    create_task.assert_not_called()


# ---------------------------------------------------------------------------
# Reconnect backoff: pure formula
# ---------------------------------------------------------------------------


def test_reconnect_backoff_first_attempt_collapses_to_the_minimum():
    assert client_module.reconnect_backoff_seconds(1) == client_module.RECONNECT_BACKOFF_MIN
    assert client_module.reconnect_backoff_seconds(1) == 2.0


def test_reconnect_backoff_upper_bound_doubles_and_saturates_at_the_cap():
    seen_upper_bounds = []

    def fake_uniform(low, high):
        seen_upper_bounds.append(high)
        return high

    with patch("custom_components.fluvalble.core.client.random.uniform", side_effect=fake_uniform):
        for attempt in range(1, 9):
            client_module.reconnect_backoff_seconds(attempt)

    assert seen_upper_bounds == [2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 120.0, 120.0]


def test_reconnect_backoff_never_exceeds_documented_bounds():
    for attempt in range(1, 20):
        delay = client_module.reconnect_backoff_seconds(attempt)
        assert client_module.RECONNECT_BACKOFF_MIN <= delay <= client_module.RECONNECT_BACKOFF_MAX


def test_reconnect_backoff_lower_bound_never_exceeds_the_upper_bound():
    """random.uniform(a, b) requires a <= b for every attempt, including 1."""
    for attempt in range(1, 20):
        # A real (unpatched) call would raise if the formula ever produced
        # low > high; exercising it directly is the regression guard.
        client_module.reconnect_backoff_seconds(attempt)


# ---------------------------------------------------------------------------
# Classic-protocol clock sync precedes the first status read, every connect
# ---------------------------------------------------------------------------


def test_classic_ready_hook_sends_the_clock_command_for_the_legacy_profile():
    _run(_async_test_classic_ready_hook_sends_the_clock_command_for_the_legacy_profile())


async def _async_test_classic_ready_hook_sends_the_clock_command_for_the_legacy_profile():
    device = _make_device()
    device.client = _classic_client_stub()
    sent = []

    async def send_packet(packet, *, verify=True):
        sent.append(packet)
        assert verify is False
        return True

    device._async_send_packet = AsyncMock(side_effect=send_packet)

    await device._async_on_client_ready()

    assert len(sent) == 1
    assert sent[0][0] == 0x68
    assert sent[0][1] == protocol.OLD_CLOCK
    assert device._clock_sync_started is True


def test_classic_clock_sync_completes_after_the_state_ready_hook():
    _run(_async_test_classic_clock_sync_completes_after_the_state_ready_hook())


async def _async_test_classic_clock_sync_completes_after_the_state_ready_hook():
    device = _make_device()
    device.client = _classic_client_stub()
    device._async_send_packet = AsyncMock(return_value=True)

    await device._async_on_client_ready()
    assert device._clock_synced is False  # not yet — only the state hook finalizes it

    await device._async_on_client_state_ready({})

    assert device._clock_synced is True
    assert device.diagnostics["status"] == "clock_synced"


def test_classic_clock_sync_repeats_on_every_physical_reconnect():
    _run(_async_test_classic_clock_sync_repeats_on_every_physical_reconnect())


async def _async_test_classic_clock_sync_repeats_on_every_physical_reconnect():
    device = _make_device()
    device.client = _classic_client_stub()
    clock_sends = []

    async def send_packet(packet, *, verify=True):
        if packet[1] == protocol.OLD_CLOCK:
            clock_sends.append(packet)
        return True

    device._async_send_packet = AsyncMock(side_effect=send_packet)

    # First physical connect.
    await device._async_on_client_ready()
    await device._async_on_client_state_ready({})
    assert len(clock_sends) == 1
    assert device._clock_synced is True

    # A real disconnect must reset the flags Client relies on to decide
    # whether to run the handshake again.
    device.set_connected(False)
    assert device._clock_synced is False

    # Second physical connect (reconnect) — the hooks fire again exactly as
    # Client's `_initialize_session` would invoke them, and must send the
    # clock command again before any second status read.
    await device._async_on_client_ready()
    await device._async_on_client_state_ready({})

    assert len(clock_sends) == 2
    assert device._clock_synced is True


def test_classic_ready_hook_does_not_resend_the_clock_within_the_same_session():
    _run(_async_test_classic_ready_hook_does_not_resend_the_clock_within_the_same_session())


async def _async_test_classic_ready_hook_does_not_resend_the_clock_within_the_same_session():
    device = _make_device()
    device.client = _classic_client_stub()
    device._async_send_packet = AsyncMock(return_value=True)

    await device._async_on_client_ready()
    await device._async_on_client_state_ready({})
    device._async_send_packet.reset_mock()

    # Client only calls ready_callback once per _initialize_session, but a
    # defensive extra call (e.g. a retried command gap) must still be inert.
    await device._async_on_client_ready()

    device._async_send_packet.assert_not_awaited()


# ---------------------------------------------------------------------------
# async_ensure_mode
# ---------------------------------------------------------------------------


def test_async_ensure_mode_rejects_a_literal_outside_modes():
    device = _make_device()

    with pytest.raises(ValueError):
        _run(device.async_ensure_mode("bogus"))


def test_async_ensure_mode_is_a_noop_with_zero_ble_traffic_when_already_matching():
    device = _make_device()
    device.values["mode"] = "automatic"
    device._async_send_packet = AsyncMock(return_value=True)

    result = _run(device.async_ensure_mode("automatic"))

    assert result == "automatic"
    device._async_send_packet.assert_not_awaited()


def test_async_ensure_mode_sends_the_resolved_mode_packet_when_drifted():
    from custom_components.fluvalble.core.device import MODE_TO_CODE

    device = _make_device()
    device.client = _classic_client_stub()
    device.values["mode"] = "manual"
    device._async_prepare_command = AsyncMock(return_value=True)

    async def send_packet(_packet):
        # The real implementation's confirm-read is a side effect of the
        # write itself (client.request_state() -> notify -> decode_update_packet
        # -> self.values["mode"]); simulate that side effect here.
        device.values["mode"] = "automatic"
        return True

    device._async_send_packet = AsyncMock(side_effect=send_packet)

    result = _run(device.async_ensure_mode("automatic"))

    assert result == "automatic"
    device._async_send_packet.assert_awaited_once_with(protocol.old_mode_packet(MODE_TO_CODE["automatic"]))


def test_async_ensure_mode_returns_the_freshly_confirmed_mode_even_when_the_write_is_unconfirmed():
    """A confirm mismatch still returns whatever the fixture actually reported."""
    device = _make_device()
    device.client = _classic_client_stub()
    device.values["mode"] = "manual"
    device._async_prepare_command = AsyncMock(return_value=True)

    async def send_packet(_packet):
        # The device did not take the requested mode — the post-write
        # confirm-read observed "professional" instead of "automatic".
        device.values["mode"] = "professional"
        return False

    device._async_send_packet = AsyncMock(side_effect=send_packet)

    result = _run(device.async_ensure_mode("automatic"))

    assert result == "professional"


def test_async_ensure_mode_returns_none_when_no_mode_could_ever_be_confirmed():
    device = _make_device()
    device.client = _classic_client_stub()
    device.values["mode"] = None
    device._async_prepare_command = AsyncMock(return_value=True)
    device._async_send_packet = AsyncMock(return_value=False)

    result = _run(device.async_ensure_mode("automatic"))

    assert result is None


def test_async_ensure_mode_returns_none_while_parked():
    """hold_connection=False blocks the connection _async_prepare_command needs."""
    device = _make_device(hold_connection=False)
    device.values["mode"] = "manual"
    device._async_send_packet = AsyncMock(return_value=True)

    result = _run(device.async_ensure_mode("automatic"))

    assert result is None
    device._async_send_packet.assert_not_awaited()


# ---------------------------------------------------------------------------
# async_read_state / FluvalState
# ---------------------------------------------------------------------------


def test_fluval_state_is_a_frozen_dataclass():
    import dataclasses

    assert dataclasses.is_dataclass(FluvalState)
    state = FluvalState(
        mode="manual",
        power=True,
        levels={"channel_1": 10},
        auto_schedule=None,
        pro_schedule=None,
        last_state_at=1.0,
        connection_attempts=0,
        scanner_source=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.mode = "automatic"


def test_async_read_state_manual_mode_exposes_levels_and_power_without_schedule():
    _run(_async_test_async_read_state_manual_mode_exposes_levels_and_power_without_schedule())


async def _async_test_async_read_state_manual_mode_exposes_levels_and_power_without_schedule():
    device = _make_device()
    device.client = _classic_client_stub()
    device.values.update(
        {
            "mode": "manual",
            "led_on_off": True,
            "channel_1": 10,
            "channel_2": 20,
            "channel_3": 30,
            "channel_4": 40,
        }
    )
    device.client.request_state = AsyncMock(return_value=True)

    state = await device.async_read_state()

    assert state.mode == "manual"
    assert state.power is True
    assert state.levels == {"channel_1": 10, "channel_2": 20, "channel_3": 30, "channel_4": 40}
    assert state.auto_schedule is None
    assert state.pro_schedule is None


def test_async_read_state_automatic_mode_exposes_schedule_without_levels():
    _run(_async_test_async_read_state_automatic_mode_exposes_schedule_without_levels())


async def _async_test_async_read_state_automatic_mode_exposes_schedule_without_levels():
    device = _make_device()
    device.client = _classic_client_stub()
    device.client.request_state = AsyncMock(return_value=True)
    schedule = {"sunrise": {"hour": 8, "minute": 0}}
    device.values.update({"mode": "automatic", "native_auto_schedule": schedule})

    state = await device.async_read_state()

    assert state.mode == "automatic"
    assert state.power is None
    assert state.levels is None
    assert state.auto_schedule == schedule
    assert state.pro_schedule is None


def test_async_read_state_professional_mode_exposes_pro_schedule_without_levels():
    _run(_async_test_async_read_state_professional_mode_exposes_pro_schedule_without_levels())


async def _async_test_async_read_state_professional_mode_exposes_pro_schedule_without_levels():
    device = _make_device()
    device.client = _classic_client_stub()
    device.client.request_state = AsyncMock(return_value=True)
    points = [{"minute": 0}, {"minute": 720}]
    device.values.update({"mode": "professional", "native_pro_schedule": points})

    state = await device.async_read_state()

    assert state.mode == "professional"
    assert state.power is None
    assert state.levels is None
    assert state.auto_schedule is None
    assert state.pro_schedule == points


def test_async_read_state_reports_connection_attempts_and_scanner_source():
    _run(_async_test_async_read_state_reports_connection_attempts_and_scanner_source())


async def _async_test_async_read_state_reports_connection_attempts_and_scanner_source():
    device = _make_device()
    device.client = _classic_client_stub(connection_attempts=4, request_state=AsyncMock(return_value=True))
    device.conn_info["active_connection_source_address"] = "AA:BB:CC:DD:EE:FF"
    device.values["mode"] = "manual"

    state = await device.async_read_state()

    assert state.connection_attempts == 4
    assert state.scanner_source == "AA:BB:CC:DD:EE:FF"
    assert isinstance(state.last_state_at, float)
