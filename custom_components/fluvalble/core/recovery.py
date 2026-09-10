"""BLE link supervision behind this integration's repairs.

Two concerns, both feeding Home Assistant's repairs panel:

``reconcile_issue`` is the only way this integration is allowed to touch a
repair it owns. It compares the desired state against what the issue
registry actually holds, never against a remembered previous state, because
remembered state does not survive a config-entry reload. Live on
2026-09-09: ``44A6E570F18D_schedule_problem`` was raised at 12:22, the entry
reloaded at 12:38, the guardian cleared the condition at 13:00 - and the
repair was still open 13 hours later, because the code that would have
deleted it was gated on an in-memory "previously synced" flag that the
reload had reset to ``None``. ``async_create_issue`` and
``async_delete_issue`` are both idempotent, so reconciling unconditionally
(on every health notification, and once when the watcher/entity starts) is
both safe and the only form that converges after a reload.

``LinkWatcher`` raises ``<mac>_unreachable`` once this integration's own
GATT link has been continuously down for ``UNREACHABLE_AFTER_SECONDS``,
deletes it the moment the link returns, and writes down which proxy was
carrying the link while it was up: while a fixture is unreachable there is
no current holding scanner left to discover, so the fix flow has no proxy
to offer restarting unless it was recorded in advance.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from time import monotonic
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later

from . import CONF_LAST_HOLDING_PROXY, DOMAIN
from .device import (
    CONNECTION_STATE_CONNECTED,
    CONNECTION_STATE_DISCONNECTED,
    Device,
)

_LOGGER = logging.getLogger(__name__)

# How long the link has to stay down before the repairs panel is involved.
# The household's own healing (script.ble_heal_device, the hourly re-home)
# recovers a dropped link in well under this, so a shorter window would put
# a repair in front of the operator for outages that fix themselves.
UNREACHABLE_AFTER_SECONDS = 15 * 60

# Suffix of the unreachable repair's issue_id. Shared with repairs.py, which
# has to tell this integration's two repairs apart from the id alone.
UNREACHABLE_ISSUE_SUFFIX = "_unreachable"


def unreachable_issue_id_for_mac(mac: str) -> str:
    """Return the repairs issue_id for one device's unreachable alert, by MAC.

    Exposed separately from unreachable_issue_id_for() for the same reason
    the guardian's issue_id_for_mac() is: async_remove_entry only has the
    entry's stored MAC, the Device may already be gone.
    """
    return f"{mac.upper().replace(':', '')}{UNREACHABLE_ISSUE_SUFFIX}"


def unreachable_issue_id_for(device: Any) -> str:
    """Return the repairs issue_id for one device's unreachable alert."""
    return unreachable_issue_id_for_mac(device.mac)


def link_healthy(device: Device | None) -> bool:
    """Return whether this integration currently has a working link.

    `Device.connected` is the whole answer for the default held-link
    configuration (`active_time == 0`, `Device.hold_active`). A finite
    active window releases the GATT link on purpose after every idle
    period, so "not connected" is routine rather than a fault there, and
    the honest signal becomes the fixture still being heard on the radio -
    `Device.is_reachable`, the same REACHABLE_SECONDS window the entities
    already use.
    """
    if device is None:
        return False
    if device.connected:
        return True
    return not device.hold_active and device.is_reachable()


def holding_proxy_name(device: Device | None) -> str | None:
    """Return the name of the scanner carrying this fixture's link, if named.

    `None` whenever there is nothing to name: no link at all, or a link
    through a local adapter, which keeps no slot accounting and so reports
    the bare CONNECTION_STATE_CONNECTED. Reads the route Device recorded
    when GATT setup completed (refreshed on every reconnect), which is what
    the Connection sensor renders too - e.g. `plant-room-bluetooth-proxy`.
    """
    if device is None or not device.connected:
        return None
    name = device.connection_state()
    if name in (CONNECTION_STATE_DISCONNECTED, CONNECTION_STATE_CONNECTED):
        return None
    return name


@callback
def reconcile_issue(
    hass: HomeAssistant | None,
    issue_id: str,
    *,
    raised: bool,
    severity: ir.IssueSeverity,
    translation_key: str,
    translation_placeholders: dict[str, str],
    is_fixable: bool = True,
    data: dict[str, Any] | None = None,
) -> None:
    """Make the issue registry match reality for one of this domain's repairs.

    Reads the registry rather than remembering what was last written - see
    the module docstring for the reload that made that distinction a bug.
    The registry read is also what keeps this quiet: create/delete only run
    when the registry actually disagrees with `raised` (or is holding a
    stale rendering of the same repair, e.g. after the fixture was renamed
    or after an upgrade made the repair fixable), so it is safe to call on
    every single device notification.

    `hass` is optional because Device carries an optional hass reference: an
    entity built for a device that has none has no registry to reconcile.
    """
    if hass is None:
        return

    existing = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    if not raised:
        if existing is not None:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
        return

    if (
        existing is not None
        and existing.is_fixable == is_fixable
        and existing.severity == severity
        and existing.translation_key == translation_key
        and existing.translation_placeholders == translation_placeholders
    ):
        return

    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=is_fixable,
        severity=severity,
        translation_key=translation_key,
        translation_placeholders=translation_placeholders,
        data=data,
    )


class LinkWatcher:
    """Own the `device_unreachable` repair for one config entry.

    Scoped to the entry rather than to the Device, because the Device only
    comes into existence when the fixture is heard on the radio: a light
    that is dead or unplugged when Home Assistant starts never produces
    one, and that is exactly the outage most in need of reporting. The
    watcher therefore starts with the entry, counts down without a device,
    and picks up the device's connection notifications - which cover both
    GATT transitions and the reachability window expiring - once
    `attach` is called.

    Every notification converges three things: the countdown towards
    raising the repair, the repair itself, and the remembered holding proxy
    the fix flow needs while the fixture is unreachable.
    """

    def __init__(self, hass: HomeAssistant, entry: Any, mac: str) -> None:
        """Initialize the watcher for one entry's fixture."""
        self.hass = hass
        self.entry = entry
        self.mac = mac.upper()
        self.device: Device | None = None
        self.issue_id = unreachable_issue_id_for_mac(mac)
        self._down_since: float | None = None
        self._countdown_unsub: Callable[[], None] | None = None
        # Same reason FluvalEntity keeps one: deregistration has to hand
        # back the exact object that was registered.
        self._update_handler = self.reconcile

    def start(self) -> Callable[[], None]:
        """Reconcile once and return an unsubscribe callable."""
        self.reconcile()
        return self.stop

    def attach(self, device: Device) -> None:
        """Take over from the device-less countdown once a device exists."""
        self.device = device
        device.register_update("connection", self._update_handler)
        self.reconcile()

    def stop(self) -> None:
        """Stop watching and cancel any pending countdown.

        Deliberately leaves the repair alone: this runs on every reload
        (including a routine options change), and a fixture that is still
        unreachable across a reload must keep its repair - the fresh
        watcher's setup-time reconcile decides, with the live link state in
        front of it, whether the repair still belongs there. Permanent
        removal is cleaned up by async_remove_entry() in __init__.py.
        """
        self._cancel_countdown()
        if self.device is not None:
            self.device.deregister_update("connection", self._update_handler)

    @callback
    def reconcile(self) -> None:
        """Converge countdown, remembered proxy, and repair on the link's state.

        Idempotent and safe to call as often as the device notifies: the
        repair itself is reconciled against the issue registry, so a link
        that comes back deletes the repair whether or not this process is
        the one that raised it.
        """
        if link_healthy(self.device):
            self._down_since = None
            self._cancel_countdown()
            self._remember_holding_proxy()
            self._set_issue(raised=False)
            return

        if self._down_since is None:
            self._down_since = monotonic()
        if self._outage_seconds() >= UNREACHABLE_AFTER_SECONDS:
            self._cancel_countdown()
            self._set_issue(raised=True)
            return
        # Not long enough yet - and a dead link produces no further
        # notifications, so the deadline has to come from a timer.
        self._arm_countdown()

    def _outage_seconds(self) -> float:
        """Return how long this fixture is known to have been unreachable.

        Two sources, whichever is more damning. This watcher's own clock is
        the normal one, but it starts at zero on every reload, which would
        hand a fixture that has been dead for hours a fresh 15-minute grace
        period. Home Assistant's Bluetooth stack keeps the monotonic
        timestamp of the last connectable advertisement it heard from this
        address, which does survive a reload (same process), so a fixture
        that has not been heard from at all is known to have been gone at
        least that long. Nothing heard is not evidence of anything - the
        state right after an HA restart - so it contributes zero and the
        countdown gets to run its full window.
        """
        watched = 0.0 if self._down_since is None else monotonic() - self._down_since
        return max(watched, self._advertisement_silence())

    def _advertisement_silence(self) -> float:
        """Return seconds since HA last heard this fixture, or 0 if unknown.

        Defensive like the BLE-cache probe in async_setup_entry: this runs
        during entry setup and from a device notification, and a Bluetooth
        stack that is not up yet must cost at most this hint - never the
        entry's setup or the device's connect path.
        """
        last_service_info = getattr(bluetooth, "async_last_service_info", None)
        if last_service_info is None:  # pragma: no cover - very old HA
            return 0.0
        try:
            service_info = last_service_info(self.hass, self.mac, connectable=True)
        except Exception:  # noqa: BLE001 - no manager before bluetooth is set up
            _LOGGER.debug("Unable to read the last advertisement for %s", self.mac, exc_info=True)
            return 0.0
        heard_at = getattr(service_info, "time", None)
        if not isinstance(heard_at, int | float):
            return 0.0
        return max(0.0, monotonic() - float(heard_at))

    def _arm_countdown(self) -> None:
        """Schedule the unreachable deadline, without stacking timers."""
        if self._countdown_unsub is not None:
            return
        self._countdown_unsub = async_call_later(
            self.hass,
            UNREACHABLE_AFTER_SECONDS,
            self._on_countdown_elapsed,
        )

    def _cancel_countdown(self) -> None:
        """Cancel the pending unreachable deadline."""
        unsub, self._countdown_unsub = self._countdown_unsub, None
        if unsub is not None:
            unsub()

    @callback
    def _on_countdown_elapsed(self, _now: Any) -> None:
        """Raise the repair now that the window this timer measured is over.

        The timer is the duration evidence - deriving it again from
        `_outage_seconds` here would be circular - so the only question
        left is whether the link happens to be back, in which case the
        full reconcile takes over and clears everything instead.
        """
        self._countdown_unsub = None
        if link_healthy(self.device):
            self.reconcile()
            return
        self._set_issue(raised=True)

    @callback
    def _set_issue(self, *, raised: bool) -> None:
        """Reconcile the unreachable repair against the registry."""
        reconcile_issue(
            self.hass,
            self.issue_id,
            raised=raised,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="device_unreachable",
            translation_placeholders={
                "name": (self.device.name if self.device is not None else None) or self.entry.title or "Fluval",
                "minutes": str(UNREACHABLE_AFTER_SECONDS // 60),
                "proxy": self.entry.options.get(CONF_LAST_HOLDING_PROXY) or "an unrecorded route",
            },
            data={"entry_id": self.entry.entry_id},
        )

    @callback
    def _remember_holding_proxy(self) -> None:
        """Record the proxy carrying the link for the fix flow to restart.

        Only on a change: this runs on every connection notification, and
        an options write is a config-entry update - a flapping link must
        not turn into a stream of entry updates.
        """
        name = holding_proxy_name(self.device)
        if not name or self.entry.options.get(CONF_LAST_HOLDING_PROXY) == name:
            return
        _LOGGER.debug("Fluval %s is held by %s; remembering it for recovery", self.mac, name)
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, CONF_LAST_HOLDING_PROXY: name},
        )


def async_setup_link_watcher(hass: HomeAssistant, entry: Any, mac: str) -> LinkWatcher:
    """Build, start, and return the link watcher for one config entry.

    Started from async_setup_entry rather than from device creation, so the
    countdown is already running for a fixture that never shows up at all.
    """
    watcher = LinkWatcher(hass, entry, mac)
    entry.async_on_unload(watcher.start())
    return watcher
