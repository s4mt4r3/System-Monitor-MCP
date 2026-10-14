import json
import socket
from collections import namedtuple
from unittest.mock import patch

import psutil

from sysmon_mcp import server

Addr = namedtuple("Addr", "ip port")
Conn = namedtuple("Conn", "fd family type laddr raddr status pid")

TCP, UDP, V4 = socket.SOCK_STREAM, socket.SOCK_DGRAM, socket.AF_INET

CONNS = [
    Conn(3, V4, TCP, Addr("0.0.0.0", 8080), (), psutil.CONN_LISTEN, 100),
    Conn(4, V4, TCP, Addr("127.0.0.1", 5432), (), psutil.CONN_LISTEN, 200),
    Conn(5, V4, TCP, Addr("10.0.0.2", 51000), Addr("1.1.1.1", 443), psutil.CONN_ESTABLISHED, 100),
    Conn(6, V4, UDP, Addr("0.0.0.0", 53), (), psutil.CONN_NONE, None),
    Conn(7, V4, UDP, Addr("10.0.0.2", 60000), Addr("8.8.8.8", 53), psutil.CONN_NONE, 300),
    Conn(8, V4, TCP, Addr("0.0.0.0", 8080), (), psutil.CONN_LISTEN, 100),  # duplicate
]

NAMES = {100: "python3", 200: "postgres"}


class FakeProcess:
    def __init__(self, pid):
        if pid not in NAMES:
            raise psutil.NoSuchProcess(pid)
        self.pid = pid

    def name(self):
        return NAMES[self.pid]


def run(conns=CONNS, **kwargs):
    with patch.object(server.psutil, "net_connections", return_value=conns), \
         patch.object(server.psutil, "Process", FakeProcess):
        result = server.open_ports(**kwargs)
    json.dumps(result)
    return result


def test_lists_only_listening_sockets_sorted_by_port():
    result = run()
    assert result["ports"] == [
        {"port": 53, "protocol": "udp", "address": "0.0.0.0", "pid": None, "process_name": None},
        {"port": 5432, "protocol": "tcp", "address": "127.0.0.1", "pid": 200, "process_name": "postgres"},
        {"port": 8080, "protocol": "tcp", "address": "0.0.0.0", "pid": 100, "process_name": "python3"},
    ]
    assert result["port_count"] == 3


def test_limit_is_clamped():
    many = [Conn(i, V4, TCP, Addr("0.0.0.0", 1000 + i), (), psutil.CONN_LISTEN, None) for i in range(80)]
    assert run(many, limit=0)["limit"] == 1
    result = run(many, limit=999)
    assert result["limit"] == 50
    assert len(result["ports"]) == 50
    assert result["truncated"] is True


def test_access_denied_returns_explanation():
    with patch.object(server.psutil, "net_connections", side_effect=psutil.AccessDenied()):
        result = server.open_ports()
    assert result["error"]["type"] == "AccessDenied"
    assert "root" in result["error"]["message"]
    assert "ports" not in result


def test_vanished_owner_gives_null_name():
    conns = [Conn(3, V4, TCP, Addr("0.0.0.0", 9000), (), psutil.CONN_LISTEN, 999)]
    assert run(conns)["ports"][0]["process_name"] is None


def test_default_limit_is_20():
    many = [Conn(i, V4, TCP, Addr("0.0.0.0", 1000 + i), (), psutil.CONN_LISTEN, None) for i in range(30)]
    result = run(many)
    assert result["limit"] == 20
    assert len(result["ports"]) == 20


def test_raw_permission_error_gets_same_explanation():
    with patch.object(server.psutil, "net_connections", side_effect=PermissionError(1, "Operation not permitted")):
        result = server.open_ports()
    assert result["error"]["type"] == "AccessDenied"
    assert "root" in result["error"]["message"]


def test_other_errors_are_structured():
    with patch.object(server.psutil, "net_connections", side_effect=OSError("no /proc")):
        result = server.open_ports()
    assert result == {"error": {"type": "OSError", "message": "no /proc"}}


def test_unbound_udp_port_zero_is_skipped():
    conns = [Conn(3, V4, UDP, Addr("0.0.0.0", 0), (), psutil.CONN_NONE, 100)]
    assert run(conns)["ports"] == []


def test_owner_lookup_only_for_returned_rows():
    many = [Conn(i, V4, TCP, Addr("0.0.0.0", 1000 + i), (), psutil.CONN_LISTEN, 100) for i in range(40)]
    looked_up = []

    class CountingProcess(FakeProcess):
        def __init__(self, pid):
            looked_up.append(pid)
            super().__init__(pid)

    with patch.object(server.psutil, "net_connections", return_value=many), \
         patch.object(server.psutil, "Process", CountingProcess):
        result = server.open_ports(limit=5)
    assert len(result["ports"]) == 5
    assert all(p["process_name"] == "python3" for p in result["ports"])
    assert looked_up == [100]  # cached per pid, and only rows we return
