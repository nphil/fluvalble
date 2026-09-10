"""Tests for the recovery repairs: reconciliation, the watcher, and the fix flow.

The bug this file exists for is an orphaned repair. Live on 2026-09-09
``44A6E570F18D_schedule_problem`` was raised at 12:22, the config entry
reloaded at 12:38, the guardian cleared the condition at 13:00 - and the
repair was still open 13 hours later, because deletion was gated on an
in-memory "previously synced" flag the reload had reset. So the tests here
assert what the issue registry, the entry's options, and the flow results
actually hold afterwards, never who called what in which order.
"""

import asyncio
from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ha_issue_registry

from custom_components.fluvalble import DOMAIN, FluvalRuntimeData, async_remove_entry, binary_sensor, repairs
from custom_components.fluvalble.core import CONF_LAST_HOLDING_PROXY, CONF_RECOVERY_OUTLET
from custom_components.fluvalble.core import recovery
from custom_components.fluvalble.core.device import Device
from custom_components.fluvalble.core.guardian import issue_id_for

MAC = "44:A6:E5:70:F1:8D"
SCHEDULE_ISSUE = "44A6E570F18D_schedule_problem"
UNREACHABLE_ISSUE = "44A6E570F18D_unreachable"
PROXY = "plant-room-bluetooth-proxy"
PROXY_ACTION = "plant_room_bluetooth_proxy_restart_proxy"
PROXY_SOURCE = "54:32:04:3E:F3:72"
# habluetooth names a remote scanner "<node> (<MAC>)" - verified live via
# bluetooth/subscribe_scanner_details on 2026-09-09. Slugified whole it
# matches no ESPHome action.
PROXY_SCANNER_NAME = f"{PROXY} ({PROXY_SOURCE})"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeGuardian:
    """Duck-typed guardian double with only what the repair paths read."""

    def __init__(self, *, problem: bool = False) -> None:
        self.problem = problem
        self.checks = 0
        self.overrides_ended = 0
        self.check_status = "ok"
        self._listeners: list = []

    def add_listener(self, callback):
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    def notify(self):
        for callback in list(self._listeners):
            callback()

    async def async_end_override(self):
        self.overrides_ended += 1
        return None

    async def async_check(self):
        self.checks += 1
        self.problem = False
        return self.check_status


class _FakeConfigEntries:
    def __init__(self, entries=()):
        self._entries = list(entries)
        self.updates = 0
        self.reloaded: list[str] = []

    def async_entries(self, domain=None):
        return list(self._entries)

    def async_get_entry(self, entry_id):
        return next((entry for entry in self._entries if entry.entry_id == entry_id), None)

    def async_update_entry(self, entry, *, options=None, **kwargs):
        self.updates += 1
        if options is not None:
            entry.options = dict(options)

    async def async_reload(self, entry_id):
        self.reloaded.append(entry_id)


class _FakeServices:
    def __init__(self, services=None):
        self._services = services or {}
        self.calls: list[tuple[str, str, str | None]] = []

    def has_service(self, domain, service):
        return service in self._services.get(domain, {})

    async def async_call(self, domain, service, data=None, blocking=False):
        self.calls.append((domain, service, (data or {}).get("entity_id")))


class _FakeHass:
    def __init__(self, entries=(), services=None):
        self.config_entries = _FakeConfigEntries(entries)
        self.services = _FakeServices(services)
        self.data = {DOMAIN: {}}


class _FakeClock:
    """Stand in for recovery.monotonic so a test can move the outage clock.

    Seconds only ever move forward, the way the live measurement did:
    the watcher must never be handed a clock it can restart by being
    rebuilt.
    """

    def __init__(self, now: float = 100_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Countdown:
    """Stand in for async_call_later, on the same fake clock as the watcher.

    `fire()` behaves like the real timer: it moves the clock to the armed
    deadline first, so a test cannot make the deadline pass without the
    outage clock agreeing that it has.
    """

    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self.delay: float | None = None
        self.deadline: float | None = None
        self.callback = None
        self.cancellations = 0

    def schedule(self, hass, delay, callback):
        self.delay = delay
        self.deadline = self.clock.now + delay
        self.callback = callback
        return self._cancel

    def _cancel(self):
        self.cancellations += 1
        self.callback = None

    def fire(self):
        callback, self.callback = self.callback, None
        assert callback is not None, "no countdown was armed"
        self.clock.now = max(self.clock.now, self.deadline)
        callback(None)


def _patch_time(monkeypatch) -> tuple[_Countdown, _FakeClock]:
    """Put the watcher's timer and clock under the test's control."""
    clock = _FakeClock()
    countdown = _Countdown(clock)
    monkeypatch.setattr(recovery, "monotonic", clock)
    monkeypatch.setattr(recovery, "async_call_later", countdown.schedule)
    return countdown, clock


def _make_device(hass, *, active_time: int = 0) -> Device:
    return Device(
        "AquaSky3.0_Test",
        hass=hass,
        config_data={"mac": MAC, "model": "AquaSky Bluetooth LED", "product_id": 328},
        active_time=active_time,
    )


def _make_entry(options=None, *, entry_id="entry_1", title="Aquasky"):
    return SimpleNamespace(
        entry_id=entry_id,
        title=title,
        data={"mac": MAC},
        options=dict(options or {}),
        state=ConfigEntryState.LOADED,
        runtime_data=None,
        async_on_unload=lambda callback: None,
    )


def _loaded_entry(*, connected=False, options=None, services=None, guardian=None):
    """Return (hass, entry, runtime, device) for one loaded config entry."""
    entry = _make_entry(options)
    hass = _FakeHass(entries=[entry], services=services)
    device = _make_device(hass)
    device.entry_id = entry.entry_id
    device.connected = connected
    runtime = FluvalRuntimeData(device=device, guardian=guardian)
    entry.runtime_data = runtime
    hass.data[DOMAIN][entry.entry_id] = runtime
    return hass, entry, runtime, device


def _flow(hass, issue_id, entry):
    flow = repairs.BleRecoveryFixFlow(issue_id, {"entry_id": entry.entry_id})
    flow.hass = hass
    return flow


# ---------------------------------------------------------------------------
# Reconciliation - the orphaned repair
# ---------------------------------------------------------------------------


def test_setup_time_reconciliation_clears_an_orphaned_schedule_problem_issue(issue_registry):
    """A repair left over from before a reload must go once the guardian is happy.

    This is the live bug: the repair outlived the condition because the
    entity only synced on an observed transition, and the reload had wiped
    the state it compared against.
    """
    hass = _FakeHass()
    device = _make_device(hass)
    guardian = _FakeGuardian(problem=False)
    ha_issue_registry.async_create_issue(
        hass,
        DOMAIN,
        SCHEDULE_ISSUE,
        is_fixable=False,
        severity=ha_issue_registry.IssueSeverity.ERROR,
        translation_key="schedule_problem",
        translation_placeholders={"name": "Aquasky"},
    )

    entity = binary_sensor.FluvalScheduleProblemBinarySensor(device, "schedule_problem", guardian)
    asyncio.run(entity.async_added_to_hass())

    assert (DOMAIN, SCHEDULE_ISSUE) not in issue_registry.issues


def test_schedule_problem_issue_is_raised_fixable_when_the_guardian_reports_a_problem(issue_registry):
    """The repair the guardian raises has to offer the Fix button."""
    hass = _FakeHass()
    device = _make_device(hass)
    device.entry_id = "entry_1"
    guardian = _FakeGuardian(problem=False)
    entity = binary_sensor.FluvalScheduleProblemBinarySensor(device, "schedule_problem", guardian)
    asyncio.run(entity.async_added_to_hass())

    guardian.problem = True
    guardian.notify()

    issue = issue_registry.issues[(DOMAIN, SCHEDULE_ISSUE)]
    assert issue.is_fixable is True
    assert issue.data == {"entry_id": "entry_1"}
    assert issue.translation_placeholders["name"] == device.name


def test_reconciliation_replaces_a_stale_non_fixable_issue_with_a_fixable_one(issue_registry):
    """An upgrade must not leave the operator staring at a Fix-less repair."""
    hass = _FakeHass()
    device = _make_device(hass)
    guardian = _FakeGuardian(problem=True)
    ha_issue_registry.async_create_issue(
        hass,
        DOMAIN,
        SCHEDULE_ISSUE,
        is_fixable=False,
        severity=ha_issue_registry.IssueSeverity.ERROR,
        translation_key="schedule_problem",
        translation_placeholders={"name": device.name},
    )

    entity = binary_sensor.FluvalScheduleProblemBinarySensor(device, "schedule_problem", guardian)
    asyncio.run(entity.async_added_to_hass())

    assert issue_registry.issues[(DOMAIN, SCHEDULE_ISSUE)].is_fixable is True


# ---------------------------------------------------------------------------
# LinkWatcher
# ---------------------------------------------------------------------------


def _start_watcher(hass, entry, device=None):
    """Start an entry-scoped watcher the way async_setup_entry does."""
    watcher = recovery.LinkWatcher(hass, entry, MAC)
    stop = watcher.start()
    if device is not None:
        watcher.attach(device)
    return watcher, stop


def test_unreachable_issue_waits_for_the_countdown_then_clears_on_reconnect(monkeypatch, issue_registry):
    countdown, _clock = _patch_time(monkeypatch)
    hass = _FakeHass()
    entry = _make_entry()
    device = _make_device(hass)

    _watcher, stop = _start_watcher(hass, entry, device)

    key = (DOMAIN, UNREACHABLE_ISSUE)
    assert key not in issue_registry.issues
    assert countdown.delay == recovery.UNREACHABLE_AFTER_SECONDS

    countdown.fire()

    assert issue_registry.issues[key].is_fixable is True
    assert issue_registry.issues[key].severity == ha_issue_registry.IssueSeverity.WARNING
    assert issue_registry.issues[key].translation_placeholders["name"] == device.name

    device.set_connected(True)

    assert key not in issue_registry.issues
    stop()


def test_unreachable_issue_outlives_a_reload_and_is_cleared_by_the_next_watcher(monkeypatch, issue_registry):
    """The rule the whole change rests on: a reload has to converge.

    Unloading must not clear a still-valid repair (nothing is left running
    to re-decide), and the watcher the reload builds must delete it from a
    live link state rather than from anything it remembers.
    """
    countdown, _clock = _patch_time(monkeypatch)
    hass = _FakeHass()
    entry = _make_entry()
    device = _make_device(hass)
    _watcher, stop = _start_watcher(hass, entry, device)
    countdown.fire()
    key = (DOMAIN, UNREACHABLE_ISSUE)
    assert key in issue_registry.issues

    stop()
    assert key in issue_registry.issues

    reloaded_device = _make_device(hass)
    reloaded_device.set_connected(True)
    _start_watcher(hass, entry, reloaded_device)

    assert key not in issue_registry.issues


def test_unreachable_issue_is_raised_for_a_fixture_that_never_appears(monkeypatch, issue_registry):
    """A light that is dead when HA starts never produces a Device at all.

    That is the outage most worth reporting, so the countdown has to belong
    to the config entry rather than to a device that will never exist.
    """
    countdown, _clock = _patch_time(monkeypatch)
    hass = _FakeHass()
    entry = _make_entry(title="Aquasky")

    _watcher, _stop = _start_watcher(hass, entry)

    assert (DOMAIN, UNREACHABLE_ISSUE) not in issue_registry.issues

    countdown.fire()

    issue = issue_registry.issues[(DOMAIN, UNREACHABLE_ISSUE)]
    assert issue.translation_placeholders["name"] == "Aquasky"


def test_unreachable_repair_still_arrives_when_autoheal_reloads_the_entry_every_five_minutes(
    monkeypatch, issue_registry
):
    """The measured failure: a reload every 5 minutes for as long as the
    link is down, so a 15-minute countdown that lives in the watcher can
    never finish.

    Live on 2026-09-09 during a deliberate 21-minute power cut of the
    aquarium light: link drop 22:24:37, automation.ble_proxy_autoheal
    reloaded the entry at 22:35:00 and 22:40:00, each reload built a fresh
    watcher whose countdown started at zero, and no repair ever appeared.
    HA had not heard the fixture since its restart either, so the
    advertisement hint could not cover it. The deadline has to stay put at
    first-drop + 15 min across every rebuild.
    """
    countdown, clock = _patch_time(monkeypatch)
    hass = _FakeHass()
    entry = _make_entry()
    device = _make_device(hass)
    device.set_connected(True)
    _watcher, stop = _start_watcher(hass, entry, device)
    key = (DOMAIN, UNREACHABLE_ISSUE)

    device.set_connected(False)
    first_drop = clock.now
    deadline = first_drop + recovery.UNREACHABLE_AFTER_SECONDS
    assert countdown.deadline == deadline

    # 22:35:00 and 22:40:00: the autoheal's reload_config_entry. The dead
    # fixture is not in the BLE cache, so the rebuilt watcher has no Device.
    for minutes_since_drop in (5, 10):
        clock.now = first_drop + minutes_since_drop * 60
        stop()
        assert countdown.callback is None
        _watcher, stop = _start_watcher(hass, entry)
        assert countdown.deadline == deadline
        assert key not in issue_registry.issues

    countdown.fire()

    assert clock.now == deadline
    assert key in issue_registry.issues
    stop()


def test_a_fixture_that_never_advertises_gets_the_repair_despite_setup_retries(monkeypatch, issue_registry):
    """Total outage: HA never hears the fixture, so no Device ever exists.

    The entry still sets up (`not in BLE cache, will wait for
    advertisement`) and gets rebuilt on every autoheal sweep. The clock
    must be recorded on that waiting path, and rebuilding must not push
    the deadline out.
    """
    countdown, clock = _patch_time(monkeypatch)
    hass = _FakeHass()
    entry = _make_entry(title="Aquasky")
    key = (DOMAIN, UNREACHABLE_ISSUE)

    _watcher, stop = _start_watcher(hass, entry)
    deadline = clock.now + recovery.UNREACHABLE_AFTER_SECONDS
    assert countdown.deadline == deadline

    for _ in range(3):
        clock.advance(4 * 60)
        stop()
        _watcher, stop = _start_watcher(hass, entry)
        assert countdown.deadline == deadline
        assert key not in issue_registry.issues

    # The fourth rebuild happens after the deadline: no timer, repair now.
    clock.advance(4 * 60)
    stop()
    _watcher, stop = _start_watcher(hass, entry)

    assert countdown.callback is None
    assert issue_registry.issues[key].translation_placeholders["name"] == "Aquasky"


def test_a_recovered_link_gets_a_full_window_on_its_next_drop(monkeypatch, issue_registry):
    """The reload-proof clock must not outlive the outage it measured."""
    countdown, clock = _patch_time(monkeypatch)
    hass = _FakeHass()
    entry = _make_entry()
    device = _make_device(hass)
    _watcher, stop = _start_watcher(hass, entry, device)

    clock.advance(10 * 60)
    device.set_connected(True)
    clock.advance(60)
    device.set_connected(False)

    assert countdown.deadline == clock.now + recovery.UNREACHABLE_AFTER_SECONDS
    assert (DOMAIN, UNREACHABLE_ISSUE) not in issue_registry.issues
    stop()


def test_removing_the_entry_forgets_the_outage_and_its_repair(monkeypatch, issue_registry):
    """Permanent removal must leave nothing behind for a re-added fixture."""
    countdown, clock = _patch_time(monkeypatch)
    hass = _FakeHass()
    entry = _make_entry()
    _watcher, stop = _start_watcher(hass, entry)
    countdown.fire()
    assert (DOMAIN, UNREACHABLE_ISSUE) in issue_registry.issues

    stop()
    asyncio.run(async_remove_entry(hass, entry))
    assert (DOMAIN, UNREACHABLE_ISSUE) not in issue_registry.issues

    # Re-adding the fixture straight away starts a fresh window.
    clock.advance(60)
    _watcher, stop = _start_watcher(hass, entry)

    assert countdown.deadline == clock.now + recovery.UNREACHABLE_AFTER_SECONDS
    assert (DOMAIN, UNREACHABLE_ISSUE) not in issue_registry.issues
    stop()


def test_unreachable_issue_is_raised_at_setup_when_the_fixture_has_been_silent(monkeypatch, issue_registry):
    """A reload must not hand a long-dead fixture a fresh grace period."""
    countdown, clock = _patch_time(monkeypatch)
    silent_since = clock.now - (recovery.UNREACHABLE_AFTER_SECONDS + 300)
    monkeypatch.setattr(
        recovery.bluetooth,
        "async_last_service_info",
        lambda hass, address, connectable=True: SimpleNamespace(time=silent_since),
    )
    hass = _FakeHass()
    device = _make_device(hass)

    _start_watcher(hass, _make_entry(), device)

    assert (DOMAIN, UNREACHABLE_ISSUE) in issue_registry.issues
    assert countdown.callback is None


def test_idle_released_link_is_not_reported_as_unreachable(monkeypatch, issue_registry):
    """A finite active window disconnects on purpose; that is not a fault."""
    countdown, _clock = _patch_time(monkeypatch)
    hass = _FakeHass()
    device = _make_device(hass, active_time=120)
    device.touch_seen(notify=False)

    _start_watcher(hass, _make_entry(), device)

    # No deadline is pending, so nothing can raise the repair later either.
    assert countdown.callback is None
    assert (DOMAIN, UNREACHABLE_ISSUE) not in issue_registry.issues


def test_holding_proxy_is_remembered_once_per_change(issue_registry):
    hass = _FakeHass()
    entry = _make_entry()
    device = _make_device(hass)
    _start_watcher(hass, entry, device)

    device.conn_info["active_connection_source"] = PROXY
    device.set_connected(True)

    assert entry.options[CONF_LAST_HOLDING_PROXY] == PROXY
    writes = hass.config_entries.updates

    device.set_connected(False)
    device.set_connected(True)

    assert hass.config_entries.updates == writes

    device.conn_info["active_connection_source"] = "living-room-bluetooth-proxy"
    device.set_connected(False)
    device.set_connected(True)

    assert entry.options[CONF_LAST_HOLDING_PROXY] == "living-room-bluetooth-proxy"


def _held_through(monkeypatch, device, scanner) -> None:
    """Route the device's link through `scanner`, recorded the way a
    stripped-suffix miss would leave it: the display name, MAC and all."""
    device.conn_info["active_connection_source"] = PROXY_SCANNER_NAME
    device.conn_info["active_connection_source_address"] = PROXY_SOURCE
    monkeypatch.setattr(recovery.bluetooth, "async_scanner_by_source", lambda hass, source: scanner)


def test_holding_proxy_is_remembered_as_the_node_name(monkeypatch, issue_registry):
    """The record exists to name an ESPHome action, so it is the scanner's
    adapter (the node ESPHome registered with), never "<node> (<MAC>)"."""
    hass = _FakeHass()
    entry = _make_entry()
    device = _make_device(hass)
    _start_watcher(hass, entry, device)
    _held_through(monkeypatch, device, SimpleNamespace(adapter=PROXY, name=PROXY_SCANNER_NAME))

    device.set_connected(True)

    assert entry.options[CONF_LAST_HOLDING_PROXY] == PROXY


def test_holding_proxy_drops_the_address_suffix_when_the_scanner_has_no_adapter(monkeypatch, issue_registry):
    hass = _FakeHass()
    entry = _make_entry()
    device = _make_device(hass)
    _start_watcher(hass, entry, device)
    _held_through(monkeypatch, device, SimpleNamespace(name=PROXY_SCANNER_NAME))

    device.set_connected(True)

    assert entry.options[CONF_LAST_HOLDING_PROXY] == PROXY
    assert "(" not in entry.options[CONF_LAST_HOLDING_PROXY]


# ---------------------------------------------------------------------------
# Fix flow
# ---------------------------------------------------------------------------


def test_init_repushes_the_schedule_first_but_sends_an_unreachable_light_to_the_menu():
    hass, entry, _runtime, _device = _loaded_entry(connected=True, guardian=_FakeGuardian(problem=True))

    schedule_result = asyncio.run(_flow(hass, SCHEDULE_ISSUE, entry).async_step_init())
    unreachable_result = asyncio.run(_flow(hass, UNREACHABLE_ISSUE, entry).async_step_init())

    assert schedule_result["type"] == "form"
    assert schedule_result["step_id"] == "repush_schedule"
    assert unreachable_result["type"] == "menu"


def test_repush_step_uses_the_guardian_and_finishes_once_the_problem_clears():
    guardian = _FakeGuardian(problem=True)
    hass, entry, _runtime, _device = _loaded_entry(connected=True, guardian=guardian)
    flow = _flow(hass, SCHEDULE_ISSUE, entry)

    result = asyncio.run(flow.async_step_repush_schedule({}))

    assert result["type"] == "create_entry"
    assert (guardian.overrides_ended, guardian.checks) == (1, 1)


def test_menu_offers_the_ladder_with_restart_proxy_when_proxy_and_action_exist():
    hass, entry, _runtime, _device = _loaded_entry(
        options={CONF_LAST_HOLDING_PROXY: PROXY},
        services={"esphome": {PROXY_ACTION: object()}},
    )
    flow = _flow(hass, UNREACHABLE_ISSUE, entry)

    result = asyncio.run(flow.async_step_menu())

    assert result["menu_options"] == ["recheck", "reload", "restart_proxy", "power_cycle"]
    # Nothing has been tried yet, and "None" must never reach the operator.
    assert result["description_placeholders"]["last_result"] == ""
    assert result["description_placeholders"]["name"] == entry.title


def test_menu_offers_restart_proxy_for_the_live_holder_named_with_its_address(monkeypatch):
    """No remembered proxy: the rung comes from the scanner holding the link
    now, whose display name carries the MAC suffix."""
    hass, entry, _runtime, device = _loaded_entry(
        connected=True,
        services={"esphome": {PROXY_ACTION: object()}},
    )
    _held_through(monkeypatch, device, SimpleNamespace(name=PROXY_SCANNER_NAME))

    result = asyncio.run(_flow(hass, UNREACHABLE_ISSUE, entry).async_step_menu())

    assert "restart_proxy" in result["menu_options"]


def test_menu_hides_restart_proxy_when_no_proxy_is_known():
    hass, entry, _runtime, _device = _loaded_entry(services={"esphome": {PROXY_ACTION: object()}})

    result = asyncio.run(_flow(hass, UNREACHABLE_ISSUE, entry).async_step_menu())

    assert result["menu_options"] == ["recheck", "reload", "power_cycle"]


def test_menu_hides_restart_proxy_when_esphome_has_no_matching_action():
    hass, entry, _runtime, _device = _loaded_entry(
        options={CONF_LAST_HOLDING_PROXY: PROXY},
        services={"esphome": {"other_proxy_restart_proxy": object()}},
    )

    result = asyncio.run(_flow(hass, UNREACHABLE_ISSUE, entry).async_step_menu())

    assert result["menu_options"] == ["recheck", "reload", "power_cycle"]


def test_restart_proxy_calls_the_discovered_esphome_action():
    hass, entry, _runtime, device = _loaded_entry(
        options={CONF_LAST_HOLDING_PROXY: PROXY},
        services={"esphome": {PROXY_ACTION: object()}},
    )
    device.connected = True
    flow = _flow(hass, UNREACHABLE_ISSUE, entry)

    result = asyncio.run(flow.async_step_restart_proxy())

    assert result["type"] == "create_entry"
    assert hass.services.calls == [("esphome", PROXY_ACTION, None)]


def test_power_cycle_stores_the_chosen_outlet_and_cycles_it(monkeypatch):
    monkeypatch.setattr(repairs, "POWER_CYCLE_OFF_SECONDS", 0)
    hass, entry, _runtime, _device = _loaded_entry(connected=True)
    flow = _flow(hass, UNREACHABLE_ISSUE, entry)

    result = asyncio.run(flow.async_step_power_cycle({CONF_RECOVERY_OUTLET: "switch.tank_outlet"}))

    assert entry.options[CONF_RECOVERY_OUTLET] == "switch.tank_outlet"
    assert hass.services.calls == [
        ("switch", "turn_off", "switch.tank_outlet"),
        ("switch", "turn_on", "switch.tank_outlet"),
    ]
    assert result["type"] == "create_entry"


def test_unhealthy_action_returns_to_the_menu_reporting_what_was_tried(monkeypatch):
    monkeypatch.setattr(repairs, "SETTLE_SECONDS", 0)
    hass, entry, _runtime, _device = _loaded_entry(connected=False)
    flow = _flow(hass, UNREACHABLE_ISSUE, entry)

    result = asyncio.run(flow.async_step_reload())

    assert hass.config_entries.reloaded == [entry.entry_id]
    assert result["type"] == "menu"
    assert "Reloaded the integration." in result["description_placeholders"]["last_result"]


def test_flow_aborts_when_the_config_entry_is_gone():
    flow = repairs.BleRecoveryFixFlow(UNREACHABLE_ISSUE, None)
    flow.hass = _FakeHass()

    result = asyncio.run(flow.async_step_init())

    assert result == {"type": "abort", "reason": "entry_gone"}


def test_finishing_the_flow_also_reconciles_the_watchers_view(monkeypatch, issue_registry):
    """HA removes the issue on create_entry; the watcher must agree, not re-raise."""
    countdown, _clock = _patch_time(monkeypatch)
    monkeypatch.setattr(repairs, "SETTLE_SECONDS", 0)
    hass, entry, runtime, device = _loaded_entry(connected=False)
    watcher, _stop = _start_watcher(hass, entry, device)
    runtime.link_watcher = watcher
    countdown.fire()
    assert (DOMAIN, UNREACHABLE_ISSUE) in issue_registry.issues

    device.set_connected(True)
    result = asyncio.run(_flow(hass, UNREACHABLE_ISSUE, entry).async_step_recheck())

    assert result["type"] == "create_entry"
    assert (DOMAIN, UNREACHABLE_ISSUE) not in issue_registry.issues


def test_translations_cover_every_step_option_abort_reason_and_placeholder():
    """A missing key or stray placeholder is invisible in code, glaring in the card."""
    import json
    import pathlib
    import re

    strings = json.loads((pathlib.Path(repairs.__file__).parent / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (pathlib.Path(repairs.__file__).parent / "translations" / "en.json").read_text(encoding="utf-8")
    )
    menu_options = {
        repairs.MENU_RECHECK,
        repairs.MENU_RELOAD,
        repairs.MENU_RESTART_PROXY,
        repairs.MENU_POWER_CYCLE,
    }
    # What the flow and each issue creator actually supply.
    flow_placeholders = {"name", "link", "last_result"}
    issue_placeholders = {
        "schedule_problem": {"name"},
        "device_unreachable": {"name", "minutes", "proxy"},
    }

    for translations in (strings, english):
        issues = translations["issues"]
        assert set(issues) == set(issue_placeholders)
        for issue_key, issue in issues.items():
            fix_flow = issue["fix_flow"]
            assert set(fix_flow["abort"]) == {"entry_gone", "entry_not_loaded"}
            assert set(fix_flow["step"]["menu"]["menu_options"]) == menu_options
            assert "recovery_outlet" in fix_flow["step"]["power_cycle"]["data"]
            if issue_key == "schedule_problem":
                assert "repush_schedule" in fix_flow["step"]
            for field in ("title", "description"):
                assert set(re.findall(r"{(\w+)}", issue[field])) <= issue_placeholders[issue_key]
            for step in fix_flow["step"].values():
                rendered = " ".join(value for value in step.values() if isinstance(value, str))
                assert set(re.findall(r"{(\w+)}", rendered)) <= flow_placeholders


def test_issue_ids_follow_the_existing_mac_derived_convention():
    hass = _FakeHass()
    device = _make_device(hass)

    assert issue_id_for(device) == SCHEDULE_ISSUE
    assert recovery.unreachable_issue_id_for(device) == UNREACHABLE_ISSUE


# ---------------------------------------------------------------------------
# The ladder on an entry that is not loaded
# ---------------------------------------------------------------------------


def _unloaded_entry(*, services=None, heard=False, monkeypatch=None):
    """Return (hass, entry) for an entry that never finished setup.

    A fixture left dark long enough ends up here: no runtime data, no device,
    no guardian - which is exactly when the operator reaches for Fix.
    """
    entry = _make_entry()
    entry.state = ConfigEntryState.SETUP_RETRY
    entry.runtime_data = None
    hass = _FakeHass(entries=[entry], services=services)
    if monkeypatch is not None:
        monkeypatch.setattr(
            repairs.bluetooth,
            "async_last_service_info",
            lambda hass_, address, connectable=True: object() if heard else None,
        )
    return hass, entry


def test_an_unloaded_entry_still_gets_the_ladder(monkeypatch):
    """The rungs that matter - reload, proxy, mains - all work unloaded."""
    hass, entry = _unloaded_entry(monkeypatch=monkeypatch)

    result = asyncio.run(_flow(hass, UNREACHABLE_ISSUE, entry).async_step_init())

    assert result["type"] == "menu"
    assert repairs.MENU_POWER_CYCLE in result["menu_options"]
    assert repairs.MENU_RELOAD in result["menu_options"]


def test_a_schedule_repair_on_an_unloaded_entry_falls_through_to_the_ladder(monkeypatch):
    """No guardian exists to re-push through, so do not dead-end on an abort."""
    hass, entry = _unloaded_entry(monkeypatch=monkeypatch)
    flow = _flow(hass, SCHEDULE_ISSUE, entry)

    form = asyncio.run(flow.async_step_init())
    assert form["type"] == "form"

    result = asyncio.run(flow.async_step_repush_schedule({}))

    assert result["type"] == "menu"
    assert "not loaded" in result["description_placeholders"]["last_result"]


def test_an_unloaded_entry_that_is_heard_again_counts_as_recovered(monkeypatch):
    """Without a device, the advertisement setup waits for is the only signal."""
    hass, entry = _unloaded_entry(heard=True, monkeypatch=monkeypatch)

    assert _flow(hass, UNREACHABLE_ISSUE, entry)._healthy() is True


def test_an_unloaded_entry_that_is_silent_is_not_recovered(monkeypatch):
    hass, entry = _unloaded_entry(heard=False, monkeypatch=monkeypatch)

    assert _flow(hass, UNREACHABLE_ISSUE, entry)._healthy() is False
