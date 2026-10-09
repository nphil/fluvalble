"""Free a light from a silent "ghost link" on the Bluetooth proxy that last held it.

A proxy glitch can leave the proxy's radio controller holding the light's
connection after the proxy's host has forgotten it. The light believes it is
still connected, never advertises again, and nothing can reach it (seen live
2026-10-08 on another house integration: 22 h silent, freed at once by
disconnecting connection handle 0). A request/reply light sends nothing
unprompted, so the house's orphan-frame detector never sees this ghost.

The held-link supervisor (`Client._ping_loop`) asks this watch, at the top of
every connect cycle, whether the light has gone quiet. Silence means no
advertisement from the light was heard by any scanner since its last session
ended (or since this watch was created). After `GHOST_SILENCE` the proxy
that last held the light is asked, through its `force_disconnect_handle`
ESPHome action, to send an HCI Disconnect for connection handles 0..3, one at
a time, stopping as soon as the light is heard. A healthy link hit by mistake
just drops cleanly and reconnects in seconds. A proxy without that action is
left alone.

Owned by `Device`, not `Client`, for the same reason as `ConnectionHoldStats`:
`Device.async_reset_connection()` throws the Client away, and the silence
clock and the counters must survive that.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import time

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.util import slugify

_LOGGER = logging.getLogger(__name__)

#: Silence (no advertisement heard) after which the light is presumed held by a ghost.
GHOST_SILENCE = 180.0
#: Pause between later passes while the light stays silent (it may simply be unpowered).
#: Counted from the END of the previous pass; the last value repeats.
GHOST_RETRY = (1800.0, 7200.0, 21600.0)
#: Connection handles freed on that proxy, one at a time: its three slots plus the ghost.
GHOST_HANDLES = (0, 1, 2, 3)
#: How long to listen for the light after each handle.
GHOST_LISTEN = 5.0
#: How long to wait for the proxy to take each call.
GHOST_CALL_DEADLINE = 5.0
#: Poll step while listening, so a stop or the shutdown latch ends the wait quickly.
GHOST_LISTEN_STEP = 0.5
#: The proxy action that sends an HCI Disconnect for one connection handle
#: (house proxy firmware; a proxy without it is left alone).
GHOST_ACTION = "force_disconnect_handle"


class GhostLinkWatch:
    """Silence clock plus the release pass for one light."""

    def __init__(
        self,
        hass: HomeAssistant | None,
        address: str,
        last_holder: Callable[[], str | None],
        is_closing: Callable[[], bool],
    ) -> None:
        """Start watching; the silence clock begins now.

        `last_holder` returns the ESPHome node name of the proxy that last
        held the link (read live). `is_closing` is the Device's shutdown
        latch: once it is true this watch never acts again.
        """
        self._hass = hass
        self._address = address
        self._last_holder = last_holder
        self._is_closing = is_closing
        # Since when the light has had no session from us and been heard by
        # nobody; None while a session is up or once it advertised.
        self._silent_since: float | None = time.monotonic()
        self._next_try: float | None = None
        self._tries = 0
        self.links_freed = 0
        self.last_try: str | None = None

    # -------------------------------------------------------------- session edges ---

    def session_started(self) -> None:
        """A session is up: the light is demonstrably not held by a ghost."""
        self._silent_since = None
        self._next_try = None
        self._tries = 0

    def session_ended(self) -> None:
        """A session ended: from now the light should advertise again; if not, it is held elsewhere.

        Only starts a fresh silence clock when a session was actually up, so
        a teardown with nothing live (a guardian reset during a long outage)
        does not keep pushing the first try into the future.
        """
        if self._silent_since is None:
            self._silent_since = time.monotonic()
            self._next_try = None
            self._tries = 0

    def snapshot(self) -> dict[str, int | str | None]:
        """Diagnostics: how many ghosts were freed and what the last pass did."""
        return {"ghost_links_freed": self.links_freed, "last_ghost_try": self.last_try}

    # ------------------------------------------------------------------ the pass ---

    def _heard_since(self, moment: float) -> bool:
        """Any scanner heard the light after `moment` (same clock as `time.monotonic()`)."""
        info = bluetooth.async_last_service_info(self._hass, self._address, connectable=True)
        return info is not None and info.time > moment

    def _must_stop(self, abort: Callable[[], bool]) -> bool:
        return self._is_closing() or abort()

    async def free_silent_ghost(self, abort: Callable[[], bool]) -> None:
        """Run one release pass if the light has been silent long enough.

        `abort` is true while the caller is stopping or the link is up (a
        command connected while this pass ran); the pass ends at once so it
        can never disconnect a healthy link we hold ourselves. Never raises
        for a failed proxy call; cancellation passes through.
        """
        if self._hass is None or self._silent_since is None or self._must_stop(abort):
            return
        if self._heard_since(self._silent_since):
            self._silent_since = None
            return
        now = time.monotonic()
        if self._next_try is None:
            self._next_try = self._silent_since + GHOST_SILENCE
        if now < self._next_try:
            return
        pause = GHOST_RETRY[min(self._tries, len(GHOST_RETRY) - 1)]
        self._next_try = now + pause
        self._tries += 1
        holder = self._last_holder()
        service = f"{slugify(holder)}_{GHOST_ACTION}" if holder else ""
        if not service or not self._hass.services.has_service("esphome", service):
            self.last_try = f"silent; {holder or 'no proxy known'} cannot free a link"
            return
        started = time.monotonic()
        for handle in GHOST_HANDLES:
            try:
                async with asyncio.timeout(GHOST_CALL_DEADLINE):
                    await self._hass.services.async_call("esphome", service, {"handle": handle}, blocking=True)
            except Exception as err:  # noqa: BLE001 - a failed call must never stop the supervisor
                self.last_try = f"{holder} refused connection {handle}: {err or type(err).__name__}"
                _LOGGER.warning(
                    "Fluval %s: could not free connection %s on %s (%s)", self._address, handle, holder, err
                )
                return
            if await self._listen_for_light(started, abort):
                self.links_freed += 1
                self._silent_since = None
                self.last_try = f"freed connection {handle} on {holder}"
                _LOGGER.info(
                    "Fluval %s: freed a ghost link on %s (connection %s); the light is back",
                    self._address,
                    holder,
                    handle,
                )
                return
            if self._must_stop(abort):
                return
        self._next_try = time.monotonic() + pause  # the pause counts from the end of the pass
        self.last_try = f"still silent after freeing connections on {holder}"
        _LOGGER.warning(
            "Fluval %s: still silent after freeing every connection on %s; it may be unpowered (next try in %.0f min)",
            self._address,
            holder,
            pause / 60,
        )

    async def _listen_for_light(self, since: float, abort: Callable[[], bool]) -> bool:
        """Wait up to `GHOST_LISTEN` for the light to be heard; ends early on stop or latch."""
        end = time.monotonic() + GHOST_LISTEN
        while not self._must_stop(abort):
            if self._heard_since(since):
                return True
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(remaining, GHOST_LISTEN_STEP))
        return False
