"""A single Fluval BLE connected LED device."""

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
import logging
from time import monotonic
import time
from typing import Any, Concatenate, ParamSpec, TypeVar, TypedDict, cast

from bleak import AdvertisementData, BLEDevice, BleakError, BleakScanner
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time

from . import (
    CONF_LAMP_PROFILE,
    DEFAULT_LAMP_PROFILE,
    LAMP_PROFILE_AQUASKY,
    LAMP_PROFILE_AQUASKY3,
    LAMP_PROFILE_AUTO,
    LAMP_PROFILE_MARINE,
    LAMP_PROFILE_PLANT,
    LAMP_PROFILE_PLANT_PRO,
)
from .client import Client, ConnectionHoldStats
from .color import channel_percentages_to_rgb, rgb_to_channel_percentages
from .discovery import (
    CONF_MODEL,
    CONF_PRODUCT_ID,
    default_fixture_name,
    detect_model,
    is_bare_address_name,
)
from .effects import (
    effect_id,
    effect_list as classic_effect_list,
    effect_name,
    four_effect_id,
    four_effect_list,
    four_effect_name,
)
from . import protocol
from .products import product_from_id, product_id_from_manufacturer_data

_LOGGER = logging.getLogger(__name__)

# An idle GATT disconnect is expected. Treat the fixture as reachable while
# recent advertisement, connection, or successful command activity exists.
REACHABLE_SECONDS = 300

# States of the "Connection" diagnostic sensor. Anything else it reports is
# the *name* of the scanner (ESPHome proxy or local adapter) currently
# carrying this fixture's GATT link, so a heal automation can restart the
# one proxy that matters instead of every proxy.
CONNECTION_STATE_DISCONNECTED = "disconnected"
# Connected, but nothing named the route: habluetooth only accounts for
# slots on remote scanners, so a local-adapter link has no allocation to
# read and no recorded scanner name to fall back on.
CONNECTION_STATE_CONNECTED = "connected"

NUMBERS = ["channel_1", "channel_2", "channel_3", "channel_4", "channel_5"]
# FluvalConnect exposes Manual, Auto, and Professional as one operating-mode
# control for every supported light family. Schedule editors configure those
# modes; they are not a second fixture mode selector.
SELECTS = ["mode"]
SENSORS = ["rssi", "last_seen", "active_connection_source"]
AQUASKY_NUMBERS = ["channel_1", "channel_2", "channel_3", "channel_4"]
CHANNEL_NAMES_AQUASKY = {
    "channel_1": "Red",
    "channel_2": "Green",
    "channel_3": "Blue",
    "channel_4": "White",
    "channel_5": "Violet",
}
CHANNEL_NAMES_PLANT = {
    "channel_1": "Pink",
    "channel_2": "Blue",
    "channel_3": "Cold White",
    "channel_4": "White",
    "channel_5": "Warm White",
}
CHANNEL_NAMES_MARINE = {
    "channel_1": "Pink",
    "channel_2": "Cyan",
    "channel_3": "Blue",
    "channel_4": "Purple",
    "channel_5": "Cold White",
}
CHANNEL_NAMES_PLANT_PRO = {
    # Kept as a compatibility profile name. FluvalConnect assigns Plant PRO
    # and Plant 4.0 the same APK light type and five-channel order.
    "channel_1": "Pink",
    "channel_2": "Blue",
    "channel_3": "Cold White",
    "channel_4": "White",
    "channel_5": "Warm White",
}
# Back-compat alias used by tests / schedule helpers
CHANNEL_NAMES = CHANNEL_NAMES_AQUASKY
MODES = ["manual", "automatic", "professional"]
MODE_TO_CODE = {mode: index for index, mode in enumerate(MODES)}
DIAGNOSTIC_UPDATE_INTERVAL = 5
BLE_LOOKUP_TIMEOUT = 10
BLE_LOOKUP_RETRIES = 3
PREVIEW_STEP_SECONDS = 2
TRANSITION_STEP_SECONDS = 30
DAY_MINUTES = 24 * 60
# Settle-then-poll delays (seconds) for reading a schedule back after writing
# it. The AquaSky answers an immediate status read with the previous
# schedule; by ~1-2 s it reports the new one. Total worst case ~5.5 s, well
# inside the 90 s schedule-write deadline. Tests shrink this to zeros.
SCHEDULE_VERIFY_SETTLE = (0.5, 1.0, 2.0, 2.0)
# Overall ceiling on one public command's `command_transaction()` hold of
# `_command_transaction_lock`. Client already bounds every individual
# GATT-facing await, so this is a backstop - it exists for the rare case
# a step inside Device itself (not Client) stalls, and for direct service
# calls that have no per-step guardian-level timeout of their own. Kept
# comfortably above Client's own worst case (30s connect + 15s per GATT
# op) so it should never fire ahead of a more specific, more informative
# Client-level timeout.
#
# This one applies to *background* work only (guardian steps, internal
# readbacks); every guardian step is already bounded tighter by the
# guardian itself (30s), so it stays purely a backstop there.
DEFAULT_COMMAND_DEADLINE = 60.0
# The same ceiling for a *user-initiated* command (priority=True). A
# command on a held link measured 0.38-1.92s live, and every write/read
# stayed inside 200ms-1s even at -88 dBm, so 15s is generous - and it means
# a button press that cannot get through fails (and resets the link) fast
# instead of burning a full minute the way it did when a guardian check
# was wedged ahead of it.
USER_COMMAND_DEADLINE = 15.0
# Native schedule writes keep the long ceiling for both callers: the write
# itself settles, reads back, and retries once on a verification mismatch
# (SCHEDULE_VERIFY_SETTLE), which no 15s window survives. The guardian's
# drift repush is the caller this exists for.
SCHEDULE_COMMAND_DEADLINE = 90.0


_P = ParamSpec("_P")
_R = TypeVar("_R")


def serialized_device_command(
    method: Callable[Concatenate["Device", _P], Awaitable[_R]] | None = None,
    *,
    deadline: float | None = None,
    priority: bool = False,
) -> Any:
    """Run one complete device command without interleaving another.

    Usable bare (`@serialized_device_command`) or parameterized
    (`@serialized_device_command(deadline=SCHEDULE_COMMAND_DEADLINE)`) for
    commands that legitimately need longer than the default - e.g. a
    schedule write that re-reads and retries once on a verification
    mismatch. Either way, `command_transaction()` enforces the deadline;
    see its docstring for what happens when a command exceeds it.

    `priority=True` marks a command as user-initiated: it jumps ahead of
    background (guardian) work waiting for the same device lock and, unless
    an explicit `deadline` says otherwise, is bounded by
    `USER_COMMAND_DEADLINE` instead of `DEFAULT_COMMAND_DEADLINE`. Methods
    the guardian *and* the user both call declare their own keyword-only
    `priority` parameter; this wrapper reads it out of the call's kwargs so
    one call site can override the decorator's default, and the method body
    itself ignores it.
    """

    def _decorate(
        fn: Callable[Concatenate["Device", _P], Awaitable[_R]],
    ) -> Callable[Concatenate["Device", _P], Awaitable[_R]]:
        @wraps(fn)
        async def wrapped(self: "Device", *args: _P.args, **kwargs: _P.kwargs) -> _R:
            call_priority = bool(kwargs.get("priority", priority))
            if deadline is not None:
                call_deadline = deadline
            else:
                call_deadline = USER_COMMAND_DEADLINE if call_priority else DEFAULT_COMMAND_DEADLINE
            try:
                async with self.command_transaction(deadline=call_deadline, priority=call_priority):
                    return await fn(self, *args, **kwargs)
            except TimeoutError:
                # `command_transaction` already logged the warning+traceback
                # and reset the connection; converting to this method's
                # normal falsy failure value (rather than letting
                # TimeoutError propagate) is what lets every existing
                # `if not await device.async_xxx(...): raise
                # HomeAssistantError(...)` service handler fire cleanly
                # instead of surfacing a raw, unhandled TimeoutError.
                self._set_diagnostic_error(
                    "command_timeout",
                    f"Fluval BLE command timed out after {call_deadline:g}s and the connection was reset",
                )
                return cast(_R, False)

        return cast(Callable[Concatenate["Device", _P], Awaitable[_R]], wrapped)

    if method is not None:
        return _decorate(method)
    return _decorate


def _resolved_device_name(
    name: str | None,
    device: BLEDevice | None,
    address: str,
    model: str,
) -> str:
    """Resolve the device's display name, never falling back to its own address.

    ``name`` is normally the config entry's title, which the config flow
    already resolves the same way at entry-creation time. This mirrors that
    rule defensively for any other caller (e.g. a bare :class:`Device`
    constructed directly) so the device-registry name and derived entity_ids
    stay model-based rather than MAC-based.
    """
    candidate = (name or "").strip()
    if candidate and not is_bare_address_name(candidate, address):
        return candidate
    ble_name = ((device.name if device else None) or "").strip()
    if ble_name and not is_bare_address_name(ble_name, address):
        return ble_name
    return default_fixture_name(model)


def _local_now() -> datetime:
    """Return the local wall-clock time as the fixture's own clock knows it.

    The clock-sync commands (``protocol.old_clock_packet``,
    ``wifi_clock_packet``, ``mesh_clock_packet``) all write
    ``datetime.now().astimezone()`` to the fixture, not Home Assistant's
    configured time zone. Schedule interpolation must read the same clock
    it wrote, or a container whose system time zone differs from HA's
    configured one would render the wrong ramp position.
    """
    return datetime.now().astimezone()


def _normalized_time_ramp(value: Any) -> tuple[int, int, int] | None:
    """Normalize a sunrise/sunset value to a plain `(hour, minute, ramp)` tuple.

    A caller-supplied schedule (service call) carries this as a 3-tuple; a
    schedule captured from a live readback (`Device._record_native_schedule_
    readback`, persisted as the guardian's `expected_schedule` and fed right
    back into `async_set_native_auto_schedule` on drift) carries it as
    `{"hour", "minute", "ramp"}` instead. The packet builders in `protocol`
    only ever index `sunrise[0]/[1]/[2]`, so passing a readback dict straight
    through raised `KeyError` on every guardian schedule-drift repush against
    real hardware - normalizing once here, for both packet-building and
    write-verification comparison, fixes that class of bug at the source.
    """
    if isinstance(value, dict):
        try:
            return (int(value["hour"]), int(value["minute"]), int(value.get("ramp", 0)))
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            return (int(value[0]), int(value[1]), int(value[2]))
        except (TypeError, ValueError):
            return None
    return None


def _normalized_clock(value: Any) -> tuple[int, int] | None:
    """Normalize a sleep value to a plain `(hour, minute)` tuple, or None.

    Same dict-or-tuple ambiguity as `_normalized_time_ramp`, one field
    shorter (no ramp).
    """
    if value is None:
        return None
    if isinstance(value, dict):
        try:
            return (int(value["hour"]), int(value["minute"]))
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (int(value[0]), int(value[1]))
        except (TypeError, ValueError):
            return None
    return None


def _auto_schedule_mismatch(
    requested: dict[str, Any], readback: dict[str, Any] | None, *, channel_count: int
) -> str | None:
    """Return the first Auto-schedule field the readback does not confirm, or None.

    `channel_count` truncates both sides before comparing levels: a 4-channel
    fixture never stores (or reports back) values beyond its own channel
    count, so comparing the full 5-value canonical list would always "mismatch"
    on a trailing value nothing ever wrote.
    """
    if not isinstance(readback, dict):
        return "schedule"
    if _normalized_time_ramp(requested.get("sunrise")) != _normalized_time_ramp(readback.get("sunrise")):
        return "sunrise"
    if _normalized_time_ramp(requested.get("sunset")) != _normalized_time_ramp(readback.get("sunset")):
        return "sunset"
    if _normalized_clock(requested.get("sleep")) != _normalized_clock(readback.get("sleep")):
        return "sleep"
    try:
        requested_day = [int(value) for value in (requested.get("day_levels") or [])][:channel_count]
        readback_day = [int(value) for value in (readback.get("day_levels") or [])][:channel_count]
    except (TypeError, ValueError):
        return "day_levels"
    if requested_day != readback_day:
        return "day_levels"
    try:
        requested_night = [int(value) for value in (requested.get("night_levels") or [])][:channel_count]
        readback_night = [int(value) for value in (readback.get("night_levels") or [])][:channel_count]
    except (TypeError, ValueError):
        return "night_levels"
    if requested_night != readback_night:
        return "night_levels"
    return None


def _pro_schedule_mismatch(
    requested: list[dict[str, Any]], readback: list[dict[str, Any]] | None, *, channel_count: int
) -> str | None:
    """Return "points" if the readback does not confirm every requested point.

    Both sides are already in the canonical `{"minute", "channel_1".."channel_5"}`
    shape (see `Device._canonical_pro_points`) and sorted by minute, so this
    is a plain equality check once levels are truncated to the fixture's own
    channel count.
    """
    if not isinstance(readback, list):
        return "points"
    if len(requested) != len(readback):
        return "points"
    for requested_point, readback_point in zip(requested, readback, strict=True):
        if requested_point["minute"] != readback_point.get("minute"):
            return "points"
        for index in range(1, channel_count + 1):
            key = f"channel_{index}"
            if int(requested_point.get(key, 0)) != int(readback_point.get(key, 0)):
                return "points"
    return None


def allocation_source_for_address(allocations: Iterable[Any] | None, address: str) -> str | None:
    """Return the scanner source whose connection slot holds `address`.

    `allocations` is habluetooth's own slot accounting
    (`get_manager().async_current_allocations()`): one
    `HaBluetoothSlotAllocations(source, slots, free, allocated)` per remote
    scanner, where `allocated` lists the addresses that scanner currently
    has connected. This is the same source Home Assistant's
    `bluetooth/subscribe_connection_allocations` websocket reports, and the
    only honest answer to "which proxy is carrying this link right now" -
    habluetooth re-scores every proxy on each `establish_connection`, so a
    reconnect can legitimately land somewhere else than the last one did.

    Kept a plain function over duck-typed objects so it is testable without
    habluetooth installed.
    """
    target = address.upper()
    for allocation in allocations or ():
        allocated = getattr(allocation, "allocated", None) or ()
        for item in allocated:
            if str(item).upper() == target:
                source = getattr(allocation, "source", None)
                return str(source) if source else None
    return None


class Attribute(TypedDict, total=False):
    """Attributes used by entities like binary_sensor and number."""

    options: list[str]
    default: str

    min: int
    max: int
    step: int
    value: int

    is_on: bool
    extra: dict
    device_class: str
    native_unit_of_measurement: str | None


@dataclass(frozen=True)
class FluvalState:
    """A confirmed on-device snapshot returned by `Device.async_read_state`."""

    mode: str
    power: bool | None
    levels: dict[str, int] | None
    auto_schedule: dict[str, Any] | None
    pro_schedule: list[dict[str, Any]] | None
    last_state_at: float | None
    connection_attempts: int
    scanner_source: str | None


class Device:
    """Fluval BLE LED device class."""

    def __init__(
        self,
        name: str,
        device: BLEDevice | None = None,
        advertisement: AdvertisementData | None = None,
        advertisement_source: str | None = None,
        hass: HomeAssistant | None = None,
        config_data: dict[str, Any] | None = None,
        ping_interval: int = 10,
        active_time: int = 120,
    ) -> None:
        """Initialize the device."""
        config_data = config_data or {}
        self.hass = hass
        self.address = (config_data.get("mac") or (device.address if device else "")).upper()
        configured_product_id = config_data.get(CONF_PRODUCT_ID)
        self.product_id = (
            configured_product_id
            if isinstance(configured_product_id, int) and not isinstance(configured_product_id, bool)
            else None
        )
        if self.product_id is None and advertisement is not None:
            self.product_id = product_id_from_manufacturer_data(advertisement.manufacturer_data)
        product = product_from_id(self.product_id)
        self.model = (
            (product.model if product is not None else None)
            or config_data.get(CONF_MODEL)
            or detect_model((device.name if device else None) or name, advertisement)
        )
        # Prefer a real advertised/entry name; some Bluetooth stacks default a
        # nameless fixture's reported name to its own address, which is not a
        # usable device-registry name any more than it is a usable title.
        self.name = _resolved_device_name(name, device, self.address, self.model)
        self.lamp_profile = config_data.get(CONF_LAMP_PROFILE, DEFAULT_LAMP_PROFILE)
        self._channel_count_hint: int | None = None
        self.client: Client | None = None
        self._ping_interval = ping_interval
        self._active_time = active_time
        self.connected = False
        self.entry_id: str | None = None
        self.schedule_mode = "manual"
        self.conn_info = {
            "mac": self.address,
            "model": self.model,
            "product_id": self.product_id,
            "service_uuids": config_data.get("service_uuids", []),
            "service_data": config_data.get("service_data", {}),
        }
        self.facebd = self._uses_facebd_protocol(
            self.name,
            self.conn_info["service_uuids"],
            self.conn_info["service_data"],
            config_data.get("manufacturer_data", {}),
        )
        self.updates_connect: list = []
        self.updates_component: list = []
        self._last_diagnostic_update = 0.0
        self.values = {}
        for channel in NUMBERS:
            self.values[channel] = 0
        self.values["mode"] = "manual"
        self.values["led_on_off"] = False
        self.values["effect"] = None
        self.firmware_version: str | None = None
        self.diagnostics: dict[str, Any] = {
            "status": "not_run",
            "configured_mac": self.address,
        }
        self.preview_task: asyncio.Task | None = None
        self.preview_restore_values: dict[str, int] | None = None
        self.preview_restore_mode: str | None = None
        self.native_preview_active = False
        self.native_preview_schedule_type: str | None = None
        self.native_preview_restore_mode: str | None = None
        self._clock_synced = False
        self._clock_sync_started = False
        self._clock_sync_lock = asyncio.Lock()
        self._command_transaction_lock = asyncio.Lock()
        self._command_transaction_owner: asyncio.Task[Any] | None = None
        self._command_transaction_depth = 0
        self._command_generation = 0
        # User-initiated commands waiting for (or holding) the transaction
        # lock. `_priority_idle` is set exactly while that count is zero, so
        # a background acquirer can wait on it instead of spinning, and the
        # guardian can read `priority_waiting` between its own steps and
        # stand aside rather than making a button press queue behind a
        # multi-step check the way it did live.
        self._priority_waiting = 0
        self._priority_idle = asyncio.Event()
        self._priority_idle.set()
        # Survives the Client being replaced (async_reset_connection).
        self.hold_stats = ConnectionHoldStats()
        # Preserve the exact colour HA requested while the decoded physical
        # channels still match it.  Plant RGB conversion is intentionally
        # lossy, so reconstructing RGB from those five channels would otherwise
        # make the colour picker jump after every status update.
        self._commanded_rgb: tuple[int, int, int] | None = None
        self._commanded_brightness: int | None = None
        self._commanded_channels: dict[str, int] | None = None
        self._commanded_at: float | None = None
        self._effect_restore_channels: dict[str, int] | None = None
        self._reachability_unsub: Callable[[], None] | None = None
        self._connection_listeners: list[Callable[[bool], None]] = []
        self._last_state_at: float | None = None
        self.mode_changed_by_write: bool = False

        if device and advertisement:
            self.update_ble(device, advertisement, advertisement_source)

    @property
    def mac(self) -> str:
        """Expose the MAC address of the device."""
        return self.address

    @property
    def model_name(self) -> str:
        """Expose a model name for Home Assistant device info."""
        return self.model

    @property
    def controls_available(self) -> bool:
        """Return true when HA has enough BLE info to attempt commands."""
        return bool(self.client or self.conn_info.get("last_seen"))

    def register_connection_listener(self, listener: Callable[[bool], None]) -> Callable[[], None]:
        """Subscribe to GATT connect/disconnect transitions.

        `listener` is called with the new `connected` state on every
        transition (not immediately at registration time). Returns an
        idempotent unsubscribe callable.
        """
        self._connection_listeners.append(listener)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._connection_listeners.remove(listener)

        return _unsubscribe

    @property
    def connection_attempts(self) -> int:
        """Return how many BLE connection attempts the current client has made."""
        return getattr(self.client, "connection_attempts", 0) if self.client is not None else 0

    @property
    def scanner_source(self) -> str | None:
        """Return the HA scanner source address serving the active connection."""
        return self.conn_info.get("active_connection_source_address")

    @property
    def priority_waiting(self) -> int:
        """Return how many user-initiated commands want the device right now.

        Counts every `command_transaction(priority=True)` from the moment it
        starts waiting for the lock until it releases it. The guardian reads
        this between its own steps (see `ScheduleGuardian._priority_pending`)
        and defers its check rather than making a user's button press wait
        out a multi-step supervision cycle: live, a queued press burned its
        whole 60s deadline behind a wedged guardian clock sync and returned
        an error while the radio itself was answering in ~200ms.
        """
        return self._priority_waiting

    @property
    def hold_active(self) -> bool:
        """Return whether this device holds its GATT link permanently."""
        return self._active_time == 0

    async def _async_acquire_command_lock(self, *, priority: bool) -> None:
        """Take the command lock, letting user commands cut the queue.

        `asyncio.Lock` is strictly FIFO, which is the wrong order here: the
        guardian runs several separately-locked steps per check, so a user
        command arriving mid-check could otherwise sit behind the rest of
        them. A background acquirer therefore refuses to keep the lock
        while `priority_waiting` is non-zero - it releases immediately,
        which hands the lock to the priority waiter already queued on it,
        and waits on `_priority_idle` instead of spinning.
        """
        if priority:
            self._priority_waiting += 1
            self._priority_idle.clear()
            try:
                await self._command_transaction_lock.acquire()
            except BaseException:
                self._release_priority_slot()
                raise
            return

        while True:
            if self._priority_waiting:
                await self._priority_idle.wait()
            await self._command_transaction_lock.acquire()
            if not self._priority_waiting:
                return
            self._command_transaction_lock.release()

    def _release_priority_slot(self) -> None:
        """Drop one priority reservation, waking background work when empty."""
        self._priority_waiting = max(0, self._priority_waiting - 1)
        if not self._priority_waiting:
            self._priority_idle.set()

    @contextlib.asynccontextmanager
    async def command_transaction(
        self,
        *,
        supersede_transition: bool = True,
        deadline: float | None = None,
        priority: bool = False,
    ) -> AsyncIterator[None]:
        """Serialize a complete command while allowing nested device helpers.

        Bounded to `deadline` seconds so a command stuck on an unbounded
        await deep in Client/bleak cannot hold this lock - and therefore
        every other command and every guardian check behind it - forever.
        That is exactly how a live install wedged permanently: three
        ``ScheduleGuardian.async_check()`` tasks piled up (one inside the
        wedged GATT call, two waiting on the guardian's own lock behind it)
        plus an HA automation's ``Script.async_run`` awaiting a service call
        that shared this same lock, and nothing short of an HA restart
        cleared it. On timeout the connection is reset (see
        `async_reset_connection`) so the *next* command reconnects fresh,
        and the exception is logged here with a traceback before
        propagating - that traceback's innermost frame is the exact await
        that wedged.

        Nested calls on the same task (`self._command_transaction_owner is
        task`) ride the outermost call's deadline instead of starting a new
        one, so one bounded window covers a whole public command even when
        it calls other `@serialized_device_command` helpers internally.

        `priority=True` marks this as user-initiated work: it takes the lock
        ahead of any background acquirer (see
        `_async_acquire_command_lock`), is visible to the guardian through
        `priority_waiting` for as long as it is queued or running, and
        defaults to the tighter `USER_COMMAND_DEADLINE`. A nested call
        inherits the outermost transaction's priority along with its
        deadline - the flag on an inner helper is ignored on purpose, so an
        entity's whole multi-step turn-on stays one priority window.
        """
        if deadline is None:
            deadline = USER_COMMAND_DEADLINE if priority else DEFAULT_COMMAND_DEADLINE
        task = asyncio.current_task()
        if task is not None and self._command_transaction_owner is task:
            self._command_transaction_depth += 1
            try:
                yield
            finally:
                self._command_transaction_depth -= 1
            return

        await self._async_acquire_command_lock(priority=priority)
        self._command_transaction_owner = task
        self._command_transaction_depth = 1
        if supersede_transition:
            self._command_generation += 1
        try:
            async with asyncio.timeout(deadline):
                yield
        except TimeoutError:
            _LOGGER.warning(
                "Fluval command for %s exceeded its %ss deadline; resetting the connection",
                self.address,
                deadline,
                exc_info=True,
            )
            await self.async_reset_connection()
            raise
        finally:
            self._command_transaction_depth = 0
            self._command_transaction_owner = None
            self._command_transaction_lock.release()
            if priority:
                self._release_priority_slot()

    async def async_reset_connection(self) -> None:
        """Discard the current Client so the next command reconnects from scratch.

        Called after a command's `command_transaction` deadline expires:
        bounding the stuck await stopped the *hang*, but the underlying
        BleakClient may still be sitting mid-write with an adapter or proxy
        that still considers the slot in use. Stopping it outright (rather
        than trying to keep using it) is the only way to guarantee the next
        command starts from a known-good state; `Client.stop()` already
        best-effort disconnects and tears down its own background tasks, and
        its status callback (`Device.set_connected(False)`) resets clock-sync
        state the same way a real disconnect does.
        """
        client, self.client = self.client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.stop()

    def touch_seen(self, *, rssi: int | None = None, notify: bool = True) -> None:
        """Record successful advertisement, connection, or command activity."""
        self.conn_info["last_seen"] = datetime.now(UTC)
        if rssi is not None:
            self.conn_info["rssi"] = rssi
            self.conn_info["rssi_updated_at"] = self.conn_info["last_seen"]
        if notify:
            for handler in self.updates_connect:
                handler()
        if not self.connected:
            self._schedule_reachability_refresh()

    def _on_client_activity(self) -> None:
        """Record a successful GATT exchange as fixture activity.

        Passed to `Client` as its `activity_callback`. A fixture we hold a
        link to still advertises, but far more rarely - live, `last_seen`
        froze at the moment the hold began while the fixture was answering
        every heartbeat - so a completed read/write/notification has to
        count as "seen" or every advertisement-derived diagnostic (and the
        `REACHABLE_SECONDS` window behind `is_reachable`) goes stale on a
        perfectly healthy link. Throttled notification only: the heartbeat
        fires every `ping_interval` seconds, which must not write four
        entity states each time.
        """
        self.touch_seen(notify=False)
        self._notify_diagnostics_throttled()

    def connection_state(self, allocation_source: str | None = None) -> str:
        """Return the scanner name carrying the link, or "disconnected".

        `allocation_source` is the scanner source address habluetooth
        reports as holding this fixture's slot (see
        `allocation_source_for_address`). It is preferred over the route
        recorded at connect time because it is pushed live and survives a
        reconnect landing on a different proxy; the recorded route is the
        fallback, and `CONNECTION_STATE_CONNECTED` the last resort for a
        link nothing can name (a local adapter keeps no slot accounting).
        """
        if not self.connected:
            return CONNECTION_STATE_DISCONNECTED
        if allocation_source:
            name = self._source_metadata(allocation_source)["source_name"]
            if name:
                return name
        recorded = self.conn_info.get("active_connection_source")
        if isinstance(recorded, str) and recorded:
            return recorded
        return CONNECTION_STATE_CONNECTED

    def connection_hold_attributes(self) -> dict[str, Any]:
        """Return the Connection sensor's attributes.

        `drops_1h`/`last_drop` come from the integration's own accounting
        (`ConnectionHoldStats`), not from habluetooth: habluetooth counts
        failures to *connect*, never a link that dropped after connecting,
        so nothing else in the stack knows a held link went away.
        """
        return {
            "hold": self.hold_active,
            "drops_1h": self.hold_stats.drops_in_window(),
            "last_drop": self.hold_stats.last_drop_iso(),
            "reconnect_attempt": self.hold_stats.reconnect_attempt,
        }

    def cancel_reachability_refresh(self) -> None:
        """Cancel the pending reachability expiry callback."""
        if self._reachability_unsub is not None:
            self._reachability_unsub()
            self._reachability_unsub = None

    @callback
    def _on_reachability_expired(self, _now: datetime) -> None:
        """Refresh entities when the recent-activity window expires."""
        self._reachability_unsub = None
        for handler in self.updates_connect:
            handler()

    def _schedule_reachability_refresh(self) -> None:
        """Schedule a one-shot refresh at the recent-activity expiry."""
        if self.hass is None or self.connected:
            return

        self.cancel_reachability_refresh()
        last_seen = self.conn_info.get("last_seen")
        if not isinstance(last_seen, datetime):
            return
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)

        expiry = last_seen + timedelta(seconds=REACHABLE_SECONDS)
        if expiry <= datetime.now(UTC):
            self._on_reachability_expired(datetime.now(UTC))
            return
        self._reachability_unsub = async_track_point_in_time(
            self.hass,
            self._on_reachability_expired,
            expiry,
        )

    def update_ble(
        self,
        device: BLEDevice,
        advertisement: AdvertisementData,
        source: str | None = None,
    ) -> None:
        """Update BLE metadata."""
        self.address = device.address
        self.conn_info["mac"] = device.address
        advertisement_source = source or self._source_from_device(device)
        active_source = self.conn_info.get("active_connection_source_address")
        # The RSSI entity describes the route controlling the fixture while a
        # GATT session is active. An advertisement heard by another scanner is
        # still retained in downloadable diagnostics, but must not replace the
        # active route's signal sample.
        route_rssi = advertisement.rssi if not self.connected or advertisement_source == active_source else None
        self.touch_seen(rssi=route_rssi, notify=False)
        self._record_advertisement_source(advertisement_source, advertisement.rssi)
        self.conn_info["service_uuids"] = list(advertisement.service_uuids)
        self.conn_info["service_data"] = {key: bytes(value).hex() for key, value in advertisement.service_data.items()}
        product_id = product_id_from_manufacturer_data(advertisement.manufacturer_data)
        if product_id is not None:
            self.product_id = product_id
            self.conn_info["product_id"] = product_id
            self.diagnostics["product_id"] = product_id
            product = product_from_id(product_id)
            if product is not None and product.model is not None:
                self.model = product.model
                self.conn_info["model"] = self.model
        self.facebd = self._uses_facebd_protocol(
            device.name,
            advertisement.service_uuids,
            advertisement.service_data,
            advertisement.manufacturer_data,
        )

        if self.client is not None:
            # Constructing a Client immediately spawns a real connect attempt
            # (see Client.__init__), so this only refreshes an already-live
            # client's BLE route; a new client is created on demand instead -
            # by the first guardian check or command that actually needs one
            # (`_async_ensure_client()`) - so an idle install never grabs the
            # single GATT slot on its own.
            self.client.device = device

        self._notify_diagnostics_throttled()
        for handler in self.updates_component:
            handler()

    @staticmethod
    def _source_from_device(device: BLEDevice) -> str | None:
        """Return the HA scanner source embedded in a connectable BLEDevice."""
        details = device.details if isinstance(device.details, dict) else {}
        source = details.get("source")
        return str(source) if source else None

    def _source_metadata(self, source: str | None) -> dict[str, str | None]:
        """Resolve an HA scanner source to a stable name and scanner type."""
        source_name = source
        source_type = None
        if self.hass is not None and source:
            get_scanner = getattr(bluetooth, "async_scanner_by_source", None)
            scanner = get_scanner(self.hass, source) if get_scanner else None
            if scanner is not None:
                source_name = getattr(scanner, "name", None) or source
                details = getattr(scanner, "details", None)
                scanner_type = getattr(details, "scanner_type", None)
                source_type = getattr(scanner_type, "value", None) or (
                    str(scanner_type) if scanner_type is not None else None
                )
        # Scanner names commonly append the source address in parentheses.
        # Addresses belong in downloadable diagnostics, not entity state.
        address_suffix = f" ({source})" if source else ""
        if source_name and address_suffix and source_name.endswith(address_suffix):
            source_name = source_name[: -len(address_suffix)]
        return {
            "source": source,
            "source_name": source_name,
            "source_type": source_type,
        }

    def _record_advertisement_source(self, source: str | None, rssi: int | None) -> None:
        """Record the latest advertisement separately from route diagnostics."""
        metadata = self._source_metadata(source)
        self.conn_info.update(
            {
                "advertisement_source": metadata["source_name"],
                "advertisement_source_address": metadata["source"],
                "advertisement_source_type": metadata["source_type"],
                "advertisement_rssi": rssi,
                "advertisement_updated_at": self.conn_info.get("last_seen"),
            }
        )

    def _record_active_connection_source(
        self,
        device: BLEDevice,
        connected_source: str | None = None,
    ) -> None:
        """Snapshot the selected HA route after GATT setup succeeds."""
        source = connected_source or self._source_from_device(device)
        metadata = self._source_metadata(source)
        # INFO, not DEBUG: which proxy took the link is the one fact needed
        # to explain latency or to heal the right proxy, and habluetooth
        # re-scores every route on every reconnect, so it does change.
        _LOGGER.info(
            "Fluval %s connected via %s",
            self.address,
            metadata["source_name"] or metadata["source"] or "the local Bluetooth adapter",
        )
        self.conn_info.update(
            {
                "active_connection_source": metadata["source_name"],
                "active_connection_source_address": metadata["source"],
                "active_connection_source_type": metadata["source_type"],
                "active_connection_connected_at": datetime.now(UTC),
            }
        )
        route_rssi = self._scanner_rssi(source)
        if route_rssi is None:
            # A stale value from another scanner is worse than no value.
            self.conn_info.pop("rssi", None)
            self.conn_info.pop("rssi_updated_at", None)
        else:
            self.touch_seen(rssi=route_rssi, notify=False)

    def _scanner_rssi(self, source: str | None) -> int | None:
        """Return the latest connectable advertisement RSSI for one scanner."""
        if self.hass is None or not source:
            return None
        scanner_devices = bluetooth.async_scanner_devices_by_address(
            self.hass,
            self.address,
            connectable=True,
        )
        for scanner_device in scanner_devices:
            if str(scanner_device.scanner.source) == source:
                return scanner_device.advertisement.rssi
        return None

    def set_connected(self, connected: bool):
        """Set active GATT status while tracking fixture reachability."""
        self.connected = connected
        if connected:
            self.hold_stats.record_connected()
            self.touch_seen(notify=False)
            self.cancel_reachability_refresh()
        else:
            # Allow clock sync again on the next successful connect (#8).
            self._clock_synced = False
            self._clock_sync_started = False
            self._schedule_reachability_refresh()

        for handler in self.updates_connect:
            handler()
        for handler in self.updates_component:
            handler()
        for listener in list(self._connection_listeners):
            listener(connected)

    def is_reachable(self) -> bool:
        """Return whether the fixture has a live session or recent activity."""
        if self.connected:
            return True
        last_seen = self.conn_info.get("last_seen")
        if not isinstance(last_seen, datetime):
            return False
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        return (datetime.now(UTC) - last_seen).total_seconds() <= REACHABLE_SECONDS

    def command_error_message(self) -> str:
        """Return the most useful available BLE command error."""
        if self.client is not None and self.client.last_error:
            return self.client.last_error
        return self.diagnostics.get("last_error") or "Fluval BLE command failed"

    def _notify_diagnostics_throttled(self):
        """Notify diagnostic entities at most once per interval."""
        now = monotonic()
        if now - self._last_diagnostic_update < DIAGNOSTIC_UPDATE_INTERVAL:
            return

        self._last_diagnostic_update = now
        for handler in self.updates_connect:
            handler()

    def _record_native_schedule_readback(
        self,
        *,
        protocol_name: str,
        auto: dict[str, Any] | None = None,
        professional: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Store protocol-neutral fixture schedule readback."""
        if auto is None and professional is None:
            return False
        if auto is not None:
            self.values["native_auto_schedule"] = auto
            self.diagnostics["native_auto_schedule"] = auto
        if professional is not None:
            self.values["native_pro_schedule"] = professional
            self.diagnostics["native_pro_schedule"] = professional
        self.diagnostics.update(
            {
                "native_schedule_protocol": protocol_name,
                "native_schedule_readback_at": datetime.now(UTC).isoformat(),
            }
        )
        return True

    def _record_native_effect_schedule_readback(
        self,
        *,
        protocol_name: str,
        windows: list[dict[str, Any]] | None,
    ) -> bool:
        """Store protocol-neutral fixture-owned timed-effect readback."""
        if windows is None:
            return False
        normalized = [
            {
                **window,
                "effect": self._native_effect_name(window["effect_id"]),
            }
            for window in windows
        ]
        self.values["native_effect_schedule"] = normalized
        self.diagnostics["native_effect_schedule"] = normalized
        if protocol_name == "plant_pro":
            # Backward-compatible diagnostics key from the original Plant Pro service.
            self.diagnostics["plant_pro_effect_schedule"] = normalized
        self.diagnostics.update(
            {
                "native_schedule_protocol": protocol_name,
                "native_schedule_readback_at": datetime.now(UTC).isoformat(),
            }
        )
        return True

    def numbers(self) -> list[str]:
        """List of numbers provided by the device."""
        if self._resolved_channel_count() == 4:
            return list(AQUASKY_NUMBERS)
        return list(NUMBERS)

    def _resolved_channel_count(self) -> int:
        """Return the APK channel count, with fallbacks for unidentified fixtures."""
        if (product := product_from_id(self.product_id)) is not None:
            return product.channel_count
        profile = (self.lamp_profile or LAMP_PROFILE_AUTO).lower()
        if profile == LAMP_PROFILE_AQUASKY:
            return 4
        if profile == LAMP_PROFILE_AQUASKY3:
            return 4
        if profile in (LAMP_PROFILE_PLANT, LAMP_PROFILE_PLANT_PRO, LAMP_PROFILE_MARINE):
            return 5
        if self._channel_count_hint in (4, 5):
            return self._channel_count_hint
        if self._uses_plant_pro_protocol():
            # FluvalConnect's FFF0/SPP command schema always carries the five
            # Plant-family emitters. This is live protocol evidence, not a
            # product inference from the advertised name.
            return 5
        # Keep the historical five-channel superset until an APK product ID,
        # explicit profile, or decoded controller response resolves the real
        # count. Do not infer a layout from a user-editable Bluetooth name.
        return 5

    def _channel_labels(self) -> dict[str, str]:
        """Return channel labels for the active lamp profile."""
        if (product := product_from_id(self.product_id)) is not None:
            if product.spectrum == "plant":
                return CHANNEL_NAMES_PLANT
            if product.spectrum == "rgbw":
                return CHANNEL_NAMES_AQUASKY
            if product.spectrum == "marine":
                return CHANNEL_NAMES_MARINE

        profile = (self.lamp_profile or LAMP_PROFILE_AUTO).lower()
        if profile == LAMP_PROFILE_PLANT_PRO:
            return CHANNEL_NAMES_PLANT_PRO
        if profile == LAMP_PROFILE_PLANT:
            return CHANNEL_NAMES_PLANT
        if profile == LAMP_PROFILE_MARINE:
            return CHANNEL_NAMES_MARINE
        if profile in (LAMP_PROFILE_AQUASKY, LAMP_PROFILE_AQUASKY3):
            return CHANNEL_NAMES_AQUASKY
        # Unknown automatic fixtures retain generic Channel N labels until
        # product identity or an explicit profile supplies APK channel names.
        return {}

    def spectrum_profile(self) -> str | None:
        """Return the APK spectrum asset family for this exact fixture."""
        if (product := product_from_id(self.product_id)) is not None:
            return product.spectrum_profile

        # Explicit profile choices are the only safe fallback when no APK
        # product ID was decoded. Auto detection must not invent a generation.
        profile = (self.lamp_profile or LAMP_PROFILE_AUTO).lower()
        selected = {
            LAMP_PROFILE_AQUASKY: "aquasky_legacy",
            LAMP_PROFILE_AQUASKY3: "aquasky_current",
            LAMP_PROFILE_PLANT: "plant_legacy",
            LAMP_PROFILE_PLANT_PRO: "plant_current",
            LAMP_PROFILE_MARINE: "reef_legacy",
        }.get(profile)
        if selected is not None:
            return selected

        # A family or generation in a Bluetooth name is not sufficient to
        # choose between the APK's old and current measured spectrum assets.
        # Keep automatic selection product-ID based; users can still select an
        # explicit fixture profile when an advertisement has no decodable ID.
        return None

    def uses_plant_spectrum(self) -> bool:
        """Return whether the fixture uses the five-channel Plant spectrum."""
        return self._channel_labels() in (
            CHANNEL_NAMES_PLANT,
            CHANNEL_NAMES_PLANT_PRO,
        )

    def uses_marine_spectrum(self) -> bool:
        """Return whether the fixture uses the five-channel Marine spectrum."""
        return self._channel_labels() == CHANNEL_NAMES_MARINE

    def light_mode(self) -> str:
        """Return the native Home Assistant colour mode for this fixture."""
        if self.spectrum_profile() is None:
            return "brightness"
        if self.uses_plant_spectrum() or self.uses_marine_spectrum():
            return "rgb"
        return "rgb_white"

    def master_brightness(self, levels: list[int] | None = None) -> int:
        """Overall brightness as the brightest supported channel."""
        chans = levels if levels is not None else [self.values.get(ch, 0) for ch in self.numbers()]
        return max(chans, default=0)

    def light_brightness_255(self, levels: list[int] | None = None) -> int:
        """Return the current light brightness on Home Assistant's 0-255 scale."""
        if levels is None and self._commanded_state_matches() and self._commanded_brightness is not None:
            return self._commanded_brightness
        return round(self.master_brightness(levels) / 100 * 255)

    def light_rgb_255(self, levels: list[int] | None = None) -> tuple[int, int, int]:
        """Return a five-channel APK spectrum as an sRGB colour."""
        if levels is None and self._commanded_state_matches() and self._commanded_rgb is not None:
            return self._commanded_rgb

        profile = self.spectrum_profile()
        if profile is None:
            return (0, 0, 0)
        channels = levels if levels is not None else [int(self.values.get(channel, 0)) for channel in self.numbers()]
        return channel_percentages_to_rgb(profile, tuple(channels))

    def aquasky_white_mode(self) -> bool:
        """Return whether an AquaSky is using only its independent white channel."""
        return (
            all(int(self.values.get(channel, 0)) == 0 for channel in AQUASKY_NUMBERS[:3])
            and int(self.values.get("channel_4", 0)) > 0
        )

    def aquasky_rgb_255(self, levels: list[int] | None = None) -> tuple[int, int, int]:
        """Return AquaSky's APK channel state as one Home Assistant RGB colour."""
        if levels is None and self._commanded_state_matches() and self._commanded_rgb is not None:
            return self._commanded_rgb
        profile = self.spectrum_profile()
        if profile is None:
            return (0, 0, 0)
        percentages = tuple(levels) if levels is not None else tuple(int(self.values.get(channel, 0)) for channel in AQUASKY_NUMBERS)
        # FluvalConnect names channel 4 Pure White. Report that native mode as
        # neutral RGB so Home Assistant's single colour picker shows white.
        if percentages[3] > 0 and not any(percentages[:3]):
            return (255, 255, 255)
        return channel_percentages_to_rgb(
            profile,
            percentages,
        )

    def scheduled_levels_now(self, now: datetime | None = None) -> list[int] | None:
        """Return the channel levels the fixture's onboard schedule implies right now.

        Classic BLE status bodies never carry live channel levels while a
        fixture runs Auto or Professional onboard, so ``values`` cannot
        answer "what is the fixture doing right now" while the fixture is
        off BLE and coasting on its own clock. This derives that answer
        from the last schedule readback using the same interpolation the
        native ``680B`` preview command uses, driven by local wall-clock
        time - the same clock the fixture's own clock-sync commands write
        to it with (``protocol.old_clock_packet`` et al. use
        ``datetime.now().astimezone()``, not Home Assistant's configured
        time zone).

        The FACEBD/Wi-Fi and Plant Pro (mesh/SPP) protocols already report
        live channel levels over BLE regardless of mode, and store their
        schedule readbacks in shapes this classic-only interpolation does
        not understand (HH:MM-keyed points, not minute-keyed ones) - this
        is never applicable to them.
        """
        mode = self.values.get("mode")
        if mode == "automatic":
            schedule_type, schedule_key = "auto", "native_auto_schedule"
        elif mode == "professional":
            schedule_type, schedule_key = "professional", "native_pro_schedule"
        else:
            return None
        if self._uses_wifi_protocol() or self._uses_plant_pro_protocol():
            return None
        if not self.values.get(schedule_key):
            return None
        moment = now if now is not None else _local_now()
        minute = moment.hour * 60 + moment.minute
        # A malformed/incomplete readback is reported once, from the code
        # path that actually acts on it (the native preview command); this
        # read-only display path must not re-report it on every render.
        return self._classic_native_preview_levels(schedule_type, minute, report_error=False)

    def effective_levels(self) -> tuple[list[int] | None, str]:
        """Return the levels actually driving the fixture right now, and their source.

        Manual mode, and every mode on the FACEBD/Wi-Fi and Plant Pro
        protocols, report live channel_N values over BLE ('reported').
        Classic Auto/Pro modes do not; what the fixture is doing is derived
        from its onboard schedule instead ('schedule'), or is 'unknown'
        until that schedule has been read back at least once.
        """
        if self.values.get("mode") in ("automatic", "professional"):
            scheduled = self.scheduled_levels_now()
            if scheduled is not None:
                return scheduled, "schedule"
            if not (self._uses_wifi_protocol() or self._uses_plant_pro_protocol()):
                return None, "unknown"
        return [int(self.values.get(channel, 0)) for channel in self.numbers()], "reported"

    def channels_from_aquasky_rgb(
        self,
        rgb: tuple[int, int, int],
        brightness: int,
    ) -> dict[str, int]:
        """Translate HA RGB to AquaSky's APK-defined RGBW emitters."""
        # Home Assistant's colour wheel expresses neutral white as equal RGB.
        # Use Fluval's much brighter dedicated Pure White emitter for that
        # achromatic request. Chromatic requests fit only R/G/B so pastel
        # colours cannot be washed out by the physical white bank.
        if rgb[0] == rgb[1] == rgb[2] and rgb != (0, 0, 0):
            return self.channels_from_aquasky_white(brightness)
        profile = self.spectrum_profile()
        if profile is None:
            return {channel: 0 for channel in AQUASKY_NUMBERS}
        levels = rgb_to_channel_percentages(profile, rgb, brightness, channel_count=3)
        return {
            **dict(zip(AQUASKY_NUMBERS[:3], levels, strict=True)),
            "channel_4": 0,
        }

    def channels_from_aquasky_white(self, brightness: int) -> dict[str, int]:
        """Map neutral HA RGB to only the AquaSky Pure White channel."""
        return {
            "channel_1": 0,
            "channel_2": 0,
            "channel_3": 0,
            "channel_4": self._ha_component_to_percent(255, brightness),
        }

    @staticmethod
    def _ha_component_to_percent(component: int, brightness: int) -> int:
        """Scale one HA colour component and brightness to a channel percent."""
        component = max(0, min(255, int(component)))
        brightness = max(0, min(255, int(brightness)))
        return max(0, min(100, round(component / 255 * brightness / 255 * 100)))

    def channels_from_rgb(
        self,
        rgb: tuple[int, int, int],
        brightness: int,
    ) -> dict[str, int]:
        """Fit HA RGB to the APK-measured five-channel spectrum."""
        profile = self.spectrum_profile()
        if profile is None:
            return {channel: 0 for channel in self.numbers()}
        levels = rgb_to_channel_percentages(profile, rgb, brightness)
        return dict(zip(self.numbers(), levels, strict=True))

    def remember_commanded_light(
        self,
        channels: dict[str, int],
        *,
        rgb: tuple[int, int, int] | None = None,
        brightness: int,
    ) -> None:
        """Remember the exact HA colour while device channels still match it."""
        self._commanded_channels = {channel: max(0, min(100, int(channels[channel]))) for channel in self.numbers()}
        self._commanded_at = monotonic()
        self._commanded_brightness = max(1, min(255, int(brightness)))
        self._commanded_rgb = (
            (
                max(0, min(255, int(rgb[0]))),
                max(0, min(255, int(rgb[1]))),
                max(0, min(255, int(rgb[2]))),
            )
            if rgb is not None
            else None
        )

    def clear_commanded_light(self) -> None:
        """Forget a cached HA colour after a non-light channel change."""
        self._commanded_rgb = None
        self._commanded_brightness = None
        self._commanded_channels = None
        self._commanded_at = None

    def _commanded_state_matches(self) -> bool:
        """Return whether a locally commanded colour is still authoritative."""
        if self._commanded_channels is None:
            return False
        if all(int(self.values.get(channel, -1)) == value for channel, value in self._commanded_channels.items()):
            return True
        # Classic controllers can emit one pre-command status notification
        # immediately after accepting 6804.  Keep only a short grace period;
        # later device changes must replace the cached HA colour.
        return self._commanded_at is not None and monotonic() - self._commanded_at < 2.0

    @serialized_device_command(priority=True)
    async def async_apply_light_channels(self, values: dict[str, int]) -> bool:
        """Apply colour channels and ensure the physical fixture is powered on."""
        if not await self.async_set_channels(values):
            return False
        self.clear_commanded_light()
        if not self.values.get("led_on_off"):
            return await self.async_set_switch("led_on_off", True)
        return True

    def supports_classic_effects(self) -> bool:
        """Return whether available BLE evidence identifies a classic controller."""
        product = product_from_id(self.product_id)
        if product is not None:
            if product.native_effect_count != 11:
                return False
        else:
            profile = (self.lamp_profile or LAMP_PROFILE_AUTO).lower()
            if profile not in (LAMP_PROFILE_AQUASKY, LAMP_PROFILE_AQUASKY3):
                return False
        if self.client is not None and self.client.command_write_uuid:
            return self.client.command_write_uuid.lower().startswith("00001001")

        service_uuids = [str(uuid).lower() for uuid in self.conn_info.get("service_uuids", [])]
        return any(uuid.startswith(("00001000", "00001002")) for uuid in service_uuids) and not any(
            uuid.startswith(("facebd", "0000fff0")) for uuid in service_uuids
        )

    def effect_list(self) -> list[str]:
        """Return the APK-defined effect catalogue for this product."""
        product = product_from_id(self.product_id)
        if product is not None:
            if product.native_effect_count == 4:
                return four_effect_list()
            if product.native_effect_count == 11:
                return classic_effect_list()
            return []
        if self.supports_plant_pro_effects():
            return four_effect_list()
        return classic_effect_list() if self.supports_classic_effects() or self.supports_facebd_effects() else []

    def supports_facebd_effects(self) -> bool:
        """Return whether BLE evidence identifies an effect-capable FACEBD controller."""
        if not self._uses_wifi_protocol():
            return False
        product = product_from_id(self.product_id)
        if product is not None:
            return product.native_effect_count in (4, 11)
        return self.lamp_profile == LAMP_PROFILE_AQUASKY3

    def supports_plant_pro_effects(self) -> bool:
        """Return whether available evidence identifies a four-effect controller."""
        product = product_from_id(self.product_id)
        if product is not None:
            return product.native_effect_count == 4
        return self.lamp_profile == LAMP_PROFILE_PLANT_PRO

    def uses_four_effect_catalogue(self) -> bool:
        """Return whether the APK assigns this product the four-effect catalogue."""
        return self.supports_plant_pro_effects()

    def _native_effect_id(self, effect: str) -> int | None:
        """Resolve an effect name using this product's APK catalogue."""
        return four_effect_id(effect) if self.uses_four_effect_catalogue() else effect_id(effect)

    def _native_effect_name(self, effect_code: int) -> str | None:
        """Resolve a wire effect ID using this product's APK catalogue."""
        return four_effect_name(effect_code) if self.uses_four_effect_catalogue() else effect_name(effect_code)

    def _channel_snapshot(self) -> dict[str, int]:
        """Return the current supported static channel values."""
        return {channel: int(self.values.get(channel, 0)) for channel in self.numbers()}

    def _channels_after_effect(self) -> dict[str, int]:
        """Return a useful static channel mix for leaving an effect."""
        targets = self._effect_restore_channels or self._channel_snapshot()
        if any(targets.values()):
            return dict(targets)
        targets = {channel: 0 for channel in self.numbers()}
        targets["channel_4"] = 100
        return targets

    def _clear_effect_state(self) -> None:
        """Clear controller-effect state after a successful static command."""
        self.values["effect"] = None
        self._effect_restore_channels = None

    @serialized_device_command(priority=True)
    async def async_set_effect(self, effect: str) -> bool:
        """Start one APK-native effect on a supported Fluval controller."""
        if not await self._async_prepare_command():
            _LOGGER.warning("Cannot set Fluval effect before BLE device is available")
            return False

        plant_pro = self._uses_plant_pro_protocol()
        facebd = self._uses_wifi_protocol()
        effect_code = self._native_effect_id(effect)
        if effect_code is None:
            return False
        if facebd and not self.supports_facebd_effects():
            _LOGGER.warning("FACEBD weather effects require an AquaSky controller identity")
            return False
        if not plant_pro and not facebd and not self.supports_classic_effects():
            _LOGGER.warning(
                "Classic weather effects are not valid for Fluval transport %s",
                self.client.command_write_uuid if self.client else None,
            )
            return False

        old_values = dict(self.values)
        old_restore = self._effect_restore_channels
        if not self.values.get("effect"):
            static_channels = self._channel_snapshot()
            if any(static_channels.values()):
                self._effect_restore_channels = static_channels

        if self.values.get("mode") != "manual":
            if await self.async_ensure_mode("manual") != "manual":
                self.values = old_values
                self._effect_restore_channels = old_restore
                return False
            self.mode_changed_by_write = True

        packets: list[bytes] = []
        if not self.values.get("led_on_off"):
            packets.append(
                protocol.spp_switch_packet(True)
                if plant_pro
                else protocol.wifi_switch_packet(True)
                if facebd
                else protocol.old_switch_packet(True)
            )
        packets.append(
            protocol.spp_effect_packet(effect_code)
            if plant_pro
            else protocol.wifi_effect_packet(effect_code)
            if facebd
            else protocol.old_weather_effect_packet(effect_code)
        )

        for packet in packets:
            if not await self._async_send_packet(packet):
                self.values = old_values
                self._effect_restore_channels = old_restore
                return False

        self.values["mode"] = "manual"
        self.values["led_on_off"] = True
        self.values["effect"] = effect
        self.clear_commanded_light()
        for handler in self.updates_component:
            handler()
        return True

    @serialized_device_command(deadline=SCHEDULE_COMMAND_DEADLINE)
    async def async_set_native_auto_schedule(
        self,
        schedule: dict[str, Any],
        *,
        activate: bool = True,
        priority: bool = False,
    ) -> bool:
        """Store a protocol-native Auto schedule in the fixture, verifying the write.

        Writes, re-reads the schedule straight back from the fixture, and
        compares it against what was requested; a mismatch retries the whole
        write once before failing. A prior version trusted a successful GATT
        write outright and returned True regardless - on a live install this
        once left the fixture on its old day levels while the integration
        reported success and confirmed, because the only readback check
        `Client.send_now` runs (`_expected_state_for_packet`) never covers
        FACEBD schedule keys.

        `priority` is read by `serialized_device_command`, not by this body:
        the guardian's drift repush leaves it False so a user command can
        cut ahead, a schedule written from a service call passes True. Both
        keep `SCHEDULE_COMMAND_DEADLINE` - the settle-and-verify sequence
        above needs far more than a user command's usual ceiling.
        """
        sunrise = _normalized_time_ramp(schedule.get("sunrise"))
        sunset = _normalized_time_ramp(schedule.get("sunset"))
        if sunrise is None or sunset is None:
            self._set_diagnostic_error("invalid_native_schedule", "Auto schedule requires sunrise and sunset times")
            return False
        sleep = _normalized_clock(schedule.get("sleep"))
        day_levels = schedule["day_levels"]
        night_levels = schedule["night_levels"]
        canonical_request = {
            "sunrise": sunrise,
            "sunset": sunset,
            "sleep": sleep,
            "day_levels": [int(value) for value in day_levels],
            "night_levels": [int(value) for value in night_levels],
        }

        if not await self._async_prepare_command():
            return False

        channel_count = self._resolved_channel_count()
        for attempt in (1, 2):
            if self._uses_wifi_protocol():
                packet = protocol.wifi_auto_schedule_packet(
                    sunrise=sunrise,
                    sunset=sunset,
                    sleep=sleep,
                    day_levels=day_levels,
                    night_levels=night_levels,
                    channel_count=channel_count,
                )
                native_protocol = "facebd"
            elif self._uses_plant_pro_protocol():
                packet = protocol.spp_auto_schedule_packet(
                    sunrise=sunrise,
                    sunset=sunset,
                    sleep=sleep,
                    day_levels=day_levels,
                    night_levels=night_levels,
                )
                native_protocol = "plant_pro"
            else:
                packet = protocol.old_auto_schedule_packet(
                    sunrise=sunrise,
                    sunset=sunset,
                    sleep=sleep,
                    day_levels=day_levels,
                    night_levels=night_levels,
                    channel_count=channel_count,
                )
                native_protocol = "classic"

            if not await self._async_send_packet(packet):
                return False
            if activate and not await self._async_send_packet(self._native_mode_packet("automatic")):
                return False

            mismatch = await self._async_verify_native_auto_schedule(canonical_request, channel_count=channel_count)
            if mismatch is None:
                break
            if attempt == 2:
                self._set_diagnostic_error(
                    "native_auto_schedule_unverified",
                    f"Fluval did not confirm the requested Auto schedule after writing it twice ({mismatch} mismatch)",
                )
                return False
            _LOGGER.warning(
                "Fluval Auto schedule write for %s unverified (%s mismatch); retrying once",
                self.address,
                mismatch,
            )

        if activate:
            self.values["mode"] = "automatic"
        self.diagnostics.update(
            {
                "status": "native_auto_schedule_submitted",
                "native_schedule_protocol": native_protocol,
                "native_auto_schedule_packet": packet.hex(),
            }
        )
        self._notify_diagnostics_throttled()
        return True

    async def _async_verify_native_auto_schedule(self, requested: dict[str, Any], *, channel_count: int) -> str | None:
        """Re-read the fixture's Auto schedule and return the first mismatched field, or None.

        The fixture answers a status read issued right after a schedule write
        with the *previous* schedule (observed live 2026-09-05: three writes
        that were correct on the wire failed an immediate readback, while the
        same schedule read back correctly 5 s later). So the readback is
        polled with a short settle instead of trusted on the first sample.
        """
        mismatch: str | None = "unreachable"
        for delay in SCHEDULE_VERIFY_SETTLE:
            await asyncio.sleep(delay)
            if self.client is None:
                return "unreachable"
            try:
                await self.client.request_state()
            except (TimeoutError, BleakError) as err:
                _LOGGER.debug("Unable to read back Fluval Auto schedule for verification", exc_info=err)
                mismatch = "unreachable"
                continue
            mismatch = _auto_schedule_mismatch(requested, self.values.get("native_auto_schedule"), channel_count=channel_count)
            if mismatch is None:
                return None
        return mismatch

    def native_pro_schedule_limits(self) -> tuple[str, int, int]:
        """Return the APK-defined Professional-schedule limits for this fixture."""
        if self._uses_wifi_protocol():
            return "facebd", protocol.WIFI_MIN_PRO_POINTS, protocol.WIFI_MAX_PRO_POINTS
        if self._uses_plant_pro_protocol():
            return "plant_pro", protocol.SPP_MIN_PRO_POINTS, protocol.SPP_MAX_PRO_POINTS
        return "classic", protocol.OLD_MIN_PRO_POINTS, protocol.OLD_MAX_PRO_POINTS

    def _canonical_pro_points(self, points: Iterable[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """Reduce any native/generic Pro schedule point shape to sorted canonical points.

        Points arrive in one of three shapes depending on caller: the
        service's validated ``{"hour","minute","levels"}``, the generic
        save_schedule ``{"time","channel_N"}``/legacy-color shape, or a live
        readback - ``{"minute","channel_N"}`` for classic/FACEBD,
        ``{"time","levels"}`` for Plant Pro/SPP. All three must compare and
        re-encode identically, or the guardian's schedule-drift repush
        (which feeds a captured readback straight back into
        `async_set_native_pro_schedule`) silently corrupts the schedule it
        is trying to restore - the Pro-schedule counterpart of the
        sunrise/sunset dict-vs-tuple bug `_normalized_time_ramp` fixes for
        Auto schedules. Returns ``None`` if any point cannot be parsed.
        """
        canonical: list[dict[str, Any]] = []
        for point in points:
            if not isinstance(point, dict):
                return None
            try:
                if "hour" in point and "minute" in point:
                    minute = (int(point["hour"]) * 60 + int(point["minute"])) % DAY_MINUTES
                elif "minute" in point:
                    minute = int(point["minute"]) % DAY_MINUTES
                elif "time" in point:
                    minute = self._parse_time_to_minute(str(point["time"]))
                else:
                    return None
            except (TypeError, ValueError, IndexError):
                return None

            if isinstance(point.get("levels"), list):
                raw_levels = point["levels"]
            else:
                raw_levels = [
                    point.get(channel, point.get(color, 0))
                    for channel, color in (
                        ("channel_1", "red"),
                        ("channel_2", "green"),
                        ("channel_3", "blue"),
                        ("channel_4", "white"),
                        ("channel_5", "channel_5"),
                    )
                ]
            try:
                levels = [max(0, min(100, int(value))) for value in raw_levels]
            except (TypeError, ValueError):
                return None
            levels = (levels + [0] * 5)[:5]
            canonical.append({"minute": minute, **{f"channel_{index}": levels[index - 1] for index in range(1, 6)}})
        return sorted(canonical, key=lambda item: item["minute"])

    @serialized_device_command(deadline=SCHEDULE_COMMAND_DEADLINE)
    async def async_set_native_pro_schedule(
        self,
        points: list[dict[str, Any]],
        *,
        activate: bool = True,
        priority: bool = False,
    ) -> bool:
        """Store a protocol-native Professional schedule in the fixture, verifying the write.

        See `async_set_native_auto_schedule` for what `priority` does here.
        """
        normalized = self._canonical_pro_points(points)
        if normalized is None:
            self._set_diagnostic_error(
                "invalid_native_schedule",
                "Each Professional point requires a time and channel levels",
            )
            return False

        if not protocol.SPP_MIN_PRO_POINTS <= len(normalized) <= protocol.SPP_MAX_PRO_POINTS:
            self._set_diagnostic_error(
                "invalid_native_schedule",
                f"Professional schedules require {protocol.SPP_MIN_PRO_POINTS} to {protocol.SPP_MAX_PRO_POINTS} points",
            )
            return False
        if not await self._async_prepare_command():
            return False

        native_protocol, minimum, maximum = self.native_pro_schedule_limits()

        if not minimum <= len(normalized) <= maximum:
            self._set_diagnostic_error(
                "invalid_native_schedule",
                f"{native_protocol} Professional schedules require {minimum} to {maximum} points",
            )
            return False

        channel_count = self._resolved_channel_count()
        for attempt in (1, 2):
            if native_protocol == "facebd":
                packet = protocol.wifi_pro_schedule_packet(
                    normalized,
                    channel_count=channel_count,
                )
            elif native_protocol == "plant_pro":
                spp_points = [
                    {
                        "hour": point["minute"] // 60,
                        "minute": point["minute"] % 60,
                        "levels": [point.get(f"channel_{index}", 0) for index in range(1, 6)],
                    }
                    for point in normalized
                ]
                packet = protocol.spp_pro_schedule_packet(spp_points)
            else:
                packet = protocol.old_pro_schedule_packet(
                    normalized,
                    channel_count=channel_count,
                )

            if not await self._async_send_packet(packet):
                return False
            if activate and not await self._async_send_packet(self._native_mode_packet("professional")):
                return False

            mismatch = await self._async_verify_native_pro_schedule(normalized, channel_count=channel_count)
            if mismatch is None:
                break
            if attempt == 2:
                self._set_diagnostic_error(
                    "native_pro_schedule_unverified",
                    f"Fluval did not confirm the requested Professional schedule after writing it twice ({mismatch} mismatch)",
                )
                return False
            _LOGGER.warning(
                "Fluval Professional schedule write for %s unverified (%s mismatch); retrying once",
                self.address,
                mismatch,
            )

        if activate:
            self.values["mode"] = "professional"
        self.diagnostics.update(
            {
                "status": "native_pro_schedule_submitted",
                "native_schedule_protocol": native_protocol,
                "native_pro_schedule_points": len(normalized),
                "native_pro_schedule_packet": packet.hex(),
            }
        )
        self._notify_diagnostics_throttled()
        return True

    async def _async_verify_native_pro_schedule(
        self, requested: list[dict[str, Any]], *, channel_count: int
    ) -> str | None:
        """Re-read the fixture's Professional schedule and return "points" on mismatch, or None.

        Same settle-then-poll as the Auto verify: an immediate readback can
        still show the previous schedule.
        """
        mismatch: str | None = "unreachable"
        for delay in SCHEDULE_VERIFY_SETTLE:
            await asyncio.sleep(delay)
            if self.client is None:
                return "unreachable"
            try:
                await self.client.request_state()
            except (TimeoutError, BleakError) as err:
                _LOGGER.debug("Unable to read back Fluval Professional schedule for verification", exc_info=err)
                mismatch = "unreachable"
                continue
            readback = self._canonical_pro_points(self.values.get("native_pro_schedule") or [])
            mismatch = _pro_schedule_mismatch(requested, readback, channel_count=channel_count)
            if mismatch is None:
                return None
        return mismatch

    @serialized_device_command(priority=True)
    async def async_set_native_effect_schedule(self, windows: list[dict[str, Any]]) -> bool:
        """Store APK-native timed weather-effect windows in the fixture."""
        if not await self._async_prepare_command():
            return False

        if self._uses_plant_pro_protocol():
            native_protocol = "plant_pro"
            packet_builder = protocol.spp_effect_schedule_packet
        elif self._uses_wifi_protocol() and self.supports_facebd_effects():
            native_protocol = "facebd"
            packet_builder = protocol.wifi_effect_schedule_packet
        elif self.supports_classic_effects():
            native_protocol = "classic"
            packet_builder = protocol.old_effect_schedule_packet
        else:
            self._set_diagnostic_error(
                "unsupported_transport",
                "Timed native effects require a supported classic, AquaSky 3.0/FACEBD, or Plant Pro controller",
            )
            return False

        wire_windows = []
        for window in windows:
            effect_code = window.get("effect_id")
            if isinstance(window.get("effect"), str):
                effect_code = self._native_effect_id(window["effect"])
            if not isinstance(effect_code, int) or self._native_effect_name(effect_code) is None:
                self._set_diagnostic_error(
                    "invalid_native_effect_schedule",
                    f"Effect {window.get('effect', effect_code)!r} is not supported by this product",
                )
                return False
            wire_windows.append({**window, "effect_id": effect_code})

        try:
            packet = packet_builder(wire_windows)
        except (KeyError, TypeError, ValueError) as err:
            self._set_diagnostic_error("invalid_native_effect_schedule", str(err))
            return False
        if not await self._async_send_packet(packet):
            return False
        normalized = [
            {
                "enabled": bool(window.get("enabled", True)),
                "weekdays": list(window["weekdays"]),
                "start": f"{window['start_hour']:02d}:{window['start_minute']:02d}",
                "end": f"{window['end_hour']:02d}:{window['end_minute']:02d}",
                "effect_id": window["effect_id"],
                "effect": self._native_effect_name(window["effect_id"]),
            }
            for window in wire_windows
        ]
        self.values["native_effect_schedule"] = normalized
        self.diagnostics.update(
            {
                "status": "native_effect_schedule_submitted",
                "native_schedule_protocol": native_protocol,
                "native_effect_schedule": normalized,
                "native_effect_schedule_packet": packet.hex(),
            }
        )
        if native_protocol == "plant_pro":
            self.diagnostics["plant_pro_effect_schedule"] = normalized
        self._notify_diagnostics_throttled()
        return True

    @serialized_device_command(priority=True)
    async def async_stop_effect(self) -> bool:
        """Stop a native effect by restoring the preceding static channel mix."""
        if not self.values.get("effect"):
            return True
        return await self.async_set_channels(self._channels_after_effect(), force=True)

    @serialized_device_command(priority=True)
    async def async_set_master_brightness(self, level: int) -> bool:
        """Scale all supported channels to level, preserving ratios."""
        level = min(100, max(0, round(level / 10) if level > 100 else int(level)))
        chans = self.numbers()
        current_max = max((self.values.get(ch, 0) for ch in chans), default=0)
        if current_max <= 0:
            targets = {channel: level for channel in chans}
        else:
            factor = level / current_max
            targets = {channel: min(100, max(0, round(self.values.get(channel, 0) * factor))) for channel in chans}
        return await self.async_set_channels(targets)

    def entity_name(self, attr: str) -> str:
        """Return a user-facing entity suffix for this device attribute."""
        labels = self._channel_labels()
        if attr in labels:
            return labels[attr]
        return attr.replace("_", " ").title()

    def selects(self) -> list[str]:
        """List of select boxes provided by the device."""
        return list(SELECTS)

    def sensors(self) -> list[str]:
        """List of diagnostics sensors provided by the device."""
        return list(SENSORS)

    def supports_facebd_dst_control(self) -> bool:
        """Return whether this fixture uses FluvalConnect's FACEBD DST setting."""
        return self._uses_wifi_protocol()

    def attribute(self, attr: str) -> Attribute:
        """Provide attributes to the entities like switches, numbers etc."""
        if attr == "connection":
            extra = dict(self.conn_info)
            extra["gatt_connected"] = self.connected
            return Attribute(is_on=self.is_reachable(), extra=extra)
        if attr.startswith("channel_"):
            return Attribute(min=0, max=100, step=1, value=self.values[attr])
        if attr == "mode":
            return Attribute(options=MODES, default=self.values[attr])
        if attr == "led_on_off":
            return Attribute(is_on=self.values[attr])
        if attr == "daylight_saving_time":
            value = self.values.get(attr)
            return Attribute(is_on=value) if isinstance(value, bool) else Attribute()
        if attr == "rssi":
            return Attribute(
                value=self.conn_info.get("rssi"),
                native_unit_of_measurement="dBm",
                extra={
                    "last_updated": self.conn_info.get("rssi_updated_at"),
                },
            )
        if attr == "active_connection_source":
            return Attribute(
                value=self.conn_info.get("active_connection_source") if self.connected else None,
                extra={
                    "source_type": self.conn_info.get("active_connection_source_type"),
                    "connected_at": self.conn_info.get("active_connection_connected_at"),
                    "gatt_connected": self.connected,
                },
            )
        if attr == "last_seen":
            return Attribute(value=self.conn_info.get("last_seen"))
        return Attribute()

    def register_update(self, attr: str, handler: Callable):
        """Register handlers for updates."""
        if attr in ("connection", "rssi", "last_seen", "active_connection_source"):
            self.updates_connect.append(handler)
        else:
            self.updates_component.append(handler)

    def deregister_update(self, attr: str, handler: Callable):
        """Remove a previously registered update handler."""
        target = (
            self.updates_connect
            if attr in ("connection", "rssi", "last_seen", "active_connection_source")
            else self.updates_component
        )
        with contextlib.suppress(ValueError):
            target.remove(handler)

    @serialized_device_command(priority=True)
    async def async_set_value(self, attr: str, value: int) -> bool:
        """Set values received by entities such as numbers and switches."""
        if attr.startswith("channel_"):
            return await self.async_set_channels({attr: int(value)})

        _LOGGER.debug("Value %s changed to %s", attr, value)
        return False

    async def async_set_channels(
        self,
        values: dict[str, int],
        *,
        transition: int = 0,
        step_seconds: int = TRANSITION_STEP_SECONDS,
        force: bool = False,
    ) -> bool:
        """Set multiple channel values, optionally ramping over time.

        Every caller is user-initiated (the light entity, `async_set_value`,
        the `set_channels` service), so each transaction here is a priority
        one. A ramp takes one priority transaction *per step* rather than
        one for the whole ramp: the sleeps between steps are exactly when a
        guardian check should be allowed to run.
        """
        if transition <= 0:
            async with self.command_transaction(priority=True):
                return await self._async_set_channels_now(values, force=force)

        async with self.command_transaction(priority=True):
            generation = self._command_generation
            channels = self.numbers()
            targets = {
                channel: max(0, min(100, int(values.get(channel, self.values[channel])))) for channel in channels
            }
            start_values = {channel: int(self.values[channel]) for channel in channels}

        steps = max(1, int(transition / max(1, step_seconds)))
        for step in range(1, steps + 1):
            async with self.command_transaction(supersede_transition=False, priority=True):
                if generation != self._command_generation:
                    self.diagnostics["status"] = "transition_interrupted"
                    self._notify_diagnostics_throttled()
                    return True
                ratio = step / steps
                step_values = {
                    channel: round(start_values[channel] + ((targets[channel] - start_values[channel]) * ratio))
                    for channel in channels
                }
                if not await self._async_set_channels_now(step_values, force=force):
                    return False
            if step < steps:
                await asyncio.sleep(step_seconds)
        return True

    async def _async_set_channels_now(
        self,
        values: dict[str, int],
        *,
        force: bool = False,
    ) -> bool:
        """Apply one channel frame inside an active command transaction."""
        channels = self.numbers()
        effect_active = bool(self.values.get("effect"))
        force = force or effect_active
        targets = {channel: max(0, min(100, int(values.get(channel, self.values[channel])))) for channel in channels}
        if not targets:
            return False

        if not force and all(int(self.values.get(channel, -1)) == value for channel, value in targets.items()):
            _LOGGER.debug("Skipping Fluval channel write because targets are unchanged: %s", targets)
            return True

        old_values = dict(self.values)
        changed_channels = [channel for channel, value in targets.items() if int(old_values.get(channel, -1)) != value]
        single_channel = changed_channels[0] if len(changed_channels) == 1 and not force else None
        if not await self._async_prepare_command():
            _LOGGER.warning("Cannot set Fluval channel before BLE device is available")
            self.values = old_values
            return False

        if self.values.get("mode") != "manual":
            if await self.async_ensure_mode("manual") != "manual":
                self.values = old_values
                return False
            self.mode_changed_by_write = True

        for channel, value in targets.items():
            self.values[channel] = value
        ok = await self._async_send_channel_state(
            old_values,
            force_power=force,
            single_channel=single_channel,
        )
        if ok and effect_active:
            self._clear_effect_state()
            for handler in self.updates_component:
                handler()
        return ok

    async def _async_send_channel_state(
        self,
        old_values: dict[str, Any],
        *,
        force_power: bool = False,
        single_channel: str | None = None,
    ) -> bool:
        """Send the current channel values to the controller."""
        channel_index = self.numbers().index(single_channel) if single_channel is not None else None
        if self._uses_wifi_protocol():
            any_channel_on = any(self._channel_values())
            if any_channel_on and (force_power or not self.values["led_on_off"]):
                self.values["led_on_off"] = True
                if not await self._async_send_packet(protocol.wifi_switch_packet(True)):
                    self.values = old_values
                    return False
            packet = (
                protocol.wifi_single_zone_packet(channel_index, self.values[single_channel])
                if channel_index is not None and single_channel is not None
                else protocol.wifi_all_zone_packet(self._channel_values())
            )
            ok = await self._async_send_packet(packet)
            if ok and not any_channel_on and (force_power or self.values["led_on_off"]):
                ok = await self._async_send_packet(protocol.wifi_switch_packet(False))
                if ok:
                    self.values["led_on_off"] = False
        elif self._uses_plant_pro_protocol():
            any_channel_on = any(self._channel_values())
            if any_channel_on and (force_power or not self.values["led_on_off"]):
                self.values["led_on_off"] = True
                if not await self._async_send_packet(protocol.spp_switch_packet(True)):
                    self.values = old_values
                    return False
            packet = (
                protocol.spp_single_zone_packet(channel_index, self.values[single_channel])
                if channel_index is not None and single_channel is not None
                else protocol.spp_all_zone_packet(self._channel_values())
            )
            ok = await self._async_send_packet(packet)
            if ok and not any_channel_on and (force_power or self.values["led_on_off"]):
                ok = await self._async_send_packet(protocol.spp_switch_packet(False))
                if ok:
                    self.values["led_on_off"] = False
        else:
            any_channel_on = any(self._channel_values())
            # Establish power before applying the 6804 channel frame, matching
            # the app's switch-then-manual-colour ordering for an off fixture.
            if any_channel_on and (force_power or not self.values["led_on_off"]):
                if not await self._async_send_packet(protocol.old_switch_packet(True)):
                    self.values = old_values
                    return False
                self.values["led_on_off"] = True
            ok = await self._async_send_packet(protocol.old_all_zone_packet(self._channel_values()))

        if not ok:
            self.values = old_values
            for handler in self.updates_component:
                handler()
        return ok

    @serialized_device_command(priority=True)
    async def async_preview_schedule(
        self,
        points: list[dict[str, Any]],
        *,
        duration: int = 60,
        step_seconds: int = PREVIEW_STEP_SECONDS,
    ) -> bool:
        """Preview a 24-hour schedule on the real light in compressed time."""
        if not await self.async_stop_preview():
            return False
        self.preview_restore_values = {channel: int(self.values.get(channel, 0)) for channel in self.numbers()}
        self.preview_restore_mode = (
            self.values.get("mode") if self.values.get("mode") in {"automatic", "professional"} else None
        )
        self.preview_task = asyncio.create_task(self._async_preview_schedule(points, duration, step_seconds))
        return True

    @serialized_device_command(priority=True)
    async def async_preview_native_schedule(self, minute: int, schedule_type: str) -> bool:
        """Preview one minute of a schedule already stored by the fixture."""
        if schedule_type not in {"auto", "professional"} or not 0 <= minute < DAY_MINUTES:
            self._set_diagnostic_error(
                "invalid_native_preview", "Native preview requires Auto or Professional and minute 0-1439"
            )
            return False

        schedule_key = "native_auto_schedule" if schedule_type == "auto" else "native_pro_schedule"
        if not self.values.get(schedule_key):
            self._set_diagnostic_error(
                "native_preview_unavailable",
                f"Load the fixture's {schedule_type.title()} schedule before previewing it",
            )
            return False

        if self.preview_task is not None or self.preview_restore_values is not None:
            if not await self.async_stop_preview():
                return False
        if not await self._async_prepare_command():
            return False

        target_mode = "automatic" if schedule_type == "auto" else "professional"
        starting = not self.native_preview_active
        previous_type = self.native_preview_schedule_type
        if starting:
            current_mode = self.values.get("mode")
            self.native_preview_restore_mode = current_mode if current_mode in MODES else "manual"

        mode_changed = self.values.get("mode") != target_mode or previous_type not in (None, schedule_type)
        if mode_changed and (self._uses_wifi_protocol() or self._uses_plant_pro_protocol()):
            if not await self._async_send_packet(self._native_mode_packet(target_mode)):
                if starting:
                    self.native_preview_restore_mode = None
                return False
            self.values["mode"] = target_mode

        if self._uses_wifi_protocol():
            packet = protocol.wifi_auto_preview_packet(minute)
            native_protocol = "facebd"
        elif self._uses_plant_pro_protocol():
            packet = protocol.spp_schedule_preview_packet(minute)
            native_protocol = "plant_pro"
        else:
            levels = self._classic_native_preview_levels(schedule_type, minute)
            if levels is None:
                return False
            packet = protocol.old_auto_preview_packet(levels)
            native_protocol = "classic"

        if not await self._async_send_packet(packet):
            if starting:
                await self._async_restore_native_preview_mode()
            return False

        self.native_preview_active = True
        self.native_preview_schedule_type = schedule_type
        self.diagnostics.update(
            {
                "status": "native_preview_running",
                "native_preview_protocol": native_protocol,
                "native_preview_schedule_type": schedule_type,
                "preview_minute": minute,
                "preview_time": self._format_minute(minute),
            }
        )
        self._notify_diagnostics_throttled()
        return True

    @serialized_device_command(priority=True)
    async def async_stop_preview(self, *, restore: bool = True) -> bool:
        """Stop any running preview, optionally restoring its preceding state."""
        restored = True
        had_editor_preview = any(
            value is not None
            for value in (
                self.preview_task,
                self.preview_restore_mode,
                self.preview_restore_values,
            )
        )
        if self.preview_task and not self.preview_task.done():
            self.preview_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.preview_task
        self.preview_task = None
        restore_mode = self.preview_restore_mode
        restore_values = self.preview_restore_values
        self.preview_restore_mode = None
        self.preview_restore_values = None
        if restore and restore_mode is not None:
            restored = await self.async_select_option("mode", restore_mode)
        elif restore and restore_values:
            restored = await self.async_set_channels(restore_values)
        elif had_editor_preview:
            self.diagnostics["status"] = "preview_interrupted"

        if self.native_preview_active:
            if not await self._async_prepare_command():
                return False
            if self._uses_wifi_protocol():
                stopped = await self._async_send_packet(protocol.wifi_auto_preview_packet(None))
            elif self._uses_plant_pro_protocol():
                stopped = await self._async_send_packet(protocol.spp_schedule_preview_packet(None))
            else:
                stopped = await self._async_send_packet(protocol.old_auto_preview_packet(None))
            if not stopped:
                self._set_diagnostic_error("native_preview_stop_failed", "Unable to stop fixture schedule preview")
                return False
            if restore:
                if not await self._async_restore_native_preview_mode():
                    self._set_diagnostic_error(
                        "native_preview_restore_failed", "Preview stopped but fixture mode was not restored"
                    )
                    return False
                self.diagnostics["status"] = "native_preview_stopped"
            else:
                self.native_preview_active = False
                self.native_preview_schedule_type = None
                self.native_preview_restore_mode = None
                self.diagnostics["status"] = "native_preview_interrupted"
            self._notify_diagnostics_throttled()
        return restored

    async def _async_restore_native_preview_mode(self) -> bool:
        """Restore the fixture mode saved before native preview."""
        restore_mode = self.native_preview_restore_mode
        should_restore = self._uses_wifi_protocol() or self._uses_plant_pro_protocol()
        if restore_mode in MODES and should_restore and self.values.get("mode") != restore_mode:
            if not await self._async_send_packet(self._native_mode_packet(restore_mode)):
                return False
            self.values["mode"] = restore_mode
        self.native_preview_active = False
        self.native_preview_schedule_type = None
        self.native_preview_restore_mode = None
        return True

    async def _async_preview_schedule(
        self,
        points: list[dict[str, Any]],
        duration: int,
        step_seconds: int,
    ) -> None:
        """Run the schedule preview task."""
        normalized = self._normalize_schedule_points(points)
        if len(normalized) < 2:
            self._set_diagnostic_error(
                "preview_failed",
                "Schedule preview requires at least two points",
            )
            return

        steps = max(1, int(duration / max(1, step_seconds)))
        self.diagnostics.update(
            {
                "status": "preview_running",
                "schedule_points": normalized,
            }
        )
        for handler in self.updates_connect:
            handler()

        try:
            for step in range(steps + 1):
                minute = round((step / steps) * DAY_MINUTES) % DAY_MINUTES
                channels = self._interpolate_schedule(normalized, minute)
                self.diagnostics.update(
                    {
                        "status": "preview_running",
                        "preview_minute": minute,
                        "preview_time": self._format_minute(minute),
                        "spectrum": self._spectrum_report(channels),
                    }
                )
                await self.async_set_channels(channels)
                if step < steps:
                    await asyncio.sleep(step_seconds)
        except asyncio.CancelledError:
            self.diagnostics["status"] = "preview_stopped"
            raise
        else:
            self.diagnostics["status"] = "preview_complete"
        finally:
            for handler in self.updates_connect:
                handler()

    def _normalize_schedule_points(self, points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalize schedule points to minutes and channel values."""
        normalized = []
        for point in points:
            minute = self._parse_time_to_minute(str(point["time"]))
            channels = {
                channel: max(0, min(100, int(point.get(channel, point.get(color, 0)))))
                for channel, color in (
                    ("channel_1", "red"),
                    ("channel_2", "green"),
                    ("channel_3", "blue"),
                    ("channel_4", "white"),
                    ("channel_5", "channel_5"),
                )
            }
            normalized.append({"minute": minute, "time": self._format_minute(minute), **channels})

        return sorted(normalized, key=lambda item: item["minute"])

    def _interpolate_schedule(self, points: list[dict[str, Any]], minute: int) -> dict[str, int]:
        """Return interpolated channel values for one minute of the day."""
        previous = points[-1]
        next_point = points[0]
        for index, point in enumerate(points):
            if point["minute"] <= minute:
                previous = point
                next_point = points[(index + 1) % len(points)]

        start = previous["minute"]
        end = next_point["minute"]
        if end <= start:
            end += DAY_MINUTES
        current = minute if minute >= start else minute + DAY_MINUTES
        ratio = 0 if end == start else (current - start) / (end - start)

        return {
            channel: round(previous[channel] + ((next_point[channel] - previous[channel]) * ratio))
            for channel in NUMBERS
        }

    def _classic_native_preview_levels(
        self, schedule_type: str, minute: int, *, report_error: bool = True
    ) -> list[int] | None:
        """Calculate the APK's classic ``680B`` values from fixture readback."""
        if schedule_type == "professional":
            raw_points = self.values.get("native_pro_schedule")
            if not isinstance(raw_points, list):
                raw_points = []
            points = [
                {
                    "minute": int(point["minute"]) % DAY_MINUTES,
                    **{channel: max(0, min(100, int(point.get(channel, 0)))) for channel in NUMBERS},
                }
                for point in raw_points
                if isinstance(point, dict) and "minute" in point
            ]
        else:
            points = self._classic_auto_preview_points(self.values.get("native_auto_schedule"))

        if len(points) < 2:
            if report_error:
                self._set_diagnostic_error(
                    "native_preview_unavailable",
                    f"The fixture did not report a complete {schedule_type.title()} schedule",
                )
            return None
        points.sort(key=lambda point: point["minute"])
        channels = self._interpolate_schedule(points, minute)
        return [channels[channel] for channel in self.numbers()]

    def _classic_auto_preview_points(self, schedule: object) -> list[dict[str, Any]]:
        """Expand classic Auto readback into the points used by the APK preview."""
        if not isinstance(schedule, dict):
            return []
        sunrise = self._native_schedule_minute(schedule.get("sunrise"))
        sunset = self._native_schedule_minute(schedule.get("sunset"))
        sleep = self._native_schedule_minute(schedule.get("sleep"))
        day_levels = schedule.get("day_levels")
        night_levels = schedule.get("night_levels")
        if sunrise is None or sunset is None or not isinstance(day_levels, list) or not isinstance(night_levels, list):
            return []

        channel_count = len(self.numbers())
        if len(day_levels) < channel_count or len(night_levels) < channel_count:
            return []
        day = [max(0, min(100, int(value))) for value in day_levels[:channel_count]]
        night = [max(0, min(100, int(value))) for value in night_levels[:channel_count]]
        off = [0] * channel_count
        sunrise_ramp = max(0, min(240, int(schedule["sunrise"].get("ramp", 0))))
        sunset_ramp = max(0, min(240, int(schedule["sunset"].get("ramp", 0))))

        def point(point_minute: int, levels: list[int]) -> dict[str, Any]:
            return {
                "minute": point_minute % DAY_MINUTES,
                **{channel: levels[index] if index < len(levels) else 0 for index, channel in enumerate(NUMBERS)},
            }

        points = [
            point(sunrise, off if sleep is not None else night),
            point(sunrise + sunrise_ramp, day),
            point(sunset - sunset_ramp, day),
            point(sunset, night),
        ]
        if sleep is not None:
            # FluvalConnect uses two points at the sleep minute: retain night
            # until that minute, then switch off for the overnight segment.
            points.extend((point(sleep, night), point(sleep, off)))
        return points

    @staticmethod
    def _native_schedule_minute(value: object) -> int | None:
        """Read one protocol-neutral fixture time object."""
        if not isinstance(value, dict):
            return None
        hour = value.get("hour")
        minute = value.get("minute")
        if not isinstance(hour, int) or not isinstance(minute, int) or not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None
        return hour * 60 + minute

    def _spectrum_report(self, channels: dict[str, int]) -> dict[str, Any]:
        """Return graph-friendly spectrum data for diagnostics and previews."""
        color_values = {
            "red": channels["channel_1"],
            "green": channels["channel_2"],
            "blue": channels["channel_3"],
            "white": channels["channel_4"],
            "channel_5": channels["channel_5"],
        }
        return {
            "channels": color_values,
            "peak": max(color_values.values()),
            "total": sum(color_values.values()),
        }

    def _parse_time_to_minute(self, value: str) -> int:
        """Parse HH:MM into minutes from midnight."""
        hour, minute = value.split(":", 1)
        return ((int(hour) % 24) * 60) + int(minute)

    def _format_minute(self, minute: int) -> str:
        """Format minutes from midnight as HH:MM."""
        minute %= DAY_MINUTES
        return f"{minute // 60:02d}:{minute % 60:02d}"

    @serialized_device_command(priority=True)
    async def async_set_switch(self, attr: str, value: bool) -> bool:
        """Set switch values and send the updated state to the light."""
        _LOGGER.debug("Switch %s changed to %s", attr, value)
        old_values = dict(self.values)
        self.values[attr] = value
        if not await self._async_prepare_command():
            _LOGGER.warning("Cannot set Fluval switch before BLE device is available")
            self.values = old_values
            return False

        if self.values.get("mode") != "manual":
            if await self.async_ensure_mode("manual") != "manual":
                self.values = old_values
                return False
            self.mode_changed_by_write = True

        if self._uses_wifi_protocol():
            ok = await self._async_send_packet(protocol.wifi_switch_packet(value))
        elif self._uses_plant_pro_protocol():
            ok = await self._async_send_packet(protocol.spp_switch_packet(value))
        else:
            ok = await self._async_send_packet(protocol.old_switch_packet(value))

        if not ok:
            self.values = old_values
            for handler in self.updates_component:
                handler()
        elif attr == "led_on_off" and not value and self.values.get("effect"):
            self._clear_effect_state()
            for handler in self.updates_component:
                handler()
        return ok

    @serialized_device_command(priority=True)
    async def async_set_daylight_saving_time(self, enabled: bool) -> bool:
        """Set the fixture-owned FACEBD daylight-saving flag."""
        if not await self._async_prepare_command():
            _LOGGER.warning("Cannot set Fluval daylight-saving time before BLE is available")
            return False
        if not self._uses_wifi_protocol():
            self._set_diagnostic_error(
                "unsupported_daylight_saving_time",
                "Daylight-saving control is supported only by FACEBD fixtures",
            )
            return False

        previous = self.values.get("daylight_saving_time")
        self.values["daylight_saving_time"] = enabled
        if not await self._async_send_packet(protocol.wifi_dst_packet(enabled)):
            if isinstance(previous, bool):
                self.values["daylight_saving_time"] = previous
            else:
                self.values.pop("daylight_saving_time", None)
            for handler in self.updates_component:
                handler()
            return False

        self.diagnostics["daylight_saving_time"] = enabled
        return True

    def _manual_preset_values(self, slot: int) -> list[int] | None:
        """Return one complete classic preset from fixture readback."""
        presets = self.values.get("native_manual_presets")
        channel_count = self._resolved_channel_count()
        if (
            not isinstance(presets, list)
            or len(presets) != 4
            or not isinstance(presets[slot - 1], list)
            or len(presets[slot - 1]) != channel_count
        ):
            return None
        return [int(value) for value in presets[slot - 1]]

    @serialized_device_command(priority=True)
    async def async_recall_manual_preset(self, slot: int) -> bool:
        """Apply one fixture-resident classic P1-P4 preset as FluvalConnect does."""
        if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 4:
            self._set_diagnostic_error("invalid_manual_preset", "Manual preset slot must be between 1 and 4")
            return False
        if not await self._async_prepare_command():
            return False
        if self._uses_wifi_protocol() or self._uses_plant_pro_protocol():
            self._set_diagnostic_error(
                "unsupported_manual_preset",
                "Fixture-resident manual presets are supported only by classic Fluval controllers",
            )
            return False

        preset = self._manual_preset_values(slot)
        if preset is None:
            await self.async_refresh_state()
            preset = self._manual_preset_values(slot)
        if preset is None:
            self._set_diagnostic_error(
                "manual_preset_unavailable",
                "Manual preset readback is unavailable; put the fixture in Manual mode and retry",
            )
            return False
        if not self.values.get("led_on_off"):
            self._set_diagnostic_error(
                "manual_preset_requires_light_on",
                "Turn on the fixture before recalling a manual preset",
            )
            return False

        targets = {channel: int(preset[index]) for index, channel in enumerate(self.numbers())}
        if not await self.async_set_channels(targets, force=True):
            return False
        self.diagnostics.update(
            {
                "status": "manual_preset_recalled",
                "manual_preset_slot": slot,
                "last_error": None,
            }
        )
        return True

    @serialized_device_command(priority=True)
    async def async_save_manual_preset(self, slot: int) -> bool:
        """Save the current classic channel state in fixture slot P1-P4."""
        if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 4:
            self._set_diagnostic_error("invalid_manual_preset", "Manual preset slot must be between 1 and 4")
            return False
        if not await self._async_prepare_command():
            return False
        if self._uses_wifi_protocol() or self._uses_plant_pro_protocol():
            self._set_diagnostic_error(
                "unsupported_manual_preset",
                "Fixture-resident manual presets are supported only by classic Fluval controllers",
            )
            return False
        if self.values.get("mode") != "manual":
            self._set_diagnostic_error(
                "manual_preset_requires_manual_mode",
                "Select Manual mode before saving a fixture preset",
            )
            return False
        if not await self._async_send_packet(protocol.old_save_manual_preset_packet(slot - 1)):
            return False

        presets = self.values.get("native_manual_presets")
        channel_count = self._resolved_channel_count()
        if (
            isinstance(presets, list)
            and len(presets) == 4
            and all(isinstance(preset, list) and len(preset) == channel_count for preset in presets)
        ):
            updated_presets = [list(preset) for preset in presets]
            updated_presets[slot - 1] = self._channel_values()
            self.values["native_manual_presets"] = updated_presets
            self.diagnostics["native_manual_presets"] = updated_presets
        self.diagnostics.update(
            {
                "status": "manual_preset_saved",
                "manual_preset_slot": slot,
                "last_error": None,
            }
        )
        return True

    @serialized_device_command(priority=True)
    async def async_identify(self) -> bool:
        """Ask the fixture to identify itself using FluvalConnect's Find command."""
        if not await self._async_prepare_command():
            _LOGGER.warning("Cannot identify Fluval light before BLE device is available")
            return False

        if self._uses_wifi_protocol():
            packet = protocol.wifi_find_packet()
        elif self._uses_plant_pro_protocol():
            packet = protocol.spp_find_packet()
        else:
            packet = protocol.old_find_packet()
        return await self._async_send_packet(packet)

    @serialized_device_command(priority=True)
    async def async_select_option(self, attr: str, option: str) -> bool:
        """Set select values and send the updated state to the light."""
        if attr != "mode" or option not in MODES:
            return False

        _LOGGER.debug("Mode changed to %s", option)
        old_values = dict(self.values)
        self.values[attr] = option
        if not await self._async_prepare_command():
            _LOGGER.warning("Cannot set Fluval mode before BLE device is available")
            self.values = old_values
            return False

        if self._uses_wifi_protocol():
            ok = await self._async_send_packet(protocol.wifi_mode_packet(MODE_TO_CODE[option]))
        elif self._uses_plant_pro_protocol():
            ok = await self._async_send_packet(protocol.spp_mode_packet(MODE_TO_CODE[option]))
        else:
            ok = await self._async_send_packet(protocol.old_mode_packet(MODE_TO_CODE[option]))

        if not ok:
            self.values = old_values
            for handler in self.updates_component:
                handler()
        else:
            self.mode_changed_by_write = False
        return ok

    @serialized_device_command
    async def async_ensure_mode(self, mode: str) -> str | None:
        """Ensure the fixture confirms `mode`, writing only when it must change.

        Returns the freshly confirmed mode string (which may differ from
        `mode` if the fixture did not take the write) or None if the write
        or its confirmation could not be completed at all (unreachable, or
        the on-demand connect itself failed).
        Raises ValueError for a `mode` outside MODES - that is a programming
        error, not a runtime BLE failure.
        """
        if mode not in MODES:
            raise ValueError(f"Unknown Fluval mode: {mode!r}")
        if self.values.get("mode") == mode:
            return mode
        if not await self._async_prepare_command():
            return None
        if await self._async_send_packet(self._native_mode_packet(mode)):
            self.mode_changed_by_write = False
        confirmed = self.values.get("mode")
        return confirmed if isinstance(confirmed, str) else None

    async def _async_on_client_ready(self) -> None:
        """Send the APK's clock command before the initial parameter read."""
        async with self._clock_sync_lock:
            if self._clock_synced or self._clock_sync_started:
                return
            self._clock_sync_started = await self._async_send_clock_command()
            if not self._clock_sync_started:
                _LOGGER.warning("Fluval clock sync failed after connect for %s", self.address)

    async def _async_on_client_state_ready(self, state: dict[int, object]) -> None:
        """Finish APK initialization after the initial parameter read."""
        async with self._clock_sync_lock:
            if self._clock_synced or not self._clock_sync_started:
                return
            if not await self._async_finish_clock_sync(state):
                _LOGGER.warning("Fluval timezone sync failed after connect for %s", self.address)

    @serialized_device_command
    async def async_sync_clock(self, *, force: bool = False, priority: bool = False) -> bool:
        """Run the APK's clock, state-read, and timezone initialization sequence.

        `priority` is read by `serialized_device_command`: the Sync clock
        button passes True, the guardian's own per-check clock sync leaves
        it False.
        """
        if self._clock_synced and not force:
            return True

        async with self._clock_sync_lock:
            if self._clock_synced and not force:
                return True

            if self.client is None:
                if not await self._async_ensure_client():
                    return False
            elif not await self.client.ensure_connected():
                self._set_diagnostic_error(
                    "clock_sync_failed",
                    self.client.last_error or "Unable to connect for clock sync",
                )
                return False

            self._clock_sync_started = await self._async_send_clock_command()
            if not self._clock_sync_started:
                self._set_diagnostic_error("clock_sync_failed", "Unable to sync lamp clock")
                return False

            try:
                await self.client.request_state()
            except (TimeoutError, BleakError) as err:
                _LOGGER.debug("Unable to read Fluval state during clock sync", exc_info=err)

            return await self._async_finish_clock_sync(self.client.observed_state)

    async def _async_send_clock_command(self) -> bool:
        """Send only the fixture clock command used before the APK state read."""
        if self._uses_wifi_protocol():
            packet = protocol.wifi_clock_packet()
        elif self._uses_plant_pro_protocol():
            # FluvalConnect treats Plant Pro as a mesh light and writes the
            # raw 0xCD + local date/time frame to its FFF2 SPP endpoint.
            packet = protocol.mesh_clock_packet()
        else:
            packet = protocol.old_clock_packet()
        return await self._async_send_packet(packet, verify=False)

    async def _async_finish_clock_sync(self, state: dict[int, object]) -> bool:
        """Apply the APK's FACEBD timezone follow-up and record completion."""
        if self._uses_wifi_protocol() and protocol.WIFI_TZ_OFFSET_KEY in state:
            if not await self._async_send_packet(protocol.wifi_timezone_packet(), verify=False):
                self._set_diagnostic_error("clock_sync_failed", "Unable to sync lamp timezone")
                return False

        self._clock_synced = True
        self._clock_sync_started = False
        self.diagnostics.update(
            {
                "status": "clock_synced",
                "clock_synced_at": datetime.now(UTC).isoformat(),
                "last_error": None,
            }
        )
        for handler in self.updates_connect:
            handler()
        return True

    def _uses_plant_pro_protocol(self) -> bool:
        """Return true for the live Plant Pro 4.0 SPP-over-BLE profile."""
        return bool(self.client is not None and getattr(self.client, "plant_pro_spp", False) is True)

    def _uses_wifi_protocol(self) -> bool:
        """Prefer the live GATT profile over advertisement heuristics."""
        if self.client is not None and getattr(self.client, "command_write_uuid", None):
            if self._uses_plant_pro_protocol():
                self.facebd = False
                return False
            if getattr(self.client, "wifi_facebd", False):
                self.facebd = True
                return True
            write_uuid = self.client.command_write_uuid.lower()
            if write_uuid.startswith("facebd"):
                self.facebd = True
                return True
            if write_uuid.startswith(("00001001", "0000fff2")):
                self.facebd = False
                return False

        return self.facebd

    def _native_mode_packet(self, mode: str) -> bytes:
        """Build the mode command for the active fixture protocol."""
        mode_code = MODE_TO_CODE[mode]
        if self._uses_wifi_protocol():
            return protocol.wifi_mode_packet(mode_code)
        if self._uses_plant_pro_protocol():
            return protocol.spp_mode_packet(mode_code)
        return protocol.old_mode_packet(mode_code)

    async def _async_prepare_command(self) -> bool:
        """Resolve the BLE device and connect far enough to know the protocol."""
        if not await self._async_ensure_client() or self.client is None:
            self._set_diagnostic_error("device_not_found", "BLE device is not available")
            return False
        client = self.client
        ok = await client.ensure_connected()
        if not ok:
            self._set_diagnostic_error(
                "connect_failed",
                client.last_error or "Unable to connect to BLE device",
            )
        return ok

    async def _async_send_packet(self, packet: bytes, *, verify: bool = True) -> bool:
        """Send one already-built command packet to the controller."""
        if not await self._async_ensure_client() or self.client is None:
            _LOGGER.warning("Cannot send Fluval state before BLE device is available")
            return False
        client = self.client

        _LOGGER.debug(
            "Sending Fluval packet via %s (facebd=%s raw=%s): %s",
            client.command_write_uuid,
            self.facebd,
            client.raw_facebd,
            packet.hex(),
        )
        expected_state = self._expected_state_for_packet(packet)
        classic_target = self._classic_confirmation_target(packet) if verify else None
        if not await client.send_now(packet, expected_state=expected_state, verify=verify):
            self._set_diagnostic_error(
                "write_failed",
                client.last_error or "BLE write failed",
            )
            return False

        write_verified = client.last_write_verified
        if classic_target is not None:
            write_verified = await self._async_confirm_classic_state(classic_target)
            if not write_verified:
                self._set_diagnostic_error(
                    "write_unconfirmed",
                    f"Fluval did not confirm {', '.join(classic_target)} after the write",
                )
                return False

        self.diagnostics.update(
            {
                "status": ("last_write_verified" if write_verified else "last_write_unverified"),
                "last_write_at": datetime.now(UTC).isoformat(),
                "last_write_packet": packet.hex(),
                "last_write_targets": list(client.last_write_targets),
                "last_write_verified": write_verified,
                "connection_profile": client.profile,
                "command_write_uuid": client.command_write_uuid,
                "last_expected_state": (
                    dict(classic_target) if classic_target is not None else dict(client.last_expected_state)
                ),
                "last_confirmed_state": (
                    {key: self.values.get(key) for key in classic_target}
                    if classic_target is not None
                    else dict(client.last_confirmed_state)
                ),
                "last_verification_mismatches": dict(client.last_verification_mismatches),
                "last_error": None,
            }
        )

        self.touch_seen(notify=False)
        for handler in self.updates_component:
            handler()
        for handler in self.updates_connect:
            handler()
        return True

    def _classic_confirmation_target(self, packet: bytes) -> dict[str, Any] | None:
        """Return the field(s) a classic write must confirm, or None if unconfirmable.

        Only the classic (legacy-encrypted) transport reaches this path -
        FACEBD/SPP writes are already confirmed through
        `_expected_state_for_packet` and the CBOR-keyed verification in
        `Client.send_now`.
        """
        if self.client is None or self.client.raw_facebd or len(packet) < 3 or packet[0] != 0x68:
            return None
        opcode = packet[1]
        if opcode == protocol.OLD_MODE and packet[2] < len(MODES):
            return {"mode": MODES[packet[2]]}
        if opcode == protocol.OLD_SWITCH:
            return {"led_on_off": bool(packet[2])}
        if opcode == protocol.OLD_ALL_ZONE:
            return {channel: int(self.values.get(channel, 0)) for channel in self.numbers()}
        return None

    async def _async_confirm_classic_state(self, expected: dict[str, Any]) -> bool:
        """Re-read classic state and confirm it reflects `expected` after a write."""
        client = self.client
        if client is None:
            return False
        try:
            await client.request_state()
        except (TimeoutError, BleakError) as err:
            _LOGGER.debug("Unable to read Fluval state to confirm a command", exc_info=err)
            return False
        if "mode" in expected:
            return self.values.get("mode") == expected["mode"]
        if self.values.get("mode") != "manual":
            # Auto/Pro bodies never carry power or channel levels, so a mode
            # that moved away from manual cannot confirm either one here.
            return False
        return all(self.values.get(key) == value for key, value in expected.items())

    def _expected_state_for_packet(self, packet: bytes) -> dict[int, Any] | None:
        """Return exact supported FACEBD values expected after a command."""
        if self.client is None or not self.client.raw_facebd:
            return None
        try:
            decoded = protocol.decode_cbor_update(packet)
        except ValueError:
            return None
        if not decoded:
            return None
        if self._uses_plant_pro_protocol():
            supported_keys = {
                protocol.SPP_MODE_KEY,
                protocol.SPP_SWITCH_KEY,
                *(protocol.SPP_CHANNEL_KEYS[index] for index, _channel in enumerate(self.numbers())),
                protocol.SPP_AUTO_SUNRISE_KEY,
                protocol.SPP_AUTO_SUNSET_KEY,
                protocol.SPP_AUTO_SLEEP_KEY,
                protocol.SPP_AUTO_DAY_LEVELS_KEY,
                protocol.SPP_AUTO_NIGHT_LEVELS_KEY,
                protocol.SPP_PRO_SCHEDULE_KEY,
                protocol.SPP_EFFECT_KEY,
                protocol.SPP_EFFECT_SCHEDULE_KEY,
            }
        else:
            supported_keys = {
                protocol.WIFI_MODE_KEY,
                protocol.WIFI_SWITCH_KEY,
                protocol.WIFI_DST_KEY,
                *(protocol.WIFI_CHANNEL_KEYS[index] for index, _channel in enumerate(self.numbers())),
            }
        return {key: value for key, value in decoded.items() if key in supported_keys}

    @serialized_device_command
    async def async_refresh_state(self, *, priority: bool = False) -> bool:
        """Resolve the controller and request its current state.

        `priority` is read by `serialized_device_command`: the schedule
        dashboard's explicit refresh passes True, background callers do not.
        """
        if not await self._async_ensure_client() or self.client is None:
            return False
        client = self.client

        if not await client.ensure_connected():
            return False

        try:
            await client.request_state()
        except (TimeoutError, BleakError) as err:
            _LOGGER.debug("Unable to refresh Fluval state", exc_info=err)
            return False

        return True

    @serialized_device_command
    async def async_read_state(self) -> FluvalState | None:
        """Read and return a confirmed snapshot of the fixture's on-device state.

        Bypasses `async_refresh_state`'s HA-bluetooth-component device lookup
        once a client already exists, reusing Client's own device_provider-
        based route refresh directly. The very first call (no client yet,
        connect-on-demand) goes through
        `_async_ensure_client()` once to create one; every call after that
        talks to `self.client` directly.
        """
        if self.client is None and not await self._async_ensure_client():
            return None
        if self.client is None:
            return None
        try:
            await self.client.request_state()
        except (TimeoutError, BleakError) as err:
            _LOGGER.debug("Unable to read Fluval state", exc_info=err)
            return None
        self._last_state_at = time.time()
        mode = self.values.get("mode")
        if not isinstance(mode, str):
            return None
        if mode == "manual":
            power: bool | None = bool(self.values.get("led_on_off"))
            levels: dict[str, int] | None = {channel: int(self.values.get(channel, 0)) for channel in self.numbers()}
        else:
            power = None
            levels = None
        return FluvalState(
            mode=mode,
            power=power,
            levels=levels,
            auto_schedule=self.values.get("native_auto_schedule") if mode == "automatic" else None,
            pro_schedule=self.values.get("native_pro_schedule") if mode == "professional" else None,
            last_state_at=self._last_state_at,
            connection_attempts=self.connection_attempts,
            scanner_source=self.scanner_source,
        )

    async def async_collect_diagnostics(self) -> dict[str, Any]:
        """Collect a practical snapshot without changing the BLE session."""
        now = datetime.now(UTC)
        report: dict[str, Any] = {
            "status": "ok",
            "checked_at": now.isoformat(),
            "configured_mac": self.address,
            "name": self.name,
            "model": self.model_name,
            "lamp_profile": self.lamp_profile,
            "channel_count": self._resolved_channel_count(),
            "facebd": self.facebd,
            "connected": self.connected,
            "controls_available": self.controls_available,
            "schedule_mode": self.schedule_mode,
            "connection_options": {
                "ping_interval": self._ping_interval,
                "active_time": self._active_time,
            },
            "values": dict(self.values),
            "connection_info": dict(self.conn_info),
            "last_diagnostics": dict(self.diagnostics),
            "active_connection": {
                "source": self.conn_info.get("active_connection_source_address"),
                "source_name": self.conn_info.get("active_connection_source"),
                "source_type": self.conn_info.get("active_connection_source_type"),
                "connected_at": self.conn_info.get("active_connection_connected_at"),
                "gatt_connected": self.connected,
                "rssi": self.conn_info.get("rssi"),
                "rssi_updated_at": self.conn_info.get("rssi_updated_at"),
            },
            "latest_advertisement": {
                "source": self.conn_info.get("advertisement_source_address"),
                "source_name": self.conn_info.get("advertisement_source"),
                "source_type": self.conn_info.get("advertisement_source_type"),
                "rssi": self.conn_info.get("advertisement_rssi"),
                "received_at": self.conn_info.get("advertisement_updated_at"),
            },
        }

        if self.client is not None:
            report["gatt"] = {
                "profile": self.client.profile,
                "wifi_facebd": self.client.wifi_facebd,
                "plant_pro_spp": self.client.plant_pro_spp,
                "raw_facebd": self.client.raw_facebd,
                "command_write_uuid": self.client.command_write_uuid,
                "notify_uuids": list(self.client.notify_uuids),
                "last_error": self.client.last_error,
                "last_write_targets": list(self.client.last_write_targets),
                "last_write_verified": self.client.last_write_verified,
            }

        if self.hass is not None:
            service_info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
            if service_info is None:
                service_info = bluetooth.async_last_service_info(self.hass, self.address)
            report["ha_ble_cache"] = service_info is not None
            if service_info is not None:
                report["advertisement_name"] = service_info.device.name
                report["advertisement_rssi"] = service_info.advertisement.rssi
                report["advertisement_service_uuids"] = list(service_info.advertisement.service_uuids)
                report["service_data"] = dict(service_info.advertisement.service_data)
                report["manufacturer_data"] = dict(service_info.advertisement.manufacturer_data)

        return report

    def _channel_values(self) -> list[int]:
        """Return supported channel values in Fluval app order."""
        return [self.values[channel] for channel in self.numbers()]

    def _new_client(self, device: BLEDevice) -> Client:
        """Create a client that refreshes HA's preferred BLE route on reconnect."""
        return Client(
            device,
            self.set_connected,
            self.decode_update_packet,
            ping_interval=self._ping_interval,
            active_time=self._active_time,
            device_provider=self._connectable_ble_device,
            connection_ready_callback=self._record_active_connection_source,
            ready_callback=self._async_on_client_ready,
            state_ready_callback=self._async_on_client_state_ready,
            activity_callback=self._on_client_activity,
            hold_stats=self.hold_stats,
        )

    async def _async_ensure_client(self) -> bool:
        """Create or refresh a client using HA's best connectable BLE route.

        Always allowed to connect - this is the sole place a `Client` is
        created, so a Guardian check or command on an idle install connects
        on demand instead of the fixture's single BLE slot being grabbed
        eagerly at startup.
        """
        if not self.address:
            return False

        device = await self._async_find_device()

        if device is None:
            return self.client is not None

        self._update_from_ble_device(device)
        if self.client is None:
            self.client = self._new_client(device)
        else:
            self.client.device = device
        return True

    async def _async_find_device(self) -> BLEDevice | None:
        """Find the configured device through HA, including ESPHome proxies."""
        if self.hass is not None:
            return self._connectable_ble_device()

        for attempt in range(1, BLE_LOOKUP_RETRIES + 1):
            try:
                device = await BleakScanner.find_device_by_address(self.address, timeout=BLE_LOOKUP_TIMEOUT)
            except (TimeoutError, BleakError) as err:
                _LOGGER.debug(
                    "Unable to resolve Fluval device by address, attempt %s",
                    attempt,
                    exc_info=err,
                )
                await asyncio.sleep(attempt)
                continue
            if device is not None:
                return device

        return None

    def _connectable_ble_device(self) -> BLEDevice | None:
        """Ask HA for the best local adapter or ESPHome proxy route."""
        if self.hass is not None:
            device = bluetooth.async_ble_device_from_address(
                self.hass,
                self.address,
                connectable=True,
            )
            if device is not None:
                return device
            service_info = bluetooth.async_last_service_info(
                self.hass,
                self.address,
                connectable=True,
            )
            if service_info is not None:
                return service_info.device
        return self.client.device if self.client is not None else None

    def _set_diagnostic_error(self, status: str, message: str) -> None:
        """Store command failures for downloadable diagnostics."""
        self.diagnostics.update(
            {
                "status": status,
                "last_error": message,
                "last_error_at": datetime.now(UTC).isoformat(),
                "configured_mac": self.address,
                "known_connection_info": dict(self.conn_info),
            }
        )
        for handler in self.updates_connect:
            handler()

    def _update_from_ble_device(self, device: BLEDevice) -> None:
        """Populate metadata from a directly resolved BLEDevice."""
        self.address = device.address
        self.conn_info["mac"] = device.address
        details = device.details if isinstance(device.details, dict) else {}
        props = details.get("props", {})
        self.touch_seen(rssi=props.get("RSSI"), notify=False)

        service_uuids = list(props.get("UUIDs", self.conn_info.get("service_uuids", [])))
        self.conn_info["service_uuids"] = service_uuids
        self.facebd = self._uses_facebd_protocol(
            device.name,
            service_uuids,
            props.get("ServiceData", {}),
            props.get("ManufacturerData", {}),
        )
        self._notify_diagnostics_throttled()

    def _uses_facebd_protocol(
        self,
        name: str | None,
        service_uuids: list[str],
        service_data: dict,
        manufacturer_data: dict,
    ) -> bool:
        """Return true only when advertisements expose the FACEBD protocol.

        Fluval manufacturer data is shared by classic and FACEBD controllers,
        so it is vendor evidence for discovery but never protocol evidence.
        """
        if any(uuid.lower().startswith("facebd") for uuid in service_uuids):
            return True

        if any(str(uuid).lower().startswith("facebd") for uuid in service_data):
            return True

        return False

    def decode_update_packet(self, data: bytes | bytearray) -> bool:
        """Decode the received Fluval packet and sort into values."""
        if data and data[0] == protocol.SPP_STATUS_HEADER:
            try:
                cbor = protocol.decode_cbor_update(data)
            except ValueError as err:
                _LOGGER.debug("Ignoring unsupported Plant Pro CBOR packet", exc_info=err)
                return False
            if cbor is not None:
                return self._decode_plant_pro_update(cbor)
            return False

        is_cbor_map = bool(data and data[0] >> 5 == 5)
        if is_cbor_map:
            try:
                cbor = protocol.decode_cbor_map(data)
            except ValueError as err:
                _LOGGER.debug("Ignoring unsupported Fluval CBOR packet", exc_info=err)
                return False

            if cbor is not None:
                return self._decode_wifi_update(cbor)
            return False

        channel_count = self._resolved_channel_count()
        decoded = protocol.decode_old_state_packet(data, channel_count=channel_count)
        if decoded is None:
            _LOGGER.debug("Ignoring invalid classic Fluval state packet: %s", data.hex())
            return False

        mode = int(decoded["mode"])
        body = decoded["body"]
        self.values["mode"] = MODES[mode]

        if self.values["mode"] == "manual":
            self.values["led_on_off"] = bool(decoded["power"])
            if self.supports_classic_effects():
                self.values["effect"] = self._native_effect_name(int(decoded["effect_id"]))
            presets = [list(preset) for preset in decoded["presets"]]
            self.values["native_manual_presets"] = presets
            self.diagnostics.update(
                {
                    "native_manual_presets": presets,
                    "native_manual_presets_readback_at": datetime.now(UTC).isoformat(),
                }
            )
            # Wire scale is 0-1000 (percent * 10); HA entities use 0-100.
            channels = decoded["channels"]
            self._channel_count_hint = channel_count
            for index, raw in enumerate(channels):
                self.values[f"channel_{index + 1}"] = max(0, min(100, round(raw / 10)))
            for index in range(len(channels), 5):
                self.values[f"channel_{index + 1}"] = 0
        elif self.values["mode"] == "automatic":
            auto_schedule = protocol.decode_old_auto_schedule(body, channel_count=channel_count)
            self._record_native_schedule_readback(protocol_name="classic", auto=auto_schedule)
            self._record_native_effect_schedule_readback(
                protocol_name="classic",
                windows=protocol.decode_old_effect_schedule(body, channel_count=channel_count),
            )
        elif self.values["mode"] == "professional":
            pro_schedule = protocol.decode_old_pro_schedule(body, channel_count=channel_count)
            self._record_native_schedule_readback(protocol_name="classic", professional=pro_schedule)
            self._record_native_effect_schedule_readback(
                protocol_name="classic",
                windows=protocol.decode_old_effect_schedule(body, channel_count=channel_count),
            )

        _LOGGER.debug(
            "led: %s mode: %s channels: %s / %s / %s / %s / %s",
            self.values["led_on_off"],
            self.values["mode"],
            self.values["channel_1"],
            self.values["channel_2"],
            self.values["channel_3"],
            self.values["channel_4"],
            self.values["channel_5"],
        )

        for handler in self.updates_component:
            handler()
        return True

    def _decode_wifi_update(self, data: dict[int, Any]) -> bool:
        """Decode a FACEBD WiFi-over-BLE CBOR state update."""
        updated = False
        if protocol.WIFI_FIRMWARE_VERSION_KEY in data:
            updated = self._store_firmware_version(data[protocol.WIFI_FIRMWARE_VERSION_KEY]) or updated

        if protocol.WIFI_MODE_KEY in data:
            mode = data[protocol.WIFI_MODE_KEY]
            if isinstance(mode, int) and 0 <= mode < len(MODES):
                self.values["mode"] = MODES[mode]
                updated = True

        if protocol.WIFI_SWITCH_KEY in data:
            self.values["led_on_off"] = bool(data[protocol.WIFI_SWITCH_KEY])
            updated = True

        if protocol.WIFI_DST_KEY in data and isinstance(data[protocol.WIFI_DST_KEY], bool):
            self.values["daylight_saving_time"] = data[protocol.WIFI_DST_KEY]
            self.diagnostics["daylight_saving_time"] = data[protocol.WIFI_DST_KEY]
            updated = True

        if (
            self.supports_facebd_effects()
            and protocol.WIFI_MANUAL_KEY in data
            and isinstance(data[protocol.WIFI_MANUAL_KEY], int)
        ):
            effect_code = data[protocol.WIFI_MANUAL_KEY]
            self.values["effect"] = self._native_effect_name(effect_code) if effect_code else None
            updated = True

        present = 0
        for channel, key in zip(NUMBERS, protocol.WIFI_CHANNEL_KEYS, strict=False):
            if key in data and isinstance(data[key], int):
                self.values[channel] = max(0, min(100, int(data[key])))
                present += 1
                updated = True
        if isinstance(data.get(protocol.WIFI_CHANNEL_KEYS[4]), int):
            self._channel_count_hint = 5
        elif present >= 4:
            self._channel_count_hint = 4

        unambiguous_facebd_schedule_keys = (
            protocol.WIFI_AUTO_SUNSET_KEY,
            protocol.WIFI_AUTO_SLEEP_KEY,
            protocol.WIFI_AUTO_DAY_LEVELS_KEY,
            protocol.WIFI_AUTO_NIGHT_LEVELS_KEY,
            protocol.WIFI_PRO_COUNT_KEY,
            protocol.WIFI_PRO_TIMES_KEY,
            protocol.WIFI_PRO_LEVELS_KEY,
            protocol.WIFI_SCHEDULED_EFFECT_KEY,
        )
        has_auto_sunrise = isinstance(data.get(protocol.WIFI_AUTO_SUNRISE_KEY), list)
        if has_auto_sunrise or any(key in data for key in unambiguous_facebd_schedule_keys):
            auto_schedule = protocol.decode_wifi_auto_schedule(data)
            pro_schedule = protocol.decode_wifi_pro_schedule(data, channel_count=self._resolved_channel_count())
            updated = (
                self._record_native_schedule_readback(
                    protocol_name="facebd",
                    auto=auto_schedule,
                    professional=pro_schedule,
                )
                or updated
            )
            updated = (
                self._record_native_effect_schedule_readback(
                    protocol_name="facebd",
                    windows=protocol.decode_wifi_effect_schedule(data),
                )
                or updated
            )

        if updated:
            for handler in self.updates_component:
                handler()
        return updated

    def _decode_plant_pro_update(self, data: dict[int, Any]) -> bool:
        """Decode a Plant Pro 4.0 D2 status map."""
        updated = False
        if protocol.SPP_FIRMWARE_VERSION_KEY in data:
            updated = self._store_firmware_version(data[protocol.SPP_FIRMWARE_VERSION_KEY]) or updated

        if protocol.SPP_MODE_KEY in data:
            mode = data[protocol.SPP_MODE_KEY]
            if isinstance(mode, int) and 0 <= mode < len(MODES):
                self.values["mode"] = MODES[mode]
                updated = True

        if protocol.SPP_SWITCH_KEY in data:
            self.values["led_on_off"] = bool(data[protocol.SPP_SWITCH_KEY])
            updated = True

        present = 0
        for channel, key in zip(NUMBERS, protocol.SPP_CHANNEL_KEYS, strict=False):
            if key in data and isinstance(data[key], int):
                self.values[channel] = max(0, min(100, int(data[key])))
                present += 1
                updated = True
        if present:
            self._channel_count_hint = 5 if present >= 5 else 4

        if protocol.SPP_EFFECT_KEY in data and isinstance(data[protocol.SPP_EFFECT_KEY], int):
            effect_code = data[protocol.SPP_EFFECT_KEY]
            self.values["effect"] = self._native_effect_name(effect_code) if effect_code else None
            updated = True

        auto_schedule = protocol.decode_spp_auto_schedule(data)
        pro_schedule = protocol.decode_spp_pro_schedule(data)
        if self._record_native_schedule_readback(
            protocol_name="plant_pro",
            auto=auto_schedule,
            professional=pro_schedule,
        ):
            updated = True
        if auto_schedule is not None:
            self.diagnostics["plant_pro_auto_schedule"] = auto_schedule
        if pro_schedule is not None:
            self.diagnostics["plant_pro_pro_schedule"] = pro_schedule

        effect_schedule = protocol.decode_spp_effect_schedule(data)
        if self._record_native_effect_schedule_readback(
            protocol_name="plant_pro",
            windows=effect_schedule,
        ):
            updated = True

        if updated:
            for handler in self.updates_component:
                handler()
        return updated

    def _store_firmware_version(self, value: Any) -> bool:
        """Store a locally reported fixture firmware version."""
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False

        firmware_version = str(value)
        changed = firmware_version != self.firmware_version
        self.firmware_version = firmware_version
        self.diagnostics["firmware_version"] = firmware_version
        return changed
