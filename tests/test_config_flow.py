"""
Tests for config flow helpers — MAC normalisation, validation, and title generation.

All HA stubs are registered by conftest.py before this module loads.
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_MAC

# conftest.py registers all stubs before collection; just ensure path is set.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.fluvalble.config_flow import (
    ConfigFlow,
    OptionsFlowHandler,
    _device_display_name,
    normalize_mac,
    unique_id_from_mac,
    validate_active_time,
    validate_input,
    MAC_REGEX,
)


def test_config_flow_keeps_the_released_version_two_schema():
    """Do not strand entries created by the earlier version-two build."""
    assert ConfigFlow.VERSION == 2


def test_version_one_config_entry_migrates_without_changing_user_data():
    """Upstream version-one entries advance without rewriting their settings."""
    from custom_components.fluvalble import async_migrate_entry

    entry = config_entries.ConfigEntry(
        data={"mac": "AA:BB:CC:DD:EE:FF"},
        options={"lamp_profile": "plant", "active_time": 120},
        version=1,
    )
    hass = MagicMock()

    assert asyncio.run(async_migrate_entry(hass, entry)) is True
    hass.config_entries.async_update_entry.assert_called_once_with(entry, version=2)
    assert entry.data == {"mac": "AA:BB:CC:DD:EE:FF"}
    assert entry.options == {"lamp_profile": "plant", "active_time": 120}


def test_current_config_entry_version_is_accepted_without_rewrite():
    """A version-two entry is already current and requires no mutation."""
    from custom_components.fluvalble import async_migrate_entry

    entry = config_entries.ConfigEntry(version=2)
    hass = MagicMock()

    assert asyncio.run(async_migrate_entry(hass, entry)) is True
    hass.config_entries.async_update_entry.assert_not_called()


def test_options_flow_uses_home_assistant_reload_helper():
    """Options changes rely on HA's single automatic reload path."""
    assert issubclass(OptionsFlowHandler, config_entries.OptionsFlowWithReload)


def test_options_flow_uses_saved_values_as_suggestions():
    """Stored options are supplied through HA's suggested-value helper."""
    options = {
        "lamp_profile": "plant",
        "ping_interval": 15,
        "active_time": 0,
    }
    flow = OptionsFlowHandler()
    flow.config_entry.options = options
    suggested_schema = object()
    flow.add_suggested_values_to_schema = MagicMock(return_value=suggested_schema)
    flow.async_show_form = MagicMock(return_value={"type": "form"})

    result = asyncio.run(flow.async_step_init())

    assert result == {"type": "form"}
    flow.add_suggested_values_to_schema.assert_called_once()
    assert flow.add_suggested_values_to_schema.call_args.args[1] is options
    flow.async_show_form.assert_called_once_with(step_id="init", data_schema=suggested_schema)


def test_options_flow_submission_is_owned_by_reload_helper():
    """The handler only creates options; its HA base class owns the reload."""
    flow = OptionsFlowHandler()
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    submitted = {
        "lamp_profile": "auto",
        "ping_interval": 10,
        "active_time": 120,
    }

    result = asyncio.run(flow.async_step_init(submitted))

    assert result == {"type": "create_entry"}
    flow.async_create_entry.assert_called_once_with(title="", data=submitted)


def test_options_flow_rejects_connection_windows_between_one_and_twenty_nine():
    """The serializable numeric schema retains the documented validation gap."""
    flow = OptionsFlowHandler()
    suggested_schema = object()
    flow.add_suggested_values_to_schema = MagicMock(return_value=suggested_schema)
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    submitted = {
        "lamp_profile": "auto",
        "ping_interval": 10,
        "active_time": 1,
    }

    result = asyncio.run(flow.async_step_init(submitted))

    assert result == {"type": "form"}
    flow.async_show_form.assert_called_once_with(
        step_id="init",
        data_schema=suggested_schema,
        errors={"active_time": "invalid_active_time"},
    )


class TestActiveTimeSchema:
    @pytest.mark.parametrize("value", [0, 30, 120, 600])
    def test_accepts_persistent_or_bounded_idle_window(self, value):
        assert validate_active_time(value) == value

    @pytest.mark.parametrize("value", [-1, 1, 29, 601])
    def test_rejects_churn_prone_or_out_of_range_values(self, value):
        with pytest.raises(vol.Invalid):
            validate_active_time(value)


class TestNormalizeMac:
    def test_already_normalized(self):
        assert normalize_mac("AA:BB:CC:DD:EE:FF") == "AA:BB:CC:DD:EE:FF"

    def test_lowercase(self):
        assert normalize_mac("aa:bb:cc:dd:ee:ff") == "AA:BB:CC:DD:EE:FF"

    def test_hyphens(self):
        assert normalize_mac("AA-BB-CC-DD-EE-FF") == "AA:BB:CC:DD:EE:FF"

    def test_no_separator_12_chars(self):
        assert normalize_mac("AABBCCDDEEFF") == "AA:BB:CC:DD:EE:FF"

    def test_spaces_stripped(self):
        assert normalize_mac("  AA:BB:CC:DD:EE:FF  ") == "AA:BB:CC:DD:EE:FF"

    def test_mixed_case_hyphens(self):
        assert normalize_mac("aa-BB-cc-DD-ee-FF") == "AA:BB:CC:DD:EE:FF"


class TestUniqueIdFromMac:
    def test_lowercase_stable(self):
        assert unique_id_from_mac("B8:80:4F:3D:67:C0") == "b8:80:4f:3d:67:c0"

    def test_matches_discovery_style(self):
        assert unique_id_from_mac("b8:80:4f:3d:67:c0") == unique_id_from_mac("B8:80:4F:3D:67:C0")


class TestMacRegex:
    @pytest.mark.parametrize(
        "mac",
        [
            "AA:BB:CC:DD:EE:FF",
            "00:11:22:33:44:55",
            "AB:CD:EF:01:23:45",
        ],
    )
    def test_valid_macs(self, mac):
        assert MAC_REGEX.match(mac)

    @pytest.mark.parametrize(
        "mac",
        [
            "AA:BB:CC:DD:EE",  # too short
            "AA:BB:CC:DD:EE:FF:00",  # too long
            "AABBCCDDEEFF",  # no colons
            "ZZ:BB:CC:DD:EE:FF",  # invalid hex
            "",  # empty
        ],
    )
    def test_invalid_macs(self, mac):
        assert not MAC_REGEX.match(mac)


class TestValidateInput:
    """validate_input is async so we test normalize + regex path inline."""

    def test_invalid_mac_raises_invalid_format(self):
        mac = normalize_mac("not-a-mac")
        assert not MAC_REGEX.match(mac)  # would trigger InvalidFormat in validate_input

    def test_valid_mac_passes(self):
        mac = normalize_mac("AA:BB:CC:DD:EE:FF")
        assert MAC_REGEX.match(mac)

    def test_ble_name_used_as_title(self):
        """A real advertised local name always wins as the entry title."""
        hass = MagicMock()
        with patch(
            "custom_components.fluvalble.config_flow.bluetooth.async_last_service_info",
            return_value=None,
        ):
            result = asyncio.run(
                validate_input(hass, {CONF_MAC: "AA:BB:CC:DD:EE:FF"}, ble_name="Fluval Plant 3.0")
            )
        assert result["title"] == "Fluval Plant 3.0"

    def test_unnamed_advert_titles_by_resolved_model_not_the_address(self):
        """No local name must never fall back to the bare BLE address."""
        hass = MagicMock()
        hass.config_entries.async_entries.return_value = []
        advertisement = SimpleNamespace(
            local_name=None,
            service_uuids=[],
            service_data={},
            manufacturer_data={65535: b"\x00" * 8 + (322).to_bytes(2, "big")},
        )
        service_info = SimpleNamespace(name="AA:BB:CC:DD:EE:FF", advertisement=advertisement)
        with patch(
            "custom_components.fluvalble.config_flow.bluetooth.async_last_service_info",
            return_value=service_info,
        ):
            result = asyncio.run(validate_input(hass, {CONF_MAC: "AA:BB:CC:DD:EE:FF"}))
        assert result["title"] == "Fluval Aquasky 900mm"
        assert result["data"]["model"] == "Aquasky 900mm"

    def test_advert_reporting_its_own_address_as_name_is_treated_as_unnamed(self):
        """Some BLE stacks default name to the address; that is not a real name."""
        hass = MagicMock()
        hass.config_entries.async_entries.return_value = []
        advertisement = SimpleNamespace(
            local_name="AA:BB:CC:DD:EE:FF",
            service_uuids=[],
            service_data={},
            manufacturer_data={65535: b"\x00" * 8 + (322).to_bytes(2, "big")},
        )
        service_info = SimpleNamespace(name="AA:BB:CC:DD:EE:FF", advertisement=advertisement)
        with patch(
            "custom_components.fluvalble.config_flow.bluetooth.async_last_service_info",
            return_value=service_info,
        ):
            result = asyncio.run(validate_input(hass, {CONF_MAC: "AA:BB:CC:DD:EE:FF"}))
        assert result["title"] == "Fluval Aquasky 900mm"

    def test_second_fixture_with_same_model_gets_octet_disambiguated_title(self):
        """A duplicate model title still needs to be told apart in the entity list."""
        hass = MagicMock()
        hass.config_entries.async_entries.return_value = [SimpleNamespace(data={"model": "Aquasky 900mm"})]
        advertisement = SimpleNamespace(
            local_name=None,
            service_uuids=[],
            service_data={},
            manufacturer_data={65535: b"\x00" * 8 + (322).to_bytes(2, "big")},
        )
        service_info = SimpleNamespace(name="AA:BB:CC:DD:EE:FF", advertisement=advertisement)
        with patch(
            "custom_components.fluvalble.config_flow.bluetooth.async_last_service_info",
            return_value=service_info,
        ):
            result = asyncio.run(validate_input(hass, {CONF_MAC: "AA:BB:CC:DD:EE:FF"}))
        assert result["title"] == "Fluval Aquasky 900mm (EE:FF)"


class TestDeviceDisplayName:
    """_device_display_name feeds the discovery dropdown and confirm placeholder."""

    def test_named_advert_keeps_its_name(self):
        service_info = SimpleNamespace(
            address="AA:BB:CC:DD:EE:FF",
            advertisement=SimpleNamespace(local_name="Fluval Plant 3.0", manufacturer_data={}),
        )
        assert _device_display_name(service_info, is_fluval=True) == "Fluval Plant 3.0 (AA:BB:CC:DD:EE:FF)"

    def test_unnamed_advert_uses_resolved_model(self):
        service_info = SimpleNamespace(
            address="AA:BB:CC:DD:EE:FF",
            advertisement=SimpleNamespace(
                local_name=None,
                service_uuids=[],
                service_data={},
                manufacturer_data={65535: b"\x00" * 8 + (322).to_bytes(2, "big")},
            ),
        )
        assert _device_display_name(service_info, is_fluval=True) == "Fluval Aquasky 900mm (AA:BB:CC:DD:EE:FF)"

    def test_advert_naming_itself_by_address_uses_resolved_model(self):
        """Some stacks default the advertised name to the address itself."""
        service_info = SimpleNamespace(
            address="AA:BB:CC:DD:EE:FF",
            advertisement=SimpleNamespace(
                local_name="AA:BB:CC:DD:EE:FF",
                service_uuids=[],
                service_data={},
                manufacturer_data={65535: b"\x00" * 8 + (322).to_bytes(2, "big")},
            ),
        )
        assert _device_display_name(service_info, is_fluval=True) == "Fluval Aquasky 900mm (AA:BB:CC:DD:EE:FF)"
