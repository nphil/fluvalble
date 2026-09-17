"""Tests for preferred-proxy affinity wiring: Device -> ble_affinity.

`ble_affinity.make_affinity_client_class` is vendored, shared, and already
covers its own selection logic in isolation elsewhere; what is specific to
this repo is the wiring around it - `Device._affinity_client_class()` reads
a live `preferred_getter` (the same closure `_create_device` in
`__init__.py` builds over `entry.options`) and records the outcome for the
Connection sensor. These tests exercise that wiring end to end with small
fakes for `manager`/`scanner`/`scanner_device`/`connector` and a fake
`HaBleakClientWrapper`-shaped base class, per `ble_affinity.py`'s docstring.
"""

from types import SimpleNamespace
from unittest.mock import patch

from custom_components.fluvalble.core import CONF_PREFERRED_PROXY
from custom_components.fluvalble.core.device import Device


def _make_device(*, preferred_proxy=""):
    """A Device wired the way `__init__._create_device` wires production
    ones: `preferred_getter` reads a live (mutable) options dict, so a
    later change to `options` takes effect without rebuilding the Device."""
    options = {CONF_PREFERRED_PROXY: preferred_proxy}
    device = Device(
        "AquaSky3.0_Test",
        config_data={"mac": "44:A6:E5:70:F1:8D", "model": "AquaSky Bluetooth LED", "product_id": 328},
        preferred_getter=lambda: options.get(CONF_PREFERRED_PROXY) or None,
    )
    return device, options


class _FakeBackend:
    """Stands in for whatever `_async_get_backend_for_ble_device` returns."""

    def __init__(self, scanner, ble_device):
        self.scanner = scanner
        self.ble_device = ble_device


class _FakeHaBleakClientWrapper:
    """Stands in for habluetooth's real wrapper: only the two hooks
    `ble_affinity` overrides/calls, plus the address it reads off an
    unconnected client."""

    def __init__(self, *_args, **_kwargs):
        self._HaBleakClientWrapper__address = "44:A6:E5:70:F1:8D"

    def _async_get_best_available_backend_and_device(self, manager):
        del manager
        return _FakeBackend(SimpleNamespace(name="default-scanner"), "default-ble-device")

    def _async_get_backend_for_ble_device(self, manager, scanner, ble_device):
        del manager
        return _FakeBackend(scanner, ble_device)


def _fake_scanner(name, *, can_connect=True, failures=0):
    return SimpleNamespace(
        adapter=name,
        source=name,
        name=name,
        connector=SimpleNamespace(can_connect=lambda: can_connect),
        connection_failures=lambda address: failures,
    )


def _fake_scanner_device(scanner, *, rssi=-55, ble_device="ble-device"):
    return SimpleNamespace(scanner=scanner, ble_device=ble_device, advertisement=SimpleNamespace(rssi=rssi))


def _fake_manager(scanner_devices):
    return SimpleNamespace(async_scanner_devices_by_address=lambda address, connectable: scanner_devices)


def _select_backend(device, manager):
    """Build the device's affinity class against the fake base and run its
    selection hook once, the way `Client._ensure_client` -> `establish_connection`
    would via `HaBleakClientWrapper.connect()`."""
    with patch("custom_components.fluvalble.core.device.brc.BleakClient", _FakeHaBleakClientWrapper):
        client_class = device._affinity_client_class()
        instance = client_class()
        return instance._async_get_best_available_backend_and_device(manager)


def test_preferred_proxy_present_and_connectable_is_selected():
    """(a) Preferred scanner present, connectable, and failure-free -> its
    backend is chosen, and the choice is recorded for the Connection sensor."""
    device, _options = _make_device(preferred_proxy="plant-room-bluetooth-proxy")
    scanner = _fake_scanner("plant-room-bluetooth-proxy")
    manager = _fake_manager([_fake_scanner_device(scanner)])

    backend = _select_backend(device, manager)

    assert backend.scanner is scanner
    assert device._via_preferred_proxy is True
    assert device.connection_hold_attributes()["preferred_proxy"] == "plant-room-bluetooth-proxy"
    assert device.connection_hold_attributes()["via_preferred_proxy"] is True


def test_preferred_proxy_absent_falls_back_to_default_selection():
    """(b) The preferred scanner is not among the devices currently
    advertising this address -> the default (RSSI-scored) selection runs."""
    device, _options = _make_device(preferred_proxy="plant-room-bluetooth-proxy")
    other_scanner = _fake_scanner("office-bluetooth-proxy")
    manager = _fake_manager([_fake_scanner_device(other_scanner)])

    backend = _select_backend(device, manager)

    assert backend.scanner.name == "default-scanner"
    assert device._via_preferred_proxy is False


def test_preferred_proxy_after_repeated_failures_falls_back_to_default_selection():
    """(c) The preferred scanner is present but has failed this address 3
    times in a row (the default `max_failures`) -> default selection runs
    until it succeeds again."""
    device, _options = _make_device(preferred_proxy="plant-room-bluetooth-proxy")
    failing_scanner = _fake_scanner("plant-room-bluetooth-proxy", failures=3)
    manager = _fake_manager([_fake_scanner_device(failing_scanner)])

    backend = _select_backend(device, manager)

    assert backend.scanner.name == "default-scanner"
    assert device._via_preferred_proxy is False


def test_automatic_default_never_consults_ble_affinity_selection():
    """`preferred_proxy` unset (the "" default) is the existing automatic
    path - the affinity wrapper must not even ask which scanner is preferred."""
    device, _options = _make_device(preferred_proxy="")
    scanner = _fake_scanner("plant-room-bluetooth-proxy")
    manager = _fake_manager([_fake_scanner_device(scanner)])

    backend = _select_backend(device, manager)

    assert backend.scanner.name == "default-scanner"
    assert device._via_preferred_proxy is False
