"""Targeted logical acquisition: collect chosen files, not a whole disk.

Modern IR rarely images 2 TB of disk; it collects the paths that matter. This
writes those files into a single ZIP container alongside a manifest that
records, per file, the source path, size, MD5/SHA1 and the full-resolution
timestamps the ZIP format itself cannot hold. The container is then verified by
re-reading every member and re-hashing it.

The container is a plain ZIP on purpose: it opens anywhere, and every claim it
makes about the evidence lives in a manifest that can be checked independently.
"""

import fnmatch
import json
import os
import stat
import zipfile

from .hashing import MultiHash

MANIFEST_CSV = "MemoryLane-manifest.csv"
MANIFEST_JSON = "MemoryLane-manifest.json"
COMPRESSION = {"none": zipfile.ZIP_STORED,
               "fast": zipfile.ZIP_DEFLATED,
               "best": zipfile.ZIP_DEFLATED}
_LEVEL = {"none": None, "fast": 1, "best": 9}

CSV_COLUMNS = ("archive_path", "source_path", "type", "size", "md5", "sha1",
               "modified", "accessed", "changed", "mode", "uid", "gid",
               "symlink_target", "error")


class CollectionError(Exception):
    pass


def archive_name(path):
    """Absolute source path -> a collision-free, provenance-preserving name."""
    path = os.path.abspath(path)
    drive, rest = os.path.splitdrive(path)
    rest = rest.replace(os.sep, "/").lstrip("/")
    if drive:
        rest = f"{drive.rstrip(':').lower()}/{rest}"
    return rest or "root"


def _kind(st):
    if stat.S_ISDIR(st.st_mode):
        return "directory"
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return "special"


class Entry:
    """One collected object and everything recorded about it."""

    __slots__ = CSV_COLUMNS

    def __init__(self, **kw):
        for column in CSV_COLUMNS:
            setattr(self, column, kw.get(column))

    def as_dict(self):
        return {c: getattr(self, c) for c in CSV_COLUMNS}


def walk(paths, *, excludes=(), max_size=0, follow_symlinks=False,
         include_dirs=False):
    """Yield absolute paths to collect, deterministically ordered.

    Anything unreadable is still yielded; the collector records the error
    rather than silently dropping the file from the evidence.
    """
    seen = set()
    for root in paths:
        root = os.path.abspath(root)
        if not os.path.lexists(root):
            yield root, FileNotFoundError(f"{root}: no such file or directory")
            continue
        if os.path.isdir(root) and not os.path.islink(root):
            for base, dirs, names in os.walk(root, followlinks=follow_symlinks):
                dirs.sort()
                if include_dirs and base not in seen:
                    seen.add(base)
                    yield base, None
                for name in sorted(names):
                    candidate = os.path.join(base, name)
                    if candidate in seen:
                        continue
                    seen.add(candidate)
                    if _excluded(candidate, excludes):
                        continue
                    if max_size and _too_big(candidate, max_size):
                        continue
                    yield candidate, None
        else:
            if root in seen or _excluded(root, excludes):
                continue
            seen.add(root)
            yield root, None


def _excluded(path, excludes):
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p)
               for p in excludes)


def _too_big(path, max_size):
    try:
        return os.lstat(path).st_size > max_size
    except OSError:
        return False


class Collector:
    """Writes the container and accumulates the manifest as it goes."""

    def __init__(self, target, *, compression="fast", hashes=("md5", "sha1"),
                 follow_symlinks=False):
        if compression not in COMPRESSION:
            raise CollectionError(f"unknown compression: {compression}")
        self.target = target
        self.compression = compression
        self.hashes = tuple(hashes)
        self.follow_symlinks = follow_symlinks
        self.entries = []
        self.errors = []
        self.bytes_collected = 0
        kwargs = {"compression": COMPRESSION[compression], "allowZip64": True}
        if _LEVEL[compression] is not None:
            kwargs["compresslevel"] = _LEVEL[compression]
        self._zip = zipfile.ZipFile(target, "w", **kwargs)

    def add(self, path, error=None, progress=None):
        name = archive_name(path)
        if error is not None:
            self._record(Entry(archive_path=name, source_path=path,
                               type="missing", error=str(error)))
            return
        try:
            st = os.lstat(path)
        except OSError as e:
            self._record(Entry(archive_path=name, source_path=path,
                               type="unknown", error=f"stat failed: {e.strerror}"))
            return

        entry = Entry(archive_path=name, source_path=path, type=_kind(st),
                      size=st.st_size, modified=st.st_mtime,
                      accessed=st.st_atime, changed=st.st_ctime,
                      mode=stat.filemode(st.st_mode), uid=st.st_uid,
                      gid=st.st_gid)

        if entry.type == "symlink" and not self.follow_symlinks:
            try:
                entry.symlink_target = os.readlink(path)
            except OSError as e:
                entry.error = f"readlink failed: {e.strerror}"
            self._record(entry)
            return
        if entry.type == "directory":
            self._record(entry)
            return
        if entry.type == "special":
            entry.error = "not a regular file; contents not collected"
            self._record(entry)
            return

        try:
            self._store(path, name, entry, progress)
        except OSError as e:
            # _record files the message; appending here too would double-count.
            entry.error = f"read failed: {e.strerror}"
        self._record(entry)

    def _store(self, path, name, entry, progress):
        hasher = MultiHash(self.hashes)
        info = zipfile.ZipInfo(name, date_time=_zip_time(entry.modified))
        info.compress_type = COMPRESSION[self.compression]
        info.external_attr = (os.lstat(path).st_mode & 0xFFFF) << 16
        written = 0
        try:
            with open(path, "rb") as src, self._zip.open(info, "w") as dst:
                while True:
                    block = src.read(1 << 20)
                    if not block:
                        break
                    dst.write(block)
                    hasher.update(block)
                    written += len(block)
                    if progress:
                        progress.advance(len(block))
        finally:
            hasher.close()
        # Per-file digests are MD5 + SHA1, matching what logical evidence
        # formats carry; any extra algorithms cover the container as a whole.
        digests = hasher.digests()
        entry.md5 = digests.get("md5")
        entry.sha1 = digests.get("sha1")
        if written != entry.size:
            # The file changed under us; the manifest states what was captured.
            entry.error = (f"size changed while reading: stat said "
                           f"{entry.size}, captured {written}")
            entry.size = written
        self.bytes_collected += written

    def _record(self, entry):
        self.entries.append(entry)
        if entry.error:
            self.errors.append(f"{entry.source_path}: {entry.error}")

    def close(self, meta=None):
        """Write the manifests into the container and finish it."""
        self._zip.writestr(MANIFEST_CSV, self.manifest_csv())
        self._zip.writestr(MANIFEST_JSON, self.manifest_json(meta))
        self._zip.close()
        return self.target

    def manifest_csv(self):
        import csv
        import io
        buf = io.StringIO(newline="")
        writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        for entry in self.entries:
            writer.writerow(entry.as_dict())
        return buf.getvalue()

    def manifest_json(self, meta=None):
        return json.dumps({
            "manifest_version": 1,
            "case": meta or {},
            "totals": self.totals(),
            "files": [e.as_dict() for e in self.entries],
        }, indent=2, sort_keys=True)

    def totals(self):
        kinds = {}
        for entry in self.entries:
            kinds[entry.type] = kinds.get(entry.type, 0) + 1
        return {"objects": len(self.entries), "bytes": self.bytes_collected,
                "errors": len(self.errors), "by_type": kinds}


def _zip_time(mtime):
    import time
    if not mtime:
        return (1980, 1, 1, 0, 0, 0)
    t = time.localtime(mtime)
    # ZIP cannot represent anything before 1980.
    if t.tm_year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min,
            t.tm_sec - t.tm_sec % 2)


def verify(target, hashes=("md5", "sha1"), progress=None):
    """Re-read every member and re-hash it against the stored manifest.

    Returns (checked, [problems]).
    """
    problems = []
    checked = 0
    with zipfile.ZipFile(target) as zf:
        try:
            manifest = json.loads(zf.read(MANIFEST_JSON))
        except KeyError:
            raise CollectionError(f"{target}: no {MANIFEST_JSON} in container")
        names = set(zf.namelist())
        for record in manifest["files"]:
            if record["type"] != "file" or record.get("error"):
                continue
            name = record["archive_path"]
            if name not in names:
                problems.append(f"{name}: listed in the manifest but not in "
                                "the container")
                continue
            hasher = MultiHash(hashes)
            try:
                with zf.open(name) as f:
                    while True:
                        block = f.read(1 << 20)
                        if not block:
                            break
                        hasher.update(block)
                        if progress:
                            progress.advance(len(block))
                digests = hasher.digests()
            except (OSError, zipfile.BadZipFile) as e:
                problems.append(f"{name}: unreadable in the container ({e})")
                continue
            finally:
                hasher.close()
            checked += 1
            for algorithm in ("md5", "sha1"):
                want = record.get(algorithm)
                if want and digests.get(algorithm) != want:
                    problems.append(
                        f"{name}: {algorithm} mismatch "
                        f"(manifest {want}, container {digests.get(algorithm)})")
        for name in names - {MANIFEST_CSV, MANIFEST_JSON}:
            if not any(r["archive_path"] == name for r in manifest["files"]):
                problems.append(f"{name}: in the container but not in the "
                                "manifest")
    return checked, problems
