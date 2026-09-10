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

The moment the link went down is kept per BLE address in
``hass.data[DOMAIN]``, not on the watcher, because the watcher is rebuilt
with the config entry and the entry is rebuilt for as long as the link is
down. Measured live on 2026-09-09 during a deliberate 21-minute power cut
of the aquarium light, with the first version of this file keeping the
timestamp on the watcher::

    22:19:04  entry setup            -> countdown starts
    22:24:37  link drop              -> countdown (re)armed
    22:35:00  autoheal reload        -> fresh watcher, countdown restarts at zero
    22:40:00  autoheal reload        -> fresh watcher, countdown restarts at zero

``automation.ble_proxy_autoheal`` calls ``homeassistant.reload_config_entry``
on any device whose link is down, every 5 minutes, so a 15-minute window
owned by anything the reload throws away is unreachable by construction.
HA's own last-advertisement timestamp did not cover it either: the fixture
had not been heard since HA restarted (``not in BLE cache, will wait for
advertisement``), which is exactly the case that hint contributes zero for.
A watcher therefore only ever *records* a first-drop time it finds absent,
never overwrites one, and arms its timer for whatever is left of the
window rather than for a fresh one.
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

# hass.data[DOMAIN] key of the {MAC: monotonic()} map recording when each
# fixture's link was first seen down. Process-scoped on purpose - see the
# module docstring for the reload that made anything entry-scoped useless.
# async_unload_entry only pops the entry's own key, so this survives it.
LINK_DOWN_SINCE = "_link_down_since"


def _link_down_since(hass: HomeAssistant) -> dict[str, float]:
    """Return the process-scoped first-drop clock, creating it on first use."""
    return hass.data.setdefault(DOMAIN, {}).setdefault(LINK_DOWN_SINCE, {})


@callback
def async_forget_link_outage(hass: HomeAssistant, mac: str) -> None:
    """Drop the recorded first-drop time for one fixture.

    Called when the link is healthy again and when the config entry is
    removed for good, so a fixture added back later starts a fresh window.
    """
    hass.data.get(DOMAIN, {}).get(LINK_DOWN_SINCE, {}).pop(mac.upper(), None)


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
    """Return the ESPHome node name of the scanner carrying this fixture's link.

    `None` whenever there is nothing to name: no link at all, or a link
    through a local adapter, which keeps no slot accounting and so reports
    the bare CONNECTION_STATE_CONNECTED. Reads the route Device recorded
    when GATT setup completed (refreshed on every reconnect), then
    normalises it to the bare node - e.g. `plant-room-bluetooth-proxy` -
    because this name exists to be turned into
    `esphome.<node>_restart_proxy` and to be remembered for that purpose.

    habluetooth builds a remote scanner's ``name`` as "<adapter> (<source>)"
    - verified live on 2026-09-09 via `bluetooth/subscribe_scanner_details`,
    where every proxy reported ``name="plant-room-bluetooth-proxy
    (54:32:04:3E:F3:72)"`` beside ``adapter="plant-room-bluetooth-proxy"``
    and the registered action was
    ``esphome.plant_room_bluetooth_proxy_restart_proxy``. Slugifying the
    display name looks up an action nobody registered and the wizard's
    proxy rung silently disappears. ``adapter`` is preferred because it is
    the node name ESPHome registered with, so it survives the proxy's HA
    device being renamed or moved between areas
    (`downstairs-bluetooth-proxy` kept its node name after its HA device
    moved to the Tool Room); the split covers a scanner that exposes only
    its display name. Neither yields a MAC or parentheses.
    """
    if device is None or not device.connected:
        return None
    name = device.connection_state()
    if name in (CONNECTION_STATE_DISCONNECTED, CONNECTION_STATE_CONNECTED):
        return None
    scanner = (
        bluetooth.async_scanner_by_source(device.hass, device.scanner_source)
        if device.hass is not None and device.scanner_source
        else None
    )
    adapter = getattr(scanner, "adapter", None)
    if adapter:
        return adapter
    return name.split(" (")[0]


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

    The watcher owns the timer but not the clock: the first-drop time it
    counts from lives in hass.data (LINK_DOWN_SINCE) and outlives it, so
    the watcher a reload builds picks up the remaining part of the window
    instead of a new one.

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

        Cancelling the timer is likewise safe only because the clock it was
        measuring is not cancelled with it: the next watcher's start()
        re-arms for the time that is left, or raises at once if none is.
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
            self._cancel_countdown()
            async_forget_link_outage(self.hass, self.mac)
            self._remember_holding_proxy()
            self._set_issue(raised=False)
            return

        # Record only if absent: a watcher rebuilt mid-outage must inherit
        # the first drop, never restamp it.
        _link_down_since(self.hass).setdefault(self.mac, monotonic())
        remaining = UNREACHABLE_AFTER_SECONDS - self._outage_seconds()
        if remaining <= 0:
            self._cancel_countdown()
            self._set_issue(raised=True)
            return
        # Not long enough yet - and a dead link produces no further
        # notifications, so the deadline has to come from a timer.
        self._arm_countdown(remaining)

    def _outage_seconds(self) -> float:
        """Return how long this fixture is known to have been unreachable.

        Two sources, whichever is more damning. The process-scoped
        first-drop clock is the reliable one: it is recorded once per
        outage and survives the entry reloads the autoheal automation
        issues while the link is down. Home Assistant's Bluetooth stack
        additionally keeps the monotonic timestamp of the last connectable
        advertisement it heard from this address, which can predate the
        first drop this process observed (an outage that began before HA
        or this integration started watching). Nothing heard is not
        evidence of anything - the state right after an HA restart - so
        that hint contributes zero and the clock decides alone.
        """
        down_since = _link_down_since(self.hass).get(self.mac)
        watched = 0.0 if down_since is None else monotonic() - down_since
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

    def _arm_countdown(self, remaining: float) -> None:
        """Schedule the unreachable deadline, without stacking timers.

        Idempotent: an armed timer was computed from the same first-drop
        time and both grow at the same rate, so it already carries this
        deadline. `remaining` is the rest of the window, not a full one -
        that is the difference between this and the version measured
        never to fire (module docstring).
        """
        if self._countdown_unsub is not None:
            return
        self._countdown_unsub = async_call_later(self.hass, remaining, self._on_countdown_elapsed)

    def _cancel_countdown(self) -> None:
        """Cancel the pending unreachable deadline."""
        unsub, self._countdown_unsub = self._countdown_unsub, None
        if unsub is not None:
            unsub()

    @callback
    def _on_countdown_elapsed(self, _now: Any) -> None:
        """Re-read live state now that the deadline has passed.

        Nothing is concluded from the timer having fired: the link may be
        back, or the clock may have been cleared meanwhile, and reconcile
        reads both. When the fixture is still down the process clock now
        says the window is over, so this raises through the same path a
        setup-time reconcile of a long-dead fixture takes.
        """
        self._countdown_unsub = None
        self.reconcile()

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
