"""Base entity of a Fluval BLE connected LED device for home assistant."""

from collections.abc import Callable
from typing import NoReturn

from homeassistant.core import callback
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

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Stay added when the entity_id is changed in the entity registry.

        Home Assistant 2026.11 changes an entity_id in place instead of removing
        and re-adding the entity, but only for classes that define this hook
        next to `async_added_to_hass` (every Fluval entity class that defines
        `async_added_to_hass` or `async_will_remove_from_hass` repeats it, since
        core checks each class that defines one). Nothing here is keyed on the
        entity_id: updates are registered by attribute name and the BLE link
        belongs to the Device, so there is nothing to move and the link must
        never be touched by a rename.
        """
        super().async_entity_id_changed(old_entity_id)

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
        self._guardian_unsubscribe: Callable[[], None] | None = None
        self._subscribe_guardian()

    def _subscribe_guardian(self) -> None:
        """Listen to the guardian; the removal callback also forgets the subscription."""
        unsubscribe = self.guardian.add_listener(self._update_handler)

        def _unsubscribe() -> None:
            self._guardian_unsubscribe = None
            unsubscribe()

        self._guardian_unsubscribe = _unsubscribe
        self.async_on_remove(_unsubscribe)

    async def async_added_to_hass(self) -> None:
        """Run Entity's own setup without Device.register_update wiring.

        Home Assistant 2026.10 and earlier re-add the same entity object after
        an entity_id change, having already run its removal callbacks, so the
        guardian subscription made in __init__ must be made again.
        """
        await Entity.async_added_to_hass(self)
        if self._guardian_unsubscribe is None:
            self._subscribe_guardian()

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place entity_id changes; see FluvalEntity."""
        super().async_entity_id_changed(old_entity_id)
