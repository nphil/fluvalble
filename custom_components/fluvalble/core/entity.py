"""Base entity of a Fluval BLE connected LED device for home assistant."""

from typing import NoReturn

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.entity import DeviceInfo, Entity

from . import DOMAIN
from .device import Device


class FluvalEntity(Entity):
    """Base entity class."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, device: Device, attr: str) -> None:
        """Initialize the entity."""
        self.device = device
        self.attr = attr

        # HA unique_ids and device identifiers are case-sensitive. Proxies can
        # report mixed-case MACs; keep a single uppercase form so entities are
        # not duplicated or orphaned across reloads.
        mac = device.mac.upper()
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, mac)},
            identifiers={(DOMAIN, mac)},
            manufacturer="Fluval",
            model=device.model_name,
            name=device.name or "Fluval",
            sw_version=device.firmware_version,
        )
        # Channel labels vary by lamp profile (Plant Pink/Blue/CW/White/WW vs RGBWV).
        if attr.startswith("channel_"):
            self._attr_name = device.entity_name(attr)
            self._attr_translation_key = None
        else:
            self._attr_translation_key = attr
        self._attr_unique_id = mac.replace(":", "") + "_" + attr

        # Store the bound method so deregistration uses the exact same object.
        self._update_handler = self.internal_update
        self._update_handler()

    async def async_added_to_hass(self) -> None:
        """Subscribe to device updates after Home Assistant adds the entity."""
        await super().async_added_to_hass()
        self.device.register_update(self.attr, self._update_handler)
        self.async_on_remove(
            lambda: self.device.deregister_update(
                self.attr,
                self._update_handler,
            )
        )

    def _raise_command_error(self) -> NoReturn:
        """Raise a translated Home Assistant error for a failed device command."""
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"error": self.device.command_error_message()},
        )

    def internal_update(self):
        """Provide a function for internal updates."""
        pass


class FluvalGuardianEntity(FluvalEntity):
    """Base for entities backed by a ScheduleGuardian instead of Device state.

    Guardian outcomes (status, corrections, override state) live on the
    ScheduleGuardian for one config entry, not in Device.values/attribute().
    These entities subscribe to the guardian's own listener list instead of
    Device.register_update/deregister_update.
    """

    def __init__(self, device: Device, attr: str, guardian) -> None:
        """Initialize a guardian-backed entity and subscribe immediately.

        Unlike Device.register_update (deferred to async_added_to_hass), the
        guardian listener is a plain in-process callback list with no HA
        registry side effects, so subscribing here means callers who never
        run the full add-to-hass lifecycle (unit tests, or a guardian check
        that completes before the entity platform finishes loading) still
        see live updates.
        """
        self.guardian = guardian
        super().__init__(device, attr)
        self.async_on_remove(self.guardian.add_listener(self._update_handler))

    async def async_added_to_hass(self) -> None:
        """Run Entity's own setup without Device.register_update wiring."""
        await Entity.async_added_to_hass(self)
