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
from .core.device import Device
from .core.entity import FluvalEntity, FluvalGuardianEntity
from .core.guardian import ScheduleGuardian, issue_id_for
from .core.recovery import reconcile_issue

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

    Also owns the matching repairs issue, reconciled against the issue
    registry - never against a remembered previous state - on every
    guardian notification and once when this entity is added to hass, so
    that a config-entry reload always converges. It used to sync only on an
    observed transition, which is how live on 2026-09-09 the entry reloaded
    at 12:38 between the repair being raised at 12:22 and the guardian
    clearing the condition at 13:00: the reload reset the in-memory
    "previously synced" flag to None, the first notification afterwards
    recorded the new state without syncing, and the orphaned repair was
    still open 13 hours later.

    Reconciling at add-to-hass means a repair raised before a reload is
    dropped as soon as a freshly built guardian says there is no problem.
    That is deliberate: a fixture that is still broken re-raises it after
    `alert_after_failures` failed corrections, the exact bar that raised it
    the first time, whereas an orphan never clears at all.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    async def async_added_to_hass(self) -> None:
        """Reconcile the repair once this entity is live for the entry."""
        await super().async_added_to_hass()
        self._sync_repair_issue()

    def internal_update(self) -> None:
        """Update state from the guardian and keep the repair issue in sync."""
        self._attr_is_on = self.guardian.problem
        self._sync_repair_issue()
        if self.hass:
            self._async_write_ha_state()

    def _sync_repair_issue(self) -> None:
        """Reconcile the schedule-problem repair with guardian.problem."""
        reconcile_issue(
            self.device.hass,
            issue_id_for(self.device),
            raised=self.guardian.problem,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="schedule_problem",
            translation_placeholders={"name": self.device.name or "Fluval"},
            data={"entry_id": self.device.entry_id} if self.device.entry_id else None,
        )
