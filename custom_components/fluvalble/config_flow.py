"""Config flow for Fluval Aquarium LED integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant import config_entries

try:
    from homeassistant.config_entries import ConfigFlowResult
except ImportError:  # Home Assistant before 2024.4
    from homeassistant.data_entry_flow import FlowResult as ConfigFlowResult

try:
    from homeassistant.config_entries import OptionsFlowWithReload as OptionsFlowBase
except ImportError:  # Home Assistant before 2025.8
    from homeassistant.config_entries import OptionsFlow as OptionsFlowBase  # type: ignore[no-redef]
from homeassistant.const import CONF_MAC
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import format_mac

from .core import (
    CONFIG_ENTRY_VERSION,
    CONF_ACTIVE_TIME,
    CONF_ALERT_AFTER_FAILURES,
    CONF_CHECK_INTERVAL_MIN,
    CONF_EXPECTED_MODE,
    CONF_LAMP_PROFILE,
    CONF_OVERRIDE_RETURN_MIN,
    CONF_PING_INTERVAL,
    DEFAULT_ACTIVE_TIME,
    DEFAULT_ALERT_AFTER_FAILURES,
    DEFAULT_CHECK_INTERVAL_MIN,
    DEFAULT_EXPECTED_MODE,
    DEFAULT_LAMP_PROFILE,
    DEFAULT_OVERRIDE_RETURN_MIN,
    DEFAULT_PING_INTERVAL,
    DOMAIN,
    EXPECTED_MODE_AUTO,
    EXPECTED_MODE_MANUAL,
    EXPECTED_MODE_PRO,
    EXPECTED_MODE_UNSUPERVISED,
    LAMP_PROFILE_AQUASKY,
    LAMP_PROFILE_AQUASKY3,
    LAMP_PROFILE_AUTO,
    LAMP_PROFILE_MARINE,
    LAMP_PROFILE_PLANT,
    LAMP_PROFILE_PLANT_PRO,
)
from .core.discovery import (
    CONF_MODEL,
    default_fixture_name,
    detect_model,
    discovery_metadata,
    is_bare_address_name,
    is_likely_fluval,
)

_LOGGER = logging.getLogger(__name__)

# Bluetooth address filter expects uppercase with colons (e.g. AA:BB:CC:DD:EE:FF)
MAC_REGEX = re.compile(
    r"^([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2})$"
)

MANUAL_ENTRY = "__manual__"


def validate_active_time(value: Any) -> int:
    """Accept persistent mode (0) or a non-churning finite idle window."""
    try:
        active_time = int(value)
    except (TypeError, ValueError) as err:
        raise vol.Invalid("Active connection window must be an integer") from err

    if active_time == 0 or 30 <= active_time <= 600:
        return active_time
    raise vol.Invalid("Active connection window must be 0 or between 30 and 600 seconds")


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_LAMP_PROFILE, default=DEFAULT_LAMP_PROFILE): vol.In(
            {
                LAMP_PROFILE_AUTO: "Auto-detect (APK product ID first)",
                LAMP_PROFILE_PLANT: "Plant 5-channel (Pink–Warm White)",
                LAMP_PROFILE_PLANT_PRO: "Current Plant 5-channel (Pink–Warm White)",
                LAMP_PROFILE_MARINE: "Marine/Reef 5-channel spectrum",
                LAMP_PROFILE_AQUASKY: "AquaSky 2.0 (4-channel RGBW)",
                LAMP_PROFILE_AQUASKY3: "AquaSky 3.0 / FACEBD (4-channel RGBW)",
            }
        ),
        vol.Optional(CONF_PING_INTERVAL, default=DEFAULT_PING_INTERVAL): vol.All(
            int,
            vol.Range(min=5, max=60),
        ),
        # Keep the form schema serializable by Home Assistant. The 1-29 gap is
        # enforced explicitly in the options step below.
        vol.Optional(CONF_ACTIVE_TIME, default=DEFAULT_ACTIVE_TIME): vol.All(
            int,
            vol.Range(min=0, max=600),
        ),
        vol.Optional(CONF_EXPECTED_MODE, default=DEFAULT_EXPECTED_MODE): vol.In(
            {
                EXPECTED_MODE_AUTO: "Auto - keep the fixture on its onboard Auto schedule",
                EXPECTED_MODE_PRO: "Professional - keep the fixture on its onboard Professional schedule",
                EXPECTED_MODE_MANUAL: "Manual - keep the fixture in Manual mode",
                EXPECTED_MODE_UNSUPERVISED: "Unsupervised - report status only, never correct mode or schedule",
            }
        ),
        vol.Optional(CONF_CHECK_INTERVAL_MIN, default=DEFAULT_CHECK_INTERVAL_MIN): vol.All(
            int,
            vol.Range(min=1, max=1440),
        ),
        vol.Optional(CONF_OVERRIDE_RETURN_MIN, default=DEFAULT_OVERRIDE_RETURN_MIN): vol.All(
            int,
            vol.Range(min=0, max=1440),
        ),
        vol.Optional(CONF_ALERT_AFTER_FAILURES, default=DEFAULT_ALERT_AFTER_FAILURES): vol.All(
            int,
            vol.Range(min=1, max=20),
        ),
    }
)


def normalize_mac(mac: str) -> str:
    """Normalize MAC to uppercase colon-separated for HA Bluetooth API.

    HA's habluetooth stack stores addresses in uppercase (e.g. AA:BB:CC:DD:EE:FF).
    The address filter in async_register_callback must match that format exactly.
    """
    mac = mac.strip().upper().replace("-", ":").replace(" ", "")
    if len(mac) == 12 and mac.isalnum():
        return ":".join(mac[i : i + 2] for i in range(0, 12, 2))
    if MAC_REGEX.match(mac):
        return mac.upper()
    return mac


def _format_bluetooth_mac(mac: str) -> str:
    """Normalize MAC with HA's helper, falling back to local normalization."""
    try:
        return format_mac(mac)
    except (TypeError, ValueError):
        return normalize_mac(mac)


def unique_id_from_mac(mac: str) -> str:
    """Stable config-entry unique_id for a MAC (always lowercase via format_mac).

    Discovery uses format_mac (lowercase). Manual setup used to store uppercase
    unique_ids, so HA treated the same lamp as a new discovery prompt.
    """
    return _format_bluetooth_mac(mac).lower()


def _is_likely_fluval(info: bluetooth.BluetoothServiceInfoBleak) -> bool:
    """True only for Fluval LED advertisements (strict — avoids discovery spam)."""
    try:
        adv = info.advertisement if info else None
        name = (adv.local_name if adv else None) or getattr(info, "name", None) or ""
        return is_likely_fluval(name, adv)
    except Exception:  # noqa: BLE001
        return False


def _device_display_name(
    service_info: bluetooth.BluetoothServiceInfoBleak | None,
    *,
    is_fluval: bool = False,
) -> str:
    """Build a clear display name so Fluval lights are easy to find in the list.

    Some Bluetooth stacks report a device's ``name`` as its own address when
    the advertisement carries no local name. That is not a usable name, so a
    Fluval fixture falls back to its resolved model instead of the address.
    """
    if service_info is None:
        return "Unknown device"
    try:
        adv = service_info.advertisement
        local_name = ((adv.local_name if adv else None) or "").strip()
        address = getattr(service_info, "address", "") or ""
        if local_name and not is_bare_address_name(local_name, address):
            name = local_name
        elif is_fluval:
            name = default_fixture_name(detect_model(local_name, adv))
        else:
            name = "Unknown device"
    except Exception:  # noqa: BLE001
        return "Unknown device"
    return f"{name} ({address})"


def _default_title_for_model(hass: HomeAssistant, model: str, mac: str) -> str:
    """Return the model-based default title, disambiguated if needed.

    A fixture with no usable local name is titled after its resolved model
    instead of its BLE address. If another configured entry already uses
    that same model, append this fixture's last two MAC octets so the two
    remain distinguishable in the entity list.
    """
    base = default_fixture_name(model)
    lowered_model = model.strip().lower()
    duplicate = any(
        (entry.data.get(CONF_MODEL) or "").strip().lower() == lowered_model
        for entry in hass.config_entries.async_entries(DOMAIN)
    )
    if not duplicate:
        return base
    octets = mac.split(":")[-2:]
    return f"{base} ({':'.join(octets)})" if len(octets) == 2 else base


async def _get_discovered_devices(
    hass: HomeAssistant,
) -> list[bluetooth.BluetoothServiceInfoBleak]:
    """Return advertisements with an APK-supported Fluval light product ID."""
    try:
        get_discovered = getattr(bluetooth, "async_discovered_service_info", None)
        if not get_discovered:
            return []
        all_devices = get_discovered(hass, connectable=True)
    except Exception:  # noqa: BLE001
        return []
    # The manifest's names and service UUIDs only wake the config flow. Apply
    # the APK's product-ID gate before showing any device to the user.
    return [info for info in all_devices if _is_likely_fluval(info)]


async def validate_input(hass: HomeAssistant, data: dict[str, Any], ble_name: str = "") -> dict[str, Any]:
    """Validate the user input and return cleaned config data."""
    mac = normalize_mac(data[CONF_MAC])
    if not MAC_REGEX.match(mac):
        raise InvalidFormat
    config_data: dict[str, Any] = {CONF_MAC: mac}

    local_name = ble_name.strip()
    service_info = bluetooth.async_last_service_info(hass, mac, connectable=True)
    if service_info is None:
        service_info = bluetooth.async_last_service_info(hass, mac)
    advertisement = service_info.advertisement if service_info is not None else None
    if not local_name and advertisement is not None:
        local_name = (advertisement.local_name or "").strip()

    if service_info is not None:
        config_data.update(discovery_metadata(service_info.name or local_name, advertisement))

    if local_name and not is_bare_address_name(local_name, mac):
        title = local_name
    else:
        model = config_data.get(CONF_MODEL) or detect_model(local_name, advertisement)
        title = _default_title_for_model(hass, model, mac)

    return {"title": title, "data": config_data}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Fluval Aquarium LED."""

    VERSION = CONFIG_ENTRY_VERSION

    def __init__(self) -> None:
        super().__init__()
        self._discovered_devices: list[bluetooth.BluetoothServiceInfoBleak] = []
        self._bluetooth_discovery_info: bluetooth.BluetoothServiceInfoBleak | None = None

    # ------------------------------------------------------------------
    # Bluetooth auto-discovery (triggered by manifest.json bluetooth key)
    # ------------------------------------------------------------------

    def _mac_already_configured(self, mac: str) -> bool:
        """True if any entry already owns this MAC (case-insensitive)."""
        target = unique_id_from_mac(mac)
        for entry in self._async_current_entries():
            for candidate in (entry.unique_id, entry.data.get(CONF_MAC)):
                if candidate and unique_id_from_mac(str(candidate)) == target:
                    return True
        return False

    async def async_step_bluetooth(self, discovery_info: bluetooth.BluetoothServiceInfoBleak) -> ConfigFlowResult:
        """Handle Bluetooth auto-discovery when a Fluval light is seen."""
        mac = unique_id_from_mac(discovery_info.address)
        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()
        # Legacy entries stored uppercase unique_ids; still treat as configured.
        if self._mac_already_configured(mac):
            return self.async_abort(reason="already_configured")

        # Secondary filter after manifest matchers. The APK accepts lights by
        # product ID, not by a brand-looking name or a shared service UUID.
        adv = discovery_info.advertisement
        local_name = (adv.local_name if adv else None) or discovery_info.name
        if not is_likely_fluval(local_name, adv):
            return self.async_abort(reason="not_fluval")

        self._bluetooth_discovery_info = discovery_info
        name = _device_display_name(discovery_info, is_fluval=True)
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm adding a device found via Bluetooth auto-discovery."""
        errors: dict[str, str] = {}
        if user_input is not None and self._bluetooth_discovery_info is not None:
            discovery = self._bluetooth_discovery_info
            mac = _format_bluetooth_mac(discovery.address)
            ble_name = (
                (discovery.advertisement.local_name if discovery.advertisement else None)
                or getattr(discovery, "name", None)
                or ""
            )
            try:
                info = await validate_input(self.hass, {CONF_MAC: mac}, ble_name=ble_name)
            except InvalidFormat:
                errors["base"] = "invalid_format"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception during Bluetooth confirm")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=info["title"], data=info["data"])

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self.context.get("title_placeholders", {}).get("name", "Fluval LED"),
            },
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Manual config flow (initiated by user from Integrations page)
    # ------------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step: pick from discovered devices or enter MAC manually."""
        configured = {entry.data.get(CONF_MAC) for entry in self._async_current_entries() if entry.data.get(CONF_MAC)}
        configured_normalized = {normalize_mac(m) for m in configured if m}

        errors: dict[str, str] = {}
        if user_input is not None:
            selected = user_input.get(CONF_MAC)
            if selected == MANUAL_ENTRY:
                return await self.async_step_manual()
            mac = normalize_mac(selected)
            if MAC_REGEX.match(mac):
                await self.async_set_unique_id(unique_id_from_mac(mac))
                self._abort_if_unique_id_configured()
                if self._mac_already_configured(mac):
                    return self.async_abort(reason="already_configured")
                try:
                    info = await validate_input(self.hass, {CONF_MAC: mac})
                except InvalidFormat:
                    errors["base"] = "invalid_format"
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("Unexpected exception")
                    errors["base"] = "unknown"
                else:
                    return self.async_create_entry(title=info["title"], data=info["data"])

        self._discovered_devices = await _get_discovered_devices(self.hass)
        options = self._device_options(configured_normalized)
        # If no discoverable devices (or all already configured), go straight to manual entry
        if len(options) <= 1:
            return await self.async_step_manual()

        schema = vol.Schema({vol.Required(CONF_MAC): vol.In(options)})
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"count": str(len([o for o in options if o != MANUAL_ENTRY]))},
        )

    def _device_options(self, configured_normalized: set[str]) -> dict[str, str]:
        """Build dropdown options: value -> label. Exclude already configured."""
        options: dict[str, str] = {}
        for info in self._discovered_devices:
            mac = normalize_mac(info.address)
            if mac in configured_normalized:
                continue
            options[mac] = _device_display_name(info, is_fluval=True)
        options[MANUAL_ENTRY] = "My device isn't in the list — enter MAC address manually"
        return options

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle manual MAC address entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            mac = normalize_mac(user_input[CONF_MAC])
            if not MAC_REGEX.match(mac):
                errors["base"] = "invalid_format"
            else:
                await self.async_set_unique_id(unique_id_from_mac(mac))
                self._abort_if_unique_id_configured()
                if self._mac_already_configured(mac):
                    return self.async_abort(reason="already_configured")
                try:
                    info = await validate_input(self.hass, {**user_input, CONF_MAC: mac})
                except InvalidFormat:
                    errors["base"] = "invalid_format"
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("Unexpected exception")
                    errors["base"] = "unknown"
                else:
                    return self.async_create_entry(title=info["title"], data=info["data"])

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_MAC): str}),
            errors=errors,
            description_placeholders={"mac_example": "AA:BB:CC:DD:EE:FF"},
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler."""
        if hasattr(config_entries, "OptionsFlowWithReload"):
            return OptionsFlowHandler()
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(OptionsFlowBase):
    """Handle options and let Home Assistant reload the config entry once."""

    def __init__(self, legacy_config_entry: config_entries.ConfigEntry | None = None) -> None:
        super().__init__()
        self._legacy_config_entry = legacy_config_entry

    def _config_entry(self) -> config_entries.ConfigEntry:
        """Return the entry on both legacy and current options-flow APIs."""
        if self._legacy_config_entry is not None:
            return self._legacy_config_entry
        return self.config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show and handle the options form."""
        if user_input is not None:
            try:
                validate_active_time(user_input[CONF_ACTIVE_TIME])
            except vol.Invalid:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self.add_suggested_values_to_schema(
                        OPTIONS_SCHEMA,
                        user_input,
                    ),
                    errors={CONF_ACTIVE_TIME: "invalid_active_time"},
                )
            # Preserve options this form never shows - currently just the
            # guardian's expected_schedule, written by the schedule-programming
            # services/entities rather than this form. A plain `data=user_input`
            # would silently wipe it on every options save.
            merged = {**self._config_entry().options, **user_input}
            return self.async_create_entry(title="", data=merged)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA,
                self._config_entry().options,
            ),
        )


class InvalidFormat(HomeAssistantError):
    """Error to indicate the MAC address format is invalid."""
