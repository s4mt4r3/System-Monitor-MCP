import asyncio
import contextlib
import json
from collections import namedtuple
from unittest.mock import patch

import psutil
import pytest

from sysmon_mcp import server

MemInfo = namedtuple("MemInfo", "rss vms")
MB = 1024**2


class FakeProc:
    """Stands in for psutil.Process. Pass an exception instance to make a field raise."""

    def __init__(self, pid, name="proc", cpu=0.0, rss_mb=0, username="me", gone=False):
        self.pid = pid
        self._name, self._cpu, self._rss, self._user, self._gone = name, cpu, rss_mb, username, gone
        self.cpu_calls = 0

    def _get(self, value):
        if self._gone:
            raise psutil.NoSuchProcess(self.pid)
        if isinstance(value, Exception):
            raise value
        return value

    def oneshot(self):
        return contextlib.nullcontext()

    def name(self):
        return self._get(self._name)

    def username(self):
        return self._get(self._user)

    def cpu_percent(self, interval=None):
        self.cpu_calls += 1
        return self._get(self._cpu)

    def memory_info(self):
        rss = self._get(self._rss)
        return MemInfo(rss=rss * MB, vms=0)


def run(procs, **kwargs):
    with patch.object(server.psutil, "process_iter", return_value=procs), \
         patch.object(server, "CPU_SAMPLE_SECONDS", 0):
        result = asyncio.run(server.top_processes(**kwargs))
    json.dumps(result)
    return result


def test_sorts_by_memory_by_default():
    procs = [FakeProc(1, "a", rss_mb=10), FakeProc(2, "b", rss_mb=300), FakeProc(3, "c", rss_mb=50)]
    result = run(procs)
    assert [p["pid"] for p in result["processes"]] == [2, 3, 1]
    assert result["processes"][0] == {
        "pid": 2, "name": "b", "cpu_percent": 0.0, "memory_mb": 300.0, "username": "me",
    }


def test_sorts_by_cpu():
    procs = [FakeProc(1, cpu=5.0), FakeProc(2, cpu=150.0), FakeProc(3, cpu=20.0)]
    result = run(procs, sort_by="cpu")
    assert [p["pid"] for p in result["processes"]] == [2, 3, 1]


def test_cpu_is_primed_before_reading():
    p = FakeProc(1, cpu=12.0)
    run([p], sort_by="cpu")
    assert p.cpu_calls == 2


def test_rejects_bad_sort_by():
    result = run([FakeProc(1)], sort_by="disk")
    assert result["error"]["type"] == "InvalidArgument"


@pytest.mark.parametrize("limit, expected", [(0, 1), (-5, 1), (3, 3), (500, 50)])
def test_limit_is_clamped(limit, expected):
    procs = [FakeProc(i, rss_mb=i) for i in range(1, 61)]
    result = run(procs, limit=limit)
    assert result["limit"] == expected
    assert len(result["processes"]) == expected
    assert result["truncated"] is True
    assert result["process_count"] == 60


def test_access_denied_fields_are_null_and_sort_last():
    procs = [
        FakeProc(1, rss_mb=psutil.AccessDenied(1), username=psutil.AccessDenied(1)),
        FakeProc(2, rss_mb=5),
    ]
    result = run(procs)
    assert [p["pid"] for p in result["processes"]] == [2, 1]
    assert result["processes"][1]["memory_mb"] is None
    assert result["processes"][1]["username"] is None
    assert result["access_denied_count"] == 1


def test_vanished_process_is_skipped():
    result = run([FakeProc(1, gone=True), FakeProc(2, rss_mb=1)])
    assert [p["pid"] for p in result["processes"]] == [2]


def test_schema_exposes_sort_by_enum():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    schema = tools["top_processes"].inputSchema
    assert schema["properties"]["sort_by"]["enum"] == ["cpu", "memory"]


def test_default_limit_is_10():
    result = run([FakeProc(i, rss_mb=i) for i in range(1, 31)])
    assert result["limit"] == 10
    assert len(result["processes"]) == 10


def test_bad_sort_by_rejected_at_mcp_layer():
    # Over MCP, pydantic validates the Literal before the function runs; the
    # client sees an isError result, the server keeps running.
    from mcp.server.fastmcp.exceptions import ToolError

    with patch.object(server.psutil, "process_iter") as process_iter:
        with pytest.raises(ToolError, match="'cpu' or 'memory'"):
            asyncio.run(server.mcp.call_tool("top_processes", {"sort_by": "disk"}))
    process_iter.assert_not_called()
