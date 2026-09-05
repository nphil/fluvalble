# What changed in v1.0.3

## 1. Auto/Pro light state now follows the onboard schedule

**Bug:** Classic BLE status frames never carry live channel levels while a
fixture runs Auto or Professional mode onboard - only Manual-mode frames do.
`light.py` derived `is_on` from `values['led_on_off']` and colour/brightness
from `values channel_N` unconditionally, so a fixture running its onboard
schedule at full midday output was shown as off with every channel at zero.

**Fix:**
- `custom_components/fluvalble/core/device.py`
  - Added `Device.scheduled_levels_now(now=None) -> list[int] | None`: in
    `automatic`/`professional` mode, with a matching native schedule
    readback present, returns `_classic_native_preview_levels(schedule_type,
    minute)` for the given (or current, via `homeassistant.util.dt.now()`)
    local time; `None` in manual mode or without a readback.
  - Added `Device.effective_levels() -> tuple[list[int] | None, str]`:
    `(reported values, "reported")` in manual mode; `(scheduled levels,
    "schedule")` in Auto/Pro with a readback; `(None, "unknown")` in Auto/Pro
    without one yet.
  - `master_brightness`, `light_brightness_255`, `light_rgb_255`, and
    `aquasky_rgb_255` gained an optional `levels` override parameter so the
    light entity can render explicit schedule-derived levels while every
    other caller (channel writes, brightness scaling) keeps using live
    `values` and the existing commanded-colour override, unchanged.
- `custom_components/fluvalble/light.py`
  - `internal_update()` now reads `Device.effective_levels()` for the
    non-effect branch, deriving `is_on` from "any level > 0" in Auto/Pro
    (keeping the `led_on_off` semantics for Manual, and for the effect
    branch, which is otherwise untouched), and passing the resolved levels
    into the brightness/colour helpers only when they are schedule-derived
    (Manual and "unknown" states keep reading live `values` plus any
    still-fresh commanded colour, exactly as before).
  - Added `extra_state_attributes["level_source"]`
    (`reported`/`schedule`/`unknown`) and, while `schedule`,
    `extra_state_attributes["scheduled_levels"]`.
  - Added a 60 s re-render tick (`async_track_time_interval`, `@callback`,
    registered in `async_added_to_hass` and unregistered via
    `async_on_remove`) that calls `internal_update()` only while the mode is
    Auto/Pro. It never sends BLE traffic; it only recomputes from the last
    schedule readback already in memory, so ramps update visually with no
    fixture round-trip.
- `custom_components/fluvalble/number.py`: does not exist (the `number`
  platform is already retired in `__init__.py`'s `RETIRED_ENTITY_DOMAINS`),
  so no per-channel number entities needed updating.

**Tests added** (`tests/test_device.py`, `tests/test_entities.py`): the
`Device.scheduled_levels_now`/`effective_levels` interpolation at 06:30
(sunrise-ramp midpoint), 12:00 (day levels), 20:00 (night levels), and 23:00
(off, past sleep), using the exact real-world readback from the bug report;
`effective_levels` in Manual/unknown-Auto/Pro states; the light entity's
`is_on`/`rgb_color`/`level_source`/`scheduled_levels` at noon and after
sleep time in Auto mode; Manual mode ignoring a stale schedule readback; and
the re-render tick's registration (marked `_hass_callback`), invocation, and
unregistration on removal.

## 2. Fixtures are named after their model, not their MAC address (scope
   added mid-session by the user via the orchestrating agent)

**Bug:** A fixture whose advertisement carries no local name - or whose
Bluetooth stack reports its own address as `BluetoothServiceInfoBleak.name`,
which some stacks do when there is no local name - was titled by its raw
MAC address (e.g. `44:A6:E5:70:F1:8D`) on the discovery confirmation screen,
as the created config entry's title, and as the device-registry name (which
also drives derived `entity_id`s), even though `products.py` already
resolves the real model (e.g. "Aquasky 900mm") from the same advertisement's
manufacturer data.

**Fix:**
- `custom_components/fluvalble/core/discovery.py`
  - Added `is_bare_address_name(name, address)`: true when a reported name
    is, after normalizing separators/case, identical to the device address.
  - Added `default_fixture_name(model)`: returns `f"Fluval {model}"`, or the
    model unchanged if it is already Fluval-prefixed (e.g. "Fluval Plant PRO
    LED"), avoiding a doubled prefix.
- `custom_components/fluvalble/config_flow.py`
  - `_device_display_name()` (feeds both the discovery dropdown list and the
    Bluetooth-confirm title placeholder) now uses the advertisement's
    `local_name` only when it is non-empty and not a bare-address name;
    otherwise, for a Fluval-identified advertisement, it resolves the model
    via `detect_model()` and falls back to `default_fixture_name(model)`.
  - Added `_default_title_for_model(hass, model, mac)`: returns
    `default_fixture_name(model)`, or that name suffixed with `(<last two
    MAC octets>)` if another already-configured entry (`entry.data["model"]`)
    has the same model, so two same-model fixtures stay distinguishable in
    the entity list.
  - `validate_input()` (the single path both the Bluetooth-confirm and
    manual-entry flows create entries through) now resolves a usable local
    name from either the caller-supplied `ble_name` or the looked-up
    `BluetoothServiceInfoBleak.advertisement.local_name`, rejects it if it is
    a bare-address name, and otherwise falls back to
    `_default_title_for_model(...)` for the entry title.
- `custom_components/fluvalble/core/device.py`
  - `Device.__init__` now resolves `self.address` and `self.model` before
    `self.name`, and computes `self.name` via a new `_resolved_device_name()`
    module helper: keep the given `name` (normally the config entry's title,
    already resolved by `config_flow.py`) or the underlying `BLEDevice.name`
    when either is a real, non-address name; otherwise fall back to
    `default_fixture_name(self.model)`. This keeps `DeviceInfo.name` (built
    in `core/entity.py` from `device.name`, unchanged) and every derived
    `entity_id` model-based even for a `Device` built directly with a blank
    or address-shaped name, not only through the config-flow path.
  - `unique_id`s are untouched - they still key off the address via
    `unique_id_from_mac()`/`_attr_unique_id`.

**Tests added:**
- `tests/test_config_flow.py`: `validate_input` titling a real local name
  as-is, an unnamed advertisement as `Fluval Aquasky 900mm`, an
  advertisement that names itself after its own address as the same
  model-based title, and duplicate-model disambiguation
  (`Fluval Aquasky 900mm (EE:FF)`); `_device_display_name` covering the same
  three cases for the discovery placeholder/dropdown.
- `tests/test_device.py`: `Device.name` keeping a real given name, and
  falling back to `Fluval <model>` both when given only the fixture's own
  address and when given a blank name.
- Deleted `TestValidateInput.test_ble_name_used_as_title` and
  `test_fallback_title_when_no_ble_name`: both asserted a hand-copied inline
  expression (`ble_name.strip() or f"Fluval {mac}"`) rather than calling
  `validate_input()`, and the second literally pinned the exact MAC-as-title
  bug this release fixes. Replaced with real `validate_input()` calls
  above.

## Other

- `custom_components/fluvalble/manifest.json`: version bumped to `1.0.3`.
- `CHANGELOG.md`: added a `## [1.0.3]` section for both fixes.
- `docs/releases/v1.0.3.md`: added, starting with the required
  `# Fluval BLE v1.0.3` line.

## Verification

`uv run --python 3.13 --with pytest --with pytest-asyncio --with bleak --with bleak-retry-connector --with voluptuous --with homeassistant python -m pytest tests -q`
→ 787 passed (765 baseline − 2 deleted incidental tests + 24 new). `python
-m py_compile` clean on every changed module.
