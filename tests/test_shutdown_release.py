"""Tests for the Stage-1 shutdown job that releases the Bluetooth link.

Home Assistant does not unload config entries on shutdown and the Bluetooth
stack goes away on EVENT_HOMEASSISTANT_STOP, so each entry registers one
`async_add_shutdown_job` that drops whatever link is open (held or transient)
while the stack and the proxies are still alive. After it ran nothing in the
process may connect again.
"""

import asyncio
import inspect
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.fluvalble as integration
from custom_components.fluvalble import (
    DOMAIN,
    SHUTTING_DOWN,
    FluvalRuntimeData,
    _async_register_shutdown_release,
    _async_release_link_at_shutdown,
    _async_release_links,
    _domain_shutting_down,
)
from custom_components.fluvalble.core.client import Client
from custom_components.fluvalble.core.device import Device
from custom_components.fluvalble.core.guardian import ScheduleGuardian

MAC = "44:A6:E5:70:F1:8D"


def _run(coro):
    return asyncio.run(coro)


def _make_device(**kwargs):
    return Device("Fluval Test", config_data={"mac": MAC, "model": "AquaSky 2.0 Bluetooth LED"}, **kwargs)


def _ble_device():
    return SimpleNamespace(address=MAC, name="AquaSky", details={"source": "esphome_proxy"})


def _advertisement():
    return SimpleNamespace(rssi=-60, service_uuids=[], service_data={}, manufacturer_data={})


class _FakeGatt:
    def __init__(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False


class _FakeTask:
    """Task-like object so Client.__init__ starts no real BLE work."""

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


def _connected_device(*, active_time):
    """A Device owning a real Client whose GATT link is open."""
    device = _make_device(active_time=active_time)
    with patch("asyncio.create_task", side_effect=lambda coro: _FakeTask(coro)):
        client = Client(_ble_device(), device.set_connected, active_time=active_time)
    gatt = _FakeGatt()
    client.client = gatt
    device.client = client
    device.set_connected(True)
    return device, client, gatt


def _hass():
    return SimpleNamespace(data={DOMAIN: {}})


def _entry():
    return SimpleNamespace(entry_id="entry_1", title="Fluval Test")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class _FakeShutdownHass:
    """Collects shutdown jobs the way core's `async_add_shutdown_job` does."""

    def __init__(self):
        self.jobs = []
        self.data = {DOMAIN: {}}

    def async_add_shutdown_job(self, job, *args):
        entry = (job, args)
        self.jobs.append(entry)
        return lambda: self.jobs.remove(entry)


class _FakeUnloadEntry:
    entry_id = "entry_1"
    title = "Fluval Test"

    def __init__(self):
        self.unload_callbacks = []

    def async_on_unload(self, func):
        self.unload_callbacks.append(func)


def test_one_shutdown_job_per_entry_is_registered_and_removed_on_unload():
    hass = _FakeShutdownHass()
    first, second = _FakeUnloadEntry(), _FakeUnloadEntry()
    second.entry_id = "entry_2"

    _async_register_shutdown_release(hass, first, FluvalRuntimeData())
    _async_register_shutdown_release(hass, second, FluvalRuntimeData())

    assert len(hass.jobs) == 2
    assert all(inspect.iscoroutinefunction(job.target) for job, _ in hass.jobs)

    for unload in first.unload_callbacks:
        unload()

    assert len(hass.jobs) == 1


# ---------------------------------------------------------------------------
# Release + latch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("active_time", [0, 120], ids=["held", "transient"])
def test_shutdown_job_drops_the_open_link_and_latches_closed(active_time, caplog):
    device, client, gatt = _connected_device(active_time=active_time)
    watcher = MagicMock()
    order = []
    watcher.stop.side_effect = lambda: order.append("watcher stopped")
    real_disconnect = gatt.disconnect

    async def recording_disconnect():
        order.append("disconnected")
        await real_disconnect()

    gatt.disconnect = recording_disconnect
    runtime = FluvalRuntimeData(device=device, link_watcher=watcher)
    hass = _hass()

    with caplog.at_level(logging.INFO, logger="custom_components.fluvalble"):
        _run(_async_release_link_at_shutdown(hass, _entry(), runtime))

    assert gatt.is_connected is False
    assert device.connected is False
    assert device.client is None
    assert device.closing is True
    assert runtime.closing is True
    assert _domain_shutting_down(hass)
    # The watchers are quiet before the deliberate disconnect happens.
    assert order == ["watcher stopped", "disconnected"]
    assert any(
        "Released BLE link to Fluval Test at shutdown in" in record.getMessage() and record.levelno == logging.INFO
        for record in caplog.records
    )


def test_after_the_shutdown_job_nothing_connects_again():
    device, _client, _gatt = _connected_device(active_time=0)
    _run(_async_release_link_at_shutdown(_hass(), _entry(), FluvalRuntimeData(device=device)))

    with patch.object(Device, "_new_client", side_effect=AssertionError("must not connect")) as new_client:
        # Held-link supervisor re-arm on the next advertisement.
        device.update_ble(_ble_device(), _advertisement(), "esphome_proxy")
        # A command, and the on-demand client creation behind it.
        assert _run(device._async_ensure_client()) is False
        assert _run(device.async_identify()) is False
        assert _run(device.async_refresh_state()) is False

    new_client.assert_not_called()
    assert device.client is None


def test_after_the_shutdown_job_a_guardian_check_neither_connects_nor_fails():
    device, _client, _gatt = _connected_device(active_time=0)
    guardian = ScheduleGuardian(device)
    notified = MagicMock()
    guardian.add_listener(notified)
    _run(_async_release_link_at_shutdown(_hass(), _entry(), FluvalRuntimeData(device=device)))
    device.async_sync_clock = AsyncMock(side_effect=AssertionError("must not touch the fixture"))
    device.async_read_state = AsyncMock(side_effect=AssertionError("must not touch the fixture"))

    status = _run(guardian.async_check())

    assert status == guardian.status
    assert guardian.consecutive_failures == 0
    assert guardian.consecutive_unreachable == 0
    notified.assert_not_called()
    device.async_read_state.assert_not_called()


def test_shutdown_job_without_a_device_latches_so_a_later_device_is_never_built():
    hass = _hass()
    runtime = FluvalRuntimeData()

    _run(_async_release_link_at_shutdown(hass, _entry(), runtime))

    assert runtime.closing is True
    assert _domain_shutting_down(hass)


def test_stopped_link_watcher_ignores_late_notifications():
    from custom_components.fluvalble.core.recovery import LinkWatcher

    hass = _hass()
    watcher = LinkWatcher(hass, SimpleNamespace(entry_id="e", title="t", options={}), MAC)
    watcher.device = _make_device()
    watcher.stop()

    with patch("custom_components.fluvalble.core.recovery.reconcile_issue") as reconcile_issue:
        watcher.reconcile()

    reconcile_issue.assert_not_called()
    assert watcher._countdown_unsub is None
    assert DOMAIN not in hass.data or "_link_down_since" not in hass.data[DOMAIN]


# ---------------------------------------------------------------------------
# Bounded and never raising
# ---------------------------------------------------------------------------


def test_hanging_disconnect_is_bounded_and_does_not_raise(monkeypatch, caplog):
    monkeypatch.setattr(integration, "RELEASE_AT_SHUTDOWN_TIMEOUT", 0.05)
    device = _make_device()
    never = asyncio.Event
    stuck = SimpleNamespace(stop=None)

    async def hang(*, stop_notify=True):
        await never().wait()

    stuck.stop = hang
    device.client = stuck
    runtime = FluvalRuntimeData(device=device)

    with caplog.at_level(logging.INFO, logger="custom_components.fluvalble"):
        _run(asyncio.wait_for(_async_release_link_at_shutdown(_hass(), _entry(), runtime), timeout=2))

    assert device.closing is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "timed out" in warnings[0].getMessage()
    assert not any("Released BLE link" in r.getMessage() for r in caplog.records)


def test_failing_disconnect_is_absorbed_with_one_warning(caplog):
    device = _make_device()
    device.client = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("proxy went away")))
    runtime = FluvalRuntimeData(device=device)

    with caplog.at_level(logging.INFO, logger="custom_components.fluvalble"):
        _run(_async_release_link_at_shutdown(_hass(), _entry(), runtime))

    assert device.closing is True
    assert [r.levelno for r in caplog.records if r.name == "custom_components.fluvalble"] == [logging.WARNING]


# ---------------------------------------------------------------------------
# release_link keeps working; its resume timer cannot outlive the latch
# ---------------------------------------------------------------------------


def _release_link_hass(loaded_entry):
    hass = _FakeShutdownHass()
    hass.config_entries = SimpleNamespace(
        async_entries=lambda _domain: [loaded_entry],
        async_unload=AsyncMock(return_value=True),
        async_setup=AsyncMock(return_value=True),
    )
    return hass


def _loaded_entry():
    state = integration.ConfigEntryState.LOADED
    return SimpleNamespace(entry_id="entry_1", title="Fluval Test", state=state)


def test_release_link_without_shutdown_still_unloads_and_resumes():
    entry = _loaded_entry()
    hass = _release_link_hass(entry)
    timers = {}

    def call_later(_hass, delay, callback):
        timers["resume"] = callback
        return MagicMock()

    with patch.object(integration, "async_call_later", call_later):
        _run(_async_release_links(hass, 180))
        entry.state = integration.ConfigEntryState.NOT_LOADED
        _run(timers["resume"](None))

    hass.config_entries.async_unload.assert_awaited_once_with("entry_1")
    hass.config_entries.async_setup.assert_awaited_once_with("entry_1")
    assert hass.data[DOMAIN][integration.RESUME_CANCELS] == {}  # a fired timer is forgotten


# ---------------------------------------------------------------------------
# Addendum A: domain-lifetime latch job
# ---------------------------------------------------------------------------


def test_async_setup_registers_one_domain_latch_job_that_survives_entry_unload():
    hass = _FakeShutdownHass()
    assert _run(integration.async_setup(hass, {})) is True

    assert len(hass.jobs) == 1
    job, _args = hass.jobs[0]
    assert inspect.iscoroutinefunction(job.target) is False
    assert getattr(job.target, "_hass_callback", False) is True  # runs on the loop, not an executor

    # Entry unloads only remove their own jobs; the domain job stays.
    entry = _FakeUnloadEntry()
    _async_register_shutdown_release(hass, entry, FluvalRuntimeData())
    for unload in entry.unload_callbacks:
        unload()
    assert len(hass.jobs) == 1

    job.target()
    assert _domain_shutting_down(hass)


def test_domain_latch_job_cancels_pending_release_link_resume_timers_without_entry_jobs():
    entry = _loaded_entry()
    hass = _release_link_hass(entry)
    cancel = MagicMock()
    timers = {}

    def call_later(_hass, delay, callback):
        timers["resume"] = callback
        return cancel

    _run(integration.async_setup(hass, {}))
    domain_job, _ = hass.jobs[0]

    with patch.object(integration, "async_call_later", call_later):
        _run(_async_release_links(hass, 180))  # unloads the entry: its own job is gone

    domain_job.target()  # Stage 1 starts

    cancel.assert_called_once_with()
    entry.state = integration.ConfigEntryState.NOT_LOADED
    _run(timers["resume"](None))  # a timer already in flight still refuses
    hass.config_entries.async_setup.assert_not_awaited()


# ---------------------------------------------------------------------------
# Addendum B: setup refuses while latched
# ---------------------------------------------------------------------------


def _setup_entry(mac=MAC):
    return SimpleNamespace(
        entry_id="entry_1",
        title="Fluval Test",
        data={"mac": mac},
        options={},
        unique_id=None,
        async_on_unload=MagicMock(),
    )


def test_setup_refuses_before_anything_starts_when_the_process_is_latched():
    hass = _hass()
    hass.data[DOMAIN][SHUTTING_DOWN] = True
    entry = _setup_entry()

    with (
        patch.object(integration, "Device", side_effect=AssertionError("no Device while shutting down")) as device,
        patch.object(integration, "async_setup_link_watcher") as watcher,
        patch.object(integration, "_register_static_paths", new=AsyncMock()) as static,
    ):
        with pytest.raises(integration.ConfigEntryNotReady):
            _run(integration.async_setup_entry(hass, entry))

    device.assert_not_called()
    watcher.assert_not_called()
    static.assert_not_awaited()


def test_setup_refuses_when_the_latch_is_set_during_the_static_path_await():
    hass = _hass()
    entry = _setup_entry()

    async def latch_while_awaiting(_hass):
        hass.data[DOMAIN][SHUTTING_DOWN] = True

    with (
        patch.object(integration, "Device", side_effect=AssertionError("no Device while shutting down")) as device,
        patch.object(integration, "async_setup_link_watcher") as watcher,
        patch.object(integration, "_register_static_paths", new=latch_while_awaiting),
        patch.object(integration, "_async_register_services"),
    ):
        with pytest.raises(integration.ConfigEntryNotReady):
            _run(integration.async_setup_entry(hass, entry))

    device.assert_not_called()
    watcher.assert_not_called()


def test_setup_overtaken_by_the_latch_during_platform_forwarding_releases_and_refuses():
    hass = _FakeShutdownHass()
    hass.state = integration.CoreState.running
    hass.config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(),
        async_unload_platforms=AsyncMock(return_value=True),
        async_update_entry=MagicMock(),
    )
    entry = _setup_entry()
    gatt_device = _make_device()
    stopped = AsyncMock()
    gatt_device.client = SimpleNamespace(stop=stopped)

    async def forward(_entry, _platforms):
        hass.data[DOMAIN][SHUTTING_DOWN] = True

    hass.config_entries.async_forward_entry_setups = forward

    def create_cached_device(*_args, **_kwargs):
        return gatt_device

    with (
        patch.object(integration, "Device", create_cached_device),
        patch.object(integration, "async_setup_link_watcher", return_value=MagicMock()),
        patch.object(integration, "_register_static_paths", new=AsyncMock()),
        patch.object(integration, "_register_websocket"),
        patch.object(integration, "_register_services"),
        patch.object(integration, "_async_register_services"),
        patch.object(integration, "_migrate_legacy_registry_entries"),
        patch.object(integration, "_cleanup_duplicate_devices"),
        patch.object(integration, "_sync_product_identity"),
        patch.object(integration, "async_setup_guardian", return_value=MagicMock()),
        patch.object(
            integration.bluetooth,
            "async_last_service_info",
            return_value=SimpleNamespace(device=_ble_device(), advertisement=_advertisement(), source="p"),
            create=True,
        ),
    ):
        with pytest.raises(integration.ConfigEntryNotReady):
            _run(integration.async_setup_entry(hass, entry))

    assert gatt_device.closing is True
    stopped.assert_awaited_once()
    hass.config_entries.async_unload_platforms.assert_awaited_once()
    assert "entry_1" not in hass.data[DOMAIN]


# ---------------------------------------------------------------------------
# Addendum D: refusals are not faults
# ---------------------------------------------------------------------------


def test_end_override_refused_by_the_latch_records_no_failure_and_raises_no_repair():
    device = _make_device()
    guardian = ScheduleGuardian(device, expected_mode="auto")
    guardian.override_active = True
    guardian.status = "ok"
    notified = MagicMock()
    guardian.add_listener(notified)
    device.closing = True  # latched before the user presses Return to schedule

    result = _run(guardian.async_end_override())

    assert result is None
    assert guardian.status == "ok"
    assert guardian.consecutive_failures == 0
    assert guardian.override_active is True
    notified.assert_not_called()


def test_end_override_overtaken_by_the_latch_mid_restore_records_no_failure():
    device = _make_device()
    guardian = ScheduleGuardian(device, expected_mode="auto")
    guardian.override_active = True
    guardian.status = "ok"
    notified = MagicMock()
    guardian.add_listener(notified)

    async def refused_after_latch(_mode):
        device.closing = True
        return None  # the command path refused to connect

    device.async_ensure_mode = refused_after_latch

    result = _run(guardian.async_end_override())

    assert result is None
    assert guardian.consecutive_failures == 0
    assert guardian.status == "ok"
    notified.assert_not_called()
