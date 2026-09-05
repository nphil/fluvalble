# WHATCHANGED — tests

Scope: `tests/` additions only, coordinated live over hub with `FluvalConn` (core/device.py,
core/client.py, `__init__.py`, `config_flow.py`) and `FluvalGuardian` (core/guardian.py, entity
wiring in sensor/binary_sensor/button/switch/select.py, `__init__.py` services/repairs).

## New files

### tests/test_connection_policy.py (36 tests)
Behavior tests for the connection-lifecycle hardening on `Device`/`Client`:
- `hold_connection` bool property/setter on `Device`: default `True`, reads `config_data["hold_connection"]`,
  delegates to `Client.hold_connection` when a client exists, safe with no client yet.
- `Client.hold_connection` (real toggle, default `False` so pre-existing direct-`Client` tests are
  untouched): False collapses the idle deadline (`ping_time <= 0`) and wakes the heartbeat;
  True re-arms and (re)starts the heartbeat; no-op when unchanged.
- Connects are blocked while parked: `Device._async_ensure_client()` returns `False` without ever
  calling `_async_find_device()` while `hold_connection` is `False` and not connected; proceeds
  normally when `True`. `Device.update_ble()` (an advertisement arriving) does not create a client
  while parked, and creates exactly one client across repeated advertisements once armed.
- `register_connection_listener(cb)`: not fired at registration time, fires with the `connected`
  bool on every `set_connected()` transition, additive to the existing `updates_connect`/
  `updates_component` no-arg handlers, and unsubscribes cleanly.
- `Client` persistence override: `hold_connection=True` reconnects after a disconnect even with a
  finite `active_time` (previously only `active_time == 0` was persistent); `hold_connection=False`
  (the new default) leaves today's finite-active_time behavior unchanged.
- `reconnect_backoff_seconds(attempt)`: attempt 1 collapses to exactly `RECONNECT_BACKOFF_MIN`
  (2.0s, deterministic bounds-collapse); the upper bound passed to `random.uniform` doubles per
  attempt and saturates at `RECONNECT_BACKOFF_MAX` (120.0s) from attempt 7; every attempt stays
  within `[MIN, MAX]` and never violates `low <= high`.
- Classic-protocol clock sync via the real connect-handshake hooks (`Device._async_on_client_ready`
  / `_async_on_client_state_ready`, wired as `Client.ready_callback`/`state_ready_callback`, NOT the
  manual `async_sync_clock()` button wrapper): the clock command (`0x68 0x0E`) is sent before the
  clock-sync flag is finalized, the sync completes only after the state-ready hook, it does **not**
  resend within one physical session, and it fires again after `set_connected(False)` resets the
  flags on a full disconnect/reconnect cycle — i.e. every physical connect, not once per HA runtime.
- `async_ensure_mode(mode)`: rejects a literal outside `MODES` (`ValueError`, programmer error);
  zero-BLE-traffic no-op returning `mode` when already matching; sends the resolved mode packet and
  returns `self.values["mode"]` as re-populated by the write's own confirm-read side effect
  (mirrors the real "the re-read is a side effect of `_async_send_packet`'s own post-write
  confirmation, not a call to `async_read_state()`" implementation); returns the mismatched
  confirmed mode when the device didn't take the write; returns `None` when no mode could ever be
  confirmed or while parked (`hold_connection=False`).
- `FluvalState` / `Device.async_read_state()`: frozen dataclass; Manual mode exposes `power`/`levels`
  with `auto_schedule`/`pro_schedule` both `None`; Automatic/Professional expose the matching
  schedule field with `power`/`levels` both `None` (auto/pro wire bodies never carry live levels);
  `connection_attempts` delegates to `Client.connection_attempts`, `scanner_source` reflects
  `conn_info["active_connection_source_address"]`, `last_state_at` is a fresh epoch float.

### tests/test_guardian.py (36 tests)
Pure `ScheduleGuardian` state-machine tests against a duck-typed `_FakeDevice` (no hass, no BLE):
constructor defaults (`expected_mode="auto"`, `check_interval_min=10`, `override_return_min=60`,
`alert_after_failures=3`); clock-sync + state-read happen on every `async_check()`; `manual` and
`unsupervised` expected-mode branches (unsupervised never touches mode/schedule and is always
`"paused"`, even when the fixture is unreachable); mode drift between `automatic`/`professional`
corrected in the same cycle; Auto/Pro schedule drift triggers a repush via
`async_set_native_auto_schedule`/`async_set_native_pro_schedule` only when `expected_schedule` is
configured and differs, and mode+schedule drift together count as exactly one `corrections`
increment; every failure path (write exception, mismatched confirmation, schedule-repush failure,
read-state exception) reports `"failed"`/`"unreachable"` instead of raising; `consecutive_failures`
and `consecutive_unreachable` increment on their outcome and reset on any other outcome; `problem`
sets after `alert_after_failures` consecutive failures or after more than 3 consecutive unreachable
checks (the fixed, always-on safety net independent of the configured threshold), and clears on
recovery; the manual-override lifecycle end to end — a manual write observed while the target is
Auto/Pro starts the override without touching mode, holds as `"ok"` until `now_fn() >= override_until`,
restores the expected mode once the timer expires, never expires when `override_return_min == 0`,
and `async_end_override()` restores immediately regardless of the timer (or reports `"failed"` and
leaves the override active if the restore doesn't confirm); `add_listener()` fires after every
check and on override start/end, and unsubscribes cleanly.

### tests/test_guardian_entities.py (25 tests)
Guardian-backed HA entities and the schedule-problem repair, using a real `Device` plus a
duck-typed `_FakeGuardian`: `guardian_status` sensor (enum device class, `_attr_options` matches
the five outcome strings, refreshes on `guardian.add_listener` notification); `guardian_last_check`
sensor (epoch → UTC `datetime`, `None` before the first check); `guardian_corrections` sensor
(`TOTAL_INCREASING`); all three enabled by default with no entity category; `schedule_problem`
binary sensor (`PROBLEM` device class, tracks `guardian.problem`, enabled by default, no category);
`return_to_schedule` button (calls `guardian.async_end_override()`, raises `HomeAssistantError` when
the restore doesn't confirm); the `mode` select entity mirrors `override_active`/`override_until` in
`_attr_extra_state_attributes` when a guardian is supplied; the `bluetooth_connection` switch (no
guardian argument — a plain `Device.hold_connection` wrapper) is always available even when the
fixture is otherwise unreachable, reflects `hold_connection`, and writes it on toggle;
`create_entities(device, guardian=None)` on sensor/binary_sensor/button/switch/select adds its
guardian-backed entity/entities only when a guardian is supplied, and the guardian-less positional
call (`create_entities(device)`) is asserted to match every base-suite entity count exactly
unchanged; the schedule-problem repair (`issue_registry.async_create_issue`/`async_delete_issue`)
is created once when `problem` turns on, deleted once it turns off, and is not recreated on repeat
notifications while still on.

## Existing-file changes (adjusted only because a sibling's contracted change legitimately broke them)

- **tests/test_device.py** — 5 tests (`test_native_weather_effect_uses_apk_packet`,
  `test_complete_device_commands_cannot_interleave_packets`,
  `test_plant_pro_native_effect_uses_key_14_packet`,
  `test_facebd_native_effect_uses_apk_key_109_packet`,
  `test_set_channels_switches_to_manual_before_write`) mocked `device._async_send_packet` with a
  bare `AsyncMock(return_value=True)`. FluvalConn's connect-hardening slice routed every
  mode-switch-before-write path through the new `async_ensure_mode()`, whose confirm step reads
  `self.values["mode"]` back *after* the send returns — a real behavior that used to be a silent,
  unverified `self.values["mode"] = "manual"` assignment. A bare mock never updates `self.values`,
  so `async_ensure_mode` correctly reported the (still-stale) mode as unconfirmed and the whole
  write failed. Fix: each mock's `_async_send_packet` now has a small `side_effect` that sets
  `device.values["mode"] = "manual"` when it observes the mode packet, simulating the real
  notify-driven confirm-read exactly as it happens once the actual client stack decodes the
  post-write status frame. No assertions were weakened; every original packet-sequence and
  final-state assertion is unchanged.
- **tests/test_schedule_backend.py** — `test_service_descriptions_use_device_picker_and_fixture_language`
  hardcoded `services.yaml.count("integration: fluvalble") == 10`. FluvalGuardian's slice added two
  new services (`guardian_check_now`, `end_override`), each with the same device-picker selector
  block as every other service, taking the count to 12. Updated the literal; every other assertion
  in that test (no leaked `entry_id`/MAC/internal protocol-family labels) still passes unchanged
  against the new content.

## conftest.py additions (tests-owned, needed by the guardian slice)
- `homeassistant.helpers.issue_registry` stub (`IssueSeverity` enum, `async_create_issue`/
  `async_delete_issue` as `MagicMock()`s so repair tests can assert call args).
- `homeassistant.helpers.event.async_track_time_interval` stub (guardian's periodic-check timer).
- `SensorDeviceClass.ENUM`, `SensorStateClass.TOTAL_INCREASING`, `BinarySensorDeviceClass.PROBLEM`
  added to the existing HA component stubs (guardian sensors/binary_sensor need them).

## CI
No `.github/workflows/ci.yml` change was needed. The unit-test job already runs
`pip install -r requirements.txt` (pytest/pytest-asyncio/pytest-cov/voluptuous/ruff/mypy — no real
`bleak`/`homeassistant`) followed by `python -m pytest tests/`; `tests/conftest.py` stubs both
libraries unconditionally regardless of what's actually installed, so the new guardian/connection
tests run under that job exactly like the rest of the suite. None of the new tests use
`pytest-asyncio` fixtures/markers — they follow the base suite's existing `asyncio.run(...)`
convention.

## Verification
`uv run --python 3.13 --with pytest --with pytest-asyncio --with bleak --with bleak-retry-connector --with voluptuous --with homeassistant python -m pytest tests -q`
→ **760 passed** (663 base + 97 new: 36 connection-policy + 36 guardian state-machine + 25 guardian
entities/repairs), no skips, ~0.5s. Verified against the real landed implementations from both
`FluvalConn` and `FluvalGuardian` (not just against the frozen-surface proposals), including two
live coordination rounds where a wrong assumption in a draft test (guardian override-vs-drift
precedence; `async_ensure_mode`'s confirm seam) was caught by running against real code and
corrected before landing. Spot-checked test sensitivity with a manual mutation
(`UNREACHABLE_ALERT_THRESHOLD 3 → 99` in `core/guardian.py`): the corresponding test failed as
expected, then the mutation was reverted and the full suite re-confirmed green.
