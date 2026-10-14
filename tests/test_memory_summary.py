import asyncio
import json
from collections import namedtuple
from unittest.mock import patch

import psutil

from sysmon_mcp import server

VMem = namedtuple("VMem", "total available percent used free")
SMem = namedtuple("SMem", "total used free percent sin sout")

FAKE_VM = VMem(total=16_000, available=6_000, percent=62.5, used=10_000, free=1_000)
FAKE_SWAP = SMem(total=4_000, used=1_000, free=3_000, percent=25.0, sin=0, sout=0)


def test_returns_exact_mocked_values():
    with patch.object(server.psutil, "virtual_memory", return_value=FAKE_VM), \
         patch.object(server.psutil, "swap_memory", return_value=FAKE_SWAP):
        result = server.memory_summary()

    assert result == {
        "memory": {"total": 16_000, "available": 6_000, "used": 10_000, "free": 1_000, "percent": 62.5},
        "swap": {"total": 4_000, "used": 1_000, "free": 3_000, "percent": 25.0},
    }
    json.dumps(result)


def test_access_denied_is_structured_not_raised():
    with patch.object(server.psutil, "virtual_memory", return_value=FAKE_VM), \
         patch.object(server.psutil, "swap_memory", side_effect=psutil.AccessDenied()):
        result = server.memory_summary()

    assert result["memory"]["total"] == 16_000
    assert result["swap"]["error"]["type"] == "AccessDenied"
    json.dumps(result)


def test_os_error_is_structured_not_raised():
    with patch.object(server.psutil, "virtual_memory", side_effect=OSError("sysctl failed")), \
         patch.object(server.psutil, "swap_memory", return_value=FAKE_SWAP):
        result = server.memory_summary()

    assert result["memory"] == {"error": {"type": "OSError", "message": "sysctl failed"}}
    assert result["swap"]["total"] == 4_000


def test_tool_is_registered_with_description():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert "memory_summary" in tools
    assert "RAM" in tools["memory_summary"].description
