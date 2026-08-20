"""Evidence source: an attached block device or a file on disk.

Exposes the geometry/identity fields FTK Imager records in its acquisition
summary, plus a read path that survives media errors the way an imager must:
an unreadable sector is zero-filled and logged rather than aborting the job.
"""

import os
import re
import shutil
import stat
import subprocess
import sys

from ._io import close as _close_fd, pread

DEFAULT_SECTOR_SIZE = 512
# BIOS-translated geometry, which is what FTK Imager reports for modern disks.
HEADS = 255
SECTORS_PER_TRACK = 63


class SourceError(Exception):
    pass


class BadSectorLog:
    """Coalesces unreadable sectors into contiguous LBA ranges."""

    def __init__(self):
        self.ranges = []          # [start_lba, end_lba] inclusive
        self.count = 0

    def add(self, lba):
        self.count += 1
        if self.ranges and self.ranges[-1][1] == lba - 1:
            self.ranges[-1][1] = lba
        else:
            self.ranges.append([lba, lba])

    def __bool__(self):
        return bool(self.ranges)

    def as_pairs(self):
        """[(first sector, sector count)] — the shape EWF's `error2` wants."""
        return [(start, end - start + 1) for start, end in self.ranges]

    def format(self, limit=64):
        out = []
        for start, end in self.ranges[:limit]:
            out.append(f"{start}" if start == end else f"{start}-{end}")
        if len(self.ranges) > limit:
            out.append(f"... ({len(self.ranges) - limit} more ranges)")
        return out


class Source:
    """Read-only handle on the thing being imaged."""

    def __init__(self, path, sector_size=None, prefer_raw_device=True,
                 retries=2):
        self.requested_path = path
        self.path = path
        self.is_device = _is_device(path)
        if self.is_device and prefer_raw_device and sys.platform == "darwin":
            raw = _macos_raw_device(path)
            if raw:
                self.path = raw
        try:
            self.fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except OSError as e:
            raise SourceError(f"cannot open {self.path}: {e.strerror}") from e

        self.info = probe_device(path) if self.is_device else {}
        self.sector_size = int(
            sector_size or self.info.get("sector_size") or DEFAULT_SECTOR_SIZE)
        if self.sector_size <= 0 or self.sector_size % 512:
            raise SourceError(f"implausible sector size: {self.sector_size}")
        self.size = self._probe_size()
        if not self.size:
            raise SourceError(f"{self.path}: zero-length source, nothing to image")
        self.sector_count = -(-self.size // self.sector_size)  # ceil
        self.bad_sectors = BadSectorLog()
        self.retries = max(0, int(retries))

    # ------------------------------------------------------------- geometry

    @property
    def cylinders(self):
        return self.sector_count // (HEADS * SECTORS_PER_TRACK)

    @property
    def source_type(self):
        if not self.is_device:
            return "Image File"
        return "Logical" if _is_partition(self.path) else "Physical"

    @property
    def model(self):
        return self.info.get("model") or "N/A"

    @property
    def serial(self):
        return self.info.get("serial") or "N/A"

    @property
    def interface(self):
        return self.info.get("interface") or "N/A"

    @property
    def removable(self):
        return self.info.get("removable")

    def _probe_size(self):
        st = os.fstat(self.fd)
        if stat.S_ISREG(st.st_mode) and st.st_size:
            return st.st_size
        try:
            size = os.lseek(self.fd, 0, os.SEEK_END)
            os.lseek(self.fd, 0, os.SEEK_SET)
            if size:
                return size
        except OSError:
            pass
        size = self.info.get("size")
        if size:
            return int(size)
        return _size_by_bisect(self.fd, self.sector_size)

    # ----------------------------------------------------------------- read

    def read(self, offset, length):
        """Read `length` bytes at `offset`, zero-filling unreadable sectors."""
        if offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        try:
            data = pread(self.fd, length, offset)
        except OSError:
            data = b""
        if len(data) == length:
            return data
        # Short read or hard error: fall back to sector-at-a-time so a single
        # bad sector costs one sector of evidence, not the whole block.
        return data + self._read_degraded(offset + len(data), length - len(data))

    def _read_degraded(self, offset, length):
        out = bytearray()
        ss = self.sector_size
        pos = offset
        end = offset + length
        while pos < end:
            take = min(ss, end - pos)
            data = b""
            for _ in range(self.retries + 1):
                try:
                    data = pread(self.fd, take, pos)
                except OSError:
                    data = b""
                if len(data) == take:
                    break
            if len(data) < take:
                self.bad_sectors.add(pos // ss)
                data = data + b"\x00" * (take - len(data))
            out += data
            pos += take
        return bytes(out)

    def close(self):
        if getattr(self, "fd", None) is not None:
            try:
                _close_fd(self.fd)
            finally:
                self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ------------------------------------------------------------------ helpers

def _is_device(path):
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return path.startswith("\\\\.\\")
    return stat.S_ISBLK(mode) or stat.S_ISCHR(mode)


def _is_partition(path):
    name = os.path.basename(path)
    if sys.platform == "darwin":
        return bool(re.match(r"r?disk\d+s\d+$", name))
    return bool(re.match(r"(sd[a-z]+|nvme\d+n\d+p|mmcblk\d+p|vd[a-z]+)\d+$", name))


def _macos_raw_device(path):
    """/dev/disk4 -> /dev/rdisk4 (character device: far faster sequential reads)."""
    d, name = os.path.split(path)
    if name.startswith("disk"):
        raw = os.path.join(d, "r" + name)
        if os.path.exists(raw):
            return raw
    return None


def _size_by_bisect(fd, sector_size):
    """Last resort: binary-search the last readable sector."""
    def readable(lba):
        try:
            return len(pread(fd, sector_size, lba * sector_size)) == sector_size
        except OSError:
            return False

    if not readable(0):
        return 0
    lo, hi = 0, 1
    while readable(hi):
        lo, hi = hi, hi * 2
        if hi > 1 << 45:
            break
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if readable(mid):
            lo = mid
        else:
            hi = mid
    return (lo + 1) * sector_size


# --------------------------------------------------------- device metadata

def probe_device(path):
    """Best-effort drive identity. Missing fields simply render as N/A."""
    try:
        if sys.platform == "darwin":
            return _probe_macos(path)
        if sys.platform.startswith("linux"):
            return _probe_linux(path)
        if os.name == "nt":
            return _probe_windows(path)
    except Exception:
        pass
    return {}


def _probe_macos(path):
    if not shutil.which("diskutil"):
        return {}
    import plistlib
    dev = os.path.basename(path)
    if dev.startswith("rdisk"):
        dev = dev[1:]
    out = subprocess.run(["diskutil", "info", "-plist", dev],
                         capture_output=True, timeout=20)
    if out.returncode != 0:
        return {}
    d = plistlib.loads(out.stdout)
    info = {
        "model": (d.get("MediaName") or d.get("IORegistryEntryName") or "").strip(),
        "interface": d.get("BusProtocol"),
        "sector_size": d.get("DeviceBlockSize"),
        "size": d.get("Size") or d.get("TotalSize"),
        "removable": bool(d.get("RemovableMedia")
                          or d.get("RemovableMediaOrExternalDevice")),
        "os_path": d.get("DeviceNode"),
    }
    serial = _macos_serial(d.get("DeviceIdentifier") or dev)
    if serial:
        info["serial"] = serial
    return info


def _macos_serial(dev):
    if not shutil.which("ioreg"):
        return None
    out = subprocess.run(["ioreg", "-r", "-c", "IOMedia", "-l", "-w", "0"],
                         capture_output=True, text=True, timeout=20)
    m = re.search(r'"Serial Number"\s*=\s*"([^"]+)"', out.stdout or "")
    return m.group(1).strip() if m else None


def _probe_linux(path):
    dev = os.path.basename(path)
    base = f"/sys/class/block/{dev}"
    if not os.path.isdir(base):
        return {}
    # A partition inherits identity from its parent disk.
    parent = base
    if os.path.exists(os.path.join(base, "partition")):
        parent = os.path.realpath(os.path.join(base, ".."))

    def read(rel, root=None):
        try:
            with open(os.path.join(root or base, rel)) as f:
                return f.read().strip()
        except OSError:
            return None

    sectors = read("size")
    logical = read("queue/logical_block_size", parent) or DEFAULT_SECTOR_SIZE
    vendor = read("device/vendor", parent) or ""
    model = read("device/model", parent) or read("device/name", parent) or ""
    info = {
        "model": " ".join(f"{vendor} {model}".split()),
        "serial": read("device/serial", parent) or read("device/wwid", parent),
        "removable": read("removable", parent) == "1",
        "sector_size": int(logical),
        # /sys/class/block/*/size is always in 512-byte units.
        "size": int(sectors) * 512 if sectors else None,
        "interface": _linux_bus(parent),
    }
    return info


def _linux_bus(parent):
    link = os.path.realpath(parent)
    for bus, label in (("usb", "USB"), ("nvme", "NVMe"), ("ata", "ATA"),
                       ("scsi", "SCSI"), ("mmc", "MMC"), ("virtio", "VirtIO")):
        if f"/{bus}" in link:
            return label
    return None


def _probe_windows(path):
    import ctypes
    from ctypes import wintypes

    info = {}
    handle = ctypes.windll.kernel32.CreateFileW(
        path, 0, 3, None, 3, 0, None)  # 3 = OPEN_EXISTING, share read|write
    if handle and handle != -1:
        try:
            length = ctypes.c_ulonglong(0)
            returned = wintypes.DWORD(0)
            # IOCTL_DISK_GET_LENGTH_INFO
            if ctypes.windll.kernel32.DeviceIoControl(
                    handle, 0x0007405C, None, 0, ctypes.byref(length),
                    ctypes.sizeof(length), ctypes.byref(returned), None):
                info["size"] = length.value
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    m = re.search(r"PhysicalDrive(\d+)", path, re.I)
    if m and shutil.which("powershell"):
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-Disk -Number {m.group(1)} | "
             "Select-Object -Property FriendlyName,SerialNumber,BusType | Format-List"],
            capture_output=True, text=True, timeout=30)
        for line in (out.stdout or "").splitlines():
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "friendlyname":
                info["model"] = value
            elif key == "serialnumber":
                info["serial"] = value
            elif key == "bustype":
                info["interface"] = value
                info["removable"] = value.upper() == "USB"
    return info


def list_devices():
    """Enumerate candidate physical sources for `mlane devices`."""
    out = []
    if sys.platform == "darwin" and shutil.which("diskutil"):
        import plistlib
        res = subprocess.run(["diskutil", "list", "-plist", "physical"],
                             capture_output=True, timeout=20)
        if res.returncode == 0:
            for dev in plistlib.loads(res.stdout).get("WholeDisks", []):
                info = probe_device(f"/dev/{dev}")
                info["path"] = f"/dev/{dev}"
                out.append(info)
    elif sys.platform.startswith("linux"):
        for dev in sorted(os.listdir("/sys/class/block")):
            if os.path.exists(f"/sys/class/block/{dev}/partition"):
                continue
            if re.match(r"(loop|ram|dm-|zram|sr)\d*", dev):
                continue
            info = _probe_linux(dev)
            info["path"] = f"/dev/{dev}"
            out.append(info)
    elif os.name == "nt":
        for n in range(16):
            path = rf"\\.\PhysicalDrive{n}"
            info = probe_device(path)
            if info.get("size"):
                info["path"] = path
                out.append(info)
    return out


# ----------------------------------------------- destination safety checks

def whole_disk_of_device(path):
    """Whole-disk name a device node belongs to: /dev/disk4s1 -> 'disk4'."""
    name = os.path.basename(path)
    if sys.platform == "darwin":
        if name.startswith("r"):
            name = name[1:]
        m = re.match(r"(disk\d+)", name)
        return m.group(1) if m else None
    if sys.platform.startswith("linux"):
        base = f"/sys/class/block/{name}"
        if os.path.exists(os.path.join(base, "partition")):
            return os.path.basename(os.path.realpath(os.path.join(base, "..")))
        return name if os.path.isdir(base) else None
    return None


def _mount_point(path):
    path = os.path.realpath(path)
    while path != "/" and not os.path.ismount(path):
        path = os.path.dirname(path)
    return path


def _linux_holder_disk(node):
    """Resolve a /sys/dev/block node to the physical disk beneath it."""
    for _ in range(4):
        slaves = os.path.join(node, "slaves")
        if os.path.isdir(slaves):
            entries = sorted(os.listdir(slaves))
            if entries:                       # dm/LVM/md: follow the first leg
                node = os.path.realpath(os.path.join(slaves, entries[0]))
                continue
        break
    if os.path.exists(os.path.join(node, "partition")):
        return os.path.basename(os.path.realpath(os.path.join(node, "..")))
    return os.path.basename(os.path.realpath(node))


def whole_disk_of_path(path):
    """Whole-disk name that a filesystem path is stored on, or None."""
    try:
        if sys.platform == "darwin":
            import plistlib
            if not shutil.which("diskutil"):
                return None
            out = subprocess.run(
                ["diskutil", "info", "-plist", _mount_point(path)],
                capture_output=True, timeout=20)
            if out.returncode != 0:
                return None
            d = plistlib.loads(out.stdout)
            # An APFS volume lives on a synthesized disk; follow it down to the
            # physical store so /dev/disk0 vs / is recognised as one drive.
            stores = d.get("APFSPhysicalStores") or []
            identifier = (stores[0].get("APFSPhysicalStore") if stores
                          else d.get("ParentWholeDisk") or d.get("DeviceIdentifier"))
            m = re.match(r"(disk\d+)", identifier or "")
            return m.group(1) if m else None
        if sys.platform.startswith("linux"):
            dev = os.stat(path).st_dev
            node = f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}"
            return _linux_holder_disk(node) if os.path.exists(node) else None
    except Exception:
        pass
    return None


def _mount_table():
    """[(device, mount point, read-only)] for every mounted filesystem."""
    entries = []
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 4 and parts[0].startswith("/dev/"):
                        options = parts[3].split(",")
                        entries.append((parts[0],
                                        parts[1].replace("\\040", " "),
                                        "ro" in options))
        elif sys.platform == "darwin":
            out = subprocess.run(["mount"], capture_output=True, text=True,
                                 timeout=20)
            for line in (out.stdout or "").splitlines():
                m = re.match(r"^(/dev/\S+) on (.+?) \(([^)]*)\)$", line)
                if m:
                    entries.append((m.group(1), m.group(2),
                                    "read-only" in m.group(3)))
    except Exception:
        pass
    return entries


def mounted_volumes(disk):
    """Mounted filesystems backed by whole disk `disk` ('disk4' / 'sdb').

    Checks the device name first, then resolves the mount point itself, so an
    APFS synthesized volume or an LVM logical volume is still traced back to
    the physical drive underneath it.
    """
    if not disk:
        return []
    hits = []
    for device, mountpoint, readonly in _mount_table():
        owner = whole_disk_of_device(device)
        if owner != disk:
            owner = whole_disk_of_path(mountpoint)
        if owner == disk:
            hits.append((device, mountpoint, readonly))
    return hits
