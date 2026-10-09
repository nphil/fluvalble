"""Silent ghost links: a light held by a proxy's radio after the proxy's host forgot it.

The light believes it is still connected, never advertises, and (being a
request/reply light) sends nothing the house's orphan-frame detector could
see. After `GHOST_SILENCE` without any advertisement the held-link supervisor
asks the proxy that last held the light to disconnect connection handles 0..3
one at a time (`esphome.<proxy>_force_disconnect_handle`) until the light is
heard again. See `core/ghost.py`.
"""

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak import BleakError

from custom_components.fluvalble.core import client as client_module
from custom_components.fluvalble.core import ghost as ghost_module
from custom_components.fluvalble.core.client import Client
from custom_components.fluvalble.core.device import Device
from custom_components.fluvalble.core.ghost import GhostLinkWatch

ADDRESS = "AA:BB:CC:DD:EE:FF"
PROXY = "plant-room-bluetooth-proxy"
SERVICE = "plant_room_bluetooth_proxy_force_disconnect_handle"


@pytest.fixture(autouse=True)
def _fast_timing(monkeypatch):
    monkeypatch.setattr(ghost_module, "GHOST_SILENCE", 0.2)
    monkeypatch.setattr(ghost_module, "GHOST_LISTEN", 0.05)
    monkeypatch.setattr(ghost_module, "GHOST_LISTEN_STEP", 0.01)
    monkeypatch.setattr(ghost_module, "GHOST_RETRY", (0.6,))
    monkeypatch.setattr(client_module, "reconnect_delay", lambda attempt: 0.02)


class FakeLight:
    """A light that is silent while a proxy ghost holds it; `advertising` forces it to be heard."""

    def __init__(self, *, ghosted=True, heard=False):
        self.ghosted = ghosted
        self.advertising = heard
        self.connected = False

    def last_service_info(self, _hass, address, connectable=True):
        assert address == ADDRESS and connectable is True
        if self.connected or (self.ghosted and not self.advertising):
            return None
        return SimpleNamespace(time=time.monotonic())


class FakeProxy:
    """Stands in for hass.services with the proxy action registered (or not)."""

    def __init__(self, light, *, has_action=True, frees_handle=None):
        self.light = light
        self.has_action = has_action
        self.frees_handle = frees_handle
        self.calls: list[tuple[float, int]] = []

    def has_service(self, domain, service):
        return self.has_action and (domain, service) == ("esphome", SERVICE)

    async def async_call(self, domain, service, data, blocking=False):
        assert (domain, service, blocking) == ("esphome", SERVICE, True)
        self.calls.append((time.monotonic(), data["handle"]))
        if data["handle"] == self.frees_handle:
            self.light.ghosted = False


def _watch(proxy, *, closing=lambda: False, holder=PROXY):
    hass = SimpleNamespace(services=proxy)
    return GhostLinkWatch(hass, ADDRESS, lambda: holder, closing)


def _client(watch):
    ble_device = MagicMock()
    ble_device.address = ADDRESS
    with patch("asyncio.create_task", side_effect=lambda coro: coro.close()):
        client = Client(ble_device, active_time=0, ping_interval=1, ghost_watch=watch)
    client.connect_task = None
    client.ping_time = float("inf")
    client.wake_read_uuid = None
    return client


def _held_link(client, light):
    """Wire `client` so it connects exactly when the light is reachable, and stops after one session."""

    async def ensure_client():
        if light.ghosted:
            raise BleakError("no route to the light")
        light.connected = True
        client.client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())
        return client.client

    async def initialize_session(_client):
        client._stopping = True  # end the loop after the first good session
        return True

    client._ensure_client = ensure_client
    client._initialize_session = initialize_session


async def _run_loop(client, seconds):
    task = asyncio.create_task(client._ping_loop())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=seconds)
    except TimeoutError:
        pass
    finally:
        client._stopping = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _patched(light):
    return patch.object(ghost_module.bluetooth, "async_last_service_info", side_effect=light.last_service_info)


def test_ghost_is_freed_on_the_handle_that_holds_it_and_the_light_reconnects(caplog):
    asyncio.run(_async_test_ghost_is_freed(caplog))


async def _async_test_ghost_is_freed(caplog):
    light = FakeLight(ghosted=True)
    proxy = FakeProxy(light, frees_handle=2)
    watch = _watch(proxy)
    client = _client(watch)
    _held_link(client, light)

    with _patched(light), caplog.at_level(logging.INFO, logger=ghost_module.__name__):
        await asyncio.wait_for(client._ping_loop(), timeout=5)

    assert [handle for _at, handle in proxy.calls] == [0, 1, 2]  # stops once the light is heard
    assert light.connected is True  # the supervisor reconnected right after
    assert watch.links_freed == 1
    assert watch.last_try == f"freed connection 2 on {PROXY}"
    assert watch.snapshot() == {"ghost_links_freed": 1, "last_ghost_try": f"freed connection 2 on {PROXY}"}
    assert f"freed a ghost link on {PROXY} (connection 2)" in caplog.text


def test_nothing_is_sent_before_the_silence_window():
    asyncio.run(_async_test_nothing_is_sent_before_the_silence_window())


async def _async_test_nothing_is_sent_before_the_silence_window():
    light = FakeLight(ghosted=True)
    proxy = FakeProxy(light, frees_handle=0)
    watch = _watch(proxy)

    with _patched(light):
        await watch.free_silent_ghost(lambda: False)

    assert proxy.calls == []


def test_a_light_that_advertises_but_cannot_be_connected_is_never_swept():
    asyncio.run(_async_test_heard_but_unconnectable_is_never_swept())


async def _async_test_heard_but_unconnectable_is_never_swept():
    light = FakeLight(ghosted=False, heard=True)
    proxy = FakeProxy(light, frees_handle=0)
    watch = _watch(proxy)
    client = _client(watch)

    async def refuse():
        raise BleakError("connection slots full")

    client._ensure_client = refuse

    with _patched(light):
        await _run_loop(client, ghost_module.GHOST_SILENCE * 4)

    assert proxy.calls == []
    assert watch.links_freed == 0
    assert watch.last_try is None


def test_a_proxy_without_the_action_is_left_alone():
    asyncio.run(_async_test_a_proxy_without_the_action_is_left_alone())


async def _async_test_a_proxy_without_the_action_is_left_alone():
    light = FakeLight(ghosted=True)
    proxy = FakeProxy(light, has_action=False, frees_handle=0)
    watch = _watch(proxy)
    client = _client(watch)
    client._ensure_client = AsyncMock(side_effect=BleakError("no route"))

    with _patched(light):
        await _run_loop(client, ghost_module.GHOST_SILENCE * 3)

    assert proxy.calls == []
    assert watch.links_freed == 0
    assert watch.last_try == f"silent; {PROXY} cannot free a link"


def test_no_recorded_proxy_means_nothing_is_sent():
    asyncio.run(_async_test_no_recorded_proxy_means_nothing_is_sent())


async def _async_test_no_recorded_proxy_means_nothing_is_sent():
    light = FakeLight(ghosted=True)
    proxy = FakeProxy(light, frees_handle=0)
    watch = _watch(proxy, holder=None)

    with _patched(light):
        await asyncio.sleep(ghost_module.GHOST_SILENCE + 0.05)
        await watch.free_silent_ghost(lambda: False)

    assert proxy.calls == []
    assert watch.last_try == "silent; no proxy known cannot free a link"


def test_a_light_that_stays_silent_is_retried_after_the_pause_counted_from_the_end_of_the_pass(caplog):
    asyncio.run(_async_test_still_silent_waits_the_retry_pause(caplog))


async def _async_test_still_silent_waits_the_retry_pause(caplog):
    light = FakeLight(ghosted=True)  # unpowered: no handle brings it back
    proxy = FakeProxy(light, frees_handle=None)
    watch = _watch(proxy)
    client = _client(watch)
    client._ensure_client = AsyncMock(side_effect=BleakError("no route"))

    with _patched(light), caplog.at_level(logging.WARNING, logger=ghost_module.__name__):
        await _run_loop(client, ghost_module.GHOST_SILENCE + 0.6 + 0.6)

    handles = [handle for _at, handle in proxy.calls]
    assert handles[:8] == [0, 1, 2, 3, 0, 1, 2, 3]  # every handle each pass, one at a time
    first_pass_start, first_pass_last_call = proxy.calls[0][0], proxy.calls[3][0]
    second_pass_start = proxy.calls[4][0]
    listen = ghost_module.GHOST_LISTEN
    # The pause starts after the last handle's listen window, not when the pass began.
    assert second_pass_start - first_pass_last_call >= listen + ghost_module.GHOST_RETRY[0] - 0.02
    assert second_pass_start - first_pass_start >= 4 * listen + ghost_module.GHOST_RETRY[0] - 0.02
    assert watch.links_freed == 0
    assert watch.last_try == f"still silent after freeing connections on {PROXY}"
    assert (
        f"still silent after freeing every connection on {PROXY}; it may be unpowered (next try in 0 min)"
        in caplog.text
    )


def test_the_silence_clock_restarts_when_a_session_ends():
    asyncio.run(_async_test_the_silence_clock_restarts_when_a_session_ends())


async def _async_test_the_silence_clock_restarts_when_a_session_ends():
    light = FakeLight(ghosted=True)
    proxy = FakeProxy(light, frees_handle=0)
    watch = _watch(proxy)

    with _patched(light):
        await asyncio.sleep(ghost_module.GHOST_SILENCE + 0.05)
        watch.session_started()  # connected: not a ghost, nothing to sweep
        await watch.free_silent_ghost(lambda: False)
        assert proxy.calls == []
        watch.session_ended()  # the link drops; silence counts from here
        await watch.free_silent_ghost(lambda: False)
        assert proxy.calls == []
        await asyncio.sleep(ghost_module.GHOST_SILENCE + 0.05)
        await watch.free_silent_ghost(lambda: False)

    assert [handle for _at, handle in proxy.calls] == [0]


def test_ending_nothing_does_not_push_the_first_try_into_the_future():
    asyncio.run(_async_test_ending_nothing_does_not_push_the_first_try_back())


async def _async_test_ending_nothing_does_not_push_the_first_try_back():
    light = FakeLight(ghosted=True)
    proxy = FakeProxy(light, frees_handle=0)
    watch = _watch(proxy)

    with _patched(light):
        await asyncio.sleep(ghost_module.GHOST_SILENCE + 0.05)
        watch.session_ended()  # e.g. a guardian reset with no live link
        await watch.free_silent_ghost(lambda: False)

    assert [handle for _at, handle in proxy.calls] == [0]


def test_the_shutdown_latch_stops_the_sweep_before_and_during_a_pass():
    asyncio.run(_async_test_the_shutdown_latch_stops_the_sweep())


async def _async_test_the_shutdown_latch_stops_the_sweep():
    light = FakeLight(ghosted=True)
    proxy = FakeProxy(light, frees_handle=None)
    state = {"closing": True}
    watch = _watch(proxy, closing=lambda: state["closing"])

    with _patched(light):
        await asyncio.sleep(ghost_module.GHOST_SILENCE + 0.05)
        await watch.free_silent_ghost(lambda: False)
        assert proxy.calls == []  # latched: never acts

        state["closing"] = False
        original = proxy.async_call

        async def latch_after_first(domain, service, data, blocking=False):
            await original(domain, service, data, blocking=blocking)
            state["closing"] = True

        proxy.async_call = latch_after_first
        await watch.free_silent_ghost(lambda: False)

    assert [handle for _at, handle in proxy.calls] == [0]  # the latch ends the pass at once


def test_a_link_that_came_up_during_the_pass_is_never_disconnected():
    asyncio.run(_async_test_a_link_that_came_up_is_never_disconnected())


async def _async_test_a_link_that_came_up_is_never_disconnected():
    light = FakeLight(ghosted=True)
    proxy = FakeProxy(light, frees_handle=None)
    watch = _watch(proxy)
    client = _client(watch)

    with _patched(light):
        await asyncio.sleep(ghost_module.GHOST_SILENCE + 0.05)
        # A command connected through the normal path while the supervisor was about to sweep.
        client.client = SimpleNamespace(is_connected=True)
        await client._free_silent_ghost()
        assert proxy.calls == []

        client.client = None
        client._stopping = True  # stopping client: same
        await client._free_silent_ghost()
        assert proxy.calls == []


def test_a_failing_proxy_call_never_raises_out_of_the_supervisor(caplog):
    asyncio.run(_async_test_a_failing_proxy_call_never_raises(caplog))


async def _async_test_a_failing_proxy_call_never_raises(caplog):
    light = FakeLight(ghosted=True)
    proxy = FakeProxy(light, frees_handle=None)
    proxy.async_call = AsyncMock(side_effect=RuntimeError("proxy offline"))
    watch = _watch(proxy)
    client = _client(watch)

    with _patched(light), caplog.at_level(logging.WARNING, logger=ghost_module.__name__):
        await asyncio.sleep(ghost_module.GHOST_SILENCE + 0.05)
        await client._free_silent_ghost()  # must not raise

    assert watch.links_freed == 0
    assert watch.last_try == f"{PROXY} refused connection 0: proxy offline"
    assert "could not free connection 0" in caplog.text


def test_a_call_that_hangs_is_bounded(monkeypatch):
    asyncio.run(_async_test_a_call_that_hangs_is_bounded(monkeypatch))


async def _async_test_a_call_that_hangs_is_bounded(monkeypatch):
    monkeypatch.setattr(ghost_module, "GHOST_CALL_DEADLINE", 0.05)
    light = FakeLight(ghosted=True)
    proxy = FakeProxy(light, frees_handle=None)

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(60)

    proxy.async_call = hang
    watch = _watch(proxy)

    with _patched(light):
        await asyncio.sleep(ghost_module.GHOST_SILENCE + 0.05)
        await asyncio.wait_for(watch.free_silent_ghost(lambda: False), timeout=2)

    assert watch.last_try.startswith(f"{PROXY} refused connection 0")


def test_device_wires_the_watch_to_the_recorded_proxy_and_exposes_diagnostics():
    device = Device(
        "Fluval",
        config_data={"mac": ADDRESS, "model": "AquaSky 2.0 Bluetooth LED", "product_id": 328},
        last_holder_getter=lambda: PROXY,
    )

    assert device.ghost_watch._last_holder() == PROXY
    assert device.ghost_watch._is_closing() is False
    device.closing = True
    assert device.ghost_watch._is_closing() is True  # the Device's shutdown latch, read live

    attributes = device.connection_hold_attributes()
    assert attributes["ghost_links_freed"] == 0
    assert attributes["last_ghost_try"] is None
    assert asyncio.run(device.async_collect_diagnostics())["ghost_link"] == {
        "ghost_links_freed": 0,
        "last_ghost_try": None,
    }
