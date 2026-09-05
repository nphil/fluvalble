"""Throwaway smoke test for async_remove_entry - deleted after run."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, "tests")
import conftest  # noqa: F401

from custom_components.fluvalble import async_remove_entry
from homeassistant.helpers import issue_registry as ha_issue_registry


async def main():
    ha_issue_registry.async_delete_issue.reset_mock()
    hass = MagicMock()
    entry = SimpleNamespace(data={"mac": "44:A6:E5:70:F1:8D"})

    await async_remove_entry(hass, entry)

    ha_issue_registry.async_delete_issue.assert_called_once()
    args, _kwargs = ha_issue_registry.async_delete_issue.call_args
    assert args[0] is hass
    assert args[2] == "44A6E570F18D_schedule_problem", args[2]
    print("PASS: async_remove_entry deletes the correct issue_id")

    # No MAC on the entry -> no-op, must not raise or call delete_issue.
    ha_issue_registry.async_delete_issue.reset_mock()
    await async_remove_entry(hass, SimpleNamespace(data={}))
    ha_issue_registry.async_delete_issue.assert_not_called()
    print("PASS: async_remove_entry is a no-op without a stored MAC")


asyncio.run(main())
