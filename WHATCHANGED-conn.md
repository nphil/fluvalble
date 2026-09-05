# WHATCHANGED — connection slice (FluvalConn)

Scope: `custom_components/fluvalble/core/client.py`, `core/device.py` (connection /
handshake / command paths only), `__init__.py` (options wiring + advertisement
callback), `config_flow.py` (options-flow `hold_connection` field). Guardian owns
`core/guardian.py` and every guardian entity; this slice only exposes the hooks it
needs. No test files or README were touched (TestsCI-3 owns tests).

## Capability / lifecycle map (read before editing further)

**`core/client.py` — `Client`: pure BLE transport, zero Home Assistant imports.**
Owns exactly one physical GATT session at a time behind `self.client` (a raw
`bleak.BleakClient`). `_ensure_client()` connects (via `bleak_retry_connector
.establish_connection`, itself using the `device_provider` callback Device
supplies so ESPHome-proxy route selection stays HA's job) and is guarded by
`_connection_lock`; `_initialize_session()` runs the APK handshake exactly once
per physical session behind `_initialization_lock` (wake-read → `ready_callback`
→ first status read/`request_state()` → `state_ready_callback`). A single
background task (`ping_task` / `_ping_loop`) is the connection supervisor: while
connected it heart-beats every `ping_interval` seconds and flushes any
`send()`-queued packet; once idle past `active_time` (and not persistent) it
disconnects; on any disconnect (`_on_disconnected`) it decides whether to
auto-restart itself. `send_now()` is the single serialized (`_command_lock`)
write path used by every command; `request_state()` is the single read path.
Profile resolution (`legacy_encrypted` classic / `facebd_command` WiFi /
`plant_pro_spp`) happens once in `_resolve_characteristics()` and is exposed via
`raw_facebd`/`wifi_facebd`/`plant_pro_spp`/`command_write_uuid`.

**`core/device.py` — `Device`: HA-aware glue + protocol semantics.** Wraps one
`Client`, decodes wire frames into `self.values`/`self.diagnostics`
(`decode_update_packet` and friends), and is the thing every entity/service
calls. `_async_prepare_command()` / `_async_send_packet()` / `_async_ensure_client()`
are the choke points literally every write and read goes through. Mode-aware
writes (channel/effect/switch) already forced the fixture to `manual` before
writing — that pre-existed for channels/effects but was *missing* for the plain
on/off switch, a real gap now closed. The classic protocol's `_async_send_packet`
never actually verified a write took effect (see Command confirmation below) —
also a real, now-closed gap.

**`__init__.py`**: owns config-entry lifecycle — `_create_device` builds the
`Device` from merged `entry.data`/`entry.options`, an `ACTIVE`-mode bluetooth
callback (`update_ble`) keeps advertisement metadata fresh and creates the
`Device`/`Client` on first sight, and `async_unload_entry` calls `client.stop()`.

**`config_flow.py`**: `OPTIONS_SCHEMA` is the single source of truth for every
user-facing connection knob (`ping_interval`, `active_time`, now `hold_connection`).

## Ground-truth pitfalls this slice addresses

| # | Pitfall (from the brief) | Where / how |
|---|---|---|
| 1 | Light allows exactly one BLE central; permanently holding it locks the phone app out. | New `Device.hold_connection` (default **True**, `CONF_HOLD_CONNECTION` option) is a genuine "let the app in" escape hatch: `False` forces an immediate disconnect and refuses **every** new connection — including on-demand command connects and Guardian's own checks — until flipped back. Enforced once, centrally, in `Device._connection_parked()` / `_async_ensure_client()` / `_async_prepare_command()` / `async_sync_clock()` / `async_read_state()`, not scattered per call site. |
| 2 | RTC drifts on power loss; clock must be sent on **every** connect, not once per session. | Verified (not re-implemented — it already worked): `Client._initialize_session()` invokes `ready_callback` (`Device._async_on_client_ready`, sends the clock command) strictly before the first status read on every physical session, and `Device.set_connected(False)` resets `_clock_synced`/`_clock_sync_started` on every disconnect so the sequence re-runs on every reconnect. Proved end-to-end (real chunked/encrypted classic frames, two physical connect cycles) in the throwaway script — see Verification. |
| 3 | Manual writes while Auto/Pro are silently ignored unless mode is set to manual first; nothing restores Auto automatically. | All three write families (channel `_async_set_channels_now`, effect `async_set_effect`, on/off `async_set_switch`) now route the "am I in manual" check through one shared `async_ensure_mode("manual")` instead of three copies of hand-rolled mode-packet dispatch. **New gap closed**: `async_set_switch` (plain light on/off with no colour change) never forced manual before — a bare `light.turn_off()`/`turn_on()` while Auto/Pro was active would previously have been silently dropped by the fixture. `Device.mode_changed_by_write` is set `True` exactly when a write forces this escalation (cleared by any explicit `async_select_option`), so the Guardian can start an override window precisely when HA — not the schedule — changed the mode. `async_ensure_mode(mode)` is the new single, public, no-BLE-if-already-there primitive Guardian uses for its own drift correction. Restoring Auto automatically remains the Guardian's job by design (`async_ensure_mode`/`async_select_option` give it the tool; supervising *when* to call it is its business logic, not connection plumbing). |
| 4 | Status body shape depends on mode: manual carries live levels, Auto/Pro carries the schedule; never fake zeros for unknown levels. | `Device.async_read_state()` (new) returns a frozen `FluvalState(mode, power, levels, auto_schedule, pro_schedule, last_state_at, connection_attempts, scanner_source)`. `power`/`levels` are `None` (not zeroed) whenever `mode != "manual"`; `auto_schedule`/`pro_schedule` are populated only for their matching mode, straight from the fields `decode_update_packet` already parses — no new decode logic invented, this is a typed read-only view over what the base already tracks in `self.values`. |
| 5 | No live HA/hardware access; verify with the repo's suite plus new tests. | Whole slice implemented and verified against `tests/test_connection_policy.py` (36 tests, written collaboratively with TestsCI-3 against the frozen surface announced over hub) plus the full existing suite (760/760 green) plus a throwaway integration script (deleted; see Verification) driving a real simulated classic fixture — encrypted/chunked frames via the actual `core.encryption`/`core.protocol` modules, not shortcuts. |
| 6 | Classic transport frame/command details (0x54 envelope, per-frame XOR key, 15-byte plaintext chunks, XOR checksum; opcodes 68 02/03/04/05/0E). | Unchanged — already correctly implemented in `protocol.py`/`encryption.py`/`client.py`. Confirmed by the throwaway script decoding/encoding real frames end-to-end through the unmodified encode/decode path. One asymmetry worth documenting for the next person: the classic **status response** (68 05 reply, `decode_old_state_packet`) is little-endian per channel, but the classic **all-zone command** (68 04, `old_all_zone_packet`) is big-endian per channel (`packet.extend((scaled >> 8, scaled & 0xFF))`, matching the decompiled APK's own quirk) — same fixture, two different byte orders depending on direction. Not a bug; just easy to get backwards when writing a fixture simulator (I did, once, in the throwaway script). |
| — | "1005 register write" mentioned in the ground-truth doc. | Not referenced anywhere in this codebase; `start_notify`'s CCCD write is handled transparently by `bleak`. Left alone — no behavior in this fork depends on an explicit 0x1005 write, and nothing in the Contract asked for one. |

## Contract additions, file by file

### `core/client.py`
- `hold_connection: bool` constructor param (**default `False`** — see "Why Client
  defaults False" below), property + setter. Setter is idempotent (no-op if
  unchanged); `True` wakes a backed-off wait and calls `ping()`; `False`
  collapses `ping_time` to `0` and cancels `ping_future`, letting the
  **existing, already-tested** idle-disconnect path (`_ping_loop` →
  `_safe_disconnect`) take the connection down — no new disconnect task.
- `_persistent()` = `active_time == 0 OR hold_connection` — the single place
  that decides "should this client hold and repair the connection forever".
  Every place that used to test `self._active_time == 0` (`ping()`,
  `_on_disconnected`, `_connect()`'s finally) now tests `_persistent()` instead;
  behaviourally identical when `hold_connection` is left at its default `False`.
- `notify_advertisement_seen()` — sets an `asyncio.Event`; the persistent
  supervisor's backoff wait (see next point) is interruptible by it, so a
  proxy hearing the fixture again short-circuits a long backoff instead of
  waiting it out.
- Reconnect backoff: module-level pure `reconnect_backoff_seconds(attempt)`,
  `RECONNECT_BACKOFF_MIN=2.0`, `RECONNECT_BACKOFF_MAX=120.0`, formula
  `random.uniform(MIN, min(MAX, MIN * 2**(attempt-1)))` — attempt 1 collapses
  to exactly `2.0`, the upper bound saturates at exactly `120.0` from attempt 7.
  `_ping_loop`'s post-failure wait now uses this (jittered, capped, interruptible
  by `notify_advertisement_seen()`/wakes) **only** when `_persistent()` is true;
  the non-persistent (today's finite `active_time`) path is untouched byte-for-byte.
  `_reconnect_failures` resets to `0` on every successful (re)connect.
- `connection_attempts: int` — incremented once per `establish_connection(...)`
  call inside `_ensure_client()` (i.e. once per real attempt, not per success).
- `_async_disconnect(final=True)` (i.e. `Client.stop()`, called on integration
  unload) now also does a best-effort `stop_notify` over every resolved notify
  characteristic before the existing disconnect — "stop notify, disconnect,
  cancel supervisor" as three explicit steps instead of two. Scoped to `final`
  only so routine idle/reconnect cycles stay exactly as fast as before.

**Why `Client.hold_connection` defaults `False` while `Device.hold_connection`
defaults `True`:** `Client` is constructed directly by ~40 pre-existing unit
tests that never pass this new parameter and expect today's `active_time`-only
behaviour unchanged. Making the *product* default (always hold, always
supervise) live entirely in `Device` — which explicitly passes
`hold_connection=self._hold_connection` into every `Client` it creates —
means the transport layer's own default stays a no-op, and every existing
`Client`-level test needed zero changes.

**Known residual interaction, deliberately left as-is:** `_persistent()` is
`active_time == 0 OR hold_connection` (an OR, not an override) so that
`hold_connection`'s frozen semantics (agreed with TestsCI-3, see
`tests/test_connection_policy.py`'s module docstring) stay exactly
"persistent when either says so" — flipping `hold_connection` to `False`
does not, by itself, stop the `Client`-level supervisor from self-healing a
dropped connection for an entry whose numeric `active_time` option is
explicitly `0`. In practice this cannot surface as "the switch didn't work":
`Device` always drives `hold_connection` as the sole persistence knob and
leaves the numeric option at its finite default (120s) unless a user
deliberately sets *both* "persistent" (0) and turns the switch off — a
self-contradictory combination. The `Device`-level `_connection_parked()`
guard (keyed on `hold_connection` alone, see below) is what actually
enforces "stay off until True" for every real command/read/Guardian-check
path regardless of this Client-level nuance, so the observable contract
("hold_connection=False refuses new connections") holds either way — only
the *heartbeat* keeps quietly redialing in that edge case, which harms
nothing since every consumer already refuses to use the resulting link.

### `core/device.py`
- `hold_connection` property + setter (backed by `self._hold_connection`,
  read from `config_data["hold_connection"]`, default `True`). Setter
  delegates to `self.client.hold_connection` when a client exists and fires
  the existing `updates_connect` handlers on change (so an entity mapped to
  it — `HALayer`'s job — repaints for free with the pattern already used by
  `daylight_saving_time`).
- `_connection_parked()` = `not hold_connection and not self.connected` — the
  one check `_async_ensure_client()`, `_async_prepare_command()`,
  `async_sync_clock()`, and `async_read_state()` all call first, so "stay off
  until True" is enforced once, not re-derived per call site. Deliberately
  keyed on `self.connected` (the existing, already-tested GATT-status flag)
  rather than reaching into the client, so an in-flight connection is allowed
  to finish even if `hold_connection` flips mid-command.
- `update_ble()` (the advertisement handler) now only creates a `Client` the
  first time a fixture is seen when `hold_connection` is true — previously it
  unconditionally created one (which immediately opens a GATT session),
  which would have silently defeated "stay off" the instant any advertisement
  arrived.
- `register_connection_listener(listener) -> unsubscribe` — additive to the
  existing `updates_connect`/`updates_component`/`register_update` mechanism,
  not a replacement. Fires `listener(connected: bool)` on every transition
  from `set_connected()`; deliberately **not** fired immediately at
  registration (matches the existing convention where `FluvalEntity.__init__`
  calls its own `internal_update()` once before subscribing) — a late
  subscriber reads `device.connected` itself if it needs the current value,
  with no race since nothing awaits between registration and that read.
- `async_ensure_mode(mode)` — sends the resolved transport's mode packet only
  if `self.values["mode"] != mode`, then relies on the write's own
  confirmation (see below) to have refreshed `self.values["mode"]`, and
  returns that freshly confirmed value (which may legitimately differ from
  what was requested). Raises `ValueError` for a literal outside `MODES`
  (programmer error); returns `None` only for a real comms failure or while
  parked. Used internally by all three mode-aware write sites and externally
  by the Guardian for drift correction — one implementation, not two.
- `async_read_state()` — talks to `self.client` **directly**
  (`client.request_state()`), deliberately bypassing
  `_async_ensure_client()`/`async_refresh_state()`'s HA-bluetooth-component
  device lookup: `Client` already refreshes its own route via the
  `device_provider` callback it was constructed with, so the extra HA lookup
  bought nothing except a dependency on `Device.hass` being set. Returns
  `None` when parked or on a real read failure.
- Classic-protocol command confirmation (the real gap): `_async_send_packet`
  now computes `_classic_confirmation_target(packet)` — the intended
  mode/power/levels for a classic (non-FACEBD/non-SPP) `68 02`/`68 03`/`68 04`
  packet, captured from the packet bytes (mode/switch) or from `self.values`
  (all-zone, captured *before* the write so it holds the caller's intent, not
  a post-write echo) — and, when present, performs a fresh `68 05` read via
  `_async_confirm_classic_state` and fails the whole write (same
  `return False` → `HomeAssistantError` path every other command failure
  already uses) if the fixture doesn't confirm it. Before this change, every
  classic write (`raw_facebd == False`, i.e. our target AquaSky) reported
  success the instant the bytes went out over the air, with zero verification
  that the fixture actually did anything — `client.last_write_verified` was
  hard-wired `False` forever on this transport. Schedules/presets/effects/
  identify/clock stay submission-only (unchanged), matching the existing
  "confirms submission, not readback" comment for schedules.
- `FluvalState` — new frozen dataclass, the return type of `async_read_state()`.

### `__init__.py`
- `.core` import gains `CONF_HOLD_CONNECTION`/`DEFAULT_HOLD_CONNECTION`.
- New **passive** `bluetooth.async_register_callback` (separate from the
  existing `ACTIVE`-mode `update_ble` registration, which keeps doing exactly
  what it did) whose only job is `device.client.notify_advertisement_seen()`
  — waking a backed-off reconnect the instant any scanner (including an
  ESPHome proxy) hears the fixture again, without asking the scanner to run
  more aggressively than `update_ble`'s own `ACTIVE` registration already does.

### `config_flow.py`
- `OPTIONS_SCHEMA` gains `vol.Optional(CONF_HOLD_CONNECTION, default=True): bool`
  next to `CONF_LAMP_PROFILE`, ahead of `CONF_PING_INTERVAL`/`CONF_ACTIVE_TIME`.
  No new validation function needed (plain boolean, HA renders it as a toggle).

## Verification

- `python -m py_compile` clean on all four touched files (plus `core/__init__.py`).
- Full suite: **760 passed**, 0 failed (started at 663; net growth is
  `test_connection_policy.py` plus the Guardian slice's own new test files).
- Throwaway script (written, run, then deleted — not part of the repo): built a
  real classic 4-channel AquaSky fixture simulator (own mode/power/channel
  state, correct XOR checksum via `protocol.old_packet`, correct 15-byte
  chunking/per-chunk-random-key encryption via
  `encryption.encode_message_chunks`/`decode_message`, correct big-endian
  `68 04` vs little-endian `68 05` byte order) behind a patched
  `bleak_retry_connector.establish_connection`, then drove the **real**
  `Client`/`Device` classes (no mocked confirm-read, no shortcuts) through:
  1. Initial connect → clock (`0x0E`) strictly precedes the first status read
     (`0x05`) — **pass**.
  2. Forced disconnect + reconnect → clock resent, again strictly before the
     next status read (proves "every connect", not "once per runtime") — **pass**.
  3. `async_set_channels({"channel_1": 80})` while the simulated fixture
     reports Auto → packet order `68 02 00` (mode → manual) then `68 04`
     (channels), fixture firmware actually flips to manual, write is confirmed,
     `mode_changed_by_write` is `True` — **pass**.
  4. `hold_connection = False` then `notify_advertisement_seen()` → zero new
     connection attempts; `hold_connection = True` → exactly one new
     connection attempt happens shortly after — **pass**.

## Coordination notes for future readers

Frozen hook surface (names, exact semantics, backoff formula, mocking seams)
was negotiated live over `hub` with `TestsCI-3` (tests) and `FluvalGuardian`
(`core/guardian.py` + guardian entities) before and during implementation;
`tests/test_connection_policy.py` and `core/guardian.py` were written against
those same names concurrently. `config_flow.py`'s `OPTIONS_SCHEMA` is shared
with the Guardian slice (its 4 options were appended after `hold_connection`
landed here, to avoid a concurrent-edit collision on the same dict).
