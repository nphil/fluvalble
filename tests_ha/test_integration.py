"""Real Home Assistant tests for the Fluval BLE integration.

The Bluetooth radio is faked (an advertisement is handed to the integration and
the GATT ``Client`` is a stand-in that records calls); everything else - config
entry setup, the entity platforms, the device and entity registries, flows and
the issue registry - is real Home Assistant. They run on Home Assistant 2026.10
and 2026.11 alike.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity as ha_entity
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

try:  # Home Assistant 2026.11 and later
    from pytest_homeassistant.common import MockConfigEntry
except ImportError:  # the harness that shipped up to Home Assistant 2026.10
    from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fluvalble import DOMAIN, PLATFORMS, entry_runtime_data
from custom_components.fluvalble.core import CONFIG_ENTRY_VERSION

MAC = "AA:BB:CC:DD:EE:FF"
TITLE = "Fluval Plant 3.0"
# Exists only on Home Assistant 2026.11 and later: entity ids change in place there.
_requires_readd = getattr(ha_entity, "_entity_class_requires_readd", None)


class FakeClient:
    """Stand-in for ``core.client.Client``: no radio, records every teardown call."""

    instances: list[FakeClient] = []

    def __init__(self, device, *args, **kwargs):
        self.device = device
        self.is_connected = True
        self.stop = AsyncMock()
        self.disconnect = AsyncMock()
        self.ensure_connected = AsyncMock(return_value=True)
        self.request_state = AsyncMock(return_value=False)
        self.send_now = AsyncMock(return_value=False)
        self.send = MagicMock()
        self.ping = MagicMock()
        FakeClient.instances.append(self)

    @property
    def teardown_calls(self) -> int:
        return self.stop.await_count + self.disconnect.await_count

# Company id 0x3130 + payload "31" spell the ASCII product id "0131" (305, Fresh & Plant 500mm).
MANUFACTURER_DATA = {0x3130: b"31"}

def _service_info() -> BluetoothServiceInfoBleak:
    device = BLEDevice(MAC, TITLE, {"source": "local"})
    advertisement = AdvertisementData(
        local_name=TITLE,
        manufacturer_data=MANUFACTURER_DATA,
        service_data={},
        service_uuids=["00001000-0000-1000-8000-00805f9b34fb"],
        tx_power=None,
        rssi=-60,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=TITLE,
        address=MAC,
        rssi=-60,
        manufacturer_data=MANUFACTURER_DATA,
        service_data={},
        service_uuids=advertisement.service_uuids,
        source="local",
        device=device,
        advertisement=advertisement,
        connectable=True,
        time=time.monotonic(),
        tx_power=None,
        raw=None,
    )


@pytest.fixture
async def entry(hass, enable_bluetooth):
    """Set the integration up with a heard lamp and a fake GATT client."""
    FakeClient.instances.clear()
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={"mac": MAC},
        unique_id=MAC.lower(),
        title=TITLE,
        version=CONFIG_ENTRY_VERSION,
    )
    config_entry.add_to_hass(hass)
    with (
        patch.object(bluetooth, "async_last_service_info", return_value=_service_info()),
        patch("custom_components.fluvalble.core.device.Client", FakeClient),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        yield config_entry
        await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


def _own_entities(hass, config_entry) -> list[er.RegistryEntry]:
    """Registry entries that are enabled (the RSSI sensor ships disabled by default)."""
    entries = er.async_entries_for_config_entry(er.async_get(hass), config_entry.entry_id)
    return [e for e in entries if e.disabled_by is None]


def _live_entities(hass) -> dict[str, ha_entity.Entity]:
    """Every entity object this integration has added, by current entity_id."""
    found: dict[str, ha_entity.Entity] = {}
    for platform in entity_platform.async_get_platforms(hass, DOMAIN):
        found.update(platform.entities)
    return found


async def test_setup_creates_every_platform_and_the_device(hass, entry):
    """Setup through real Home Assistant builds all platforms and one registry device."""
    assert entry.state.name == "LOADED"
    # The switch platform only offers the FACEBD daylight-saving switch, which this fixture lacks.
    domains = {registry_entry.domain for registry_entry in _own_entities(hass, entry)}
    assert domains == {platform.value for platform in PLATFORMS} - {"switch"}

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    device = devices[0]
    assert (DOMAIN, MAC) in device.identifiers
    assert (dr.CONNECTION_BLUETOOTH, MAC) in device.connections
    assert device.manufacturer == "Fluval"
    assert device.name


async def test_no_update_listener_next_to_options_flow_with_reload(hass, entry):
    """Home Assistant 2026.12 raises an error for a listener next to OptionsFlowWithReload."""
    assert entry.update_listeners == []


@pytest.mark.skipif(_requires_readd is None, reason="entity classes are only checked on Home Assistant 2026.11+")
async def test_no_entity_class_needs_remove_and_readd(hass, entry):
    """Every class gets an in-place entity_id change, not the compatibility remove + add."""
    classes = {type(entity) for entity in _live_entities(hass).values()}
    assert len(classes) >= 8
    assert [cls.__name__ for cls in classes if _requires_readd(cls)] == []


async def test_entity_id_change_keeps_the_ble_link(hass, entry):
    """Renaming an entity must never drop, stop or rebuild the lamp's GATT link."""
    runtime = entry_runtime_data(hass, entry)
    device = runtime.device
    client = device.client
    assert isinstance(client, FakeClient)
    assert FakeClient.instances == [client]
    assert device.updates_component, "entities must be subscribed to the device"
    component_handlers = list(device.updates_component)
    connect_handlers = list(device.updates_connect)
    guardian_listeners = list(runtime.guardian._listeners) if hasattr(runtime.guardian, "_listeners") else None
    entities_before = _live_entities(hass)
    assert len(entities_before) == len(_own_entities(hass, entry))

    registry = er.async_get(hass)
    renamed: dict[str, str] = {}
    for registry_entry in _own_entities(hass, entry):
        new_id = f"{registry_entry.domain}.renamed_{registry_entry.unique_id.rsplit('_', 1)[-1]}"
        registry.async_update_entity(registry_entry.entity_id, new_entity_id=new_id)
        renamed[registry_entry.entity_id] = new_id
        await hass.async_block_till_done()

    entities_after = _live_entities(hass)
    for old_id, new_id in renamed.items():
        assert hass.states.get(new_id) is not None, new_id
        assert hass.states.get(old_id) is None, old_id
        assert old_id not in entities_after
        assert new_id in entities_after
        if _requires_readd is not None:
            # 2026.11: updated in place, the same object stays added.
            assert entities_after[new_id] is entities_before[old_id]

    # The link and the device were never touched, on either Home Assistant version.
    assert runtime.device is device
    assert device.client is client
    assert FakeClient.instances == [client]
    assert client.teardown_calls == 0
    assert device.connected == device.connected  # unchanged by a rename
    # Nothing stale is left subscribed and nothing is subscribed twice.
    assert len(device.updates_component) == len(component_handlers)
    assert len(device.updates_connect) == len(connect_handlers)
    assert set(map(id, device.updates_component)) == set(map(id, component_handlers)) or _requires_readd is None
    if guardian_listeners is not None:
        assert len(runtime.guardian._listeners) == len(guardian_listeners)
    # The registry still holds exactly the same entities, under their new ids.
    assert {e.entity_id for e in _own_entities(hass, entry)} == set(renamed.values())


async def test_entity_id_change_keeps_the_schedule_problem_repair(hass, entry):
    """The repair issue is keyed on the device, so a rename neither drops nor duplicates it."""
    guardian = entry_runtime_data(hass, entry).guardian
    registry = er.async_get(hass)
    problem = next(e for e in _own_entities(hass, entry) if e.unique_id.endswith("schedule_problem"))
    with patch.object(type(guardian), "problem", new=True):
        live = _live_entities(hass)[problem.entity_id]
        live.internal_update()
        await hass.async_block_till_done()
        issues = [i for i in ir.async_get(hass).issues.values() if i.domain == DOMAIN]
        assert len(issues) == 1
        assert issues[0].is_fixable is False

        registry.async_update_entity(problem.entity_id, new_entity_id="binary_sensor.renamed_problem")
        await hass.async_block_till_done()

        assert hass.states.get("binary_sensor.renamed_problem").state == "on"
        assert [i.issue_id for i in ir.async_get(hass).issues.values() if i.domain == DOMAIN] == [issues[0].issue_id]


async def test_config_flow_forms_have_a_step_id(hass, enable_bluetooth):
    """Every form a config flow shows names its step (an error from Home Assistant 2027.5)."""
    with patch.object(bluetooth, "async_discovered_service_info", return_value=[_service_info()]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"].name == "FORM"
    assert result["step_id"] == "user"
    hass.config_entries.flow.async_abort(result["flow_id"])

    with patch.object(bluetooth, "async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"].name == "FORM"
    assert result["step_id"] == "manual"
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_options_flow_has_a_step_id_and_reloads_exactly_once(hass, entry):
    """The options form names its step and saving changed options reloads the entry once."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"].name == "FORM"
    assert result["step_id"] == "init"

    with patch(
        "custom_components.fluvalble.async_unload_entry",
        wraps=__import__("custom_components.fluvalble", fromlist=["async_unload_entry"]).async_unload_entry,
    ) as unload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                "lamp_profile": "plant",
                "ping_interval": 15,
                "active_time": 0,
            },
        )
        await hass.async_block_till_done()
    assert result["type"].name == "CREATE_ENTRY"
    assert entry.options["lamp_profile"] == "plant"
    assert entry.update_listeners == []
    assert unload.await_count <= 1
