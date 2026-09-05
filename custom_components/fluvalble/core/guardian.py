"""Schedule and mode supervision for one Fluval BLE device.

The on-device schedule (Auto/Professional stored in the fixture) is the
safety net: the light keeps running it with no Home Assistant involvement at
all. ``ScheduleGuardian`` is the active supervisor layered on top - it makes
sure the fixture is actually in the mode the user expects, that its clock is
accurate enough for that schedule to fire at the right time, and that the
schedule stored in the fixture still matches the one the user last
programmed through this integration. It corrects drift automatically and
raises a Home Assistant repair when it cannot.

``ScheduleGuardian`` itself is a pure, ``hass``-free state machine: it is
constructed with a device object exposing a small async duck-typed
interface (``async_sync_clock``, ``async_read_state``, ``async_ensure_mode``,
``async_set_native_auto_schedule``, ``async_set_native_pro_schedule``) and
records outcomes as plain attributes. Nothing here imports or requires a
running Home Assistant core, which makes the whole decision engine testable
with a fake device and a fake clock. ``start_runner`` / ``async_setup_guardian``
are the thin Home Assistant-facing half: they wire the engine to
``async_track_time_interval`` and the device's connection listener.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from . import (
    CONF_ALERT_AFTER_FAILURES,
    CONF_CHECK_INTERVAL_MIN,
    CONF_EXPECTED_MODE,
    CONF_EXPECTED_SCHEDULE,
    CONF_OVERRIDE_RETURN_MIN,
    DEFAULT_ALERT_AFTER_FAILURES,
    DEFAULT_CHECK_INTERVAL_MIN,
    DEFAULT_EXPECTED_MODE,
    DEFAULT_OVERRIDE_RETURN_MIN,
    EXPECTED_MODE_MANUAL,
    EXPECTED_MODE_UNSUPERVISED,
)

_LOGGER = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_CORRECTED = "corrected"
STATUS_FAILED = "failed"
STATUS_UNREACHABLE = "unreachable"
STATUS_PAUSED = "paused"
GUARDIAN_STATUSES = [STATUS_OK, STATUS_CORRECTED, STATUS_FAILED, STATUS_UNREACHABLE, STATUS_PAUSED]

# Guardian expected_mode option values that are actively enforced on the
# fixture, mapped to the Device/MODES vocabulary ("manual"/"automatic"/
# "professional"). "manual" and "unsupervised" have no schedule to compare.
_SUPERVISED_DEVICE_MODE = {
    "auto": "automatic",
    "pro": "professional",
}

# A fixture that misses this many consecutive checks is flagged even if the
# user configured a looser alert_after_failures - unreachable is a distinct,
# always-on safety net separate from the configurable correction-failure count.
UNREACHABLE_ALERT_THRESHOLD = 3


def issue_id_for_mac(mac: str) -> str:
    """Return the repairs issue_id for one device's schedule-problem alert, by MAC.

    Exposed separately from issue_id_for() so callers that only have the
    entry's stored MAC (e.g. async_remove_entry, after the Device may already
    be gone) can compute the exact same id without constructing a device.
    """
    return f"{mac.upper().replace(':', '')}_schedule_problem"


def issue_id_for(device: Any) -> str:
    """Return the repairs issue_id for one device's schedule-problem alert."""
    return issue_id_for_mac(device.mac)


def json_safe(value: Any) -> Any:
    """Recursively normalize a value the way a JSON round-trip would.

    ConfigEntry options persist through Home Assistant's JSON-backed store,
    which turns tuples into lists. A schedule captured fresh from device
    readback (which may still contain tuples) must compare equal to the same
    schedule after it has been saved to options and reloaded - otherwise the
    guardian would see permanent "drift" and repush the schedule every check.
    """
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    return value


class ScheduleGuardian:
    """Supervise one Fluval fixture's mode, schedule, and clock.

    Pure decision engine: every device interaction goes through the small
    async duck-type documented on the module, and every input needed to
    decide what to do (the current time) is injectable via ``now_fn`` so the
    whole thing is testable without real sleeps or a running Home Assistant
    core.
    """

    def __init__(
        self,
        device: Any,
        *,
        expected_mode: str = DEFAULT_EXPECTED_MODE,
        check_interval_min: int = DEFAULT_CHECK_INTERVAL_MIN,
        override_return_min: int = DEFAULT_OVERRIDE_RETURN_MIN,
        alert_after_failures: int = DEFAULT_ALERT_AFTER_FAILURES,
        expected_schedule: dict | list | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        """Initialize the guardian for one device."""
        self.device = device
        self.expected_mode = expected_mode
        self.check_interval_min = check_interval_min
        self.override_return_min = override_return_min
        self.alert_after_failures = alert_after_failures
        self.expected_schedule = expected_schedule
        self._now = now_fn

        self.status: str = STATUS_PAUSED if expected_mode == EXPECTED_MODE_UNSUPERVISED else STATUS_OK
        self.last_check_at: float | None = None
        self.corrections: int = 0
        self.consecutive_failures: int = 0
        self.consecutive_unreachable: int = 0
        self.override_active: bool = False
        self.override_until: float | None = None

        self._listeners: list[Callable[[], None]] = []
        self._check_lock = asyncio.Lock()

    @property
    def problem(self) -> bool:
        """Return whether the guardian currently needs the user's attention."""
        return (
            self.consecutive_failures >= self.alert_after_failures
            or self.consecutive_unreachable > UNREACHABLE_ALERT_THRESHOLD
        )

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback fired after every check and override transition."""
        self._listeners.append(callback)

        def _unsub() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _unsub

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - one bad listener must not break others
                _LOGGER.exception("Fluval guardian listener failed")

    def set_expected_schedule(self, schedule: dict | list | None) -> None:
        """Update the schedule this guardian enforces on future checks."""
        self.expected_schedule = schedule

    # ------------------------------------------------------------------
    # Pure check/override engine
    # ------------------------------------------------------------------

    async def async_check(self) -> str:
        """Run one full guardian check cycle and return its outcome status."""
        async with self._check_lock:
            return await self._async_check_locked()

    async def _async_check_locked(self) -> str:
        """Sync clock, read state, correct mode/schedule drift, report outcome."""
        self.last_check_at = self._now()

        if not getattr(self.device, "hold_connection", True):
            # The connection switch is deliberately off - stay off and don't
            # even attempt BLE traffic. This is an intentional pause, not a
            # comms failure, so it must never be confused with "unreachable".
            return self._finish(STATUS_PAUSED)

        try:
            await self.device.async_sync_clock()
        except Exception:  # noqa: BLE001 - best effort; read_state below reports reachability
            _LOGGER.debug("Fluval guardian clock sync failed", exc_info=True)

        state = await self._async_read_state_quiet()

        if self.expected_mode == EXPECTED_MODE_UNSUPERVISED:
            # Still attempted clock sync/read above for visibility, but an
            # unreachable fixture while unsupervised is not a "problem" -
            # supervision was explicitly turned off.
            return self._finish(STATUS_PAUSED)

        if not state:
            return self._finish(STATUS_UNREACHABLE)

        if self.expected_mode != EXPECTED_MODE_MANUAL and self.expected_mode not in _SUPERVISED_DEVICE_MODE:
            _LOGGER.warning("Unknown Fluval guardian expected_mode %r; pausing supervision", self.expected_mode)
            return self._finish(STATUS_PAUSED)

        target_mode = _SUPERVISED_DEVICE_MODE.get(self.expected_mode)  # None means expected_mode == "manual"
        corrected = False
        failed = False
        current_mode = getattr(state, "mode", None)

        if target_mode is None:
            self._clear_override()
            if current_mode != "manual":
                ok = await self._try_ensure_mode("manual")
                corrected, failed = ok, not ok
        elif current_mode == target_mode:
            self._clear_override()
        elif current_mode == "manual":
            if self._override_grace_active():
                return self._finish(STATUS_OK)
            ok = await self._try_ensure_mode(target_mode)
            corrected, failed = ok, not ok
            self._clear_override()
            if ok:
                state = await self._async_read_state_quiet() or state
        else:
            # Neither the target scheduled mode nor manual - plain drift.
            ok = await self._try_ensure_mode(target_mode)
            corrected, failed = ok, not ok
            self._clear_override()
            if ok:
                state = await self._async_read_state_quiet() or state

        if target_mode is not None and not failed and getattr(state, "mode", None) == target_mode:
            schedule_ok, schedule_attempted = await self._async_reconcile_schedule(target_mode, state)
            if schedule_attempted:
                corrected = corrected or schedule_ok
                failed = failed or not schedule_ok

        if failed:
            return self._finish(STATUS_FAILED)
        if corrected:
            return self._finish(STATUS_CORRECTED)
        return self._finish(STATUS_OK)

    async def async_end_override(self) -> str | None:
        """End an active manual override immediately, restoring expected mode.

        Used by the "return to schedule" button/service. A no-op (returns
        ``None``) if no override is currently active. Returns the resulting
        status string ("corrected"/"failed") otherwise.
        """
        async with self._check_lock:
            if not self.override_active:
                return None

            target_mode = _SUPERVISED_DEVICE_MODE.get(self.expected_mode)
            if target_mode is None:
                self._clear_override()
                self._notify()
                return self.status

            ok = await self._try_ensure_mode(target_mode)
            if ok:
                self._clear_override()
                self.consecutive_failures = 0
                self.corrections += 1
                self.status = STATUS_CORRECTED
            else:
                # A failed restore must not silently clear the override - the
                # fixture is still not on the expected schedule.
                self.consecutive_failures += 1
                self.status = STATUS_FAILED
            self._notify()
            return self.status

    def _override_grace_active(self) -> bool:
        """Arm an override window on first observation; report if grace still applies."""
        now = self._now()
        if not self.override_active:
            self.override_active = True
            self.override_until = None if self.override_return_min <= 0 else now + (self.override_return_min * 60)
            return True
        if self.override_until is None:
            return True  # override_return_min == 0: the override never auto-expires
        return now < self.override_until

    def _clear_override(self) -> None:
        self.override_active = False
        self.override_until = None

    async def _try_ensure_mode(self, mode: str) -> bool:
        try:
            confirmed = await self.device.async_ensure_mode(mode)
        except Exception:  # noqa: BLE001 - transport failure means the correction failed
            _LOGGER.warning("Fluval guardian could not set mode to %s", mode, exc_info=True)
            return False
        if confirmed is None:
            return False
        if isinstance(confirmed, str):
            return confirmed == mode
        return bool(confirmed)

    async def _async_read_state_quiet(self) -> Any:
        try:
            return await self.device.async_read_state()
        except Exception:  # noqa: BLE001 - unreachable is a normal, expected outcome
            _LOGGER.debug("Fluval guardian could not read device state", exc_info=True)
            return None

    async def _async_reconcile_schedule(self, target_mode: str, state: Any) -> tuple[bool, bool]:
        """Compare the enforced schedule against readback; repush on mismatch.

        Returns ``(success, attempted)``; ``attempted`` is False when there is
        nothing to enforce yet (no schedule has been programmed through this
        integration) or the readback already matches.
        """
        if self.expected_schedule is None:
            return True, False

        current = getattr(state, "auto_schedule", None) if target_mode == "automatic" else getattr(state, "pro_schedule", None)
        if json_safe(current) == json_safe(self.expected_schedule):
            return True, False

        try:
            if target_mode == "automatic":
                ok = await self.device.async_set_native_auto_schedule(self.expected_schedule)
            else:
                ok = await self.device.async_set_native_pro_schedule(self.expected_schedule)
        except Exception:  # noqa: BLE001 - transport failure means the correction failed
            _LOGGER.warning("Fluval guardian could not repush the expected schedule", exc_info=True)
            return False, True
        return bool(ok), True

    def _finish(self, status: str) -> str:
        if status == STATUS_FAILED:
            self.consecutive_failures += 1
            self.consecutive_unreachable = 0
        elif status == STATUS_UNREACHABLE:
            self.consecutive_unreachable += 1
            self.consecutive_failures = 0
        else:
            self.consecutive_failures = 0
            self.consecutive_unreachable = 0
        if status == STATUS_CORRECTED:
            self.corrections += 1
        self.status = status
        self._notify()
        return status

    # ------------------------------------------------------------------
    # Home Assistant-facing runner
    # ------------------------------------------------------------------

    def start_runner(self, hass: HomeAssistant) -> Callable[[], None]:
        """Wire this guardian to Home Assistant's clock and the connection state.

        Schedules a check on every connect and every ``check_interval_min``,
        and returns an unsubscribe callable that tears both down. The
        schedule-problem repair is synced by FluvalScheduleProblemBinarySensor
        (it needs a live hass reference, which Device already carries; the
        pure engine and this runner stay hass-free apart from this method).
        """
        unsubs: list[Callable[[], None]] = []

        def _run_check_soon() -> None:
            hass.async_create_task(self.async_check())

        unsubs.append(
            async_track_time_interval(
                hass,
                lambda _now: _run_check_soon(),
                timedelta(minutes=max(1, self.check_interval_min)),
            )
        )

        def _on_connection_changed(connected: bool) -> None:
            if connected:
                _run_check_soon()

        unsubs.append(self.device.register_connection_listener(_on_connection_changed))

        # The device may already be connected (or connect before the first
        # interval elapses) - run an initial check rather than waiting.
        _run_check_soon()

        def _unsub() -> None:
            for unsub in unsubs:
                unsub()

        return _unsub


def async_setup_guardian(hass: HomeAssistant, entry: Any, device: Any) -> ScheduleGuardian:
    """Build, start, and return a ScheduleGuardian for one config entry's device."""
    options = entry.options
    guardian = ScheduleGuardian(
        device,
        expected_mode=options.get(CONF_EXPECTED_MODE, DEFAULT_EXPECTED_MODE),
        check_interval_min=options.get(CONF_CHECK_INTERVAL_MIN, DEFAULT_CHECK_INTERVAL_MIN),
        override_return_min=options.get(CONF_OVERRIDE_RETURN_MIN, DEFAULT_OVERRIDE_RETURN_MIN),
        alert_after_failures=options.get(CONF_ALERT_AFTER_FAILURES, DEFAULT_ALERT_AFTER_FAILURES),
        expected_schedule=options.get(CONF_EXPECTED_SCHEDULE),
    )
    unsub_runner = guardian.start_runner(hass)
    entry.async_on_unload(unsub_runner)
    # Deliberately no per-unload issue_registry cleanup here: async_on_unload
    # fires on every reload (including a routine options change), not just
    # permanent removal. Deleting the issue there would clear a still-valid
    # "problem" alert on reload, before the freshly built guardian has run
    # enough checks to know the fixture is still broken. Permanent-removal
    # cleanup lives in async_remove_entry() in __init__.py instead.
    return guardian
