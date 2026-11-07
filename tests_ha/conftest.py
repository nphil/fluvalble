"""Fixtures for the real Home Assistant tests.

Unlike ``tests/`` (a stub suite that runs on plain Python with no Home Assistant
installed), everything here runs against a real Home Assistant core. Run it from
the repository root with ``pytest -c tests_ha/pytest.ini``.
"""

import pytest


@pytest.fixture(autouse=True)
def _allow_custom_integrations(request):
    """Enable ``custom_components/`` on harnesses that still need asking.

    Home Assistant 2026.11's ``pytest-homeassistant`` enables custom integrations
    by default; the older ``pytest-homeassistant-custom-component`` needs the
    ``enable_custom_integrations`` fixture.
    """
    try:
        request.getfixturevalue("enable_custom_integrations")
    except pytest.FixtureLookupError:
        pass
