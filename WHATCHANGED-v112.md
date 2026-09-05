# WHATCHANGED — v1.1.2

Three independent problems, all fixed in this release: (1) a wedged BLE
operation could hang the whole integration forever, (2) native schedule
writes did not verify the fixture applied them, and (3) a dead guardian
could report stale-but-plausible status forever. This note covers the
wedge analysis first (the original assignment), then the two additions
that came in from the parent agent mid-session, then every test and the
handful of pre-existing tests whose contract legitimately changed.

## 1. The wedge: every unbounded await in the chain, and which one wedged

Chain: `ScheduleGuardian` → `Device` (`command_transaction` /
`serialized_device_command`) → `Client` (`_command_lock`,
`_connection_lock`, `_ensure_client`/`connect_task`, GATT reads/writes,
`_state_update_event`, `ping_future`).

Every await below was unbounded before this release (file:line refers to
the pre-fix source):

**`custom_components/fluvalble/core/client.py`**
- `_ensure_client` (was ~267-331): `establish_connection(...)` at the
  `client = await establish_connection(...)` call — no outer bound; the
  library's own `BLEAK_SAFETY_TIMEOUT` (60s) × `CONNECT_RETRIES` (3) can
  already run ~180s worst case, and a genuinely stuck dbus/proxy call can
  exceed even that.
- `_ensure_client`: `await client.start_notify(uuid, self.notify_callback)`
  (was line ~308) — unbounded per candidate notify UUID.
- `_initialize_session` (was ~436-462): `await
  client.read_gatt_char(self.wake_read_uuid)` (wake read) and the
  branches calling `await self.request_state()` /
  `await self._write_packet(...)` — all transitively unbounded.
- `_ping_loop` (was ~476-530): the heartbeat's
  `await client.read_gatt_char(self.wake_read_uuid)` and
  `await self._write_packet(self.command_write_uuid, self.send_data)` —
  **this loop runs on every idle connection**, so this is the single
  highest-probability wedge site for a fixture that was already connected
  (matches "the fixture was connected (slot allocated) at the time").
- `_write_packet` (was ~532-574): `await
  self.client.write_gatt_char(uuid, data=payload, response=response)` —
  the actual command write. Given the bug report's proxy (bleak-esphome)
  and the "connected" precondition, **this is the most likely single wedge
  point**: bleak-esphome's write path awaits a future resolved by an
  incoming GATT-write-response frame from the ESP32 proxy; if the proxy
  silently drops that frame (a known esp-idf/ESPHome BLE stack failure
  mode under load or after a partial link failure) the future never
  resolves and the await is stuck forever with no exception, no
  cancellation, nothing.
- `request_state` (was ~576-615): the wake read, the per-UUID FACEBD state
  read loop (`await client.read_gatt_char(read_uuid)`), and the
  `init_write_uuid`/`plant_pro_spp` write branches — all unbounded except
  the final `self._state_update_event.wait()`, which was already correctly
  bounded by `asyncio.timeout(STATE_NOTIFY_TIMEOUT)` (0.75s) — the one
  Event/Future wait in this file that was already safe.
- `send_now` (was ~650-736): the wake read and the write-retry loop's
  `await self._write_packet(...)` — unbounded (transitively, via
  `_write_packet`).

**`custom_components/fluvalble/core/device.py`**
- `command_transaction` (was ~327-350): `await
  self._command_transaction_lock.acquire()` has no timeout, and once
  acquired the method body runs with **no bound at all** — this is the
  lock the bug report's profiler saw two guardian tasks and the service
  call all waiting on. It is not itself an "unbounded await deep in a
  library", but it is the reason one wedged Client-level await turns into
  an integration-wide hang: everything funnels through it.

**`custom_components/fluvalble/core/guardian.py`**
- `_async_check_locked` (was ~194-256): `await
  self.device.async_sync_clock()`, `await self._async_read_state_quiet()`
  (itself `await self.device.async_read_state()`), and the mode/schedule
  correction calls — all transitively unbounded via the Device→Client
  chain above.
- `start_runner`'s `_run_check_soon` (was ~383-385): unconditionally did
  `hass.async_create_task(self.async_check())` on every interval tick
  **regardless of whether the previous check had finished** — this is
  exactly why the profiler saw *three* `ScheduleGuardian.async_check()`
  tasks: the interval timer kept firing (it's an independent
  `async_track_time_interval` callback, unaffected by the guardian's own
  wedge) and kept creating new tasks that immediately blocked on
  `_check_lock.acquire()` behind the first, forever-stuck one.

**Most likely wedge given "fixture was connected (slot allocated) at the
time":** a GATT write (`Client._write_packet` → `write_gatt_char`) or the
heartbeat's periodic wake read (`Client._ping_loop`) via bleak-esphome,
awaiting a proxy response that never arrives on an already-established
connection. Both are now bounded identically (`GATT_OP_DEADLINE`, 15s).

## 2. Client: every GATT-facing await bounded

`custom_components/fluvalble/core/client.py`:
- New `_bounded()` helper wraps one awaitable in `asyncio.timeout()`; on
  timeout it sets `self._broken = True`, logs a warning, optionally
  best-effort disconnects a given `BleakClient` (only for load-bearing ops
  where tearing the connection down is correct — not for optional
  wake-reads or exploratory notify/state-read UUID candidates, which keep
  their existing "try the next candidate" semantics unchanged), and raises
  `BleakOperationTimeoutError` (a `BleakError` subclass, so every existing
  `except BleakError` / `except (TimeoutError, BleakError)` handler in
  this file and in `device.py` catches it without any changes).
- New constants: `CONNECT_DEADLINE = 30.0`, `GATT_OP_DEADLINE = 15.0`,
  `DISCONNECT_DEADLINE = 5.0` (matches the assignment's defaults exactly).
- Bounded: `establish_connection` (connect), `start_notify` (per
  candidate), the `_initialize_session`/`_ping_loop`/`send_now` wake reads,
  `_write_packet`'s `write_gatt_char`, and `request_state`'s per-UUID
  `read_gatt_char` calls. The existing `_state_update_event.wait()` bound
  was already correct and untouched.
- `_ensure_client` now also discards and reconnects when `self._broken` is
  set, even if `client.is_connected` still (incorrectly) reports `True` -
  the whole point of a wedge is that the transport layer's own
  "connected" flag cannot be trusted afterward. `_broken` is cleared once
  a fresh connect+init sequence completes successfully, so a transient
  timeout on one exploratory candidate (e.g. a notify UUID that isn't
  actually present) does not permanently poison an otherwise-healthy
  session.
- The three pre-existing `asyncio.wait_for(..., timeout=5)` disconnect
  bounds now reference the `DISCONNECT_DEADLINE` constant instead of a
  literal `5` (no behavior change, single source of truth).
- `grep -n 'await' core/client.py` reviewed line by line (recorded above);
  every GATT-facing await is bounded, either via `_bounded()` or a
  pre-existing `asyncio.wait_for`/`asyncio.timeout`.

## 3. Device: command deadline + `async_reset_connection`

`custom_components/fluvalble/core/device.py`:
- `command_transaction(self, *, supersede_transition=True, deadline=DEFAULT_COMMAND_DEADLINE)`
  now wraps its body in `asyncio.timeout(deadline)`. On timeout it logs a
  `WARNING` with `exc_info=True` (the traceback's innermost frame is the
  exact await that wedged — this is how a future on-call engineer finds
  the wedge site without needing this note), calls
  `await self.async_reset_connection()`, and re-raises `TimeoutError`.
  Nested calls on the same task (already-reentrant for
  `@serialized_device_command`-decorated helpers calling each other) ride
  the outermost call's single deadline rather than starting a new one.
- `serialized_device_command` is now usable bare or parameterized
  (`@serialized_device_command(deadline=90.0)`) and its wrapper catches
  `TimeoutError` from `command_transaction`, sets a diagnostic error, and
  returns `False` — this is what lets every existing
  `if not await device.async_xxx(...): raise HomeAssistantError(...)`
  service handler in `__init__.py` fire cleanly instead of an unhandled
  `TimeoutError` propagating past the check. (`False` is safe for every
  current caller: `async_ensure_mode`'s `str | None` contract and
  `async_read_state`'s `FluvalState | None` contract are both consumed
  only via falsy checks in `guardian.py`, never a strict `is None`.)
  `DEFAULT_COMMAND_DEADLINE = 60.0`; the two schedule-setting commands
  (below) use `deadline=90.0` to comfortably fit one write+verify retry
  cycle.
- New `Device.async_reset_connection()`: swaps `self.client` to `None` and
  best-effort `await client.stop()`s the old one. `Client.stop()` already
  best-effort disconnects and tears down its background tasks, and its
  `status_callback` (`Device.set_connected(False)`) already resets
  clock-sync state the same way a real disconnect does, so no extra
  bookkeeping is needed here.
- Added to the guardian's documented device duck-type (module docstring)
  and to the `_FakeDevice` test double.

## 4. Guardian: bounded checks, hard cap, no unbounded lock queueing

`custom_components/fluvalble/core/guardian.py`:
- New per-step deadlines, each wrapping the corresponding
  `await self.device.async_xxx(...)` call in `asyncio.timeout(...)`:
  `CHECK_CLOCK_SYNC_TIMEOUT = 30.0`, `CHECK_READ_STATE_TIMEOUT = 30.0`,
  `CHECK_ENSURE_MODE_TIMEOUT = 30.0`, `CHECK_SCHEDULE_REPUSH_TIMEOUT = 60.0`.
  On timeout: `WARNING` with `exc_info=True`, best-effort
  `await self.device.async_reset_connection()`, and the step reports its
  normal failure outcome (`False`/`None`/`(False, True)` as appropriate) -
  the check then finishes as `unreachable` or `failed` exactly as it would
  for any other transport failure.
- `CHECK_OVERALL_TIMEOUT = 120.0` wraps the whole
  `await self._async_check_locked()` call in `async_check()` as a hard
  cap, independent of the per-step ones — verified by a test that sets a
  step's own deadline to something absurdly large and confirms the
  overall cap still bounds the check.
- `start_runner`'s `_run_check_soon` now checks `self._check_lock.locked()`
  before creating a new check task; if one is already running it logs at
  `debug` and calls `self._notify()` instead of queuing another
  `hass.async_create_task(self.async_check())` — this is the direct fix
  for the profiler's "two tasks waiting on `_check_lock`" symptom (the
  interval timer fires independently of whether the guardian's own work
  finished, so previously every tick added another queued task forever).

## 5. Service handlers (`__init__.py`) — verified, not changed

Every service handler already follows
`if not await device.async_xxx(...): raise HomeAssistantError(...)`
(`async_set_channels`, `async_preview_schedule`,
`async_preview_native_schedule`, `async_stop_preview`,
`async_set_native_auto_schedule`, `async_set_native_pro_schedule`,
`async_set_native_effect_schedule`, `async_recall_manual_preset`,
`async_save_manual_preset`, `async_end_override`). Since a timed-out
`serialized_device_command`-decorated method now returns `False` (§3)
instead of hanging, every one of these handlers already raises
`HomeAssistantError` on a wedge with no code changes required. Verified
with a device-level test that mirrors this exact pattern against a
command whose deadline has expired (`test_serialized_device_command_converts_deadline_timeout_to_false`,
`tests/test_device.py`).

## 6. Live scope addition: schedule writes did not verify

A live write returned success while the fixture's readback stayed on old
day levels. Root cause: `Client.send_now`'s own write verification
(`_expected_state_for_packet`/`_state_matches`) never included the FACEBD
schedule CBOR keys in its `supported_keys` set for non-Plant-Pro
protocols, so `expected_state` was empty for a schedule packet and the
comparison trivially "matched".

Fix, exactly as scoped (a Device-level fix, not touching the shared
low-level `_expected_state_for_packet` used by every other command):
- `async_set_native_auto_schedule` / `async_set_native_pro_schedule`
  (`core/device.py`) now re-read the schedule after writing
  (`_async_verify_native_auto_schedule` / `_async_verify_native_pro_schedule`,
  via `Client.request_state()`) and compare it against the request
  (`_auto_schedule_mismatch`/`_pro_schedule_mismatch` module functions).
  On a mismatch the whole write (schedule packet + mode packet) is retried
  once; a second mismatch fails with `_set_diagnostic_error` naming the
  mismatched field (`sunrise`/`sunset`/`sleep`/`day_levels`/`night_levels`/
  `points`/`unreachable`), which flows straight into the existing
  `HomeAssistantError(device.diagnostics.get("last_error") or ...)` in the
  service handlers.
- A verified write **no longer pops** `values["native_auto_schedule"]`/
  `values["native_pro_schedule"]` (the old "discard so preview can't use
  stale data" behavior) — it is repopulated by the verify step's own fresh
  readback, which is both correct (the write is now proven, not merely
  assumed) and required for the new attributes below.
- `select.py`'s mode entity now exposes `auto_schedule`/`pro_schedule`
  extra-state attributes (`device.values.get("native_auto_schedule"/"native_pro_schedule")`,
  `None` before any readback), independent of whether a guardian is
  configured (unlike `override_active`/`override_until`, which stay
  guardian-only).

### Advisor-confirmed real bug found and fixed while implementing the above

Building the request-vs-readback comparison required normalizing
sunrise/sunset shapes, which surfaced a **pre-existing, previously
untested bug**: `guardian._async_reconcile_schedule` repushes a captured
readback (`self.expected_schedule`, sourced from
`_async_capture_expected_schedule` in `__init__.py`) straight into
`async_set_native_auto_schedule`. A live readback's `sunrise`/`sunset` are
`{"hour", "minute", "ramp"}` dicts (see
`protocol.decode_wifi_auto_schedule`/`decode_old_auto_schedule`/
`decode_spp_auto_schedule`, all via `_ramp_dict`), but
`protocol.wifi_auto_schedule_packet`/`old_auto_schedule_packet`/
`spp_auto_schedule_packet` index `sunrise[0]`/`[1]`/`[2]` positionally -
passing a dict there raises `KeyError`, caught by guardian's broad
`except Exception` and reported as a plain "failed" correction. **The
guardian's own schedule-drift repush has never worked against real
FACEBD or classic hardware.** The equivalent Professional-schedule bug
also existed for two different readback point shapes (`{"minute",
"channel_N"}` for classic/FACEBD, `{"time","levels"}` for Plant Pro) that
the old ad-hoc shape-sniff in `async_set_native_pro_schedule` did not
recognize (raising `KeyError` for one, silently zeroing every channel for
the other).

Fixed at the source, once, reused by both packet-building and
verification:
- `_normalized_time_ramp`/`_normalized_clock` (`core/device.py` module
  functions) normalize sunrise/sunset/sleep to plain tuples regardless of
  whether the caller passed a tuple (service call) or a readback dict
  (guardian repush); `async_set_native_auto_schedule` normalizes at the
  top before building any packet, so every protocol's packet builder
  always receives tuples.
- `Device._canonical_pro_points` normalizes all three Pro-schedule point
  shapes (service `{"hour","minute","levels"}`, generic
  `{"time","channel_N"}`, and both readback shapes) to one canonical
  `{"minute","channel_1".."channel_5"}` form, replacing the old
  `"time" not in point and "levels" in point` shape-sniff.

Covered by `test_auto_schedule_write_accepts_readback_shaped_sunrise_sunset_dicts`,
`test_pro_schedule_write_accepts_plant_pro_readback_shaped_points`, and
`test_pro_schedule_write_accepts_wifi_readback_shaped_points`
(`tests/test_device.py`) — all three build the real packet from a
readback-shaped input and assert on the resulting bytes.

## 7. Live scope addition: honest "stale" status

A wedged guardian (pre-fix) kept its `guardian_status` sensor on its last
completed outcome (e.g. "ok") while `last_check_at` went stale, because
`status` only updates when a check *completes* (`_finish`). With every
step now bounded this specific wedge can no longer happen, but the
supervisor should say so honestly if checks ever stop completing for any
other reason.

`core/guardian.py`:
- New `STATUS_STALE = "stale"`, added to `GUARDIAN_STATUSES`.
- `ScheduleGuardian._stale_seconds()`: seconds since `last_check_at`, or
  `0.0` if no check has ever run or one is currently in flight
  (`_check_lock.locked()`) — a bounded-but-slow check must not flicker
  "stale".
- `ScheduleGuardian.effective_status` property: `STATUS_STALE` once
  `_stale_seconds() > 2 * check_interval_min * 60`, else `status`.
- `ScheduleGuardian.problem` also trips once `_stale_seconds() >
  UNREACHABLE_ALERT_THRESHOLD * check_interval_min * 60` (same "more than
  3 intervals" bar `consecutive_unreachable` already uses), in addition to
  its existing conditions.
- `start_runner`'s skip-while-running path (§4) calls `self._notify()`
  so listeners re-evaluate `effective_status`/`problem` even while no
  check has completed recently — the HA-clock interval timer is a
  reliable heartbeat for this independent of whether checks themselves
  complete.
- `sensor.FluvalGuardianStatusSensor` now reads `guardian.effective_status`
  instead of `guardian.status`; `GUARDIAN_STATUSES`-derived
  `_attr_options` picks up `"stale"` automatically.
  `binary_sensor.FluvalScheduleProblemBinarySensor` needed no changes -
  it already reads `guardian.problem` directly.
- `strings.json` / `translations/en.json`: added `"stale": "Stale"` to
  `guardian_status.state`.

## 8. Tests

All new; existing 770 stayed green except the two noted below whose
contract legitimately changed.

- `tests/test_client.py`: `establish_connection` timeout raises
  `BleakOperationTimeoutError` and marks `_broken`;
  `write_gatt_char` timeout marks `_broken` and best-effort disconnects;
  `_broken` forces a fresh reconnect even when `is_connected` still
  reports `True` (and clears on a successful reconnect); disconnect
  timeout is bounded and swallowed.
- `tests/test_device.py`: `command_transaction(deadline=...)` timeout
  resets the connection and raises; `serialized_device_command` converts
  that timeout into the method's normal falsy return (the
  service-handler-pattern proof); `async_reset_connection` discards the
  client (including when `stop()` itself raises, and when there was no
  client to begin with); schedule-write mismatch→retry→success and
  mismatch→retry→failure-naming-the-field for both Auto and Pro (mocked
  verify, proving the retry control flow); a real (unmocked) verify
  reproducing the exact live bug (day levels stayed `[68,100,100,90]`
  against a request for `[40,60,60,54]`) and a matching real-verify
  success case that also proves `values` stays populated after a verified
  write; the three readback-shape acceptance tests from §6.
- `tests/test_guardian.py`: a hung `async_read_state`/`async_ensure_mode`/
  schedule-push each report the correct outcome (`unreachable`/`failed`)
  within their bounded deadline, call `async_reset_connection`, and leave
  `_check_lock` free afterward; the overall cap bounds the check even when
  a step's own deadline is deliberately huge; an interval trigger is
  skipped (not queued) while a check already holds `_check_lock`, and
  still notifies listeners. New `_short_check_timeouts` fixture
  monkeypatches the module-level deadline constants (they are read fresh
  per call, not baked into a decorator, so this works); the overall
  timeout is kept a fixed multiple *larger* than the per-step ones in
  every test, because both clocks start from the same `async_check()`
  call and equal durations would let the overall cap always win the race,
  masking which specific step timed out.
- `tests/test_guardian_entities.py`: `_FakeGuardian.effective_status`
  falls back to `.status` unless a test overrides it, so existing tests
  needed no changes beyond the enum-options set; new tests for the
  stale-status sensor value and the mode select's schedule-readback
  attributes (present, `None` before any readback, and present without a
  guardian).

### Existing tests adjusted (contract legitimately changed)

- `tests/test_guardian_entities.py::test_guardian_status_sensor_reflects_enum_state_and_options`:
  the enum now includes `"stale"` (§7) — the options set assertion is
  updated to match.
- `tests/test_device.py::test_plant_pro_native_schedule_actions_write_fixture_packets`
  and `::test_five_channel_facebd_auto_schedule_writes_all_fixture_levels`:
  both now mock the new `_async_verify_native_auto/pro_schedule` steps
  (verified, no mismatch) so their existing packet-sequence assertions are
  unaffected; the first also asserts `values["native_auto_schedule"]`/
  `values["native_pro_schedule"]` are **retained** rather than popped
  (§6's "no longer pops on success" change).

## Acceptance

- Full suite green: 797 passed (770 original + 27 new; two adjusted per
  above).
- `python3 -m py_compile` clean on every changed module.
- `grep -n 'await' core/client.py` reviewed line by line (§2); every
  BLE-facing await is bounded.
- Version bumped to 1.1.2 (`manifest.json`); `CHANGELOG.md` and
  `docs/releases/v1.1.2.md` (starts with `# Fluval BLE v1.1.2`, format
  matches `docs/releases/v1.0.1.md`) added.
