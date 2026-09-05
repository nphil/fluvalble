"""Pure state-machine tests for ScheduleGuardian.

ScheduleGuardian never touches hass directly (that lives in the
``async_setup_guardian`` factory) — it is constructed with a duck-typed
device double exposing four async methods:

    await device.async_sync_clock()
    state = await device.async_read_state()      # .mode / .auto_schedule / .pro_schedule
    confirmed = await device.async_ensure_mode(mode)
    ok = await device.async_set_native_auto_schedule(schedule)
    ok = await device.async_set_native_pro_schedule(points)

and an injectable ``now_fn`` (epoch seconds) so override-timer expiry is
deterministic without real sleeps.
"""

import asyncio
from types import SimpleNamespace

import pytest

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
    ):
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

        self.sync_clock_calls = 0
        self.read_state_calls = 0
        self.ensure_mode_calls: list[str] = []
        self.auto_schedule_pushes: list[object] = []
        self.pro_schedule_pushes: list[object] = []

    async def async_sync_clock(self):
        self.sync_clock_calls += 1
        return True

    async def async_read_state(self):
        self.read_state_calls += 1
        if not self.read_state_ok:
            raise RuntimeError("fixture unreachable")
        return SimpleNamespace(mode=self.mode, auto_schedule=self.auto_schedule, pro_schedule=self.pro_schedule)

    async def async_ensure_mode(self, mode):
        self.ensure_mode_calls.append(mode)
        if self.raise_on_ensure_mode:
            raise RuntimeError("write failed")
        confirmed = mode if self.ensure_mode_result == "__match__" else self.ensure_mode_result
        if confirmed:
            self.mode = confirmed
        return confirmed

    async def async_set_native_auto_schedule(self, schedule):
        self.auto_schedule_pushes.append(schedule)
        if self.raise_on_push:
            raise RuntimeError("push failed")
        if self.push_schedule_ok:
            self.auto_schedule = schedule
        return self.push_schedule_ok

    async def async_set_native_pro_schedule(self, points):
        self.pro_schedule_pushes.append(points)
        if self.raise_on_push:
            raise RuntimeError("push failed")
        if self.push_schedule_ok:
            self.pro_schedule = points
        return self.push_schedule_ok


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


def test_guardian_check_is_unaffected_by_hold_connection_being_false():
    """v1.0.1: hold_connection no longer gates the guardian - only
    expected_mode == "unsupervised" does. A device reporting connect-on-
    demand hold_connection=False (the new default) must still get a real
    check, not a silent "paused" - the guardian's own connect-on-demand
    calls (async_sync_clock/async_read_state) are how supervision keeps
    working without a permanently held link."""
    device = _FakeDevice(mode="automatic")
    device.hold_connection = False
    guardian = ScheduleGuardian(device, expected_mode="auto")

    outcome = _run(guardian.async_check())

    assert outcome == "ok"
    assert guardian.status == "ok"
    assert device.sync_clock_calls == 1
    assert device.read_state_calls == 1


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
