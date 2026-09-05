# WHATCHANGED - guardian slice (FluvalGuardian)

## New

- `custom_components/fluvalble/core/guardian.py` - `ScheduleGuardian`: a pure,
  hass-free state machine (constructed with a device duck-type +
  `now_fn: Callable[[], float] = time.time`, no `entry`/`hass`), plus
  `start_runner(hass)` (interval timer + connection listener) and the
  `async_setup_guardian(hass, entry, device)` factory that reads options and
  starts it. Statuses: `ok|corrected|failed|unreachable|paused`.
  - Mode/schedule drift while `expected_mode` is auto/pro is corrected
    immediately. Device-observed `mode == "manual"` while auto/pro is
    expected is the manual-override signal (channel/effect/switch writes
    already force manual mode pre-existing in this fork, confirmed with
    FluvalConn) - it arms a grace window (`override_return_min`, 0 = never)
    instead of correcting immediately, and auto-restores once it elapses.
    `async_end_override()` restores immediately (button/service) and does
    **not** clear `override_active` if the restore write fails.
  - `hold_connection is False` short-circuits to `paused` before any BLE
    call (per FluvalConn). `expected_mode="unsupervised"` still attempts
    clock sync/read (visibility) but always reports `paused` and never
    touches the failure/unreachable counters.
  - `problem` = `consecutive_failures >= alert_after_failures` (correction
    failures) `or consecutive_unreachable > 3` (fixed safety net,
    independent of the configurable threshold).
  - Schedule comparison uses `json_safe()` (recursive tuple->list
    normalization) on both sides - `expected_schedule` is captured by
    calling `device.async_read_state()` again right after a successful
    schedule write and persisting *that* readback shape, never the
    service's validated input shape (confirmed with FluvalConn: different,
    incompatible shapes - comparing them directly would repush every check).
- `core/entity.py`: added `FluvalGuardianEntity(FluvalEntity)` - guardian
  entities hold `self.guardian` and subscribe to `guardian.add_listener(cb)`
  **eagerly in `__init__`** (not deferred to `async_added_to_hass` like
  `Device.register_update`), so they see live updates even without a full
  HA add-to-hass lifecycle.
- `icons.json` (new file) - icons for the guardian entities and the
  connection switch only; did not touch existing inline `_attr_icon` usage.

## Entities added (all enabled by default, no `entity_category`)

| Unique ID suffix | Class | File |
|---|---|---|
| `guardian_status` | `FluvalGuardianStatusSensor` | sensor.py |
| `guardian_last_check` | `FluvalGuardianLastCheckSensor` | sensor.py |
| `guardian_corrections` | `FluvalGuardianCorrectionsSensor` | sensor.py |
| `schedule_problem` | `FluvalScheduleProblemBinarySensor` | binary_sensor.py |
| `return_to_schedule` | `FluvalReturnToScheduleButton` | button.py |
| `bluetooth_connection` | `FluvalBluetoothConnectionSwitch` | switch.py |

`FluvalScheduleProblemBinarySensor` also owns the schedule-problem repair
(`homeassistant.helpers.issue_registry`, `issue_id = f"{mac_no_colons}_schedule_problem"`,
via `core.guardian.issue_id_for`) - synced from `self.device.hass` (not
`self.hass`, which HA only sets once truly added) only on real transitions,
skipping the very-first construction-time call so it never fires spuriously.
`ScheduleGuardian.start_runner` deliberately does **not** touch repairs -
kept the pure/HA-runner split clean; only the entity needs a live `hass`.

`FluvalBluetoothConnectionSwitch` is a plain `FluvalEntity` (no guardian
param) wrapping `device.hold_connection` directly; `_attr_available = True`
unconditionally per contract ("always available").

`select.py`'s existing `FluvalSelect` (mode) gained an optional trailing
`guardian` constructor arg; when present it merges `override_active` /
`override_until` (raw values, not datetime-converted) into
`extra_state_attributes`. No new select entity.

## Platform wiring (`create_entities(device, guardian=None)`)

Every affected platform's `create_entities` now takes an optional trailing
`guardian` and only adds guardian entities (and, in switch.py, the
`bluetooth_connection` switch) when one is supplied - calls with just
`device` positionally are byte-identical to pre-guardian behavior, so
`tests/test_entities.py`'s hard-coded counts needed zero changes.
`light.py` was not touched (no guardian param) and not editable, so
`__init__.py`'s retroactive-entities loop calls a small
`_call_entity_factory()` shim that inspects the factory's signature before
deciding whether to pass `guardian`.

## `__init__.py`

- `FluvalRuntimeData` gained `guardian: ScheduleGuardian | None = None`.
- `_create_device()` calls `async_setup_guardian(hass, entry, device)` right
  after `_sync_product_identity`, before entities are retroactively added.
- Added `fluvalble.guardian_check_now` / `fluvalble.end_override` services
  (`SERVICE_TARGET_FIELDS` only, resolved via the existing `get_entry_id`
  closure + a new `_guardian_for_entry` helper - no new targeting logic).
- Hooked `expected_schedule` capture into the **existing** schedule-writing
  paths only (`async_set_native_auto_schedule`, `async_set_native_pro_schedule`
  service closures, and `_async_upload_native_schedule` - the last one also
  covers `save_schedule` mode=native and legacy migration for free) via
  `_async_capture_expected_schedule()`. Did not fork a new way to program
  schedules.
- `expected_schedule` is stored in `entry.options` (JSON-native shape, not a
  double-encoded string) via the normal `hass.config_entries.async_update_entry`
  path for durability across restarts. On this codebase's supported floor,
  `_register_legacy_options_reload` is a no-op whenever `OptionsFlowWithReload`
  exists (it does, given the 2026.1.0 HACS floor) - `OptionsFlowWithReload`'s
  reload is tied to the options-flow UI finishing, not to `async_update_entry`
  calls in general, so this persistence call does **not** trigger a reload.
  That is why `_async_capture_expected_schedule` also calls
  `runtime.guardian.set_expected_schedule(schedule)` directly on the live
  instance - that direct call, not a reload, is what actually keeps the
  running guardian in sync; the options write is durability-only.

## `config_flow.py`

Coordinated with FluvalConn (who owns `hold_connection`'s row) - added
`expected_mode` / `check_interval_min` / `override_return_min` /
`alert_after_failures` to the same `OPTIONS_SCHEMA` dict after their landing
ping. Also fixed a latent bug the moment any out-of-band option existed:
`async_step_init` was doing `data=user_input` (a full overwrite), which would
silently wipe `expected_schedule` on every options save since it isn't a form
field. Changed to `data={**entry.options, **user_input}`.

## Docs / metadata

- `manifest.json`: `version` 1.0.0, `codeowners` `@nphil`,
  `documentation`/`issue_tracker` -> `github.com/nphil/fluvalble`.
- `hacs.json`: `homeassistant` floor `2026.1.0`.
- `docs/releases/v1.0.0.md` (required by `tests/test_release_workflow.py`
  once the manifest version changed) + `CHANGELOG.md` `[1.0.0]` section.
- `README.md`: new "Schedule Guardian & connection sharing" section
  (single-connection rule, per-check behavior, override semantics,
  connection-sharing switch), entity table rows, lineage credit line.
- `services.yaml`, `strings.json`, `translations/en.json`: guardian options,
  entity names/states, `issues.schedule_problem`, the two new services.

## Known cross-slice assumptions (confirmed with FluvalConn over hub)

- `Device.async_read_state()` returns `FluvalState | None`; `None`/exception
  = unreachable/parked, never raised for a plain comms failure.
- `Device.async_ensure_mode(mode)` returns the confirmed mode string or
  `None`; never raises except `ValueError` for an out-of-vocabulary `mode`
  (never passed here).
- `register_connection_listener` fires only on transitions, not immediately
  on registration - `start_runner` also fires one check unconditionally at
  startup so a client that's already connected isn't missed.

## Verification

- `python -m py_compile` clean across every touched/added file.
- Full suite: `760 passed` (was 663 before the three slices; includes
  `tests/test_guardian.py` and `tests/test_guardian_entities.py`, both
  owned by TestsCI-3).
- `strings.json`, `translations/en.json`, `icons.json`, `manifest.json`,
  `hacs.json` parse as JSON; `services.yaml` parses as YAML and contains
  both new service keys.
- Ran a throwaway script (deleted after) driving `ScheduleGuardian` directly
  through: ok; mode drift -> corrected; schedule drift -> corrected; N
  failures -> problem on -> recovery -> problem off; override start -> hold
  -> auto-return after timer + `async_end_override`; unreachable ->
  `problem` on after >3 consecutive misses; `hold_connection=False` ->
  paused with zero device calls; `unsupervised` stays paused even
  unreachable. All 14 scenarios passed.

## Not done / explicitly out of scope

- `core/client.py`, `core/device.py` - FluvalConn's.
- `tests/` - TestsCI-3's (I found and reported a genuine test-authoring
  inconsistency in 5 of their `test_guardian.py` cases before they fixed it;
  see hub history with `TestsCI-3` if useful context later).
- `light.py` - never needed a change; the override signal is inferred
  entirely from `Device.async_read_state().mode`, no write-path hook needed.
- Did not normalize other entities' `entity_category`/enabled-by-default
  state (e.g. Identify/Sync clock/DST switch) to the house rule - out of
  this slice's Target list, not touched to avoid scope creep into files I
  don't own the intent for.

## Post-review fixes

- Added `hold_connection` labels to `strings.json`/`translations/en.json` -
  FluvalConn's option had no translation yet; trivial addition in the same
  section I was already editing, not a scope change to their file's intent.
- Moved the guardian's schedule-problem repair cleanup off `async_on_unload`
  (which fires on every reload, including a routine options change - would
  have silently cleared a still-valid alert before the freshly rebuilt
  guardian re-checks) into a new module-level `async_remove_entry()` in
  `__init__.py`, which only runs on permanent config-entry removal. Removed
  the now-unused `issue_registry` import from `guardian.py` accordingly.
- Corrected the reload claim above after review - see the `expected_schedule`
  bullet; the direct `set_expected_schedule()` call, not a reload, is what
  keeps the live guardian in sync.
- Follow-up sweep: `DOMAIN` also went unused in `guardian.py` once the
  per-unload delete moved out - removed. Factored the issue-id format into
  `issue_id_for_mac(mac)` (used by `async_remove_entry`, which only has the
  entry's stored MAC, not a live Device) with `issue_id_for(device)` now a
  thin wrapper over it, so the two call sites can't silently diverge.
  `async_remove_entry` isn't covered by the existing suite (no test
  constructs a permanent-removal scenario) - verified with a throwaway
  script (deleted after): correct issue_id computed from `entry.data["mac"]`
  and deleted, and a no-op (no call, no raise) when the entry has no MAC.
