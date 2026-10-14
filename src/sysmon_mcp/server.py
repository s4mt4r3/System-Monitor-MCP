import asyncio
import socket
from typing import Literal

import psutil
from mcp.server.fastmcp import FastMCP

# FastMCP defaults to INFO, which logs "Processing request of type ..." to stderr
# for every call; clients like the Inspector display that as if it were an error.
mcp = FastMCP("sysmon", log_level="WARNING")

MAX_LIMIT = 50
CPU_SAMPLE_SECONDS = 0.5
MB = 1024**2
GB = 1024**3


def _error(exc: Exception) -> dict:
    return {"error": {"type": type(exc).__name__, "message": str(exc)}}


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def _or_none(read):
    try:
        return read()
    except psutil.AccessDenied:
        return None


@mcp.tool()
def memory_summary() -> dict:
    """Get current RAM and swap usage for this machine.

    Returns a dict with two sections, "memory" and "swap". Byte counts are
    integers in bytes; "percent" is 0-100. "available" is the memory that can
    be given to new processes without swapping, which is a better measure of
    headroom than "free". If a section can't be read, it contains an "error"
    object with "type" and "message" instead of numbers.
    """
    result: dict = {}

    try:
        vm = psutil.virtual_memory()
        result["memory"] = {
            "total": int(vm.total),
            "available": int(vm.available),
            "used": int(vm.used),
            "free": int(vm.free),
            "percent": float(vm.percent),
        }
    except (psutil.Error, OSError) as exc:
        result["memory"] = _error(exc)

    # Read separately so a swap failure (e.g. restricted sysctl in a sandbox)
    # doesn't hide the RAM numbers.
    try:
        sw = psutil.swap_memory()
        result["swap"] = {
            "total": int(sw.total),
            "used": int(sw.used),
            "free": int(sw.free),
            "percent": float(sw.percent),
        }
    except (psutil.Error, OSError) as exc:
        result["swap"] = _error(exc)

    return result


@mcp.tool()
async def top_processes(sort_by: Literal["cpu", "memory"] = "memory", limit: int = 10) -> dict:
    """List the processes using the most CPU or memory right now.

    sort_by: "memory" (resident memory) or "cpu" (CPU use over a ~0.5 s sample).
    limit: how many processes to return; clamped to 1-50.

    Each process has pid, name, cpu_percent, memory_mb (resident set size in
    MiB) and username. cpu_percent is relative to one core, so it can exceed
    100 on multi-core machines. A field is null when the OS denied access to
    it, which is common for system or other users' processes when not running
    as root; those processes sort last. "access_denied_count" says how many
    processes had at least one null field.
    """
    if sort_by not in ("cpu", "memory"):
        return {"error": {"type": "InvalidArgument", "message": f"sort_by must be 'cpu' or 'memory', got {sort_by!r}"}}
    limit = _clamp_limit(limit)

    try:
        procs = list(psutil.process_iter())
    except (psutil.Error, OSError) as exc:
        return _error(exc)

    # cpu_percent(None) measures since the previous call on the same Process
    # object and returns 0.0 the first time, so prime every process, wait, then
    # read. Awaiting keeps the stdio event loop free during the sample.
    for p in procs:
        try:
            p.cpu_percent(None)
        except (psutil.Error, OSError):
            pass
    await asyncio.sleep(CPU_SAMPLE_SECONDS)

    rows = []
    for p in procs:
        try:
            with p.oneshot():
                cpu = _or_none(lambda: p.cpu_percent(None))
                rss = _or_none(lambda: p.memory_info().rss)
                rows.append({
                    "pid": p.pid,
                    "name": _or_none(p.name),
                    "cpu_percent": None if cpu is None else round(cpu, 1),
                    "memory_mb": None if rss is None else round(rss / MB, 1),
                    "username": _or_none(p.username),
                })
        except psutil.NoSuchProcess:
            continue  # exited during the sample (ZombieProcess is a subclass)
        except (psutil.Error, OSError):
            continue

    key = "cpu_percent" if sort_by == "cpu" else "memory_mb"
    rows.sort(key=lambda r: (r[key] is None, -(r[key] or 0)))
    denied = sum(1 for r in rows if None in (r["name"], r["cpu_percent"], r["memory_mb"], r["username"]))

    return {
        "sort_by": sort_by,
        "limit": limit,
        "process_count": len(rows),
        "truncated": len(rows) > limit,
        "access_denied_count": denied,
        "processes": rows[:limit],
    }


@mcp.tool()
def disk_usage() -> dict:
    """Get space usage for each mounted disk partition.

    Each partition has mountpoint, device, fstype, total_gb, used_gb, free_gb
    (GiB, 1024^3 bytes) and percent (0-100). Partitions the OS won't let us
    stat are listed under "skipped" with a reason instead of failing the call.
    On macOS, APFS volumes share one container, so several mountpoints can
    report the same total and free space; don't add them together. At most 50
    partitions are returned.
    """
    try:
        parts = psutil.disk_partitions(all=False)
    except (psutil.Error, OSError) as exc:
        return _error(exc)

    partitions, skipped = [], []
    for part in parts:
        try:
            u = psutil.disk_usage(part.mountpoint)
        except (psutil.Error, OSError) as exc:
            skipped.append({"mountpoint": part.mountpoint, "reason": type(exc).__name__})
            continue
        partitions.append({
            "mountpoint": part.mountpoint,
            "device": part.device,
            "fstype": part.fstype,
            "total_gb": round(u.total / GB, 2),
            "used_gb": round(u.used / GB, 2),
            "free_gb": round(u.free / GB, 2),
            "percent": float(u.percent),
        })

    return {
        "partition_count": len(partitions),
        "truncated": len(partitions) > MAX_LIMIT,
        "partitions": partitions[:MAX_LIMIT],
        "skipped": skipped[:MAX_LIMIT],
    }


@mcp.tool()
def open_ports(limit: int = 20) -> dict:
    """List listening network ports: TCP sockets in LISTEN state and bound UDP sockets.

    limit: how many entries to return; clamped to 1-50.

    Each entry has port, protocol ("tcp" or "udp"), address (the local bind
    address; 0.0.0.0 or :: means all interfaces), pid and process_name. pid and
    process_name are null when the OS doesn't reveal the owner. Sorted by port.
    On macOS, reading the system-wide socket table requires root; when the
    server runs as a normal user this returns an "error" of type AccessDenied
    explaining that, not a partial list.
    """
    limit = _clamp_limit(limit)

    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        return {"error": {
            "type": "AccessDenied",
            "message": (
                "The OS denied access to the system-wide socket table. On macOS this "
                "requires root privileges, so it fails when the server runs as a normal "
                "user. This server is read-only and does not try to elevate privileges."
            ),
        }}
    except (psutil.Error, OSError) as exc:
        return _error(exc)

    names: dict[int, str | None] = {}

    def process_name(pid: int | None) -> str | None:
        if pid is None:
            return None
        if pid not in names:
            try:
                names[pid] = psutil.Process(pid).name()
            except (psutil.Error, OSError):
                names[pid] = None
        return names[pid]

    seen = set()
    ports = []
    for c in conns:
        # Port 0 is a UDP socket that was created but never bound; nothing can reach it.
        if not c.laddr or not c.laddr.port:
            continue
        if c.type == socket.SOCK_STREAM and c.status == psutil.CONN_LISTEN:
            protocol = "tcp"
        elif c.type == socket.SOCK_DGRAM and not c.raddr:
            protocol = "udp"
        else:
            continue
        key = (protocol, c.laddr.ip, c.laddr.port, c.pid)
        if key in seen:
            continue
        seen.add(key)
        ports.append({
            "port": c.laddr.port,
            "protocol": protocol,
            "address": c.laddr.ip,
            "pid": c.pid,
            "process_name": None,
        })

    ports.sort(key=lambda r: (r["port"], r["protocol"], r["address"]))
    shown = ports[:limit]
    # Look up owners only for rows we return; a busy host can have thousands of sockets.
    for row in shown:
        row["process_name"] = process_name(row["pid"])
    return {
        "limit": limit,
        "port_count": len(ports),
        "truncated": len(ports) > limit,
        "ports": shown,
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
