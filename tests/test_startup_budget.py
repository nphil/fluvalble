"""Startup must never wait on the fixture: setup returns at once, radio work runs in the background.

Home Assistant reports "initialized" only after every integration's setup
returns and after every task it tracks has finished. A Fluval fixture that is
absent, behind a busy proxy or hanging mid-handshake must therefore cost
startup nothing: setup builds objects and returns, and every connect (the
held-link supervisor, the guardian's first check) runs in a task owned by the
config entry (`entry.async_create_background_task`), which HA does not wait for.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import custom_components.fluvalble as integration
from custom_components.fluvalble import DOMAIN
from custom_components.fluvalble.core import client as client_module
from custom_components.fluvalble.light import FluvalLight

MAC = "44:A6:E5:70:F1:8D"
SETUP_BUDGET_SECONDS = 5


def _ble_device():
    return SimpleNamespace(address=MAC, name="AquaSky", details={"source": "esphome_proxy"})


def _advertisement():
    return SimpleNamespace(rssi=-60, service_uuids=[], service_data={}, manufacturer_data={})


def _service_info():
    return SimpleNamespace(device=_ble_device(), advertisement=_advertisement(), source="proxy")


class _Entry:
    """Config entry double that records the tasks it was asked to own."""

    entry_id = "entry_1"
    title = "Fluval Test"

    def __init__(self, *, hold=True):
        self.data = {"mac": MAC, "model": "AquaSky 2.0 Bluetooth LED"}
        # active_time 0 = held link: the supervisor connects as soon as the device exists.
        self.options = {"active_time": 0} if hold else {}
        self.unique_id = None
        self.background_tasks: list[asyncio.Task] = []
        self.task_names: list[str] = []
        self.unload_callbacks: list = []

    def async_on_unload(self, func):
        self.unload_callbacks.append(func)

    def async_create_background_task(self, hass, target, name, eager_start=True):
        task = asyncio.ensure_future(target)
        self.background_tasks.append(task)
        self.task_names.append(name)
        return task


def _hass(entry):
    hass = MagicMock()
    hass.data = {DOMAIN: {}}
    hass.state = integration.CoreState.running
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_update_entry = MagicMock()
    return hass


def _patches(registered, cached):
    return (
        patch.object(integration, "async_setup_link_watcher", return_value=MagicMock()),
        patch.object(integration, "_register_static_paths", new=AsyncMock()),
        patch.object(integration, "_register_websocket"),
        patch.object(integration, "_register_services"),
        patch.object(integration, "_async_register_services"),
        patch.object(integration, "_migrate_legacy_registry_entries"),
        patch.object(integration, "_cleanup_duplicate_devices"),
        patch.object(integration, "_sync_product_identity"),
        patch.object(integration, "_async_migrate_legacy_auto_schedule", new=AsyncMock()),
        patch.object(
            integration.bluetooth,
            "async_last_service_info",
            return_value=cached,
            create=True,
        ),
        patch.object(
            integration.bluetooth,
            "async_register_callback",
            side_effect=lambda _hass, callback, *_args: registered.append(callback) or (lambda: None),
        ),
    )


async def _cancel(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def test_setup_returns_at_once_when_every_connect_step_hangs_forever():
    """Fixture in the BLE cache, link held, establish_connection never answers."""

    async def scenario():
        entry = _Entry(hold=True)
        hass = _hass(entry)
        connect_started = asyncio.Event()

        async def hangs_forever(*_args, **_kwargs):
            connect_started.set()
            await asyncio.Event().wait()

        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in _patches([], _service_info()):
                stack.enter_context(p)
            stack.enter_context(patch.object(client_module, "establish_connection", hangs_forever))
            started = time.monotonic()
            result = await asyncio.wait_for(integration.async_setup_entry(hass, entry), SETUP_BUDGET_SECONDS)
            elapsed = time.monotonic() - started

            assert result is True
            assert elapsed < 1.0
            # The radio is being worked on, but only in the background.
            await asyncio.wait_for(connect_started.wait(), 1)
            runtime = integration.entry_runtime_data(hass, entry)
            assert runtime.device is not None and runtime.device.client is not None
            assert runtime.device.connected is False
            # Home Assistant's own tracked task list must not hold anything
            # that connects: it would delay "initialized". The first guardian
            # check is not started at all while the held link is connecting;
            # it runs (as an entry background task) when the link comes up,
            # or after a grace period (see test_startup_tracking).
            hass.async_create_task.assert_not_called()
            assert not any("guardian check" in name for name in entry.task_names)

            await _cancel(entry.background_tasks)
            await runtime.device.client.stop()

    asyncio.run(scenario())


def test_setup_returns_at_once_when_the_fixture_was_never_seen():
    async def scenario():
        entry = _Entry(hold=True)
        hass = _hass(entry)

        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in _patches([], None):
                stack.enter_context(p)
            started = time.monotonic()
            result = await asyncio.wait_for(integration.async_setup_entry(hass, entry), SETUP_BUDGET_SECONDS)

        assert result is True
        assert time.monotonic() - started < 1.0
        assert integration.entry_runtime_data(hass, entry).device is None
        assert not any("guardian check" in name for name in entry.task_names)

    asyncio.run(scenario())


def test_entities_are_added_when_the_first_advertisement_arrives_after_setup():
    async def scenario():
        entry = _Entry(hold=False)
        hass = _hass(entry)
        registered: list = []
        added: list = []

        async def forward(_entry, _platforms):
            # What light.async_setup_entry does with no device yet.
            runtime = integration.entry_runtime_data(hass, entry)
            runtime.pending_add_entities[integration.Platform.LIGHT] = added.extend

        hass.config_entries.async_forward_entry_setups = forward

        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in _patches(registered, None):
                stack.enter_context(p)
            assert await integration.async_setup_entry(hass, entry) is True
            assert added == []  # nothing fabricated while the fixture is unseen

            registered[0](_service_info(), None)  # first advertisement, long after setup

        assert [type(entity) for entity in added] == [FluvalLight]
        runtime = integration.entry_runtime_data(hass, entry)
        assert runtime.device is not None
        await _cancel(entry.background_tasks)

    asyncio.run(scenario())


def test_late_entities_and_first_data_never_actuate_the_fixture():
    """Entities appearing, and data arriving, only read state: no command goes out."""

    async def scenario():
        entry = _Entry(hold=False)
        hass = _hass(entry)
        registered: list = []
        added: list = []

        async def forward(_entry, _platforms):
            runtime = integration.entry_runtime_data(hass, entry)
            runtime.pending_add_entities[integration.Platform.LIGHT] = added.extend

        hass.config_entries.async_forward_entry_setups = forward

        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in _patches(registered, None):
                stack.enter_context(p)
            await integration.async_setup_entry(hass, entry)
            device = None
            with patch.object(integration, "async_setup_guardian", return_value=MagicMock()):
                registered[0](_service_info(), None)
            device = integration.entry_runtime_data(hass, entry).device

            actuated = []
            for name in (
                "async_set_switch",
                "async_apply_light_channels",
                "async_set_master_brightness",
                "async_select_option",
                "async_ensure_mode",
                "async_sync_clock",
            ):
                setattr(device, name, AsyncMock(side_effect=lambda *a, _n=name, **k: actuated.append(_n)))

            entity = added[0]
            entity.internal_update()  # unavailable -> first state
            device.values["led_on_off"] = True
            entity.internal_update()

        assert actuated == []

    asyncio.run(scenario())


def test_every_connect_and_gatt_step_is_capped_at_ten_seconds():
    """Startup contract S4: no single connect/subscribe/handshake step may hang longer than 10 s."""
    assert client_module.CONNECT_DEADLINE <= 10
    assert client_module.GATT_OP_DEADLINE <= 10
    assert client_module.DISCONNECT_DEADLINE <= 10
