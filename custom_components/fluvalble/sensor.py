"""Sensor platform for Fluval Aquarium LED diagnostics."""

from datetime import UTC, datetime
import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import require_entry_runtime_data
from .core.device import Device, allocation_source_for_address
from .core.entity import FluvalEntity, FluvalGuardianEntity
from .core.guardian import GUARDIAN_STATUSES, ScheduleGuardian

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


def bluetooth_manager():
    """Return habluetooth's manager, or None when it is unavailable.

    Imported lazily and defensively: habluetooth ships inside Home
    Assistant's Bluetooth stack, so the unit-test environment has neither,
    and `get_manager()` raises until that stack is set up. Losing the
    manager only costs this sensor its proxy *name* - it still reports
    connected/disconnected from Device state.
    """
    try:
        from habluetooth import get_manager  # noqa: PLC0415
    except ImportError:
        return None
    try:
        return get_manager()
    except Exception:  # noqa: BLE001 - no manager before bluetooth is set up
        _LOGGER.debug("habluetooth manager is unavailable", exc_info=True)
        return None


def create_entities(device: Device, guardian: ScheduleGuardian | None = None) -> list:
    """Build the entity list for this platform."""
    entities: list = [FluvalSensor(device, sensor) for sensor in device.sensors()]
    entities.append(FluvalConnectionSensor(device, "connection"))
    if guardian is not None:
        entities.extend(
            [
                FluvalGuardianStatusSensor(device, "guardian_status", guardian),
                FluvalGuardianLastCheckSensor(device, "guardian_last_check", guardian),
                FluvalGuardianCorrectionsSensor(device, "guardian_corrections", guardian),
            ]
        )
    return entities


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    runtime = require_entry_runtime_data(hass, config_entry)
    device = runtime.device

    if device:
        add_entities(create_entities(device, runtime.guardian))
    else:
        runtime.pending_add_entities[Platform.SENSOR] = add_entities


class FluvalSensor(FluvalEntity, SensorEntity):
    """Fluval diagnostics sensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: Device, attr: str) -> None:
        """Initialize a diagnostic sensor."""
        if attr == "rssi":
            # Persistent GATT sessions do not provide live RSSI. Keep this
            # optional diagnostic disabled by default for new installations.
            self._attr_entity_registry_enabled_default = False
        super().__init__(device, attr)

    def internal_update(self):
        """Update sensor state from the device."""
        attribute = self.device.attribute(self.attr)
        if not attribute:
            self._attr_available = False
            if self.hass:
                self._async_write_ha_state()
            return

        self._attr_available = "value" in attribute
        self._attr_native_value = attribute.get("value")
        self._attr_native_unit_of_measurement = attribute.get("native_unit_of_measurement")
        self._attr_extra_state_attributes = attribute.get("extra")

        if self.attr == "rssi":
            self._attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_native_unit_of_measurement = "dBm"
        elif self.attr == "last_seen":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        elif self.attr == "active_connection_source":
            self._attr_icon = "mdi:bluetooth"
        if self.hass:
            self._async_write_ha_state()


class FluvalConnectionSensor(FluvalEntity, SensorEntity):
    """Names the proxy currently carrying this fixture's GATT link.

    Reports the scanner/proxy *name* (e.g. `plant-room-bluetooth-proxy`)
    while the integration holds a link through it, and the literal
    `disconnected` otherwise - so a heal automation can restart the one
    proxy that carries this fixture instead of one that other devices are
    holding. Enabled by default on purpose: automations read it.

    The name comes from habluetooth's slot allocations, pushed through
    `async_register_allocation_callback` (the same source as core's
    `bluetooth/subscribe_connection_allocations` websocket), because a
    reconnect is re-scored across every proxy and can legitimately land
    somewhere new. `hold`/`drops_1h`/`last_drop`/`reconnect_attempt` come
    from the integration's own accounting: habluetooth counts connect
    failures, never a link that dropped after connecting.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bluetooth-connect"

    async def async_added_to_hass(self) -> None:
        """Subscribe to device updates and habluetooth allocation pushes."""
        await super().async_added_to_hass()
        manager = bluetooth_manager()
        register = getattr(manager, "async_register_allocation_callback", None)
        if register is not None:
            # None = every scanner: an allocation change on any proxy can
            # mean this fixture's link moved there.
            self.async_on_remove(register(self._allocations_changed, None))
        self.internal_update()

    @callback
    def _allocations_changed(self, *_args) -> None:
        """Re-derive the holding proxy after habluetooth reports a change."""
        self.internal_update()

    def internal_update(self) -> None:
        """Refresh the reported proxy name and hold diagnostics."""
        self._attr_native_value = self.device.connection_state(self._allocation_source())
        self._attr_extra_state_attributes = self.device.connection_hold_attributes()
        if self.hass:
            self._async_write_ha_state()

    def _allocation_source(self) -> str | None:
        """Return the scanner source habluetooth says holds this address."""
        manager = bluetooth_manager()
        current = getattr(manager, "async_current_allocations", None)
        if current is None:
            return None
        try:
            allocations = current()
        except Exception:  # noqa: BLE001 - a diagnostic must never raise into HA
            _LOGGER.debug("Unable to read habluetooth slot allocations", exc_info=True)
            return None
        return allocation_source_for_address(allocations, self.device.address)


class FluvalGuardianStatusSensor(FluvalGuardianEntity, SensorEntity):
    """Reports ScheduleGuardian's outcome from its most recent check.

    Reads `effective_status`, not `status` directly: a wedged check leaves
    `status` frozen at whatever the last *completed* check reported, so the
    guardian derives a "stale" override from elapsed time once checks have
    gone silent for too long - see `ScheduleGuardian.effective_status`.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(GUARDIAN_STATUSES)

    def internal_update(self) -> None:
        """Update the sensor state from the guardian."""
        self._attr_native_value = self.guardian.effective_status
        if self.hass:
            self._async_write_ha_state()


class FluvalGuardianLastCheckSensor(FluvalGuardianEntity, SensorEntity):
    """Reports when ScheduleGuardian last completed a check."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def internal_update(self) -> None:
        """Update the sensor state from the guardian."""
        last_check_at = self.guardian.last_check_at
        self._attr_native_value = datetime.fromtimestamp(last_check_at, tz=UTC) if last_check_at is not None else None
        if self.hass:
            self._async_write_ha_state()


class FluvalGuardianCorrectionsSensor(FluvalGuardianEntity, SensorEntity):
    """Counts corrections ScheduleGuardian has made since startup."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def internal_update(self) -> None:
        """Update the sensor state from the guardian."""
        self._attr_native_value = self.guardian.corrections
        if self.hass:
            self._async_write_ha_state()
