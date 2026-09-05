# WHATCHANGED — v1.0.1

Scope: connection-policy default flip, the guardian's pause short-circuit,
guardian initial status, and one Home Assistant deprecation. Read
`WHATCHANGED-conn.md` and `WHATCHANGED-guardian.md` first for the v1.0.0
design this builds on.

## 1. `hold_connection` now defaults to `False` (connect-on-demand)

**Why:** This light exposes exactly one BLE central and stops advertising
while connected. A permanently held Home Assistant link (the v1.0.0 default,
`True`) locks the Fluval app out indefinitely — the exact failure the user
reported. The onboard schedule plus the guardian's periodic checks give the
same supervision without holding the single connection slot open between
them.

**What changed:**

- `core/__init__.py`: `DEFAULT_HOLD_CONNECTION` flipped `True` → `False`.
  `config_flow.py`'s `OPTIONS_SCHEMA` (`vol.Optional(CONF_HOLD_CONNECTION,
  default=DEFAULT_HOLD_CONNECTION)`) already sourced its default from this
  constant, so the options-flow default follows with no code change there.
- `core/device.py` — the real fix, since the switch's OFF state must **not**
  mean "parked/never connect" under the new default:
  - Removed `_connection_parked()` (`not hold_connection and not
    self.connected`) and its four call sites
    (`_async_ensure_client()`, `_async_prepare_command()`,
    `async_sync_clock()`, `async_read_state()`). Previously these all
    refused to connect at all while `hold_connection` was `False` — exactly
    backwards for a default that's supposed to mean "connect on demand".
  - `async_read_state()` now calls `_async_ensure_client()` the first time
    `self.client` is `None` (instead of only ever talking to an
    already-existing client), so a Guardian check can create the client it
    needs on demand instead of finding nothing there and reporting
    unreachable forever.
  - `update_ble()` **keeps** its `if self._hold_connection:` gate on eagerly
    constructing a `Client` from an advertisement sighting — deliberately
    unchanged. `Client.__init__` spawns a real
    `asyncio.create_task(self._connect())` immediately, so constructing one
    eagerly on every advertisement would grab the single GATT slot at
    startup even with the connect-on-demand default, reintroducing the same
    lock-out this release fixes. On-demand client creation happens lazily
    instead, in `_async_ensure_client()`, the first time a check or command
    actually needs one.
  - `hold_connection` property/setter and `async_ensure_mode()` docstrings
    rewritten to describe on-demand instead of "parked".
  - `switch.py`'s `FluvalBluetoothConnectionSwitch` docstrings updated to
    match (no logic change — it was always a direct `hold_connection`
    mirror).
- `strings.json` / `translations/en.json`: `hold_connection` option
  description rewritten for the new default and on-demand behavior.
  `README.md`: documented the option in "Connection options" and rewrote the
  "Sharing the connection deliberately" paragraph in "Schedule Guardian &
  connection sharing".

**Known limitation carried over unchanged from v1.0.0** (not reopened by
this release): `Client._persistent()` is still `active_time == 0 OR
hold_connection` (an OR). The narrow, deliberately-contradictory
configuration described in `WHATCHANGED-conn.md` (`active_time == 0` *and*
`hold_connection = False`) still lets a self-healed connection proceed as if
`hold_connection` were `True`. That combination requires a user to
deliberately set both controls against each other and is out of scope here.

## 2. Guardian pause is now driven by `expected_mode`, not `hold_connection` (critical)

**Why:** `ScheduleGuardian._async_check_locked()` used to open with:

```python
if not getattr(self.device, "hold_connection", True):
    return self._finish(STATUS_PAUSED)
```

Under the new default this line would fire on *every* check for *every*
installation that didn't change the connection option — silently disabling
Guardian supervision the instant anyone upgraded, with no BLE traffic and no
correction ever attempted. This was the single most important line in this
release to fix.

**What changed:** `core/guardian.py` — deleted that short-circuit entirely.
The guardian already had a second, still-correct pause path a few lines
later:

```python
if self.expected_mode == EXPECTED_MODE_UNSUPERVISED:
    return self._finish(STATUS_PAUSED)
```

which runs *after* attempting `async_sync_clock()`/`async_read_state()` (for
visibility) and is unaffected by `hold_connection`. With the
`hold_connection` short-circuit gone, `"paused"` is now reported exclusively
by `expected_mode == "unsupervised"`, matching the requirement that the
switch's OFF state must not mean "never check". A three-way status was
considered and rejected — one clean rule (`unsupervised` pauses corrections,
everything else gets a real on-demand check) fully replaces it.

## 3. Guardian status starts at `"unknown"`, never `"ok"`

**Why:** The constructor previously set `self.status = STATUS_PAUSED if
expected_mode == "unsupervised" else STATUS_OK` — meaning a freshly
constructed guardian that hadn't run a single check yet reported `"ok"`,
indistinguishable from a real successful check.

**What changed:** `core/guardian.py`:

- Added `STATUS_UNKNOWN = "unknown"` and included it in `GUARDIAN_STATUSES`
  (the sensor's ENUM `_attr_options` list, so the new value is a valid
  entity state, not just an internal one).
- `self.status` now always initializes to `STATUS_UNKNOWN` regardless of
  `expected_mode` — including for `expected_mode == "unsupervised"`, which
  now also starts `"unknown"` and only becomes `"paused"` once its first
  check actually completes.
- `strings.json` / `translations/en.json`: added the `"unknown"` state
  translation to the `guardian_status` sensor.
- `README.md`: entity table row for the guardian status sensor now lists
  `unknown` as the pre-first-check state.

## 4. Stopped calling the deprecated `device_registry.async_get_device()`

**Why:** Home Assistant Core 2026.8 deprecated the ambiguous
`DeviceRegistry.async_get_device(identifiers=...)` in favor of
`async_get_device_by_identifier(identifier, config_entry_id)` /
`async_get_device_by_connection(...)`, which are unambiguous because they're
scoped to a single config entry (HA developer blog, "Devices are restricted
to a single config entry" and its follow-up post). Custom integrations only
get a logged warning until Core 2027.8, but the user's live Home Assistant
already logs it. This integration's HACS floor (`2026.1.0`, set in
`hacs.json`) predates the replacement, so the fix has to work on both.

**What changed:** `__init__.py` — added `_async_lookup_registry_device(registry,
mac, entry_id)`: detects `async_get_device_by_identifier` via `getattr` at
runtime and, when present *and* `entry_id` is known, calls it
(`config_entry_id=entry_id`) instead of the deprecated call; otherwise falls
back to the legacy `async_get_device(identifiers=...)` form (older HA still
within the supported floor, or the rare case an `entry_id` genuinely isn't
known yet). Both existing call sites
(`_sync_firmware_version_to_device_registry`, `_sync_product_identity`) now
go through this helper. `_sync_firmware_version_to_device_registry` passes
`device.entry_id` (already set by `_create_device` before this callback can
ever fire); `_sync_product_identity` passes `entry.entry_id` (already had
`entry` in scope).

## Test changes and justification

`tests/test_connection_policy.py` (contract changed: `hold_connection`
default and "parked" semantics):

- `test_hold_connection_defaults_true` → `test_hold_connection_defaults_false`:
  the documented default flipped; the test must assert the new one.
- `test_hold_connection_reads_false_from_config_data` →
  `test_hold_connection_reads_true_from_config_data`: with `False` now the
  default, the meaningful explicit-config-override case is `True` (the old
  `False` case duplicated the new defaults test).
- `test_async_ensure_client_blocked_while_hold_connection_false` →
  `test_async_ensure_client_connects_on_demand_when_hold_connection_false`:
  `_connection_parked()` no longer exists; `_async_ensure_client()` must now
  succeed and connect with `hold_connection=False`, not refuse to.
- `test_async_ensure_mode_returns_none_while_parked` →
  `test_async_prepare_command_connects_on_demand_when_hold_connection_false`:
  same reason, exercised at the `_async_prepare_command()` level where the
  removed gate actually lived (the old test's docstring/premise — "parked
  blocks the connection" — is no longer true).
- Module docstring rewritten to describe the new default/semantics instead
  of the old "blocks new connects via `_connection_parked()`" contract.
- Left unchanged (still correct): `test_async_ensure_client_proceeds_normally_when_hold_connection_true`,
  `test_hold_connection_false_prevents_client_creation_on_advertisement`,
  `test_hold_connection_true_creates_exactly_one_client_across_repeated_advertisements`,
  and every `Client`-level `_persistent()`/backoff/clock-sync test — none of
  that contract changed.

`tests/test_guardian.py` (contract changed: initial status, `hold_connection`
independence):

- `test_constructor_defaults_match_the_documented_options`: added `assert
  guardian.status == "unknown"`.
- New `test_status_transitions_from_unknown_to_ok_after_the_first_check` and
  `test_status_transitions_from_unknown_to_paused_for_unsupervised_after_first_check`:
  cover the new resting `"unknown"` status and its transition on the first
  completed check, for both a supervised and an unsupervised guardian.
- New `test_guardian_check_is_unaffected_by_hold_connection_being_false`:
  directly covers the critical fix in §2 — a device reporting
  `hold_connection = False` must still get a real check
  (`sync_clock_calls`/`read_state_calls` both increment) and never
  short-circuit to `"paused"`.

`tests/test_guardian_entities.py` (contract changed: enum options):

- `test_guardian_status_sensor_reflects_enum_state_and_options`: expected
  `_attr_options` set now includes `"unknown"`.

`tests/test_entities.py` (contract changed: device-registry lookup helper):

- `test_product_identity_updates_config_entry_and_device_registry`: the
  `entry` fixture now sets `entry_id` — a real `ConfigEntry` always has one,
  and `_sync_product_identity` now needs it to reach the new lookup API.
  `registry` is now `MagicMock(spec=["async_get_device",
  "async_update_device"])` instead of a bare `MagicMock()`: a bare mock
  auto-vivifies *any* attribute access, so `getattr(registry,
  "async_get_device_by_identifier", None)` would incorrectly "find" a method
  that doesn't exist on this project's actual supported HA floor
  (`2026.1.0`), silently exercising the wrong branch. The `spec` makes the
  double accurately represent that floor.
- `test_reported_firmware_updates_standard_device_registry_info`: same
  `registry` spec tightening for consistency (behavior itself is unaffected
  here since `device.entry_id` is left unset by this fixture, so the
  fallback path is taken either way).
- New `test_firmware_sync_prefers_the_config_entry_scoped_lookup_when_ha_supports_it`:
  proves the forward-compatible branch — when the registry *does* expose
  `async_get_device_by_identifier` and `entry_id` is known, it's preferred
  over the deprecated call.

## Verification

- `python -m py_compile` clean across every touched file.
- Full suite: **764 passed**, 0 failed (760 → 764; net growth is the four
  tests listed above with no equivalent removed — `test_hold_connection_defaults_true`
  and `test_async_ensure_client_blocked_while_hold_connection_false` and
  `test_async_ensure_mode_returns_none_while_parked` were rewritten in place
  under new names covering the corrected behavior, not deleted outright).
- Throwaway script (written, run, then deleted — not part of the repo):
  built a real `Device` + `ScheduleGuardian` pair (default options, so
  `hold_connection` is `False`) with `async_sync_clock`/`async_read_state`
  swapped for instrumented fakes (no real BLE):
  1. `guardian.status == "unknown"` before the first check — **pass**.
  2. `await guardian.async_check()` with `hold_connection=False` calls both
     `async_sync_clock` and `async_read_state` exactly once and returns
     `"ok"`, never `"paused"` — **pass**.
  3. A second guardian with `expected_mode="unsupervised"` still starts
     `"unknown"`, still calls both async methods (visibility), and reports
     `"paused"` only after that first completed check — **pass**.

## Docs / metadata

- `manifest.json`: `version` 1.0.0 → 1.0.1.
- `CHANGELOG.md`: new `[1.0.1]` section.
- `docs/releases/v1.0.1.md` (required by `tests/test_release_workflow.py`
  once the manifest version changed).
- `README.md`: documented `hold_connection` in "Connection options",
  rewrote "Sharing the connection deliberately", updated the guardian status
  sensor's entity-table row for the new `unknown` state.
