"""
Shared fixtures and stubs for Fluval BLE integration tests.

Because the integration depends on homeassistant and bleak — neither of which
are installed in the lightweight CI environment — this module registers minimal
stubs for both before any test module is collected.  All stubs live here so
there is a single place to update if HA changes its API.
"""

import enum
import re
import sys
import types
from datetime import UTC, datetime
import pytest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# BLE stubs (bleak / bleak_retry_connector)
# ---------------------------------------------------------------------------


def _stub_bleak():
    mod = types.ModuleType("bleak")
    mod.AdvertisementData = object
    mod.BLEDevice = object
    mod.BleakClient = object
    mod.BleakError = Exception
    mod.BleakGATTCharacteristic = object
    mod.BleakScanner = MagicMock()
    sys.modules["bleak"] = mod

    brc = types.ModuleType("bleak_retry_connector")
    brc.establish_connection = MagicMock()
    sys.modules["bleak_retry_connector"] = brc


# ---------------------------------------------------------------------------
# Home Assistant stubs
# ---------------------------------------------------------------------------


def _stub_homeassistant():
    """Register minimal HA stubs so integration modules can be imported."""

    # ---- voluptuous ----
    vol = types.ModuleType("voluptuous")

    class _Schema:
        def __init__(self, schema=None, *args, **kwargs):
            self.schema = schema

        def __call__(self, value):
            return value

    def _identity(*args, **kwargs):
        return args[0] if args else None

    vol.Invalid = type("Invalid", (Exception,), {})
    vol.Schema = _Schema
    vol.Optional = _identity
    vol.Required = _identity
    vol.All = lambda *args, **kwargs: lambda value: value
    vol.Range = lambda *args, **kwargs: lambda value: value
    vol.In = lambda *args, **kwargs: lambda value: value

    # ---- homeassistant.exceptions ----
    class HomeAssistantError(Exception):
        def __init__(
            self,
            message=None,
            *,
            translation_domain=None,
            translation_key=None,
            translation_placeholders=None,
        ):
            self.translation_domain = translation_domain
            self.translation_key = translation_key
            self.translation_placeholders = translation_placeholders
            if message is None and translation_placeholders:
                message = translation_placeholders.get("error")
            super().__init__(message)

    ha_exc = types.ModuleType("homeassistant.exceptions")
    ha_exc.HomeAssistantError = HomeAssistantError

    # ---- homeassistant.const ----
    class Platform(str, enum.Enum):
        NUMBER = "number"
        BINARY_SENSOR = "binary_sensor"
        BUTTON = "button"
        SELECT = "select"
        SENSOR = "sensor"
        SWITCH = "switch"
        LIGHT = "light"

    class EntityCategory(str, enum.Enum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    ha_const = types.ModuleType("homeassistant.const")
    ha_const.ATTR_DEVICE_ID = "device_id"
    ha_const.ATTR_ENTITY_ID = "entity_id"
    ha_const.CONF_MAC = "mac"
    ha_const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
    ha_const.Platform = Platform
    ha_const.EntityCategory = EntityCategory

    # ---- homeassistant.core ----
    ha_core = types.ModuleType("homeassistant.core")
    ha_core.CoreState = enum.Enum("CoreState", {"running": "running"})
    ha_core.HomeAssistant = MagicMock
    ha_core.ServiceCall = MagicMock
    def _callback(f):  # mirrors homeassistant.core.callback: mark for loop dispatch
        f._hass_callback = True
        return f

    ha_core.callback = _callback

    # ---- homeassistant.config_entries ----
    class _FakeConfigEntry:
        def __init__(self, data=None, options=None, version=1):
            self.data = data or {}
            self.options = options or {}
            self.entry_id = "test_entry_id"
            self.version = version

    class _FakeConfigFlow:
        # HA uses ConfigFlow(domain=DOMAIN) as a class keyword arg.
        # Accept and ignore it so our stub works the same way.
        def __init_subclass__(cls, domain=None, **kwargs):
            super().__init_subclass__(**kwargs)

    class _FakeOptionsFlow:
        pass

    class _FakeOptionsFlowWithConfigEntry:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__(**kwargs)

        def __init__(self, config_entry=None):
            self.config_entry = config_entry or _FakeConfigEntry()

    class _FakeOptionsFlowWithReload(_FakeOptionsFlowWithConfigEntry):
        pass

    ha_ce = types.ModuleType("homeassistant.config_entries")
    ha_ce.ConfigEntry = _FakeConfigEntry
    ha_ce.ConfigFlow = _FakeConfigFlow
    ha_ce.OptionsFlow = _FakeOptionsFlow
    ha_ce.OptionsFlowWithConfigEntry = _FakeOptionsFlowWithConfigEntry
    ha_ce.OptionsFlowWithReload = _FakeOptionsFlowWithReload
    ha_ce.ConfigFlowResult = dict  # type alias in real HA
    # Only the states this integration branches on. `is` comparisons against
    # these members are exactly what repairs.py does with the real enum.
    ha_ce.ConfigEntryState = enum.Enum("ConfigEntryState", {"LOADED": "loaded", "NOT_LOADED": "not_loaded"})

    # ---- homeassistant.components.bluetooth ----
    ha_bt = types.ModuleType("homeassistant.components.bluetooth")
    ha_bt.BluetoothServiceInfoBleak = MagicMock
    ha_bt.BluetoothScanningMode = MagicMock()
    ha_bt.BluetoothChange = MagicMock
    ha_bt.async_discovered_service_info = MagicMock(return_value=[])
    ha_bt.async_ble_device_from_address = MagicMock(return_value=None)
    ha_bt.async_last_service_info = MagicMock(return_value=None)
    ha_bt.async_scanner_by_source = MagicMock(return_value=None)
    ha_bt.async_scanner_devices_by_address = MagicMock(return_value=[])
    ha_bt.async_register_callback = MagicMock(return_value=lambda: None)

    ha_comp = types.ModuleType("homeassistant.components")
    ha_comp.bluetooth = ha_bt

    # ---- homeassistant.helpers.device_registry ----
    ha_dr = types.ModuleType("homeassistant.helpers.device_registry")
    ha_dr.DeviceEntry = MagicMock
    ha_dr.CONNECTION_BLUETOOTH = "bluetooth"
    ha_dr.format_mac = lambda mac: str(mac).strip().upper().replace("-", ":")

    # ---- homeassistant.helpers.redact ----
    ha_redact = types.ModuleType("homeassistant.helpers.redact")

    def _async_redact_data(value, keys):
        if isinstance(value, dict):
            return {
                key: "**REDACTED**" if key in keys else _async_redact_data(item, keys) for key, item in value.items()
            }
        if isinstance(value, list):
            return [_async_redact_data(item, keys) for item in value]
        return value

    ha_redact.async_redact_data = _async_redact_data

    # ---- homeassistant.helpers.issue_registry ----
    class IssueSeverity(str, enum.Enum):
        ERROR = "error"
        WARNING = "warning"
        CRITICAL = "critical"

    class _FakeIssueRegistry:
        """Enough of HA's issue registry that a stored issue can be read back.

        The integration reconciles its repairs against what the registry
        actually holds rather than against remembered state (see
        `core/recovery.reconcile_issue`), so bare create/delete mocks would
        leave the interesting half untested: a create has to be visible to
        the next read, and a delete has to remove it.
        """

        def __init__(self):
            self.issues = {}

        def async_get_issue(self, domain, issue_id):
            return self.issues.get((domain, issue_id))

    issue_registry_store = _FakeIssueRegistry()

    def _async_create_issue(hass, domain, issue_id, **kwargs):
        issue_registry_store.issues[(domain, issue_id)] = types.SimpleNamespace(
            domain=domain,
            issue_id=issue_id,
            is_fixable=kwargs.get("is_fixable"),
            severity=kwargs.get("severity"),
            translation_key=kwargs.get("translation_key"),
            translation_placeholders=kwargs.get("translation_placeholders"),
            data=kwargs.get("data"),
        )

    def _async_delete_issue(hass, domain, issue_id):
        issue_registry_store.issues.pop((domain, issue_id), None)

    ha_issue_registry = types.ModuleType("homeassistant.helpers.issue_registry")
    ha_issue_registry.IssueSeverity = IssueSeverity
    ha_issue_registry.async_create_issue = MagicMock(side_effect=_async_create_issue)
    ha_issue_registry.async_delete_issue = MagicMock(side_effect=_async_delete_issue)
    ha_issue_registry.async_get = MagicMock(return_value=issue_registry_store)
    # Reset between tests by the `issue_registry` fixture below.
    ha_issue_registry.test_store = issue_registry_store

    # ---- homeassistant.helpers.entity ----
    class _FakeEntity:
        _attr_should_poll = False
        _attr_has_entity_name = False
        _attr_available = True
        _attr_unique_id = None
        _attr_translation_key = None
        _attr_device_info = None
        _attr_is_on = None

        @property
        def hass(self):
            return None

        def _async_write_ha_state(self):
            pass

        async def async_added_to_hass(self):
            pass

        def async_on_remove(self, callback):
            callbacks = getattr(self, "_on_remove_callbacks", None)
            if callbacks is None:
                callbacks = []
                self._on_remove_callbacks = callbacks
            callbacks.append(callback)

        async def async_will_remove_from_hass(self):
            for callback in reversed(getattr(self, "_on_remove_callbacks", [])):
                callback()
            self._on_remove_callbacks = []

    ha_entity = types.ModuleType("homeassistant.helpers.entity")
    ha_entity.Entity = _FakeEntity
    ha_entity.DeviceInfo = dict

    # ---- homeassistant.data_entry_flow / components.repairs ----
    # Result shapes mirror FlowResultType's own values, so tests assert the
    # same strings a real flow would return.
    class _FakeFlowHandler:
        hass = None

        def async_show_form(self, *, step_id, data_schema=None, description_placeholders=None, errors=None):
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "description_placeholders": description_placeholders,
                "errors": errors,
            }

        def async_show_menu(self, *, step_id, menu_options, description_placeholders=None):
            return {
                "type": "menu",
                "step_id": step_id,
                "menu_options": menu_options,
                "description_placeholders": description_placeholders,
            }

        def async_create_entry(self, *, data=None, title=None, **kwargs):
            return {"type": "create_entry", "data": data, "title": title}

        def async_abort(self, *, reason, description_placeholders=None):
            return {"type": "abort", "reason": reason}

    ha_def = types.ModuleType("homeassistant.data_entry_flow")
    ha_def.FlowResult = dict  # type alias in real HA
    ha_def.FlowHandler = _FakeFlowHandler

    ha_repairs = types.ModuleType("homeassistant.components.repairs")
    ha_repairs.RepairsFlow = _FakeFlowHandler

    # ---- homeassistant.helpers.selector ----
    ha_selector = types.ModuleType("homeassistant.helpers.selector")
    ha_selector.EntitySelectorConfig = dict

    class _FakeEntitySelector:
        def __init__(self, config=None):
            self.config = config or {}

    ha_selector.EntitySelector = _FakeEntitySelector

    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_helpers.entity = ha_entity
    ha_helpers.device_registry = ha_dr
    ha_helpers.redact = ha_redact
    ha_helpers.issue_registry = ha_issue_registry
    ha_helpers.selector = ha_selector

    # ---- homeassistant.helpers.entity_platform ----
    ha_ep = types.ModuleType("homeassistant.helpers.entity_platform")
    ha_ep.AddEntitiesCallback = MagicMock

    # ---- homeassistant.components.number ----
    class NumberMode(str, enum.Enum):
        SLIDER = "slider"
        BOX = "box"

    class _FakeNumberEntity(_FakeEntity):
        _attr_native_min_value = None
        _attr_native_max_value = None
        _attr_native_step = None
        _attr_native_value = None
        _attr_mode = NumberMode.SLIDER

    ha_number = types.ModuleType("homeassistant.components.number")
    ha_number.NumberEntity = _FakeNumberEntity
    ha_number.NumberMode = NumberMode

    # ---- homeassistant.components.button ----
    class ButtonDeviceClass(str, enum.Enum):
        IDENTIFY = "identify"

    class _FakeButtonEntity(_FakeEntity):
        pass

    ha_button = types.ModuleType("homeassistant.components.button")
    ha_button.ButtonDeviceClass = ButtonDeviceClass
    ha_button.ButtonEntity = _FakeButtonEntity

    # ---- homeassistant.components.sensor ----
    class SensorDeviceClass(str, enum.Enum):
        SIGNAL_STRENGTH = "signal_strength"
        TIMESTAMP = "timestamp"
        ENUM = "enum"

    class SensorStateClass(str, enum.Enum):
        MEASUREMENT = "measurement"
        TOTAL_INCREASING = "total_increasing"

    class _FakeSensorEntity(_FakeEntity):
        pass

    ha_sensor = types.ModuleType("homeassistant.components.sensor")
    ha_sensor.SensorDeviceClass = SensorDeviceClass
    ha_sensor.SensorStateClass = SensorStateClass
    ha_sensor.SensorEntity = _FakeSensorEntity

    # ---- homeassistant.components.select ----
    class _FakeSelectEntity(_FakeEntity):
        _attr_current_option = None
        _attr_options = []

    ha_select = types.ModuleType("homeassistant.components.select")
    ha_select.SelectEntity = _FakeSelectEntity

    # ---- homeassistant.components.switch ----
    class _FakeSwitchEntity(_FakeEntity):
        pass

    ha_switch = types.ModuleType("homeassistant.components.switch")
    ha_switch.SwitchEntity = _FakeSwitchEntity

    # ---- homeassistant.components.binary_sensor ----
    class BinarySensorDeviceClass(str, enum.Enum):
        CONNECTIVITY = "connectivity"
        PROBLEM = "problem"

    class _FakeBinarySensorEntity(_FakeEntity):
        _attr_is_on = None
        _attr_extra_state_attributes = None

    ha_bs = types.ModuleType("homeassistant.components.binary_sensor")
    ha_bs.BinarySensorDeviceClass = BinarySensorDeviceClass
    ha_bs.BinarySensorEntity = _FakeBinarySensorEntity

    # ---- homeassistant.components.light ----
    class ColorMode(str, enum.Enum):
        ONOFF = "onoff"
        BRIGHTNESS = "brightness"
        RGB = "rgb"
        RGBW = "rgbw"
        WHITE = "white"

    class LightEntityFeature(enum.IntFlag):
        EFFECT = 4

    class _FakeLightEntity(_FakeEntity):
        _attr_is_on = None
        _attr_brightness = None
        _attr_color_mode = None
        _attr_supported_color_modes = None
        _attr_rgb_color = None
        _attr_rgbw_color = None
        _attr_effect = None
        _attr_effect_list = None
        _attr_supported_features = 0

    ha_light = types.ModuleType("homeassistant.components.light")
    ha_light.LightEntity = _FakeLightEntity
    ha_light.ColorMode = ColorMode
    ha_light.LightEntityFeature = LightEntityFeature
    ha_light.ATTR_BRIGHTNESS = "brightness"
    ha_light.ATTR_EFFECT = "effect"
    ha_light.ATTR_RGB_COLOR = "rgb_color"
    ha_light.ATTR_RGBW_COLOR = "rgbw_color"
    ha_light.ATTR_WHITE = "white"

    # ---- homeassistant.components.websocket_api ----
    ha_ws = types.ModuleType("homeassistant.components.websocket_api")
    ha_ws.ActiveConnection = MagicMock
    ha_ws.async_register_command = MagicMock()
    ha_ws.websocket_command = lambda schema: lambda func: func
    ha_ws.async_response = lambda func: func

    # ---- homeassistant.helpers.storage ----
    class _FakeStore:
        def __init__(self, *args, **kwargs):
            self.data = None

        async def async_load(self):
            return self.data

        async def async_save(self, data):
            self.data = data

    ha_storage = types.ModuleType("homeassistant.helpers.storage")
    ha_storage.Store = _FakeStore

    # ---- homeassistant.helpers.event ----
    ha_event = types.ModuleType("homeassistant.helpers.event")
    ha_event.async_track_point_in_time = MagicMock(return_value=MagicMock())
    ha_event.async_track_time_interval = MagicMock(return_value=MagicMock())
    ha_event.async_call_later = MagicMock(return_value=MagicMock())

    # ---- homeassistant.util.dt ----
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")
    ha_dt.now = MagicMock(return_value=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    ha_dt.utcnow = MagicMock(return_value=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    ha_util.dt = ha_dt
    # Close enough to HA's own slugify for the ESPHome action names this
    # integration derives: "plant-room-bluetooth-proxy" ->
    # "plant_room_bluetooth_proxy".
    ha_util.slugify = lambda value, separator="_": re.sub(r"[^a-z0-9]+", separator, str(value).lower()).strip(separator)

    # ---- register everything in sys.modules ----
    modules = {
        "homeassistant": types.ModuleType("homeassistant"),
        "voluptuous": vol,
        "homeassistant.exceptions": ha_exc,
        "homeassistant.const": ha_const,
        "homeassistant.core": ha_core,
        "homeassistant.config_entries": ha_ce,
        "homeassistant.components": ha_comp,
        "homeassistant.components.bluetooth": ha_bt,
        "homeassistant.components.button": ha_button,
        "homeassistant.components.number": ha_number,
        "homeassistant.components.sensor": ha_sensor,
        "homeassistant.components.select": ha_select,
        "homeassistant.components.switch": ha_switch,
        "homeassistant.components.binary_sensor": ha_bs,
        "homeassistant.components.light": ha_light,
        "homeassistant.helpers": ha_helpers,
        "homeassistant.helpers.device_registry": ha_dr,
        "homeassistant.helpers.redact": ha_redact,
        "homeassistant.helpers.issue_registry": ha_issue_registry,
        "homeassistant.helpers.entity": ha_entity,
        "homeassistant.helpers.entity_platform": ha_ep,
        "homeassistant.helpers.event": ha_event,
        "homeassistant.helpers.storage": ha_storage,
        "homeassistant.util": ha_util,
        "homeassistant.util.dt": ha_dt,
        "homeassistant.components.websocket_api": ha_ws,
        "homeassistant.components.repairs": ha_repairs,
        "homeassistant.data_entry_flow": ha_def,
        "homeassistant.helpers.selector": ha_selector,
    }
    for name, mod in modules.items():
        if name not in sys.modules:
            sys.modules[name] = mod


# ---------------------------------------------------------------------------
# Register stubs before any test module is imported
# ---------------------------------------------------------------------------
_stub_bleak()
_stub_homeassistant()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ble_device():
    """Return a mock BLEDevice for use in tests."""
    device = MagicMock()
    device.address = "AA:BB:CC:DD:EE:FF"
    device.name = "Fluval Plant 3.0"
    return device


@pytest.fixture(autouse=True)
def issue_registry():
    """Return the stub issue registry, emptied for each test.

    Autouse because stored issues would otherwise leak between tests: the
    integration reads the registry to decide whether to create or delete,
    so a leftover issue changes what the next test observes.
    """
    from homeassistant.helpers import issue_registry as ha_issue_registry

    ha_issue_registry.test_store.issues.clear()
    ha_issue_registry.async_create_issue.reset_mock()
    ha_issue_registry.async_delete_issue.reset_mock()
    return ha_issue_registry.test_store


@pytest.fixture
def advertisement():
    """Return a mock AdvertisementData for use in tests."""
    adv = MagicMock()
    adv.local_name = "Fluval Plant 3.0"
    adv.service_uuids = ["00001002-0000-1000-8000-00805f9b34fb"]
    adv.rssi = -65
    return adv


@pytest.fixture(autouse=True)
def _instant_schedule_verify(monkeypatch):
    """Keep the settle-then-poll verify (device.py SCHEDULE_VERIFY_SETTLE) instant in tests."""
    try:
        from custom_components.fluvalble.core import device as device_module
    except Exception:  # noqa: BLE001 - modules that cannot import skip this fixture
        return
    if hasattr(device_module, "SCHEDULE_VERIFY_SETTLE"):
        monkeypatch.setattr(device_module, "SCHEDULE_VERIFY_SETTLE", (0, 0, 0, 0))
