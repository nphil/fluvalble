"""Button platform for Fluval Aquarium LED."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import require_entry_runtime_data
from .core.device import Device
from .core.entity import FluvalEntity, FluvalGuardianEntity
from .core.guardian import ScheduleGuardian

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


def create_entities(device: Device, guardian: ScheduleGuardian | None = None) -> list:
    """Build the entity list for this platform."""
    entities: list = [
        FluvalIdentifyButton(device, "identify"),
        FluvalSyncClockButton(device, "sync_clock"),
    ]
    if guardian is not None:
        entities.append(FluvalReturnToScheduleButton(device, "return_to_schedule", guardian))
    return entities


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    runtime = require_entry_runtime_data(hass, config_entry)
    device = runtime.device

    if device:
        add_entities(create_entities(device, runtime.guardian))
    else:
        runtime.pending_add_entities[Platform.BUTTON] = add_entities


class FluvalSyncClockButton(FluvalEntity, ButtonEntity):
    """Button to sync the lamp RTC from Home Assistant time."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-check-outline"

    async def async_press(self) -> None:
        """Force a clock sync on the connected lamp."""
        if not await self.device.async_sync_clock(force=True, priority=True):
            self._raise_command_error()
        _LOGGER.info("Fluval clock synced for %s", self.device.mac)


class FluvalIdentifyButton(FluvalEntity, ButtonEntity):
    """Button that asks the physical fixture to identify itself."""

    _attr_device_class = ButtonDeviceClass.IDENTIFY
    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        """Send FluvalConnect's native Find command."""
        if not await self.device.async_identify():
            self._raise_command_error()


class FluvalReturnToScheduleButton(FluvalGuardianEntity, ButtonEntity):
    """Button that ends an active manual override right away.

    Equivalent to waiting for override_return_min to elapse, but immediate -
    for when the user is done making manual adjustments and wants the
    fixture back on its Auto/Professional schedule now.
    """

    _attr_icon = "mdi:calendar-sync-outline"

    async def async_press(self) -> None:
        """Ask the guardian to restore the expected mode now."""
        if await self.guardian.async_end_override() == "failed":
            self._raise_command_error()
