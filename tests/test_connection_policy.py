"""Tests for connection-lifecycle behaviour: register_connection_listener, the
connect-time clock sync, async_ensure_mode, and async_read_state.

v1.1.0 removed the permanently-held-connection concept entirely (see
WHATCHANGED-v110.md) - connection policy is now the base's connect-on-demand
only, driven solely by the upstream `active_time` option (`0` = persistent,
matching upstream's own meaning; otherwise a finite idle window). What
remains documented here:
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
- `_async_prepare_command()` always connects on demand through
  `_async_ensure_client()`; there is no "parked" state that refuses it.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.fluvalble.core import protocol
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


def _run(coro):
    return asyncio.run(coro)


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
# update_ble never eagerly creates a Client - only an on-demand connect does
# ---------------------------------------------------------------------------


def test_update_ble_never_creates_a_client():
    device = _make_device()
    assert device.client is None

    device.update_ble(_ble_device(), _advertisement(), "esphome_proxy")
    device.update_ble(_ble_device(), _advertisement(rssi=-70), "esphome_proxy")

    assert device.client is None


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


def test_async_prepare_command_connects_on_demand():
    _run(_async_test_async_prepare_command_connects_on_demand())


async def _async_test_async_prepare_command_connects_on_demand():
    """No client yet must still connect through _async_ensure_client()."""
    device = _make_device()
    device.client = _classic_client_stub(ensure_connected=AsyncMock(return_value=True))
    device._async_ensure_client = AsyncMock(return_value=True)

    result = await device._async_prepare_command()

    assert result is True
    device._async_ensure_client.assert_awaited_once()


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


# ---------------------------------------------------------------------------
# Held link: liveness, drop accounting, and the route the Connection sensor
# reports. Live, `last_seen` froze the moment the hold began because only
# advertisements fed it - a held fixture advertises rarely.
# ---------------------------------------------------------------------------


def test_gatt_activity_keeps_last_seen_current_while_the_link_is_held():
    _run(_async_test_gatt_activity_keeps_last_seen_current_while_the_link_is_held())


async def _async_test_gatt_activity_keeps_last_seen_current_while_the_link_is_held():
    from datetime import UTC, datetime, timedelta

    from custom_components.fluvalble.core.client import Client
    from custom_components.fluvalble.core.device import REACHABLE_SECONDS

    device = _make_device()
    device.set_connected(True)
    stale = datetime.now(UTC) - timedelta(seconds=REACHABLE_SECONDS * 2)
    device.conn_info["last_seen"] = stale
    assert device.is_reachable() is True  # connected short-circuits...

    device.connected = False
    assert device.is_reachable() is False  # ...but nothing else kept it alive

    device.set_connected(True)
    device.conn_info["last_seen"] = stale

    ble_device = _ble_device()
    with patch("asyncio.create_task", side_effect=lambda coro: coro.close()):
        client = Client(
            ble_device,
            device.set_connected,
            activity_callback=device._on_client_activity,
            hold_stats=device.hold_stats,
        )

    async def _heartbeat_read():
        return b"\x00"

    await client._bounded(_heartbeat_read(), 1.0, "heartbeat wake read")

    assert device.conn_info["last_seen"] > stale
    device.connected = False
    assert device.is_reachable() is True


def _holding_device(active_time=0):
    """A Device built with a real `active_time` (not a config_data key)."""
    return Device(
        "AquaSky2.0_Test",
        config_data={"mac": "44:A6:E5:70:F1:8D", "model": "AquaSky 2.0 Bluetooth LED", "product_id": 328},
        active_time=active_time,
    )


def test_hold_attributes_track_drops_and_reconnect_progress():
    device = _holding_device()
    attributes = device.connection_hold_attributes()

    assert attributes == {"hold": True, "drops_1h": 0, "last_drop": None, "reconnect_attempt": 0}

    device.hold_stats.record_drop()
    device.hold_stats.record_reconnect_attempt(3)
    attributes = device.connection_hold_attributes()

    assert attributes["drops_1h"] == 1
    assert attributes["last_drop"].endswith("+00:00")
    assert attributes["reconnect_attempt"] == 3

    # A reconnect that lands clears the attempt counter but keeps the history.
    device.set_connected(True)
    attributes = device.connection_hold_attributes()
    assert attributes["reconnect_attempt"] == 0
    assert attributes["drops_1h"] == 1

    assert _holding_device(active_time=120).connection_hold_attributes()["hold"] is False


def test_hold_statistics_survive_a_connection_reset():
    _run(_async_test_hold_statistics_survive_a_connection_reset())


async def _async_test_hold_statistics_survive_a_connection_reset():
    """`async_reset_connection` throws the Client away; `drops_1h` must not
    reset to zero just because a fresh client object replaced it."""
    device = _holding_device()
    device.hold_stats.record_drop()
    device.client = AsyncMock()

    await device.async_reset_connection()

    assert device.client is None
    assert device.connection_hold_attributes()["drops_1h"] == 1


def test_connection_state_names_the_proxy_holding_the_slot():
    from custom_components.fluvalble.core.device import allocation_source_for_address

    device = _make_device()
    device.hass = MagicMock()
    allocations = [
        SimpleNamespace(source="AA:00:00:00:00:01", slots=3, free=3, allocated=[]),
        SimpleNamespace(source="AA:00:00:00:00:02", slots=3, free=2, allocated=["44:A6:E5:70:F1:8D"]),
    ]

    source = allocation_source_for_address(allocations, device.address)
    assert source == "AA:00:00:00:00:02"
    assert allocation_source_for_address(allocations, "11:22:33:44:55:66") is None
    assert allocation_source_for_address(None, device.address) is None

    scanner = SimpleNamespace(name="plant-room-bluetooth-proxy (AA:00:00:00:00:02)", details=None)
    with patch(
        "custom_components.fluvalble.core.device.bluetooth.async_scanner_by_source",
        return_value=scanner,
    ):
        device.connected = True
        assert device.connection_state(source) == "plant-room-bluetooth-proxy"
        device.connected = False
        assert device.connection_state(source) == "disconnected"


def test_connection_state_falls_back_to_the_recorded_route_then_to_connected():
    device = _make_device()
    device.connected = True
    device.conn_info["active_connection_source"] = "office-bluetooth-proxy"

    # No allocation for this address (a local adapter keeps no slot accounting).
    assert device.connection_state(None) == "office-bluetooth-proxy"

    device.conn_info.pop("active_connection_source")
    assert device.connection_state(None) == "connected"
