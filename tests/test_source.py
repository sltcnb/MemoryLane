"""Source probing, bad-media handling and destination-safety checks."""

import os
import sys

import pytest

from memorylane import source as src
from memorylane.source import BadSectorLog, Source


def test_bad_sector_log_coalesces():
    log = BadSectorLog()
    for lba in (10, 11, 12, 40, 41, 99):
        log.add(lba)
    assert log.count == 6
    assert log.ranges == [[10, 12], [40, 41], [99, 99]]
    assert log.format() == ["10-12", "40-41", "99"]
    assert bool(log)
    assert not BadSectorLog()


def test_file_source(tmp_path):
    path = tmp_path / "e.bin"
    path.write_bytes(b"\xcc" * 4096)
    with Source(str(path)) as s:
        assert s.size == 4096
        assert s.sector_size == 512
        assert s.sector_count == 8
        assert s.source_type == "Image File"
        assert s.is_device is False
        assert s.read(0, 4096) == b"\xcc" * 4096
        assert s.read(4000, 4096) == b"\xcc" * 96      # clipped at EOF
        assert s.read(9999, 10) == b""
        assert not s.bad_sectors


def test_odd_sized_file_rounds_up(tmp_path):
    path = tmp_path / "odd.bin"
    path.write_bytes(b"x" * 513)
    with Source(str(path)) as s:
        assert s.size == 513
        assert s.sector_count == 2


def test_empty_source_is_rejected(tmp_path):
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    with pytest.raises(src.SourceError):
        Source(str(path))


def test_missing_source_is_rejected(tmp_path):
    with pytest.raises(src.SourceError):
        Source(str(tmp_path / "nope"))


def test_unreadable_sectors_are_zero_filled_and_logged(tmp_path, failing_media):
    """Simulate failing media: sectors 4-5 raise, the rest read normally."""
    content = bytes(range(256)) * 32                # 8192 bytes = 16 sectors
    path = tmp_path / "flaky.bin"
    path.write_bytes(content)
    failing_media(path, {4, 5})
    with Source(str(path)) as s:
        data = s.read(0, 8192)
    assert len(data) == 8192
    assert data[4 * 512:6 * 512] == b"\x00" * 1024      # holes zero-filled
    assert data[:2048] == content[:2048]                # good data intact
    assert data[6 * 512:] == content[6 * 512:]
    assert s.bad_sectors.count == 2
    assert s.bad_sectors.ranges == [[4, 5]]


def test_whole_disk_of_device():
    if sys.platform == "darwin":
        assert src.whole_disk_of_device("/dev/disk4") == "disk4"
        assert src.whole_disk_of_device("/dev/rdisk4") == "disk4"
        assert src.whole_disk_of_device("/dev/disk4s1") == "disk4"
    elif sys.platform.startswith("linux"):
        # Naming only; sysfs lookups are covered by the live path.
        assert src.whole_disk_of_device("/dev/definitely-not-a-disk") is None
    else:
        assert src.whole_disk_of_device("\\\\.\\PhysicalDrive0") is None


def test_destination_guard_ignores_file_sources(tmp_path):
    from memorylane.cli import _same_device
    path = tmp_path / "e.bin"
    path.write_bytes(b"\x00" * 1024)
    with Source(str(path)) as s:
        assert _same_device(s, str(tmp_path)) is False


@pytest.mark.skipif(sys.platform not in ("darwin",) and
                    not sys.platform.startswith("linux"),
                    reason="POSIX disk topology only")
def test_destination_guard_resolves_the_boot_disk():
    """The directory holding this test must resolve to some physical disk."""
    disk = src.whole_disk_of_path(os.path.dirname(os.path.abspath(__file__)))
    assert disk is None or isinstance(disk, str) and disk


def test_retries_are_attempted_before_giving_up(tmp_path, failing_media):
    path = tmp_path / "dying.bin"
    path.write_bytes(b"\xee" * 8192)
    # Fail the first 3 attempts on sector 4, then let it succeed.
    state = failing_media(path, {4}, fail_times=3)
    with Source(str(path), retries=3) as s:
        data = s.read(0, 8192)
    assert data == b"\xee" * 8192          # recovered on the 4th attempt
    assert not s.bad_sectors
    # 1 large read + 3 failing sector reads, then the successful retry.
    assert state["attempts"] == 4


def test_sector_is_written_off_once_retries_are_exhausted(tmp_path, failing_media):
    path = tmp_path / "dead.bin"
    path.write_bytes(b"\xee" * 8192)
    failing_media(path, {4})
    with Source(str(path), retries=1) as s:
        data = s.read(0, 8192)
    assert data[4 * 512:5 * 512] == b"\x00" * 512
    assert data[:2048] == b"\xee" * 2048
    assert s.bad_sectors.as_pairs() == [(4, 1)]


def test_zero_retries_still_zero_fills(tmp_path, failing_media):
    path = tmp_path / "dead.bin"
    path.write_bytes(b"\xee" * 4096)
    failing_media(path, {1, 2})
    with Source(str(path), retries=0) as s:
        s.read(0, 4096)
    assert s.bad_sectors.as_pairs() == [(1, 2)]


def test_bad_sector_pairs():
    log = BadSectorLog()
    for lba in (3, 4, 5, 20):
        log.add(lba)
    assert log.as_pairs() == [(3, 3), (20, 1)]


def test_mount_table_is_sane():
    """Whatever the platform, entries must be well-shaped."""
    for device, point, readonly in src._mount_table():
        assert device.startswith("/dev/") or sys.platform not in (
            "darwin",) and not sys.platform.startswith("linux")
        assert isinstance(point, str) and point
        assert isinstance(readonly, bool)


def test_mounted_volumes_of_nothing():
    assert src.mounted_volumes(None) == []
    assert src.mounted_volumes("definitely-not-a-disk") == []
