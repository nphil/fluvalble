"""Sensor platform for Fluval Aquarium LED diagnostics."""

from datetime import UTC, datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import require_entry_runtime_data
from .core.device import Device
from .core.entity import FluvalEntity, FluvalGuardianEntity
from .core.guardian import GUARDIAN_STATUSES, ScheduleGuardian

PARALLEL_UPDATES = 0


def create_entities(device: Device, guardian: ScheduleGuardian | None = None) -> list:
    """Build the entity list for this platform."""
    entities: list = [FluvalSensor(device, sensor) for sensor in device.sensors()]
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
