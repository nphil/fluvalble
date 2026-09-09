"""Pure state-machine tests for ScheduleGuardian.

ScheduleGuardian never touches hass directly (that lives in the
``async_setup_guardian`` factory) — it is constructed with a duck-typed
device double exposing five async methods:

    await device.async_sync_clock()
    state = await device.async_read_state()      # .mode / .auto_schedule / .pro_schedule
    confirmed = await device.async_ensure_mode(mode)
    ok = await device.async_set_native_auto_schedule(schedule)
    ok = await device.async_set_native_pro_schedule(points)
    await device.async_reset_connection()

and an injectable ``now_fn`` (epoch seconds) so override-timer expiry is
deterministic without real sleeps.
"""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.fluvalble.core import guardian as guardian_module
from custom_components.fluvalble.core.guardian import ScheduleGuardian


class _FakeDevice:
    """Minimal double implementing the guardian's device duck-type."""

    def __init__(
        self,
        *,
        mode="automatic",
        auto_schedule=None,
        pro_schedule=None,
        read_state_ok=True,
        ensure_mode_result="__match__",
        push_schedule_ok=True,
        raise_on_ensure_mode=False,
        raise_on_push=False,
        hang_on_read_state=False,
        hang_on_ensure_mode=False,
        hang_on_push=False,
        priority_waiting=0,
        priority_waiting_after_clock_sync=None,
        priority_waiting_after_read_state=None,
    ):
        self.mac = "AA:BB:CC:DD:EE:FF"
        self.mode = mode
        self.auto_schedule = auto_schedule
        self.pro_schedule = pro_schedule
        self.read_state_ok = read_state_ok
        # "__match__" means async_ensure_mode succeeds and confirms whatever
        # mode was requested; any other value (including None/"") overrides
        # the confirmed mode returned to the guardian.
        self.ensure_mode_result = ensure_mode_result
        self.push_schedule_ok = push_schedule_ok
        self.raise_on_ensure_mode = raise_on_ensure_mode
        self.raise_on_push = raise_on_push
        # Never resolves - simulates a GATT op wedged behind an unbounded
        # await, the exact live failure this guardian bounding defends
        # against. Only useful with a short guardian CHECK_*_TIMEOUT (see
        # `_short_check_timeouts` fixture below), or the test itself hangs.
        self.hang_on_read_state = hang_on_read_state
        self.hang_on_ensure_mode = hang_on_ensure_mode
        self.hang_on_push = hang_on_push
        # `Device.priority_waiting`: user-initiated commands queued for or
        # holding the device's command lock. The `*_after_*` variants make a
        # user command "arrive" mid-check so the deferral boundary between
        # two specific steps can be exercised.
        self.priority_waiting = priority_waiting
        self.priority_waiting_after_clock_sync = priority_waiting_after_clock_sync
        self.priority_waiting_after_read_state = priority_waiting_after_read_state

        self.sync_clock_calls = 0
        self.read_state_calls = 0
        self.ensure_mode_calls: list[str] = []
        self.auto_schedule_pushes: list[object] = []
        self.pro_schedule_pushes: list[object] = []
        self.reset_connection_calls = 0

    async def async_sync_clock(self):
        self.sync_clock_calls += 1
        if self.priority_waiting_after_clock_sync is not None:
            self.priority_waiting = self.priority_waiting_after_clock_sync
        return True

    async def async_read_state(self):
        self.read_state_calls += 1
        if self.hang_on_read_state:
            await asyncio.sleep(3600)
        if not self.read_state_ok:
            raise RuntimeError("fixture unreachable")
        if self.priority_waiting_after_read_state is not None:
            self.priority_waiting = self.priority_waiting_after_read_state
        return SimpleNamespace(mode=self.mode, auto_schedule=self.auto_schedule, pro_schedule=self.pro_schedule)

    async def async_ensure_mode(self, mode):
        self.ensure_mode_calls.append(mode)
        if self.hang_on_ensure_mode:
            await asyncio.sleep(3600)
        if self.raise_on_ensure_mode:
            raise RuntimeError("write failed")
        confirmed = mode if self.ensure_mode_result == "__match__" else self.ensure_mode_result
        if confirmed:
            self.mode = confirmed
        return confirmed

    async def async_set_native_auto_schedule(self, schedule):
        self.auto_schedule_pushes.append(schedule)
        if self.hang_on_push:
            await asyncio.sleep(3600)
        if self.raise_on_push:
            raise RuntimeError("push failed")
        if self.push_schedule_ok:
            self.auto_schedule = schedule
        return self.push_schedule_ok

    async def async_set_native_pro_schedule(self, points):
        self.pro_schedule_pushes.append(points)
        if self.hang_on_push:
            await asyncio.sleep(3600)
        if self.raise_on_push:
            raise RuntimeError("push failed")
        if self.push_schedule_ok:
            self.pro_schedule = points
        return self.push_schedule_ok

    async def async_reset_connection(self):
        self.reset_connection_calls += 1


@pytest.fixture
def _short_check_timeouts(monkeypatch):
    """Shrink every guardian check deadline so a hung device fails fast in tests.

    The overall cap must stay well above the per-step ones: `async_check`
    starts its `CHECK_OVERALL_TIMEOUT` clock before any step-specific one
    starts its own, so equal durations would let the overall cap always win
    the race and mask which specific step actually timed out.
    """
    for name in (
        "CHECK_CLOCK_SYNC_TIMEOUT",
        "CHECK_READ_STATE_TIMEOUT",
        "CHECK_ENSURE_MODE_TIMEOUT",
        "CHECK_SCHEDULE_REPUSH_TIMEOUT",
    ):
        monkeypatch.setattr(guardian_module, name, 0.02)
    monkeypatch.setattr(guardian_module, "CHECK_OVERALL_TIMEOUT", 0.3)


class _Clock:
    """Injectable epoch-seconds clock for deterministic override timers."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Constructor defaults
# ---------------------------------------------------------------------------


def test_constructor_defaults_match_the_documented_options():
    device = _FakeDevice(mode="automatic")
    guardian = ScheduleGuardian(device)

    assert guardian.expected_mode == "auto"
    assert guardian.check_interval_min == 10
    assert guardian.override_return_min == 60
    assert guardian.alert_after_failures == 3
    assert guardian.corrections == 0
    assert guardian.consecutive_failures == 0
    assert guardian.consecutive_unreachable == 0
    assert guardian.problem is False
    assert guardian.override_active is False
    assert guardian.override_until is None
    assert guardian.status == "unknown"
    assert guardian.last_check_at is None


# ---------------------------------------------------------------------------
# Basic ok / no-op paths
# ---------------------------------------------------------------------------

def test_status_transitions_from_unknown_to_ok_after_the_first_check():
    device = _FakeDevice(mode="automatic")
    guardian = ScheduleGuardian(device, expected_mode="auto")
    assert guardian.status == "unknown"

    outcome = _run(guardian.async_check())

    assert outcome == "ok"
    assert guardian.status == "ok"


def test_status_transitions_from_unknown_to_paused_for_unsupervised_after_first_check():
    device = _FakeDevice(mode="manual")
    guardian = ScheduleGuardian(device, expected_mode="unsupervised")
    assert guardian.status == "unknown"

    outcome = _run(guardian.async_check())

    assert outcome == "paused"
    assert guardian.status == "paused"


def test_check_returns_ok_when_mode_already_matches_and_no_schedule_configured():
    device = _FakeDevice(mode="automatic")
    guardian = ScheduleGuardian(device, expected_mode="auto")

    outcome = _run(guardian.async_check())

    assert outcome == "ok"
    assert guardian.status == "ok"
    assert device.ensure_mode_calls == []
    assert device.sync_clock_calls == 1
    assert guardian.corrections == 0
    assert guardian.problem is False


def test_check_syncs_clock_and_reads_state_on_every_call():
    device = _FakeDevice(mode="automatic")
    guardian = ScheduleGuardian(device, expected_mode="auto")

    _run(guardian.async_check())
    _run(guardian.async_check())

    assert device.sync_clock_calls == 2
    assert device.read_state_calls == 2


def test_last_check_at_uses_injected_clock():
    clock = _Clock(1_000.0)
    device = _FakeDevice(mode="automatic")
    guardian = ScheduleGuardian(device, expected_mode="auto", now_fn=clock)

    _run(guardian.async_check())
    assert guardian.last_check_at == 1_000.0

    clock.advance(60)
    _run(guardian.async_check())
    assert guardian.last_check_at == 1_060.0


# ---------------------------------------------------------------------------
# expected_mode="manual"
# ---------------------------------------------------------------------------


def test_manual_expected_mode_is_a_noop_when_already_manual():
    device = _FakeDevice(mode="manual")
    guardian = ScheduleGuardian(device, expected_mode="manual")

    assert _run(guardian.async_check()) == "ok"
    assert device.ensure_mode_calls == []


def test_manual_expected_mode_corrects_drift():
    device = _FakeDevice(mode="automatic")
    guardian = ScheduleGuardian(device, expected_mode="manual")

    assert _run(guardian.async_check()) == "corrected"
    assert device.ensure_mode_calls == ["manual"]
    assert device.mode == "manual"
    assert guardian.corrections == 1


def test_manual_expected_mode_reports_failed_when_ensure_mode_cannot_confirm():
    device = _FakeDevice(mode="automatic", ensure_mode_result=None)
    guardian = ScheduleGuardian(device, expected_mode="manual")

    assert _run(guardian.async_check()) == "failed"
    assert guardian.corrections == 0
    assert guardian.consecutive_failures == 1


# ---------------------------------------------------------------------------
# expected_mode="unsupervised"
# ---------------------------------------------------------------------------


def test_unsupervised_never_touches_mode_and_is_always_paused():
    device = _FakeDevice(mode="manual")
    guardian = ScheduleGuardian(device, expected_mode="unsupervised")

    assert _run(guardian.async_check()) == "paused"
    assert device.ensure_mode_calls == []
    assert device.auto_schedule_pushes == []
    assert guardian.status == "paused"
    assert guardian.problem is False


def test_unsupervised_stays_paused_even_when_fixture_is_unreachable():
    device = _FakeDevice(read_state_ok=False)
    guardian = ScheduleGuardian(device, expected_mode="unsupervised")

    assert _run(guardian.async_check()) == "paused"
    assert guardian.consecutive_unreachable == 0
    assert guardian.problem is False


# ---------------------------------------------------------------------------
# Auto / Pro mode-drift correction (state.mode is the OTHER scheduled mode)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expected_mode", "target", "other"),
    [("auto", "automatic", "professional"), ("pro", "professional", "automatic")],
)
def test_scheduled_mode_drift_between_auto_and_pro_is_corrected_same_cycle(expected_mode, target, other):
    device = _FakeDevice(mode=other)
    guardian = ScheduleGuardian(device, expected_mode=expected_mode)

    assert _run(guardian.async_check()) == "corrected"
    assert device.ensure_mode_calls == [target]
    assert device.mode == target
    assert guardian.corrections == 1


# ---------------------------------------------------------------------------
# Schedule drift (mode already correct, stored schedule differs)
# ---------------------------------------------------------------------------


def test_auto_schedule_drift_triggers_repush_and_reports_corrected():
    device = _FakeDevice(mode="automatic", auto_schedule={"sunrise": 480})
    guardian = ScheduleGuardian(device, expected_mode="auto", expected_schedule={"sunrise": 420})

    assert _run(guardian.async_check()) == "corrected"
    assert device.auto_schedule_pushes == [{"sunrise": 420}]
    assert device.pro_schedule_pushes == []
    assert guardian.corrections == 1


def test_pro_schedule_drift_triggers_repush_and_reports_corrected():
    device = _FakeDevice(mode="professional", pro_schedule=[{"minute": 0}])
    guardian = ScheduleGuardian(device, expected_mode="pro", expected_schedule=[{"minute": 720}])

    assert _run(guardian.async_check()) == "corrected"
    assert device.pro_schedule_pushes == [[{"minute": 720}]]
    assert device.auto_schedule_pushes == []
    assert guardian.corrections == 1


def test_matching_schedule_does_not_repush():
    schedule = {"sunrise": 420}
    device = _FakeDevice(mode="automatic", auto_schedule=schedule)
    guardian = ScheduleGuardian(device, expected_mode="auto", expected_schedule=schedule)

    assert _run(guardian.async_check()) == "ok"
    assert device.auto_schedule_pushes == []


def test_no_expected_schedule_configured_never_pushes():
    device = _FakeDevice(mode="automatic", auto_schedule={"sunrise": 999})
    guardian = ScheduleGuardian(device, expected_mode="auto", expected_schedule=None)

    assert _run(guardian.async_check()) == "ok"
    assert device.auto_schedule_pushes == []


def test_schedule_repush_failure_reports_failed():
    device = _FakeDevice(mode="automatic", auto_schedule={"sunrise": 480}, push_schedule_ok=False)
    guardian = ScheduleGuardian(device, expected_mode="auto", expected_schedule={"sunrise": 420})

    assert _run(guardian.async_check()) == "failed"
    assert guardian.corrections == 0
    assert guardian.consecutive_failures == 1


def test_mode_and_schedule_drift_together_count_as_one_correction():
    device = _FakeDevice(mode="professional", auto_schedule={"sunrise": 480})
    guardian = ScheduleGuardian(device, expected_mode="auto", expected_schedule={"sunrise": 420})

    assert _run(guardian.async_check()) == "corrected"
    assert device.ensure_mode_calls == ["automatic"]
    assert device.auto_schedule_pushes == [{"sunrise": 420}]
    assert guardian.corrections == 1


# ---------------------------------------------------------------------------
# Deferral: supervision stands aside for a user-initiated command
# ---------------------------------------------------------------------------


def test_check_defers_without_touching_the_fixture_while_a_user_command_is_pending():
    device = _FakeDevice(mode="professional", priority_waiting=1)
    guardian = ScheduleGuardian(device, expected_mode="auto")

    assert _run(guardian.async_check()) == "deferred"
    assert guardian.status == "deferred"
    # Not one GATT-facing step ran: the point is to hand the radio over.
    assert device.sync_clock_calls == 0
    assert device.read_state_calls == 0
    assert device.ensure_mode_calls == []


def test_check_defers_between_steps_when_a_user_command_arrives_mid_check():
    """The live failure was a press queued behind a check already underway."""
    device = _FakeDevice(mode="professional", priority_waiting_after_clock_sync=1)
    guardian = ScheduleGuardian(device, expected_mode="auto")

    assert _run(guardian.async_check()) == "deferred"
    assert device.sync_clock_calls == 1
    assert device.read_state_calls == 0


def test_check_defers_before_correcting_drift_when_a_user_command_arrives():
    device = _FakeDevice(mode="professional", priority_waiting_after_read_state=1)
    guardian = ScheduleGuardian(device, expected_mode="auto")

    assert _run(guardian.async_check()) == "deferred"
    assert device.read_state_calls == 1
    # The drift is real and still uncorrected - deferral must not write.
    assert device.ensure_mode_calls == []
    assert device.mode == "professional"


def test_check_defers_before_repushing_a_drifted_schedule():
    device = _FakeDevice(
        mode="automatic",
        auto_schedule={"sunrise": 480},
        priority_waiting_after_read_state=1,
    )
    guardian = ScheduleGuardian(device, expected_mode="auto", expected_schedule={"sunrise": 420})

    assert _run(guardian.async_check()) == "deferred"
    assert device.auto_schedule_pushes == []


def test_deferral_neither_counts_as_a_failure_nor_clears_a_failure_streak():
    device = _FakeDevice(mode="professional", raise_on_ensure_mode=True)
    guardian = ScheduleGuardian(device, expected_mode="auto", alert_after_failures=2)

    assert _run(guardian.async_check()) == "failed"
    assert _run(guardian.async_check()) == "failed"
    assert guardian.problem is True

    device.priority_waiting = 1
    assert _run(guardian.async_check()) == "deferred"

    # The fixture is still broken while the user is pressing buttons at it.
    assert guardian.consecutive_failures == 2
    assert guardian.problem is True


def test_deferred_check_is_not_reported_as_stale():
    """`last_check_at` still advances: the supervisor is alive, just polite."""
    clock = _Clock()
    device = _FakeDevice(mode="automatic", priority_waiting=1)
    guardian = ScheduleGuardian(device, expected_mode="auto", check_interval_min=10, now_fn=clock)

    assert _run(guardian.async_check()) == "deferred"
    clock.advance(60)

    assert guardian.effective_status == "deferred"


def test_runner_rearms_a_deferred_check_instead_of_waiting_a_full_interval(monkeypatch):
    _run(_async_test_runner_rearms_a_deferred_check_instead_of_waiting_a_full_interval(monkeypatch))


async def _async_test_runner_rearms_a_deferred_check_instead_of_waiting_a_full_interval(monkeypatch):
    from unittest.mock import MagicMock

    later: list[tuple[float, object]] = []
    cancelled: list[int] = []

    def fake_call_later(hass, delay, action):
        later.append((delay, action))
        return lambda: cancelled.append(len(later))

    monkeypatch.setattr(guardian_module, "async_track_time_interval", lambda hass, action, interval: (lambda: None))
    monkeypatch.setattr(guardian_module, "async_call_later", fake_call_later)

    device = _FakeDevice(mode="automatic", priority_waiting=1)
    device.register_connection_listener = lambda cb: (lambda: None)
    guardian = ScheduleGuardian(device, expected_mode="auto", check_interval_min=10)

    created: list[object] = []
    hass = MagicMock()
    hass.async_create_task.side_effect = created.append
    unsub = guardian.start_runner(hass)

    # start_runner's initial trigger: run the coroutine it handed to hass.
    assert len(created) == 1
    await created[0]

    assert guardian.status == "deferred"
    assert [delay for delay, _action in later] == [guardian_module.DEFERRED_RETRY_SECONDS]

    # The retry fires a fresh check; with the user command gone it completes.
    device.priority_waiting = 0
    later[0][1]()
    assert len(created) == 2
    await created[1]

    assert guardian.status == "ok"
    assert len(later) == 1  # a completed check re-arms nothing

    unsub()

# ---------------------------------------------------------------------------
# Failure / exception handling never propagates
# ---------------------------------------------------------------------------


def test_ensure_mode_exception_is_reported_as_failed_not_raised():
    device = _FakeDevice(mode="professional", raise_on_ensure_mode=True)
    guardian = ScheduleGuardian(device, expected_mode="auto")

    assert _run(guardian.async_check()) == "failed"


def test_ensure_mode_confirming_wrong_mode_is_reported_as_failed():
    device = _FakeDevice(mode="professional", ensure_mode_result="professional")
    guardian = ScheduleGuardian(device, expected_mode="auto")

    outcome = _run(guardian.async_check())

    # The device confirmed a mode other than the one requested — the write
    # did not actually take effect the way the guardian asked for it to.
    assert outcome == "failed"


def test_read_state_exception_is_reported_as_unreachable_not_raised():
    device = _FakeDevice(read_state_ok=False)
    guardian = ScheduleGuardian(device, expected_mode="auto")

    assert _run(guardian.async_check()) == "unreachable"
    assert guardian.consecutive_unreachable == 1


# ---------------------------------------------------------------------------
# Failure / unreachable counters and the "problem" escalation flag
# ---------------------------------------------------------------------------


def test_consecutive_failures_increments_on_failed_and_resets_on_success():
    device = _FakeDevice(mode="professional", ensure_mode_result=None)
    guardian = ScheduleGuardian(device, expected_mode="auto", alert_after_failures=5)

    _run(guardian.async_check())
    _run(guardian.async_check())
    assert guardian.consecutive_failures == 2

    device.ensure_mode_result = "__match__"
    _run(guardian.async_check())
    assert guardian.consecutive_failures == 0


def test_consecutive_unreachable_increments_and_resets_on_recovery():
    device = _FakeDevice(read_state_ok=False)
    guardian = ScheduleGuardian(device, expected_mode="auto")

    _run(guardian.async_check())
    _run(guardian.async_check())
    assert guardian.consecutive_unreachable == 2

    device.read_state_ok = True
    device.mode = "automatic"
    _run(guardian.async_check())
    assert guardian.consecutive_unreachable == 0


def test_problem_flag_sets_after_alert_after_failures_consecutive_failures():
    device = _FakeDevice(mode="professional", ensure_mode_result=None)
    guardian = ScheduleGuardian(device, expected_mode="auto", alert_after_failures=3)

    for _ in range(2):
        _run(guardian.async_check())
    assert guardian.problem is False

    _run(guardian.async_check())
    assert guardian.consecutive_failures == 3
    assert guardian.problem is True


def test_problem_flag_sets_after_more_than_three_consecutive_unreachable_checks():
    device = _FakeDevice(read_state_ok=False)
    guardian = ScheduleGuardian(device, expected_mode="auto", alert_after_failures=99)

    for _ in range(3):
        _run(guardian.async_check())
    assert guardian.problem is False

    _run(guardian.async_check())
    assert guardian.consecutive_unreachable == 4
    assert guardian.problem is True


def test_problem_flag_clears_once_the_fixture_recovers():
    device = _FakeDevice(mode="professional", ensure_mode_result=None)
    guardian = ScheduleGuardian(device, expected_mode="auto", alert_after_failures=2)

    _run(guardian.async_check())
    _run(guardian.async_check())
    assert guardian.problem is True

    device.ensure_mode_result = "__match__"
    _run(guardian.async_check())
    assert guardian.problem is False


# ---------------------------------------------------------------------------
# Manual override: detection, hold, timer expiry, and manual end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("expected_mode,target", [("auto", "automatic"), ("pro", "professional")])
def test_manual_write_while_scheduled_starts_an_override_without_touching_mode(expected_mode, target):
    clock = _Clock(1_000.0)
    device = _FakeDevice(mode="manual")
    guardian = ScheduleGuardian(device, expected_mode=expected_mode, override_return_min=60, now_fn=clock)

    outcome = _run(guardian.async_check())

    assert outcome == "ok"
    assert device.ensure_mode_calls == []
    assert device.mode == "manual"
    assert guardian.override_active is True
    assert guardian.override_until == pytest.approx(1_000.0 + 60 * 60)


def test_override_holds_as_ok_until_the_timer_expires():
    clock = _Clock(1_000.0)
    device = _FakeDevice(mode="manual")
    guardian = ScheduleGuardian(device, expected_mode="auto", override_return_min=60, now_fn=clock)

    _run(guardian.async_check())  # starts the override
    clock.advance(60 * 60 - 1)
    outcome = _run(guardian.async_check())

    assert outcome == "ok"
    assert guardian.override_active is True
    assert device.ensure_mode_calls == []
    assert device.mode == "manual"


def test_override_restores_expected_mode_once_the_timer_expires():
    clock = _Clock(1_000.0)
    device = _FakeDevice(mode="manual")
    guardian = ScheduleGuardian(device, expected_mode="auto", override_return_min=60, now_fn=clock)

    _run(guardian.async_check())  # starts the override
    clock.advance(60 * 60)
    outcome = _run(guardian.async_check())

    assert outcome == "corrected"
    assert device.ensure_mode_calls == ["automatic"]
    assert device.mode == "automatic"
    assert guardian.override_active is False
    assert guardian.override_until is None


def test_override_never_expires_when_override_return_min_is_zero():
    clock = _Clock(1_000.0)
    device = _FakeDevice(mode="manual")
    guardian = ScheduleGuardian(device, expected_mode="auto", override_return_min=0, now_fn=clock)

    _run(guardian.async_check())
    assert guardian.override_until is None

    clock.advance(365 * 24 * 60 * 60)
    outcome = _run(guardian.async_check())

    assert outcome == "ok"
    assert device.ensure_mode_calls == []
    assert device.mode == "manual"
    assert guardian.override_active is True


def test_async_end_override_immediately_restores_mode_regardless_of_timer():
    clock = _Clock(1_000.0)
    device = _FakeDevice(mode="manual")
    guardian = ScheduleGuardian(device, expected_mode="auto", override_return_min=60, now_fn=clock)

    _run(guardian.async_check())  # starts the override, timer far from expiry
    assert guardian.override_active is True

    outcome = _run(guardian.async_end_override())

    assert outcome == "corrected"
    assert device.ensure_mode_calls == ["automatic"]
    assert device.mode == "automatic"
    assert guardian.override_active is False
    assert guardian.override_until is None


def test_async_end_override_is_a_noop_when_no_override_is_active():
    device = _FakeDevice(mode="automatic")
    guardian = ScheduleGuardian(device, expected_mode="auto")

    outcome = _run(guardian.async_end_override())

    assert device.ensure_mode_calls == []
    assert outcome in ("ok", None)


def test_async_end_override_reports_failed_when_the_write_does_not_confirm():
    device = _FakeDevice(mode="manual", ensure_mode_result=None)
    guardian = ScheduleGuardian(device, expected_mode="auto", override_return_min=60)

    _run(guardian.async_check())
    outcome = _run(guardian.async_end_override())

    assert outcome == "failed"
    # A failed restore must not silently clear the override — the fixture is
    # still not on the expected schedule.
    assert guardian.override_active is True


# ---------------------------------------------------------------------------
# Listener notification
# ---------------------------------------------------------------------------


def test_add_listener_fires_after_every_check_and_can_be_unregistered():
    device = _FakeDevice(mode="automatic")
    guardian = ScheduleGuardian(device, expected_mode="auto")
    calls = []
    unsubscribe = guardian.add_listener(lambda: calls.append(guardian.status))

    _run(guardian.async_check())
    assert calls == ["ok"]

    unsubscribe()
    _run(guardian.async_check())
    assert calls == ["ok"]


def test_add_listener_fires_on_override_start_and_end():
    device = _FakeDevice(mode="manual")
    guardian = ScheduleGuardian(device, expected_mode="auto", override_return_min=60)
    calls = []
    guardian.add_listener(lambda: calls.append((guardian.override_active, guardian.override_until)))

    _run(guardian.async_check())
    assert calls[-1][0] is True

    _run(guardian.async_end_override())
    assert calls[-1] == (False, None)


# ---------------------------------------------------------------------------
# HA runner: the interval callback must be dispatched on the event loop
# ---------------------------------------------------------------------------


def test_runner_registers_interval_callback_marked_for_loop_dispatch(monkeypatch):
    """async_track_time_interval runs a plain function in an executor thread,
    where hass.async_create_task is unsafe (HA logged this every interval on
    the live install). The registered callable must carry HA's loop-dispatch
    marker so the check is created on the event loop."""
    from unittest.mock import MagicMock

    from custom_components.fluvalble.core import guardian as guardian_module

    captured = {}

    def fake_track(hass, action, interval):
        captured["action"] = action
        captured["interval"] = interval
        return lambda: None

    monkeypatch.setattr(guardian_module, "async_track_time_interval", fake_track)

    device = _FakeDevice(mode="automatic")
    device.register_connection_listener = lambda cb: (lambda: None)
    guardian = ScheduleGuardian(device, expected_mode="auto", check_interval_min=7)
    hass = MagicMock()
    # hass is a mock, so close each coroutine it is handed instead of leaking
    # a never-awaited ScheduleGuardian.async_check().
    hass.async_create_task.side_effect = lambda coro: coro.close()
    unsub = guardian.start_runner(hass)

    assert getattr(captured["action"], "_hass_callback", False) is True
    assert captured["interval"].total_seconds() == 7 * 60
    # The initial check and any tick both create the task on the loop side.
    hass.async_create_task.assert_called()
    unsub()


# ---------------------------------------------------------------------------
# Bounded checks: a wedged device step must not hang the guardian forever
# ---------------------------------------------------------------------------


def test_read_state_timeout_reports_unreachable_resets_connection_and_frees_lock(_short_check_timeouts):
    _run(_async_test_read_state_timeout_reports_unreachable_resets_connection_and_frees_lock())


async def _async_test_read_state_timeout_reports_unreachable_resets_connection_and_frees_lock():
    """The exact live failure mode: a GATT read that never resolves.

    Bounded, this must land as "unreachable" - not hang - reset the wedged
    connection so the next attempt reconnects fresh, and leave `_check_lock`
    free so a subsequent check (or `async_end_override`) does not queue
    behind it forever.
    """
    device = _FakeDevice(mode="automatic", hang_on_read_state=True)
    guardian = ScheduleGuardian(device, expected_mode="auto")

    outcome = await asyncio.wait_for(guardian.async_check(), timeout=5)

    assert outcome == "unreachable"
    assert guardian.status == "unreachable"
    assert device.reset_connection_calls == 1
    assert not guardian._check_lock.locked()


def test_ensure_mode_timeout_reports_failed_and_resets_connection(_short_check_timeouts):
    _run(_async_test_ensure_mode_timeout_reports_failed_and_resets_connection())


async def _async_test_ensure_mode_timeout_reports_failed_and_resets_connection():
    device = _FakeDevice(mode="professional", hang_on_ensure_mode=True)
    guardian = ScheduleGuardian(device, expected_mode="manual")

    outcome = await asyncio.wait_for(guardian.async_check(), timeout=5)

    assert outcome == "failed"
    assert device.reset_connection_calls == 1
    assert not guardian._check_lock.locked()


def test_schedule_repush_timeout_reports_failed_and_resets_connection(_short_check_timeouts):
    _run(_async_test_schedule_repush_timeout_reports_failed_and_resets_connection())


async def _async_test_schedule_repush_timeout_reports_failed_and_resets_connection():
    device = _FakeDevice(mode="automatic", auto_schedule={"sunrise": 480}, hang_on_push=True)
    guardian = ScheduleGuardian(device, expected_mode="auto", expected_schedule={"sunrise": 420})

    outcome = await asyncio.wait_for(guardian.async_check(), timeout=5)

    assert outcome == "failed"
    assert device.reset_connection_calls == 1
    assert not guardian._check_lock.locked()


def test_overall_check_timeout_is_a_hard_cap_even_when_a_step_deadline_is_generous(monkeypatch):
    """CHECK_OVERALL_TIMEOUT must bound the whole check even if a step's own
    (here deliberately huge) deadline would never have fired on its own."""
    monkeypatch.setattr(guardian_module, "CHECK_READ_STATE_TIMEOUT", 3600)
    monkeypatch.setattr(guardian_module, "CHECK_OVERALL_TIMEOUT", 0.05)
    _run(_async_test_overall_check_timeout_is_a_hard_cap())


async def _async_test_overall_check_timeout_is_a_hard_cap():
    device = _FakeDevice(mode="automatic", hang_on_read_state=True)
    guardian = ScheduleGuardian(device, expected_mode="auto")

    outcome = await asyncio.wait_for(guardian.async_check(), timeout=5)

    assert outcome == "unreachable"
    assert device.reset_connection_calls == 1
    assert not guardian._check_lock.locked()


def test_interval_check_is_skipped_while_one_is_already_running(monkeypatch):
    """A single wedged check must not let the HA interval timer pile up more
    checks behind the same lock - the exact shape a live install hung in
    (three queued ScheduleGuardian.async_check() calls)."""
    captured = {}

    def fake_track(hass, action, interval):
        captured["action"] = action
        return lambda: None

    monkeypatch.setattr(guardian_module, "async_track_time_interval", fake_track)
    _run(_async_test_interval_check_is_skipped_while_one_is_already_running(captured))


async def _async_test_interval_check_is_skipped_while_one_is_already_running(captured):
    from unittest.mock import MagicMock

    device = _FakeDevice(mode="automatic")
    device.register_connection_listener = lambda cb: (lambda: None)
    guardian = ScheduleGuardian(device, expected_mode="auto", check_interval_min=7)
    notified = []
    guardian.add_listener(lambda: notified.append(True))

    hass = MagicMock()
    created_tasks = []
    hass.async_create_task.side_effect = lambda coro: created_tasks.append(coro) or coro.close()
    guardian.start_runner(hass)

    assert len(created_tasks) == 1  # only start_runner's own initial trigger

    await guardian._check_lock.acquire()
    try:
        captured["action"]()  # the interval firing while a check is "in flight"
    finally:
        guardian._check_lock.release()

    assert len(created_tasks) == 1  # the interval trigger above was skipped, not queued
    assert notified == [True]  # but listeners still got a chance to refresh (e.g. staleness)
