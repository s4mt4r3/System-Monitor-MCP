import json
from collections import namedtuple
from unittest.mock import patch

from sysmon_mcp import server

Part = namedtuple("Part", "device mountpoint fstype opts")
Usage = namedtuple("Usage", "total used free percent")
GB = 1024**3

PARTS = [
    Part("/dev/disk1", "/", "apfs", "rw"),
    Part("/dev/disk2", "/private", "apfs", "rw"),
    Part("/dev/disk3", "/Volumes/Data", "hfs", "rw"),
]


def fake_usage(mountpoint):
    if mountpoint == "/private":
        raise PermissionError(13, "Permission denied")
    if mountpoint == "/":
        return Usage(total=500 * GB, used=200 * GB, free=300 * GB, percent=40.0)
    return Usage(total=1 * GB, used=GB // 2, free=GB // 2, percent=50.0)


def run():
    with patch.object(server.psutil, "disk_partitions", return_value=PARTS), \
         patch.object(server.psutil, "disk_usage", side_effect=fake_usage):
        result = server.disk_usage()
    json.dumps(result)
    return result


def test_reports_partitions_in_gib():
    result = run()
    assert result["partitions"][0] == {
        "mountpoint": "/", "device": "/dev/disk1", "fstype": "apfs",
        "total_gb": 500.0, "used_gb": 200.0, "free_gb": 300.0, "percent": 40.0,
    }
    assert result["partitions"][1]["used_gb"] == 0.5


def test_permission_error_partition_is_skipped():
    result = run()
    assert [p["mountpoint"] for p in result["partitions"]] == ["/", "/Volumes/Data"]
    assert result["skipped"] == [{"mountpoint": "/private", "reason": "PermissionError"}]
    assert result["partition_count"] == 2
    assert result["truncated"] is False


def test_listing_failure_is_structured():
    with patch.object(server.psutil, "disk_partitions", side_effect=OSError("boom")):
        result = server.disk_usage()
    assert result == {"error": {"type": "OSError", "message": "boom"}}


def test_output_is_capped_at_max_limit():
    parts = [Part(f"/dev/d{i}", f"/mnt/{i}", "ext4", "rw") for i in range(60)]
    usage = Usage(total=GB, used=0, free=GB, percent=0.0)
    with patch.object(server.psutil, "disk_partitions", return_value=parts), \
         patch.object(server.psutil, "disk_usage", return_value=usage):
        result = server.disk_usage()
    assert result["partition_count"] == 60
    assert len(result["partitions"]) == 50
    assert result["truncated"] is True
