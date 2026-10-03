"""Startup must not wait on the lamp: no radio-bound work in a task Home Assistant tracks.

Home Assistant reports "initialized" only after `async_block_till_done()`,
which waits for tasks created with `hass.async_create_task` (tracked) but not
for `async_create_background_task` ones. Two things kept a boot waiting on the
lamp even after 1.5.3 (observed live, three boots: setup 0.06 s, "initialized"
~30 s later, right after a guardian clock-sync deadline):

* the guardian's first clock sync ran its whole 30 s deadline at every start,
  holding the device's command lock, because of a lock-order inversion with the
  connect task finishing its first session; any command or automation that
  starts with Home Assistant is a tracked task queued behind that lock;
* the first check was started while the held link was still connecting, so it
  took the command lock for the length of the connect.

These tests use real Client/Device objects with a fake GATT link and a minimal
Home Assistant double that tracks `async_create_task` like core does.
"""

import asyncio
from contextlib import ExitStack
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import custom_components.fluvalble as integration
from custom_components.fluvalble import DOMAIN
from custom_components.fluvalble.core import client as client_module
from custom_components.fluvalble.core import guardian as guardian_module
from custom_components.fluvalble.core.device import Device
from custom_components.fluvalble.core.guardian import ScheduleGuardian

MAC = "44:A6:E5:70:F1:8D"


def _ble_device():
    return SimpleNamespace(address=MAC, name="AquaSky", details={"source": "esphome_proxy"})


def _advertisement():
    return SimpleNamespace(rssi=-60, service_uuids=[], service_data={}, manufacturer_data={})


class _Char:
    def __init__(self, uuid, properties):
        self.uuid = uuid
        self.properties = properties


class _Services:
    def __init__(self, characteristics):
        self._by_uuid = {c.uuid.lower(): c for c in characteristics}

    def get_characteristic(self, uuid):
        return self._by_uuid.get(uuid.lower())

    def __iter__(self):
        return iter(())


class _FakeGatt:
    """A healthy fixture on an open link."""

    def __init__(self):
        self.is_connected = True
        self.services = _Services(
            [
                _Char(client_module.FACEBD_COMMAND_WRITE_UUIDS[0], ["write"]),
                _Char(client_module.NOTIFY_UUIDS[0], ["notify", "read"]),
                _Char(client_module.WAKE_READ_UUIDS[2], ["read"]),
            ]
        )

    async def start_notify(self, *_args, **_kwargs):
        pass

    async def stop_notify(self, *_args, **_kwargs):
        pass

    async def read_gatt_char(self, *_args, **_kwargs):
        return b""

    async def write_gatt_char(self, *_args, **_kwargs):
        pass

    async def disconnect(self):
        self.is_connected = False


def _fast_gatt(monkeypatch):
    # No scanner in unit tests: the route is always the advertised device.
    monkeypatch.setattr(Device, "_async_find_device", AsyncMock(return_value=_ble_device()))
    monkeypatch.setattr(client_module, "COMMAND_GAP", 0.0)
    monkeypatch.setattr(client_module, "STATE_NOTIFY_TIMEOUT", 0.01)
    monkeypatch.setattr(client_module, "POST_WRITE_STATE_DELAY", 0.0)


def _hold_device(**kwargs):
    """A Device in hold mode (active_time 0): its Client connects as soon as it exists."""
    return Device(
        "Fluval Test",
        _ble_device(),
        _advertisement(),
        "esphome_proxy",
        config_data={"mac": MAC, "model": "AquaSky 2.0 Bluetooth LED"},
        active_time=0,
        **kwargs,
    )


async def _cleanup(device):
    if device.client is not None:
        await device.client.stop()


# ---------------------------------------------------------------------------
# Root cause of the 30 s stall: clock sync vs. the connect task's first session
# ---------------------------------------------------------------------------


def test_clock_sync_while_the_held_link_is_still_connecting_does_not_deadlock(monkeypatch):
    async def scenario():
        _fast_gatt(monkeypatch)

        async def slow_connect(*_args, **_kwargs):
            await asyncio.sleep(0.1)
            return _FakeGatt()

        with patch.object(client_module, "establish_connection", slow_connect):
            device = _hold_device()
            assert device.client is not None  # the hold started connecting in __init__
            started = time.monotonic()
            # The guardian's own call: no deadline of its own here, 5 s is far
            # below the 30 s the deadlock used to cost.
            result = await asyncio.wait_for(device.async_sync_clock(), 5)
            elapsed = time.monotonic() - started
            synced = device._clock_synced
            await _cleanup(device)

        assert result is True
        assert elapsed < 2.0
        assert synced is True

    asyncio.run(scenario())


def test_forced_clock_sync_button_while_connecting_does_not_deadlock(monkeypatch):
    async def scenario():
        _fast_gatt(monkeypatch)

        async def slow_connect(*_args, **_kwargs):
            await asyncio.sleep(0.1)
            return _FakeGatt()

        with patch.object(client_module, "establish_connection", slow_connect):
            device = _hold_device()
            result = await asyncio.wait_for(device.async_sync_clock(force=True, priority=True), 5)
            await _cleanup(device)

        assert result is True

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# First guardian check waits for the held link instead of sitting behind it
# ---------------------------------------------------------------------------


class _Timers:
    """Stands in for async_call_later / async_track_time_interval."""

    def __init__(self):
        self.later: list[tuple[float, object]] = []
        self.cancelled: list[float] = []

    def call_later(self, _hass, delay, action):
        self.later.append((delay, action))
        return lambda: self.cancelled.append(delay)

    @staticmethod
    def track_interval(_hass, _action, _interval):
        return lambda: None


def _patch_timers(monkeypatch):
    timers = _Timers()
    monkeypatch.setattr(guardian_module, "async_call_later", timers.call_later)
    monkeypatch.setattr(guardian_module, "async_track_time_interval", timers.track_interval)
    return timers


def test_first_check_does_not_take_the_command_lock_while_the_held_link_connects(monkeypatch):
    async def scenario():
        _fast_gatt(monkeypatch)
        timers = _patch_timers(monkeypatch)
        release_connect = asyncio.Event()

        async def connect(*_args, **_kwargs):
            await release_connect.wait()
            return _FakeGatt()

        spawned: list[asyncio.Task] = []
        with patch.object(client_module, "establish_connection", connect):
            device = _hold_device()
            guardian = ScheduleGuardian(device, expected_mode="manual")
            guardian.start_runner(MagicMock(), lambda coro: spawned.append(asyncio.ensure_future(coro)))
            await asyncio.sleep(0.05)

            # Nothing started a check and nothing holds the lock a user command needs.
            assert spawned == []
            assert guardian.last_check_at is None
            assert not device._command_transaction_lock.locked()
            assert [delay for delay, _ in timers.later] == [guardian_module.INITIAL_CHECK_GRACE_SECONDS]

            # The link comes up: the connection event itself starts the check.
            release_connect.set()
            for _ in range(100):
                await asyncio.sleep(0.02)
                if guardian.last_check_at is not None and not guardian._check_lock.locked():
                    break
            await asyncio.gather(*spawned, return_exceptions=True)
            assert len(spawned) >= 1
            assert guardian.last_check_at is not None
            assert device._clock_synced is True  # the check's sync finished, it did not stall
            # A completed check cancels the not-yet-needed grace timer.
            assert guardian_module.INITIAL_CHECK_GRACE_SECONDS in timers.cancelled
            await _cleanup(device)

    asyncio.run(scenario())


def test_first_check_still_runs_after_the_grace_when_the_link_never_comes_up(monkeypatch):
    async def scenario():
        timers = _patch_timers(monkeypatch)

        async def never_connects(*_args, **_kwargs):
            await asyncio.Event().wait()

        spawned: list[asyncio.Task] = []
        with patch.object(client_module, "establish_connection", never_connects):
            device = _hold_device()
            guardian = ScheduleGuardian(device, expected_mode="manual")
            guardian.async_check = AsyncMock(return_value=guardian_module.STATUS_UNREACHABLE)
            guardian.start_runner(MagicMock(), lambda coro: spawned.append(asyncio.ensure_future(coro)))
            assert spawned == []

            (_delay, fire), = timers.later
            fire(None)  # the grace elapsed with the link still down
            await asyncio.gather(*spawned)

            guardian.async_check.assert_awaited_once()
            await _cleanup(device)

    asyncio.run(scenario())


def test_a_connect_trigger_skipped_behind_a_running_check_is_retried_after_it(monkeypatch):
    async def scenario():
        _patch_timers(monkeypatch)
        listeners: list = []
        device = SimpleNamespace(
            mac=MAC,
            closing=False,
            register_connection_listener=lambda cb: listeners.append(cb) or (lambda: None),
        )
        guardian = ScheduleGuardian(device, expected_mode="manual")
        release = asyncio.Event()
        runs: list[int] = []

        async def check():
            runs.append(len(runs))
            if len(runs) == 1:
                await release.wait()
            return guardian_module.STATUS_OK

        # A real lock acquisition around the stub, as async_check does.
        async def locked_check():
            async with guardian._check_lock:
                return await check()

        guardian.async_check = locked_check
        spawned: list[asyncio.Task] = []
        guardian.start_runner(MagicMock(), lambda coro: spawned.append(asyncio.ensure_future(coro)))
        await asyncio.sleep(0)  # the first check is now waiting inside the lock
        assert guardian._check_lock.locked()

        listeners[0](True)  # link came up while that check was still running
        assert len(spawned) == 1  # skipped, not stacked
        release.set()
        await asyncio.gather(*spawned)

        assert runs == [0, 1]  # the skipped trigger was retried once the first finished

    asyncio.run(scenario())


def test_guardian_clock_sync_step_is_capped_at_ten_seconds():
    assert guardian_module.CHECK_CLOCK_SYNC_TIMEOUT <= 10
    assert guardian_module.CHECK_READ_STATE_TIMEOUT <= 10


# ---------------------------------------------------------------------------
# Whole-integration view: what Home Assistant's startup would wait for
# ---------------------------------------------------------------------------


class _TrackingHass:
    """Home Assistant's task bookkeeping: tracked tasks block, background ones do not."""

    def __init__(self):
        self.data = {DOMAIN: {}}
        self.state = integration.CoreState.running
        self.tracked: set[asyncio.Task] = set()
        self.background: set[asyncio.Task] = set()
        self.config_entries = SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_update_entry=MagicMock(),
        )

    def async_add_shutdown_job(self, _job, *_args):
        return lambda: None

    def async_create_task(self, coro, *_args, **_kwargs):
        task = asyncio.ensure_future(coro)
        self.tracked.add(task)
        task.add_done_callback(self.tracked.discard)
        return task

    def async_create_background_task(self, coro, *_args, **_kwargs):
        task = asyncio.ensure_future(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    async def async_block_till_done(self):
        await asyncio.sleep(0)
        while self.tracked:
            await asyncio.wait(set(self.tracked))


class _TrackingEntry:
    entry_id = "entry_1"
    title = "Fluval Test"

    def __init__(self, hass):
        self.data = {"mac": MAC, "model": "AquaSky 2.0 Bluetooth LED"}
        self.options = {"active_time": 0}
        self.unique_id = None
        self._hass = hass

    def async_on_unload(self, _func):
        pass

    def async_create_background_task(self, hass, target, name, eager_start=True):
        return hass.async_create_background_task(target, name)


def _setup_patches(stack, info):
    for target, value in (
        ("async_setup_link_watcher", MagicMock(return_value=MagicMock())),
        ("_register_static_paths", AsyncMock()),
        ("_register_websocket", MagicMock()),
        ("_register_services", MagicMock()),
        ("_async_register_services", MagicMock()),
        ("_migrate_legacy_registry_entries", MagicMock()),
        ("_cleanup_duplicate_devices", MagicMock()),
        ("_sync_product_identity", MagicMock()),
        ("_async_migrate_legacy_auto_schedule", AsyncMock()),
    ):
        stack.enter_context(patch.object(integration, target, value))
    stack.enter_context(
        patch.object(integration.bluetooth, "async_last_service_info", return_value=info, create=True)
    )
    stack.enter_context(
        patch.object(integration.bluetooth, "async_register_callback", side_effect=lambda *_a: lambda: None)
    )


def test_block_till_done_returns_at_once_after_setup_even_if_clock_sync_hangs_forever(monkeypatch):
    async def scenario():
        timers = _patch_timers(monkeypatch)
        hass = _TrackingHass()
        entry = _TrackingEntry(hass)
        info = SimpleNamespace(device=_ble_device(), advertisement=_advertisement(), source="esphome_proxy")

        async def clock_sync_hangs_forever(self, *args, **kwargs):
            await asyncio.Event().wait()

        async def connect_hangs_forever(*_args, **_kwargs):
            await asyncio.Event().wait()

        with ExitStack() as stack:
            _setup_patches(stack, info)
            stack.enter_context(patch.object(Device, "async_sync_clock", clock_sync_hangs_forever))
            stack.enter_context(patch.object(client_module, "establish_connection", connect_hangs_forever))
            assert await asyncio.wait_for(integration.async_setup_entry(hass, entry), 5) is True
            started = time.monotonic()
            await asyncio.wait_for(hass.async_block_till_done(), 2)
            assert time.monotonic() - started < 1.0
            assert hass.tracked == set()

            # The link never comes up, so the first check starts after the
            # grace period - and it is a background task, with its clock sync
            # hanging forever, so startup still has nothing to wait for.
            (_delay, grace_elapsed), = timers.later
            grace_elapsed(None)
            await asyncio.sleep(0.05)
            assert len(hass.background) >= 1
            await asyncio.wait_for(hass.async_block_till_done(), 2)
            assert hass.tracked == set()

            runtime = integration.entry_runtime_data(hass, entry)
            await _cleanup(runtime.device)
            for task in list(hass.background):
                task.cancel()
            await asyncio.gather(*hass.background, return_exceptions=True)

    asyncio.run(scenario())


def test_a_user_command_at_startup_is_not_queued_behind_the_guardian(monkeypatch):
    """The real startup stall: an automation's service call (a tracked task) waiting on the device lock."""

    async def scenario():
        _fast_gatt(monkeypatch)
        _patch_timers(monkeypatch)
        hass = _TrackingHass()
        entry = _TrackingEntry(hass)
        info = SimpleNamespace(device=_ble_device(), advertisement=_advertisement(), source="esphome_proxy")

        async def slow_connect(*_args, **_kwargs):
            await asyncio.sleep(0.2)
            return _FakeGatt()

        with ExitStack() as stack:
            _setup_patches(stack, info)
            # The real guardian, with the real clock sync - the stall happened with nothing faked.
            stack.enter_context(patch.object(client_module, "establish_connection", slow_connect))
            assert await asyncio.wait_for(integration.async_setup_entry(hass, entry), 5) is True
            runtime = integration.entry_runtime_data(hass, entry)
            device = runtime.device

            async def automation():
                async with device.command_transaction(priority=True):
                    return True

            started = time.monotonic()
            result = await asyncio.wait_for(hass.async_create_task(automation()), 5)
            waited = time.monotonic() - started
            await asyncio.wait_for(hass.async_block_till_done(), 5)

            assert result is True
            # Used to be the guardian's full 30 s clock-sync deadline.
            assert waited < 1.0

            await asyncio.sleep(0.5)  # let the connect-triggered check run to completion
            await _cleanup(device)
            for task in list(hass.background):
                task.cancel()
            await asyncio.gather(*hass.background, return_exceptions=True)

    asyncio.run(scenario())
