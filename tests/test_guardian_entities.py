"""Tests for guardian-backed HA entities and the schedule-problem repair.

Per FluvalGuardian's frozen design (confirmed over hub):
- Guardian entities are dedicated classes (`FluvalGuardianEntity`, a thin
  `FluvalEntity` subclass) holding `self.guardian` directly — they read plain
  guardian attributes, not `device.attribute()`, and refresh via
  `guardian.add_listener(cb)` instead of `device.register_update`.
- Constructors mirror the existing `(device, attr)` entities with `guardian`
  appended: `FluvalGuardianStatusSensor(device, "guardian_status", guardian)`.
- `create_entities(device, guardian=None)` on every affected platform adds
  its guardian entity/entities only when a guardian is supplied — including
  the `bluetooth_connection` switch, bundled under the same flag so the
  base's existing `switch.create_entities(device)` (positional, no guardian)
  keeps returning exactly the daylight-saving switch it does today.
- `FluvalBluetoothConnectionSwitch(device, "bluetooth_connection")` itself
  still takes NO guardian argument — it is a plain `FluvalEntity` wrapping
  `device.hold_connection` directly; only the create_entities() gate cares
  about guardian presence.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ha_issue_registry

from custom_components.fluvalble import binary_sensor, button, select, sensor, switch
from custom_components.fluvalble.core.device import Device


def _make_device(*, hold_connection=True, hass=None):
    return Device(
        "AquaSky3.0_Test",
        hass=hass,
        config_data={
            "mac": "44:A6:E5:70:F1:8D",
            "model": "AquaSky Bluetooth LED",
            "product_id": 328,
            "hold_connection": hold_connection,
        },
    )


class _FakeGuardian:
    """Duck-typed guardian double matching ScheduleGuardian's public surface."""

    def __init__(self):
        self.status = "ok"
        self.last_check_at: float | None = None
        self.corrections = 0
        self.problem = False
        self.override_active = False
        self.override_until: float | None = None
        self._listeners = []
        self.async_end_override_calls = 0
        self._end_override_result = "corrected"

    def add_listener(self, callback):
        self._listeners.append(callback)

        def unsubscribe():
            self._listeners.remove(callback)

        return unsubscribe

    def notify(self):
        for callback in list(self._listeners):
            callback()

    async def async_end_override(self):
        self.async_end_override_calls += 1
        self.override_active = False
        self.override_until = None
        return self._end_override_result


# ---------------------------------------------------------------------------
# sensor.py — guardian_status / guardian_last_check / guardian_corrections
# ---------------------------------------------------------------------------


def test_guardian_status_sensor_reflects_enum_state_and_options():
    device = _make_device()
    guardian = _FakeGuardian()
    guardian.status = "corrected"
    entity = sensor.FluvalGuardianStatusSensor(device, "guardian_status", guardian)

    assert entity._attr_device_class == SensorDeviceClass.ENUM
    assert set(entity._attr_options) == {"ok", "corrected", "failed", "unreachable", "paused"}
    assert entity._attr_native_value == "corrected"


def test_guardian_status_sensor_refreshes_when_guardian_notifies_listeners():
    device = _make_device()
    guardian = _FakeGuardian()
    entity = sensor.FluvalGuardianStatusSensor(device, "guardian_status", guardian)

    guardian.status = "failed"
    guardian.notify()

    assert entity._attr_native_value == "failed"


def test_guardian_last_check_sensor_converts_epoch_to_utc_datetime():
    device = _make_device()
    guardian = _FakeGuardian()
    guardian.last_check_at = 1_700_000_000.0
    entity = sensor.FluvalGuardianLastCheckSensor(device, "guardian_last_check", guardian)

    assert entity._attr_device_class == SensorDeviceClass.TIMESTAMP
    assert entity._attr_native_value == datetime.fromtimestamp(1_700_000_000.0, tz=UTC)


def test_guardian_last_check_sensor_is_none_before_the_first_check():
    device = _make_device()
    guardian = _FakeGuardian()
    guardian.last_check_at = None
    entity = sensor.FluvalGuardianLastCheckSensor(device, "guardian_last_check", guardian)

    assert entity._attr_native_value is None


def test_guardian_corrections_sensor_is_total_increasing():
    device = _make_device()
    guardian = _FakeGuardian()
    guardian.corrections = 4
    entity = sensor.FluvalGuardianCorrectionsSensor(device, "guardian_corrections", guardian)

    assert entity._attr_state_class == SensorStateClass.TOTAL_INCREASING
    assert entity._attr_native_value == 4


def test_guardian_sensors_are_enabled_by_default_with_no_entity_category():
    device = _make_device()
    guardian = _FakeGuardian()
    for cls, attr in (
        (sensor.FluvalGuardianStatusSensor, "guardian_status"),
        (sensor.FluvalGuardianLastCheckSensor, "guardian_last_check"),
        (sensor.FluvalGuardianCorrectionsSensor, "guardian_corrections"),
    ):
        entity = cls(device, attr, guardian)
        assert getattr(entity, "_attr_entity_registry_enabled_default", True) is True
        assert getattr(entity, "_attr_entity_category", None) is None


def test_sensor_create_entities_includes_guardian_sensors_when_guardian_present():
    device = _make_device()
    guardian = _FakeGuardian()

    entities = sensor.create_entities(device, guardian)
    guardian_attrs = {e.attr for e in entities if e.attr.startswith("guardian_")}

    assert guardian_attrs == {"guardian_status", "guardian_last_check", "guardian_corrections"}


def test_sensor_create_entities_without_guardian_matches_base_behavior():
    device = _make_device()

    with_none = {e.attr for e in sensor.create_entities(device, None)}
    positional_only = {e.attr for e in sensor.create_entities(device)}

    assert with_none == positional_only
    assert not any(attr.startswith("guardian_") for attr in positional_only)


# ---------------------------------------------------------------------------
# binary_sensor.py — schedule_problem
# ---------------------------------------------------------------------------


def test_schedule_problem_binary_sensor_tracks_guardian_problem_flag():
    device = _make_device()
    guardian = _FakeGuardian()
    guardian.problem = False
    entity = binary_sensor.FluvalScheduleProblemBinarySensor(device, "schedule_problem", guardian)

    assert entity._attr_device_class == BinarySensorDeviceClass.PROBLEM
    assert entity._attr_is_on is False

    guardian.problem = True
    guardian.notify()

    assert entity._attr_is_on is True


def test_schedule_problem_binary_sensor_enabled_by_default_no_category():
    device = _make_device()
    guardian = _FakeGuardian()
    entity = binary_sensor.FluvalScheduleProblemBinarySensor(device, "schedule_problem", guardian)

    assert getattr(entity, "_attr_entity_registry_enabled_default", True) is True
    assert getattr(entity, "_attr_entity_category", None) is None


def test_binary_sensor_create_entities_includes_schedule_problem_when_guardian_present():
    device = _make_device()
    guardian = _FakeGuardian()

    entities = binary_sensor.create_entities(device, guardian)

    assert any(e.attr == "schedule_problem" for e in entities)


def test_binary_sensor_create_entities_without_guardian_matches_base_behavior():
    device = _make_device()

    with_none = {e.attr for e in binary_sensor.create_entities(device, None)}
    positional_only = {e.attr for e in binary_sensor.create_entities(device)}

    assert with_none == positional_only
    assert "schedule_problem" not in positional_only


# ---------------------------------------------------------------------------
# select.py — mode entity mirrors override_active / override_until
# ---------------------------------------------------------------------------


def test_mode_select_exposes_override_state_from_guardian():
    device = _make_device()
    guardian = _FakeGuardian()
    guardian.override_active = True
    guardian.override_until = 1_700_003_600.0

    mode_entity = next(e for e in select.create_entities(device, guardian) if e.attr == "mode")
    mode_entity.internal_update()

    assert mode_entity._attr_extra_state_attributes.get("override_active") is True
    assert mode_entity._attr_extra_state_attributes.get("override_until") == 1_700_003_600.0


def test_mode_select_without_guardian_matches_base_behavior():
    device = _make_device()

    with_none = [e.attr for e in select.create_entities(device, None)]
    positional_only = [e.attr for e in select.create_entities(device)]

    assert with_none == positional_only


# ---------------------------------------------------------------------------
# button.py — return_to_schedule
# ---------------------------------------------------------------------------


def test_return_to_schedule_button_ends_the_override():
    device = _make_device()
    guardian = _FakeGuardian()
    guardian.override_active = True
    entity = button.FluvalReturnToScheduleButton(device, "return_to_schedule", guardian)

    asyncio.run(entity.async_press())

    assert guardian.async_end_override_calls == 1


def test_return_to_schedule_button_raises_command_error_when_restore_fails():
    device = _make_device()
    guardian = _FakeGuardian()
    guardian._end_override_result = "failed"
    entity = button.FluvalReturnToScheduleButton(device, "return_to_schedule", guardian)

    with pytest.raises(HomeAssistantError):
        asyncio.run(entity.async_press())


def test_button_create_entities_includes_return_to_schedule_when_guardian_present():
    device = _make_device()
    guardian = _FakeGuardian()

    entities = button.create_entities(device, guardian)

    assert any(getattr(e, "attr", None) == "return_to_schedule" for e in entities)


def test_button_create_entities_without_guardian_matches_base_behavior():
    device = _make_device()

    with_none = {getattr(e, "attr", None) for e in button.create_entities(device, None)}
    positional_only = {getattr(e, "attr", None) for e in button.create_entities(device)}

    assert with_none == positional_only
    assert "return_to_schedule" not in positional_only


# ---------------------------------------------------------------------------
# switch.py — bluetooth_connection (device.hold_connection, no guardian arg)
# ---------------------------------------------------------------------------


def test_bluetooth_connection_switch_created_when_guardian_present():
    device = _make_device()
    guardian = _FakeGuardian()

    entities = switch.create_entities(device, guardian)
    conn_switches = [e for e in entities if getattr(e, "attr", None) == "bluetooth_connection"]

    assert len(conn_switches) == 1


def test_switch_create_entities_without_guardian_matches_base_behavior():
    """The base's existing `switch.create_entities(device)` call keeps working."""
    device = _make_device()

    with_none = [getattr(e, "attr", None) for e in switch.create_entities(device, None)]
    positional_only = [getattr(e, "attr", None) for e in switch.create_entities(device)]

    assert with_none == positional_only
    assert "bluetooth_connection" not in positional_only


def test_bluetooth_connection_switch_reflects_hold_connection_state():
    device = _make_device(hold_connection=True)
    guardian = _FakeGuardian()
    entity = next(e for e in switch.create_entities(device, guardian) if e.attr == "bluetooth_connection")

    entity.internal_update()
    assert entity._attr_is_on is True

    device.hold_connection = False
    entity.internal_update()
    assert entity._attr_is_on is False


def test_bluetooth_connection_switch_turn_on_off_writes_hold_connection():
    device = _make_device(hold_connection=False)
    guardian = _FakeGuardian()
    entity = next(e for e in switch.create_entities(device, guardian) if e.attr == "bluetooth_connection")

    asyncio.run(entity.async_turn_on())
    assert device.hold_connection is True

    asyncio.run(entity.async_turn_off())
    assert device.hold_connection is False


def test_bluetooth_connection_switch_is_always_available_even_when_unreachable():
    device = _make_device()
    device.connected = False
    device.conn_info.pop("last_seen", None)
    guardian = _FakeGuardian()
    entity = next(e for e in switch.create_entities(device, guardian) if e.attr == "bluetooth_connection")

    entity.internal_update()

    assert entity._attr_available is True


# ---------------------------------------------------------------------------
# Repairs issue — created while the problem is on, deleted once it clears
# ---------------------------------------------------------------------------


def test_schedule_problem_binary_sensor_creates_repair_issue_when_problem_starts():
    ha_issue_registry.async_create_issue.reset_mock()
    ha_issue_registry.async_delete_issue.reset_mock()

    device = _make_device(hass=MagicMock())
    guardian = _FakeGuardian()
    binary_sensor.FluvalScheduleProblemBinarySensor(device, "schedule_problem", guardian)

    guardian.problem = True
    guardian.notify()

    ha_issue_registry.async_create_issue.assert_called_once()
    kwargs = ha_issue_registry.async_create_issue.call_args.kwargs
    assert kwargs.get("severity") == ha_issue_registry.IssueSeverity.ERROR
    ha_issue_registry.async_delete_issue.assert_not_called()


def test_schedule_problem_binary_sensor_deletes_repair_issue_when_problem_clears():
    ha_issue_registry.async_create_issue.reset_mock()
    ha_issue_registry.async_delete_issue.reset_mock()

    device = _make_device(hass=MagicMock())
    guardian = _FakeGuardian()
    binary_sensor.FluvalScheduleProblemBinarySensor(device, "schedule_problem", guardian)

    guardian.problem = True
    guardian.notify()
    ha_issue_registry.async_create_issue.reset_mock()

    guardian.problem = False
    guardian.notify()

    ha_issue_registry.async_delete_issue.assert_called_once()
    ha_issue_registry.async_create_issue.assert_not_called()


def test_schedule_problem_binary_sensor_does_not_recreate_issue_every_notify():
    ha_issue_registry.async_create_issue.reset_mock()
    ha_issue_registry.async_delete_issue.reset_mock()

    device = _make_device(hass=MagicMock())
    guardian = _FakeGuardian()
    binary_sensor.FluvalScheduleProblemBinarySensor(device, "schedule_problem", guardian)

    guardian.problem = True
    guardian.notify()
    guardian.notify()
    guardian.notify()

    ha_issue_registry.async_create_issue.assert_called_once()
