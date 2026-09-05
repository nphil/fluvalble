# WHATCHANGED — v1.1.0

Scope: complete removal of `hold_connection` and the "hold Bluetooth
connection" concept it introduced in v1.0.0 (see `WHATCHANGED-conn.md`) and
defaulted off in v1.0.1 (see `WHATCHANGED-v101.md`). Non-goals: light/schedule
behaviour (v1.0.3 work) and Guardian semantics beyond confirming it never
referenced the removed flag.

## Why

`hold_connection` added a second, confusing connection-policy knob
(`switch.<device>_bluetooth_connection`, an options-flow field, and a
`Device`/`Client` property pair) on top of the upstream `active_time`
option, which already lets a user choose a persistent connection (`0`) or a
finite idle window. It served no purpose the numeric option didn't already
cover, and its own module docstring (`WHATCHANGED-conn.md`, "Known gap, not
fixed") documented a genuine contradiction between the two knobs. Removed
entirely rather than patched.

## What changed

### `custom_components/fluvalble/core/__init__.py`

- Removed `CONF_HOLD_CONNECTION` / `DEFAULT_HOLD_CONNECTION`.

### `custom_components/fluvalble/core/client.py`

`Client` reverts to its pre-v1.0.0 connection-policy behaviour, unchanged
in every other respect (mode-aware writes, confirmed classic writes,
`connection_attempts` counter, the final-disconnect `stop_notify` cleanup
all stay):

- Removed the `hold_connection` constructor parameter, the
  `hold_connection` property/setter, `_persistent()`, and
  `notify_advertisement_seen()`.
- Removed the reconnect-backoff machinery this fork added solely to serve
  `hold_connection`'s persistent-reconnect supervisor: `import random`,
  `RECONNECT_BACKOFF_MIN`/`MAX`, `reconnect_backoff_seconds()`, the
  `_advertisement_event`/`_reconnect_failures` state, and the jittered,
  interruptible backoff wait in `_ping_loop`. Confirmed against the
  pre-fork implementation (commit `3277d59^`) that this is a byte-for-byte
  revert: before this fork, a failed persistent (`active_time == 0`)
  reconnect retried with a plain `await asyncio.sleep(1)`, identical to the
  finite-`active_time` path. `_on_disconnected`, `ping()`, and `_connect()`
  now test `self._active_time == 0` directly again, exactly as before.

### `custom_components/fluvalble/core/device.py`

- Removed the `_hold_connection` field and the `hold_connection`
  property/setter.
- `update_ble()` no longer eagerly constructs a `Client` on the first
  advertisement (previously gated on `hold_connection`); a `Client` is now
  always created on demand only, by the first command or Guardian check
  that needs one (`_async_ensure_client()`).
- `attribute()` / `register_update()` / `deregister_update()` lost their
  `"bluetooth_connection"` case now that no entity reads it.
- `async_collect_diagnostics()`'s `connection_options` no longer reports
  `hold_connection`; `_new_client()` no longer passes it to `Client`.
- Updated docstrings on `async_read_state()` and `_async_ensure_client()`
  that referenced the removed flag.

### `custom_components/fluvalble/__init__.py`

- Removed the `PASSIVE`-mode `_on_advertisement_seen` bluetooth callback
  and its registration - its only purpose was waking the backoff wait via
  `client.notify_advertisement_seen()`, both now gone. The `ACTIVE`-mode
  `update_ble` callback (device creation/metadata refresh) is unaffected
  and unchanged.

### `custom_components/fluvalble/config_flow.py`

- Removed the `hold_connection` field from `OPTIONS_SCHEMA` and the
  now-unused `CONF_HOLD_CONNECTION`/`DEFAULT_HOLD_CONNECTION` imports.

### `custom_components/fluvalble/switch.py`

- Removed `FluvalBluetoothConnectionSwitch` entirely.
- `create_entities()` no longer takes a `guardian` argument - it was the
  only entity in this platform gated on Guardian presence, so the
  parameter became dead code once the entity was gone.
  `async_setup_entry()` updated to match.

### `strings.json` / `translations/en.json` / `icons.json`

- Removed the `hold_connection` options-flow field/description and the
  `switch.bluetooth_connection` entity name (`icons.json`'s now-empty
  `switch` block removed too).

### `README.md`

- Rewrote the connection-options and "sharing the connection" prose for
  on-demand-only: there is nothing to do in the Fluval app - the link is
  simply released after the idle window - and dropped the switch/option
  mentions and the entities-table row for the removed switch.

## Guardian

Confirmed `core/guardian.py` has no residual reference to `hold_connection`
- v1.0.1 already deleted its short-circuit on the flag (its status is
driven solely by `expected_mode`). Nothing further to change there.

## Removed tests, with reasons

**`tests/test_connection_policy.py`** (17 tests removed, 1 renamed, 1
added, net -16; module docstring rewritten to drop the removed contract):

- `test_hold_connection_defaults_false`,
  `test_hold_connection_reads_true_from_config_data`,
  `test_hold_connection_setter_delegates_to_the_client_when_one_exists`,
  `test_hold_connection_setter_is_safe_with_no_client_yet` — pinned
  `Device.hold_connection`'s default/config-wiring/setter-delegation
  behaviour; the property no longer exists.
- `test_client_hold_connection_false_collapses_the_idle_deadline_and_wakes_the_heartbeat`,
  `test_client_hold_connection_true_rearms_and_starts_the_heartbeat`,
  `test_client_hold_connection_setter_is_a_noop_when_value_is_unchanged` —
  pinned `Client.hold_connection`'s setter semantics; the property no
  longer exists.
- `test_async_ensure_client_connects_on_demand_when_hold_connection_false`,
  `test_async_ensure_client_proceeds_normally_when_hold_connection_true` —
  pinned `_async_ensure_client()` behaving identically regardless of
  `hold_connection`; now trivially true since the flag doesn't exist, so
  the distinction the test name draws no longer means anything.
- `test_hold_connection_false_prevents_client_creation_on_advertisement`,
  `test_hold_connection_true_creates_exactly_one_client_across_repeated_advertisements`
  — pinned `update_ble()`'s eager-creation branch on both sides of the
  flag; replaced by **`test_update_ble_never_creates_a_client`** (added),
  which covers the same "no eager client" behaviour now that it is
  unconditional rather than flag-gated.
- `test_hold_connection_true_reconnects_after_disconnect_despite_finite_active_time`,
  `test_hold_connection_false_matches_existing_finite_no_reconnect_behavior`
  — pinned `_on_disconnected()` branching on `hold_connection` against a
  finite `active_time`; the flag no longer exists, only `active_time`
  drives this branch (already covered by `tests/test_client.py`'s
  `test_unexpected_persistent_disconnect_schedules_immediate_reconnect`
  and `test_finite_disconnect_does_not_reconnect_until_demand`).
- `test_reconnect_backoff_first_attempt_collapses_to_the_minimum`,
  `test_reconnect_backoff_upper_bound_doubles_and_saturates_at_the_cap`,
  `test_reconnect_backoff_never_exceeds_documented_bounds`,
  `test_reconnect_backoff_lower_bound_never_exceeds_the_upper_bound` —
  pinned `reconnect_backoff_seconds()`'s formula/bounds; the function is
  deleted along with the backoff supervisor it served.
- `test_async_prepare_command_connects_on_demand_when_hold_connection_false`
  — renamed to `test_async_prepare_command_connects_on_demand` (dropped
  the now-meaningless `hold_connection=False` qualifier from the name and
  body; the assertion itself is unchanged: prepare-command still connects
  on demand).

**`tests/test_guardian.py`** (1 test removed):

- `test_guardian_check_is_unaffected_by_hold_connection_being_false` —
  pinned that setting `device.hold_connection = False` (a plain dynamic
  attribute on the `_FakeDevice` test double, never read by
  `ScheduleGuardian`) didn't change the check outcome. Fully redundant
  with the very next test,
  `test_check_returns_ok_when_mode_already_matches_and_no_schedule_configured`,
  which asserts the identical outcome without the meaningless attribute
  assignment.

**`tests/test_guardian_entities.py`** (5 tests removed; module docstring,
`_make_device`, and the `switch` import updated to drop the removed
contract):

- `test_bluetooth_connection_switch_created_when_guardian_present`,
  `test_switch_create_entities_without_guardian_matches_base_behavior`,
  `test_bluetooth_connection_switch_reflects_hold_connection_state`,
  `test_bluetooth_connection_switch_turn_on_off_writes_hold_connection`,
  `test_bluetooth_connection_switch_is_always_available_even_when_unreachable`
  — the entire `switch.py — bluetooth_connection` section pinned the now
  entirely-removed `FluvalBluetoothConnectionSwitch` and its
  guardian-gated construction in `switch.create_entities()`.

Note: the `tests/test_device.py` and `tests/test_entities.py` changes
visible in this release's diff are unrelated, concurrent scheduled-levels
and default-naming work, not part of this change.

## Verification

- `python -m py_compile` clean across every touched Python file.
- Every touched `.json` file parses.
- `grep -rn hold_connection custom_components tests README.md` returns
  nothing.
- Full suite: 770 passed (was 787 before this release). This release's own
  net change is -16 (`test_connection_policy.py`) -1 (`test_guardian.py`)
  -5 (`test_guardian_entities.py`) = -22; the difference is made up by
  unrelated, concurrent scheduled-levels/default-naming work landing in
  `test_device.py`/`test_entities.py` in the same tree.

## Docs / metadata

- `manifest.json`: `version` 1.0.3 → 1.1.0.
- `CHANGELOG.md`: added a `[1.1.0]` entry.
- `docs/releases/v1.1.0.md`: added, per the release-workflow format.
