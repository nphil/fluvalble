from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import require_entry_runtime_data
from .core import DOMAIN
from .core.device import Device
from .core.entity import FluvalEntity, FluvalGuardianEntity
from .core.guardian import ScheduleGuardian, issue_id_for

PARALLEL_UPDATES = 0


def create_entities(device: Device, guardian: ScheduleGuardian | None = None) -> list:
    """Build the entity list for this platform."""
    entities: list = [FluvalSensor(device, "connection")]
    if guardian is not None:
        entities.append(FluvalScheduleProblemBinarySensor(device, "schedule_problem", guardian))
    return entities


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, add_entities: AddEntitiesCallback):
    runtime = require_entry_runtime_data(hass, config_entry)
    device = runtime.device

    if device:
        add_entities(create_entities(device, runtime.guardian))
    else:
        runtime.pending_add_entities[Platform.BINARY_SENSOR] = add_entities


class FluvalSensor(FluvalEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def internal_update(self):
        attribute = self.device.attribute(self.attr)
        if not attribute:
            return

        self._attr_is_on = attribute.get("is_on")
        self._attr_extra_state_attributes = attribute.get("extra")

        if self.hass:
            self._async_write_ha_state()


class FluvalScheduleProblemBinarySensor(FluvalGuardianEntity, BinarySensorEntity):
    """Reports whether ScheduleGuardian needs the user's attention.

    Also owns the matching repairs issue: created while the problem is on,
    deleted once ScheduleGuardian clears it. Synced only on real transitions
    (not on every guardian notification) so it never spams the registry, and
    never fires from the entity's own construction-time refresh - only from
    a live guardian notification once this entity actually exists.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, device: Device, attr: str, guardian: ScheduleGuardian) -> None:
        """Initialize the schedule-problem binary sensor."""
        self._repair_synced_problem: bool | None = None
        super().__init__(device, attr, guardian)

    def internal_update(self) -> None:
        """Update state from the guardian and keep the repair issue in sync."""
        self._attr_is_on = self.guardian.problem
        if self._repair_synced_problem is not None and self._repair_synced_problem != self.guardian.problem:
            self._sync_repair_issue()
        self._repair_synced_problem = self.guardian.problem
        if self.hass:
            self._async_write_ha_state()

    def _sync_repair_issue(self) -> None:
        """Create or delete the schedule-problem repair to match guardian.problem."""
        issue_id = issue_id_for(self.device)
        if self.guardian.problem:
            ir.async_create_issue(
                self.device.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="schedule_problem",
                translation_placeholders={"name": self.device.name or "Fluval"},
            )
        else:
            ir.async_delete_issue(self.device.hass, DOMAIN, issue_id)
