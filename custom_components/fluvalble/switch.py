"""Switch platform for Fluval Aquarium LED."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import require_entry_runtime_data
from .core.device import Device
from .core.entity import FluvalEntity
from .core.guardian import ScheduleGuardian

PARALLEL_UPDATES = 0


def create_entities(device: Device, guardian: ScheduleGuardian | None = None) -> list:
    """Build switches supported by the detected fixture transport."""
    entities: list = []
    if device.supports_facebd_dst_control():
        entities.append(FluvalDaylightSavingSwitch(device, "daylight_saving_time"))
    if guardian is not None:
        entities.append(FluvalBluetoothConnectionSwitch(device, "bluetooth_connection"))
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    add_entities: AddEntitiesCallback,
) -> None:
    """Set up Fluval switches for a config entry."""
    runtime = require_entry_runtime_data(hass, config_entry)
    device = runtime.device

    if device:
        add_entities(create_entities(device, runtime.guardian))
    else:
        runtime.pending_add_entities[Platform.SWITCH] = add_entities


class FluvalDaylightSavingSwitch(FluvalEntity, SwitchEntity):
    """Control the fixture-owned FACEBD daylight-saving flag."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:sun-clock-outline"

    def internal_update(self) -> None:
        """Refresh state from fixture readback."""
        attribute = self.device.attribute(self.attr)
        is_on = attribute.get("is_on")
        self._attr_is_on = is_on if isinstance(is_on, bool) else None
        self._attr_available = (
            isinstance(is_on, bool) and self.device.supports_facebd_dst_control() and self.device.controls_available
        )
        if self.hass:
            self._async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        """Enable fixture daylight-saving handling."""
        del kwargs
        if not await self.device.async_set_daylight_saving_time(True):
            self.internal_update()
            self._raise_command_error()
        self.internal_update()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable fixture daylight-saving handling."""
        del kwargs
        if not await self.device.async_set_daylight_saving_time(False):
            self.internal_update()
            self._raise_command_error()
        self.internal_update()


class FluvalBluetoothConnectionSwitch(FluvalEntity, SwitchEntity):
    """Hold Home Assistant's single BLE connection slot open permanently.

    The fixture accepts exactly one BLE central. On (True) keeps the GATT
    link held open and reconnects automatically, for the lowest possible
    command latency, at the cost of the FluvalConnect app (or another
    controller) being locked out until it's turned back off. Off (the
    default) is connect-on-demand, not "disconnected forever": commands and
    the Schedule Guardian's periodic checks still connect whenever they need
    to, then disconnect again after the active connection window, leaving
    the slot free for the app between checks. Always available (it must be
    usable even while the fixture itself is unreachable), and not gated on
    the guardian - it directly maps to Device.hold_connection.
    """

    _attr_icon = "mdi:bluetooth-connect"

    def internal_update(self) -> None:
        """Refresh state from the device's hold_connection flag."""
        self._attr_is_on = bool(self.device.hold_connection)
        self._attr_available = True
        if self.hass:
            self._async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        """Hold the BLE connection open permanently and allow reconnects."""
        del kwargs
        self.device.hold_connection = True
        self.internal_update()

    async def async_turn_off(self, **kwargs) -> None:
        """Release a held connection; switch to connect-on-demand."""
        del kwargs
        self.device.hold_connection = False
        self.internal_update()
