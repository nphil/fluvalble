"""Tests for config-entry lifecycle cleanup."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
import types
from unittest.mock import AsyncMock, MagicMock

from homeassistant import config_entries

from custom_components.fluvalble import (
    DOMAIN,
    FluvalRuntimeData,
    _store_entry_runtime_data,
    _register_static_paths,
    async_unload_entry,
    entry_runtime_data,
)
from custom_components.fluvalble import binary_sensor, button, light, select, sensor, switch
from custom_components.fluvalble.config_flow import OptionsFlowHandler


def test_options_reload_belongs_to_the_options_flow_alone():
    """An update listener next to OptionsFlowWithReload is an error from Home Assistant 2026.12."""
    package = Path(__file__).parents[1] / "custom_components" / "fluvalble"
    offenders = [
        path.name for path in package.rglob("*.py") if "add_update_listener" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
    assert issubclass(OptionsFlowHandler, config_entries.OptionsFlowWithReload)


def test_runtime_data_falls_back_to_hass_data_on_home_assistant_2024_1():
    """Legacy ConfigEntry objects have no runtime_data slot."""

    class LegacyConfigEntry:
        __slots__ = ("entry_id",)

        def __init__(self):
            self.entry_id = "entry_1"

    entry = LegacyConfigEntry()
    hass = SimpleNamespace(data={DOMAIN: {}})
    runtime = FluvalRuntimeData()

    _store_entry_runtime_data(hass, entry, runtime)

    assert not hasattr(entry, "runtime_data")
    assert entry_runtime_data(hass, entry) is runtime


def test_runtime_data_uses_config_entry_slot_when_available():
    entry = SimpleNamespace(entry_id="entry_1", runtime_data=None)
    hass = SimpleNamespace(data={DOMAIN: {}})
    runtime = FluvalRuntimeData()

    _store_entry_runtime_data(hass, entry, runtime)

    assert entry.runtime_data is runtime
    assert entry_runtime_data(hass, entry) is runtime


def test_all_entity_platforms_support_legacy_runtime_storage():
    asyncio.run(_async_test_all_entity_platforms_support_legacy_runtime_storage())


async def _async_test_all_entity_platforms_support_legacy_runtime_storage():
    entry = SimpleNamespace(entry_id="entry_1")
    runtime = FluvalRuntimeData()
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: runtime}})

    for platform_module, platform in (
        (binary_sensor, "binary_sensor"),
        (button, "button"),
        (light, "light"),
        (select, "select"),
        (sensor, "sensor"),
        (switch, "switch"),
    ):
        add_entities = MagicMock()
        await platform_module.async_setup_entry(hass, entry, add_entities)
        add_entities.assert_not_called()
        assert runtime.pending_add_entities[platform] is add_entities


def test_unload_stops_software_preview_task():
    """A reload must not leave the software preview writing to BLE."""
    asyncio.run(_async_test_unload_stops_software_preview_task())


async def _async_test_unload_stops_software_preview_task():
    preview_task = MagicMock()
    device = SimpleNamespace(
        preview_task=preview_task,
        native_preview_active=False,
        cancel_reachability_refresh=MagicMock(),
        async_stop_preview=AsyncMock(return_value=True),
        client=None,
    )
    runtime = FluvalRuntimeData(device=device)
    entry = SimpleNamespace(entry_id="entry_1", runtime_data=runtime)
    hass = SimpleNamespace(
        data={DOMAIN: {entry.entry_id: runtime}},
        config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)),
    )

    assert await async_unload_entry(hass, entry)

    device.cancel_reachability_refresh.assert_called_once_with()
    device.async_stop_preview.assert_awaited_once_with()
    assert entry.entry_id not in hass.data[DOMAIN]


def test_static_path_prefers_current_home_assistant_api(monkeypatch):
    """Use the collection-based API while retaining the legacy fallback."""
    asyncio.run(_async_test_static_path_prefers_current_home_assistant_api(monkeypatch))


async def _async_test_static_path_prefers_current_home_assistant_api(monkeypatch):
    class StaticPathConfig:
        def __init__(self, url_path, path, cache_headers):
            self.url_path = url_path
            self.path = path
            self.cache_headers = cache_headers

    http_module = types.ModuleType("homeassistant.components.http")
    http_module.StaticPathConfig = StaticPathConfig
    monkeypatch.setitem(sys.modules, "homeassistant.components.http", http_module)

    register_many = AsyncMock()
    register_one = AsyncMock()
    hass = SimpleNamespace(
        data={DOMAIN: {}},
        http=SimpleNamespace(
            async_register_static_paths=register_many,
            async_register_static_path=register_one,
        ),
    )

    await _register_static_paths(hass)

    register_many.assert_awaited_once()
    register_one.assert_not_awaited()
    config = register_many.await_args.args[0][0]
    assert config.url_path == "/fluvalble"
    assert config.cache_headers is False
