"""Backend timeouts beat the outer guards, and a detached link is always released.

Startup contract S8: the outer `asyncio.timeout` guards must never be what
stops a stalled ESPHome-proxy subscribe (cancelling it leaves an abandoned
notification handler behind), so each GATT call passes the backend its own
shorter `timeout`. And a link that has been detached from the Device must be
disconnected even if Home Assistant cancels the task that was doing the
disconnecting (an entry-owned guardian check on reload or release_link).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bleak import BleakError

from custom_components.fluvalble.core import client as client_module
from custom_components.fluvalble.core import guardian as guardian_module
from custom_components.fluvalble.core.client import Client
from custom_components.fluvalble.core.device import Device
from custom_components.fluvalble.core.guardian import ScheduleGuardian

MAC = "44:A6:E5:70:F1:8D"
NOTIFY_UUID = client_module.NOTIFY_UUIDS[0]


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


def _ble_device():
    return SimpleNamespace(address=MAC, name="AquaSky", details={"source": "esphome_proxy"})


def _make_client(**kwargs):
    with patch("asyncio.create_task", side_effect=lambda coro: _FakeTask(coro)):
        return Client(_ble_device(), **kwargs)


class _EsphomeLikeGatt:
    """A proxy backend whose subscribe is never acknowledged.

    Mirrors aioesphomeapi: the notification handler is registered BEFORE the
    acknowledgement is awaited and removed only when the backend itself raises
    (its own `timeout`), never when the awaiting task is cancelled.
    """

    def __init__(self):
        self.is_connected = True
        self.handlers: list[str] = []
        self.start_notify_kwargs: list[dict] = []

    async def start_notify(self, uuid, _callback, **kwargs):
        self.start_notify_kwargs.append(kwargs)
        self.handlers.append(uuid)
        try:
            await asyncio.sleep(kwargs.get("timeout", 30.0))
            raise BleakError("proxy never acknowledged the subscribe")
        except Exception:
            self.handlers.remove(uuid)
            raise

    async def disconnect(self):
        self.is_connected = False


def test_backend_timeouts_are_shorter_than_the_outer_guards():
    # bleak-esphome bounds EACH proxy round trip (subscribe, then CCCD write).
    assert 2 * client_module.START_NOTIFY_TIMEOUT <= client_module.GATT_OP_DEADLINE
    assert client_module.GATT_READ_TIMEOUT < client_module.GATT_OP_DEADLINE
    assert client_module.CONNECT_TIMEOUT < client_module.CONNECT_DEADLINE


def test_unacknowledged_subscribe_ends_through_the_backends_own_timeout(monkeypatch):
    async def scenario():
        monkeypatch.setattr(client_module, "START_NOTIFY_TIMEOUT", 0.02)
        monkeypatch.setattr(client_module, "GATT_OP_DEADLINE", 0.5)
        client = _make_client()
        gatt = _EsphomeLikeGatt()
        client._resolve_characteristics = AsyncMock()
        client.notify_uuids = [NOTIFY_UUID]

        with patch.object(client_module, "establish_connection", new=AsyncMock(return_value=gatt)):
            await asyncio.wait_for(client._ensure_client(), timeout=5)

        assert gatt.start_notify_kwargs == [{"timeout": 0.02}]
        # The backend raised on its own timeout, so it unregistered the
        # handler; an outer cancellation would have left it registered.
        assert gatt.handlers == []
        assert client._broken is False

    asyncio.run(scenario())


def test_outer_cancellation_is_what_would_have_leaked_the_handler(monkeypatch):
    """Control: with no backend timeout the outer guard fires and the handler leaks."""

    async def scenario():
        gatt = _EsphomeLikeGatt()
        monkeypatch.setattr(client_module, "GATT_OP_DEADLINE", 0.02)
        client = _make_client()
        with patch.object(Client, "_disconnect_detached", new=AsyncMock()):
            try:
                await client._bounded(gatt.start_notify(NOTIFY_UUID, None), 0.02, "start_notify")
            except BleakError:
                pass
        assert gatt.handlers == [NOTIFY_UUID]

    asyncio.run(scenario())


def test_state_and_wake_reads_get_a_backend_timeout():
    async def scenario():
        client = _make_client()
        gatt = SimpleNamespace(is_connected=True, read_gatt_char=AsyncMock(return_value=b""))
        client.client = gatt
        client.wake_read_uuid = client_module.WAKE_READ_UUIDS[0]
        client.raw_facebd = True
        client.command_write_uuid = None
        client.state_read_uuids = []
        client.init_write_uuid = None
        client._state_update_event.set()

        await client.request_state()

        gatt.read_gatt_char.assert_awaited_once_with(
            client_module.WAKE_READ_UUIDS[0], timeout=client_module.GATT_READ_TIMEOUT
        )

    asyncio.run(scenario())


class _SlowGatt:
    """An open link whose disconnect and unsubscribe take a moment, and are recorded."""

    def __init__(self):
        self.is_connected = True
        self.disconnect_started = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.stop_notify_started = asyncio.Event()

    async def stop_notify(self, _uuid):
        self.stop_notify_started.set()
        await asyncio.sleep(0.05)

    async def disconnect(self):
        self.disconnect_started.set()
        await asyncio.sleep(0.05)
        self.is_connected = False
        self.disconnected.set()


def _device_with_open_link():
    device = Device("Fluval Test", config_data={"mac": MAC, "model": "AquaSky 2.0 Bluetooth LED"})
    client = _make_client(active_time=0)
    gatt = _SlowGatt()
    client.client = gatt
    client.notify_uuids = [NOTIFY_UUID]
    device.client = client
    return device, gatt


def test_cancelling_the_task_that_resets_the_connection_still_disconnects_the_link():
    async def scenario():
        device, gatt = _device_with_open_link()
        reset = asyncio.create_task(device.async_reset_connection())
        await asyncio.wait_for(gatt.stop_notify_started.wait(), 1)
        # Reload / release_link: HA cancels the entry-owned task mid-teardown.
        reset.cancel()
        await asyncio.gather(reset, return_exceptions=True)
        assert reset.cancelled()
        assert device.client is None  # detached first: nothing else can find it

        await asyncio.wait_for(gatt.disconnected.wait(), 2)
        assert gatt.is_connected is False

    asyncio.run(scenario())


def test_cancelling_the_guardian_check_that_timed_out_still_disconnects_the_link(monkeypatch):
    async def scenario():
        monkeypatch.setattr(guardian_module, "CHECK_OVERALL_TIMEOUT", 0.02)
        device, gatt = _device_with_open_link()
        guardian = ScheduleGuardian(device, expected_mode="auto")

        async def hangs_forever():
            await asyncio.Event().wait()

        guardian._async_check_locked = hangs_forever
        check = asyncio.create_task(guardian.async_check())
        # The check times out, resets the connection and is now awaiting Client.stop().
        await asyncio.wait_for(gatt.stop_notify_started.wait(), 2)
        assert device.client is None
        check.cancel()
        await asyncio.gather(check, return_exceptions=True)
        assert check.cancelled()

        await asyncio.wait_for(gatt.disconnected.wait(), 2)
        assert gatt.is_connected is False

    asyncio.run(scenario())


def test_cancelling_a_connect_after_the_link_opened_still_disconnects_it():
    async def scenario():
        client = _make_client()
        gatt = _SlowGatt()
        started = asyncio.Event()

        async def hangs_subscribing(*_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()

        gatt.start_notify = hangs_subscribing
        client._resolve_characteristics = AsyncMock()
        client.notify_uuids = [NOTIFY_UUID]

        with patch.object(client_module, "establish_connection", new=AsyncMock(return_value=gatt)):
            connect = asyncio.create_task(client._ensure_client())
            await asyncio.wait_for(started.wait(), 1)
            connect.cancel()
            await asyncio.gather(connect, return_exceptions=True)

        await asyncio.wait_for(gatt.disconnected.wait(), 2)
        assert client.client is None

    asyncio.run(scenario())
