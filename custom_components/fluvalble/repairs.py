"""Escalating fix flow for a Fluval fixture that stopped answering.

Both of this integration's repairs describe the same underlying trouble
from different angles - the BLE link is gone, or the guardian cannot keep
the fixture on its schedule over that link - and both have the same
remedies, in increasing order of disruption: reload the config entry,
restart the ESPHome proxy that was carrying the link, or cut mains power to
the fixture. The household's own healing (`script.ble_heal_device` and the
hourly re-home) normally gets there first; this flow exists for when it
does not, so the operator can escalate from the repairs card instead of
hunting for the right proxy's restart button.

The proxy is the interesting part: while a fixture is unreachable there is
no current holding scanner left to discover, so the link watcher writes the
last one into the entry's options while the link is up (see
`core/recovery.py`), and this flow offers to restart it only when the
matching ESPHome action actually exists on this installation.
"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, CONF_MAC
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig
from homeassistant.util import slugify

from . import DOMAIN, FluvalRuntimeData, entry_runtime_data
from .core import CONF_LAST_HOLDING_PROXY, CONF_RECOVERY_OUTLET
from .core.guardian import SCHEDULE_PROBLEM_ISSUE_SUFFIX
from .core.recovery import holding_proxy_name, link_healthy

_LOGGER = logging.getLogger(__name__)

# How long each remedy is given to bring the link back before the flow
# admits it did not work and offers the next rung. A reconnect through an
# ESPHome proxy costs 2-6s, a proxy reboot rather more, hence the gap
# between these and the power-cycle window.
SETTLE_SECONDS = 45.0
POWER_CYCLE_OFF_SECONDS = 10.0
POWER_CYCLE_SETTLE_SECONDS = 60.0
# Poll granularity while waiting. Small enough that a link returning early
# ends the wait promptly, and an await either way - a repair flow must
# never hold the event loop for its whole settle window.
SETTLE_POLL_SECONDS = 1.0

ESPHOME_DOMAIN = "esphome"
RESTART_PROXY_ACTION_SUFFIX = "_restart_proxy"

# Ladder order, cheapest rung first: recheck does nothing at all, a reload
# only rebuilds this entry, a proxy restart interrupts every other device on
# that proxy, and the power cycle cuts mains to the fixture.
MENU_RECHECK = "recheck"
MENU_RELOAD = "reload"
MENU_RESTART_PROXY = "restart_proxy"
MENU_POWER_CYCLE = "power_cycle"


class BleRecoveryFixFlow(RepairsFlow):
    """Walk the operator up the recovery ladder for one Fluval fixture."""

    def __init__(self, issue_id: str, data: dict[str, Any] | None) -> None:
        """Initialize the flow for the repair the operator pressed Fix on."""
        self.issue_id = issue_id
        self._issue_data = data or {}
        # Shown in the menu description. Empty on first entry - the menu
        # says what was tried, and "nothing yet" must not read as "None".
        self._last_result = ""

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> data_entry_flow.FlowResult:
        """Start on the cheapest remedy that fits the repair."""
        if self._entry() is None:
            return self.async_abort(reason="entry_gone")
        if self._is_schedule_issue:
            # A schedule problem means the fixture answered but drifted, so
            # re-pushing the schedule is both cheaper and more likely to
            # work than anything on the menu.
            return await self.async_step_repush_schedule()
        return await self.async_step_menu()

    async def async_step_repush_schedule(self, user_input: dict[str, Any] | None = None) -> data_entry_flow.FlowResult:
        """Re-push the expected mode and schedule through the guardian."""
        if user_input is None:
            return self.async_show_form(
                step_id="repush_schedule",
                data_schema=vol.Schema({}),
                description_placeholders=self._placeholders(),
            )

        runtime = self._runtime()
        if runtime is None:
            return self.async_abort(reason="entry_not_loaded")
        guardian = runtime.guardian
        if guardian is None:
            self._last_result = "No guardian is running for this light, so its schedule could not be re-pushed."
            return await self.async_step_menu()

        # The same two paths the Return to schedule button and the
        # guardian_check_now action use: end an active manual override,
        # then run a full check, which syncs the clock, restores the
        # expected mode, and re-pushes the expected schedule on mismatch.
        await guardian.async_end_override()
        status = await guardian.async_check()
        _LOGGER.debug("Fluval repair re-push finished with guardian status %s", status)
        return await self._async_settle(
            f"Re-pushed the expected mode and schedule (guardian reported {status}).",
            timeout=SETTLE_SECONDS,
        )

    async def async_step_menu(self, user_input: dict[str, Any] | None = None) -> data_entry_flow.FlowResult:
        """Offer the recovery ladder, cheapest rung first."""
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        options = [MENU_RECHECK, MENU_RELOAD]
        if self._proxy_action(entry) is not None:
            options.append(MENU_RESTART_PROXY)
        options.append(MENU_POWER_CYCLE)
        return self.async_show_menu(
            step_id="menu",
            menu_options=options,
            description_placeholders=self._placeholders(),
        )

    # ------------------------------------------------------------------
    # Ladder rungs
    # ------------------------------------------------------------------

    async def async_step_recheck(self, user_input: dict[str, Any] | None = None) -> data_entry_flow.FlowResult:
        """Just look again - the household's own healing may have won."""
        return await self._async_settle("Waited and re-checked the link.", timeout=SETTLE_SECONDS)

    async def async_step_reload(self, user_input: dict[str, Any] | None = None) -> data_entry_flow.FlowResult:
        """Reload the config entry, rebuilding the BLE client from scratch."""
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        await self.hass.config_entries.async_reload(entry.entry_id)
        return await self._async_settle("Reloaded the integration.", timeout=SETTLE_SECONDS)

    async def async_step_restart_proxy(self, user_input: dict[str, Any] | None = None) -> data_entry_flow.FlowResult:
        """Ask the ESPHome proxy that was carrying this link to restart."""
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        action = self._proxy_action(entry)
        if action is None:
            self._last_result = "No ESPHome restart action is available for this light's proxy any more."
            return await self.async_step_menu()

        # Resolved before the call: a reconnect during the wait can land on
        # a different proxy, and the report has to name the one that was
        # actually asked.
        proxy = self._proxy_name(entry)
        await self.hass.services.async_call(ESPHOME_DOMAIN, action, blocking=True)
        # Honest wording on purpose: the proxy firmware refuses a restart
        # while its own uptime is under 20 minutes (it logs
        # "refused: up only N s"), so a request that returns fine may have
        # rebooted nothing at all.
        return await self._async_settle(
            f"Asked {proxy} to restart - it may have refused if it booted recently.",
            timeout=SETTLE_SECONDS,
        )

    async def async_step_power_cycle(self, user_input: dict[str, Any] | None = None) -> data_entry_flow.FlowResult:
        """Cut and restore mains power to the fixture through a switch."""
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        if user_input is None:
            return self.async_show_form(
                step_id="power_cycle",
                data_schema=self._outlet_schema(entry),
                description_placeholders=self._placeholders(),
            )

        outlet = user_input[CONF_RECOVERY_OUTLET]
        self._remember_outlet(entry, outlet)
        await self.hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: outlet}, blocking=True)
        await asyncio.sleep(POWER_CYCLE_OFF_SECONDS)
        await self.hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: outlet}, blocking=True)
        return await self._async_settle(
            f"Power-cycled {outlet}.",
            timeout=POWER_CYCLE_SETTLE_SECONDS,
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def _async_settle(self, attempted: str, *, timeout: float) -> data_entry_flow.FlowResult:
        """Wait for the fixture to come back, then finish or offer more."""
        if await self._async_wait_healthy(timeout):
            self._async_reconcile_issue()
            return self.async_create_entry(data={})
        self._last_result = f"{attempted} The light was still not answering {int(timeout)} seconds later."
        return await self.async_step_menu()

    async def _async_wait_healthy(self, timeout: float) -> bool:
        """Poll the health predicate in small steps for up to `timeout`."""
        deadline = monotonic() + timeout
        while True:
            if self._healthy():
                return True
            if monotonic() >= deadline:
                return False
            await asyncio.sleep(SETTLE_POLL_SECONDS)

    def _healthy(self) -> bool:
        """Return whether the condition this repair describes is over.

        Reuses the integration's own notions rather than inventing a second
        one: `link_healthy` for the BLE link, and the guardian's own
        `problem` flag - the same flag that raised the schedule repair - for
        a schedule problem.
        """
        runtime = self._runtime()
        if runtime is None or not link_healthy(runtime.device):
            return False
        if self._is_schedule_issue and runtime.guardian is not None:
            return not runtime.guardian.problem
        return True

    def _async_reconcile_issue(self) -> None:
        """Let the integration's own watcher agree the repair is over.

        `async_create_entry` removes the issue Home Assistant opened this
        flow for, but the watcher owns that issue between reloads; nudging
        it here means both views agree immediately rather than at the next
        connection notification.
        """
        runtime = self._runtime()
        if runtime is not None and runtime.link_watcher is not None:
            runtime.link_watcher.reconcile()

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    @property
    def _is_schedule_issue(self) -> bool:
        """Return whether this flow was opened for the schedule repair."""
        return self.issue_id.endswith(SCHEDULE_PROBLEM_ISSUE_SUFFIX)

    def _entry(self) -> ConfigEntry | None:
        """Return the config entry this repair belongs to, if it still exists."""
        entry_id = self._issue_data.get("entry_id")
        if isinstance(entry_id, str):
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is not None:
                return entry
        # Issue ids are MAC-derived (see `issue_id_for_mac`), so a repair
        # raised by a version that did not record the entry id still maps
        # back to its entry.
        mac = self.issue_id.partition("_")[0]
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if str(entry.data.get(CONF_MAC) or "").upper().replace(":", "") == mac:
                return entry
        return None

    def _runtime(self) -> FluvalRuntimeData | None:
        """Return runtime data for a loaded entry, or None."""
        entry = self._entry()
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return None
        return entry_runtime_data(self.hass, entry)

    def _proxy_name(self, entry: ConfigEntry) -> str | None:
        """Return the proxy holding this link now, else the last known one."""
        runtime = self._runtime()
        current = holding_proxy_name(runtime.device if runtime is not None else None)
        return current or entry.options.get(CONF_LAST_HOLDING_PROXY)

    def _proxy_action(self, entry: ConfigEntry) -> str | None:
        """Return the ESPHome action that restarts this link's proxy.

        `None` when no proxy is known, or when the one that is known does
        not expose a restart action - a scanner may be a local adapter, a
        proxy from another platform, or an ESPHome node whose configuration
        never defined the button.

        Asked through `has_service` rather than `async_services()`, which
        copies the whole service registry ("this function is expensive" in
        core's own docstring) - the menu re-derives this on every rung.
        """
        proxy = self._proxy_name(entry)
        if not proxy:
            return None
        action = f"{slugify(proxy)}{RESTART_PROXY_ACTION_SUFFIX}"
        return action if self.hass.services.has_service(ESPHOME_DOMAIN, action) else None

    def _outlet_schema(self, entry: ConfigEntry) -> vol.Schema:
        """Return the outlet picker, prefilled with the remembered switch."""
        selector = EntitySelector(EntitySelectorConfig(domain="switch"))
        remembered = entry.options.get(CONF_RECOVERY_OUTLET)
        if remembered:
            return vol.Schema({vol.Required(CONF_RECOVERY_OUTLET, default=remembered): selector})
        return vol.Schema({vol.Required(CONF_RECOVERY_OUTLET): selector})

    def _remember_outlet(self, entry: ConfigEntry, outlet: str) -> None:
        """Store the chosen outlet so the next power cycle is one click."""
        if entry.options.get(CONF_RECOVERY_OUTLET) == outlet:
            return
        self.hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_RECOVERY_OUTLET: outlet},
        )

    def _placeholders(self) -> dict[str, str]:
        """Return the shared placeholders every step in this flow renders."""
        entry = self._entry()
        return {
            "name": entry.title if entry is not None else "this light",
            "link": self._link_description(entry),
            "last_result": self._last_result,
        }

    def _link_description(self, entry: ConfigEntry | None) -> str:
        """Describe the link in one clause, naming the proxy when known."""
        runtime = self._runtime()
        device = runtime.device if runtime is not None else None
        if device is None:
            return "not set up"
        current = holding_proxy_name(device)
        if link_healthy(device):
            return f"connected through {current}" if current else "connected"
        last = entry.options.get(CONF_LAST_HOLDING_PROXY) if entry is not None else None
        return f"disconnected (last held by {last})" if last else "disconnected"


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None) -> RepairsFlow:
    """Return the fix flow for either of this integration's repairs."""
    return BleRecoveryFixFlow(issue_id, data)
