from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import require_entry_runtime_data
from .core.device import Device
from .core.entity import FluvalEntity
from .core.guardian import ScheduleGuardian

PARALLEL_UPDATES = 0


def create_entities(device: Device, guardian: ScheduleGuardian | None = None) -> list:
    """Build the entity list for this platform."""
    return [FluvalSelect(device, s, guardian) for s in device.selects()]


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, add_entities: AddEntitiesCallback):
    runtime = require_entry_runtime_data(hass, config_entry)
    device = runtime.device

    if device:
        add_entities(create_entities(device, runtime.guardian))
    else:
        runtime.pending_add_entities[Platform.SELECT] = add_entities


class FluvalSelect(FluvalEntity, SelectEntity):
    _attr_icon = "mdi:tune"

    def __init__(self, device: Device, attr: str, guardian: ScheduleGuardian | None = None) -> None:
        """Initialize a select entity, optionally mirroring guardian override state."""
        self.guardian = guardian
        super().__init__(device, attr)

    def internal_update(self):
        attribute = self.device.attribute(self.attr)
        if not attribute:
            self._attr_available = False
            if self.hass:
                self._async_write_ha_state()
            return
        self._attr_current_option = attribute.get("default")
        self._attr_options = attribute.get("options", [])
        self._attr_available = "default" in attribute and self.device.controls_available

        if self.attr == "mode":
            # Expose the fixture's current native schedule readback so
            # automations can check it directly instead of parsing
            # diagnostics - available regardless of whether a guardian is
            # configured; None until the first successful readback.
            attributes: dict[str, object] = {
                "auto_schedule": self.device.values.get("native_auto_schedule"),
                "pro_schedule": self.device.values.get("native_pro_schedule"),
            }
            if self.guardian is not None:
                # A manual write from Home Assistant while the guardian
                # expects Auto/Professional shows up here as an active
                # override, with the timestamp the guardian will restore the
                # expected mode by.
                attributes["override_active"] = self.guardian.override_active
                attributes["override_until"] = self.guardian.override_until
            self._attr_extra_state_attributes = attributes

        if self.hass:
            self._async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        # Outermost transaction for a user's service call - see FluvalLight.
        async with self.device.command_transaction(priority=True):
            await self._async_select_option(option)

    async def _async_select_option(self, option: str) -> None:
        """Apply one complete mode-selection transaction."""
        if not await self.device.async_stop_preview(restore=False):
            self.internal_update()
            self._raise_command_error()
        if not await self.device.async_select_option(self.attr, option):
            self.internal_update()
            self._raise_command_error()

        self._attr_current_option = option
        self._async_write_ha_state()
