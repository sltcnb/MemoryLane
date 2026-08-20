"""EWF (Expert Witness Format / EnCase .E01) writer and reader.

Pure stdlib implementation of the EWF-E01 (EnCase 5/6) layout that FTK Imager
and EnCase produce, so images written here open in FTK Imager, EnCase, X-Ways,
Autopsy/The Sleuth Kit and libewf without conversion.

Segment layout
    header2, header2, header, disk|volume      (segment 1)
    data                                       (segments 2+)
    [ sectors, table, table2 ] x N             (chunk groups)
    next                                       (non-final segment)
    digest, hash, done                         (final segment)

Every section starts with a 76-byte descriptor:
    type (16) | next offset (8) | size (8) | padding (40) | adler32 (4)
Sector data is stored in fixed chunks (default 64 sectors = 32 KiB), either
deflate-compressed or verbatim with a trailing adler32; the table sections map
chunk number to file offset.
"""

import os
import re
import struct
import time
import uuid
import zlib

from ._io import close as _close_fd, pread

SIGNATURE = b"EVF\x09\x0d\x0a\xff\x00"
DESCRIPTOR_SIZE = 76
VOLUME_SIZE = 1052
DEFAULT_SECTORS_PER_CHUNK = 64
DEFAULT_SEGMENT_SIZE = 1500 * 1024 * 1024   # FTK Imager's 1500 MB default
MAX_TABLE_ENTRIES = 16375                   # EnCase's per-table ceiling

MEDIA_TYPE = {"removable": 0x00, "fixed": 0x01, "optical": 0x03,
              "logical": 0x0e, "memory": 0x10}
MEDIA_FLAG_IMAGE = 0x01
MEDIA_FLAG_PHYSICAL = 0x02
COMPRESSION = {"none": 0, "fast": 1, "best": 2}
_ZLIB_LEVEL = {"none": 0, "fast": 1, "best": 9}


def default_workers():
    """Deflate threads to use. zlib drops the GIL, so threads really do run
    in parallel and compression stops being the acquisition bottleneck."""
    return max(1, min(8, (os.cpu_count() or 1) - 1))


class EwfError(Exception):
    pass


def _u32(b, o=0):
    return int.from_bytes(b[o:o + 4], "little")


def _u64(b, o=0):
    return int.from_bytes(b[o:o + 8], "little")


def segment_name(base, number):
    """image.E01 ... image.E99, then image.EAA ... (EnCase naming)."""
    if number < 1:
        raise ValueError("segment numbers start at 1")
    if number <= 99:
        return f"{base}.E{number:02d}"
    n = number - 100
    if n >= 26 * 26 * 25:
        raise EwfError("segment count exhausted")
    first = "EFGHIJKLMNOPQRSTUVWXYZ"[n // (26 * 26)]
    return f"{base}.{first}{chr(65 + (n // 26) % 26)}{chr(65 + n % 26)}"


def glob_segments(path):
    """Every segment of the set that `path` belongs to, in order."""
    m = re.match(r"(?i)^(.*)\.[eEsSlL](?:01|x01)$", path)
    if not m:
        return [path]
    base = m.group(1)
    segments, number = [], 1
    while True:
        candidate = segment_name(base, number)
        for probe in (candidate, candidate.lower()):
            if os.path.exists(probe):
                segments.append(probe)
                break
        else:
            break
        number += 1
    return segments or [path]


def _descriptor(section_type, next_offset, size):
    body = (section_type.ljust(16, b"\x00")
            + struct.pack("<QQ", next_offset, size)
            + b"\x00" * 40)
    return body + struct.pack("<I", zlib.adler32(body) & 0xFFFFFFFF)


# ------------------------------------------------------------------- header

# EnCase orders these differently in the two header flavours; both name their
# own columns, so readers key by name rather than position.
_HEADER_FIELDS = ("c", "n", "a", "e", "t", "av", "ov", "m", "u", "p", "r")
_HEADER2_FIELDS = ("a", "c", "n", "e", "t", "av", "ov", "m", "u", "p")


def _header_value(key, meta, acquired, now, compression):
    return {
        "c": meta.get("case_number", ""),
        "n": meta.get("evidence_number", ""),
        "a": meta.get("description", ""),
        "e": meta.get("examiner", ""),
        "t": (meta.get("notes", "") or "").replace("\t", " ").replace("\n", " "),
        "av": meta.get("software", ""),
        "ov": meta.get("os", ""),
        "m": str(int(acquired)),           # acquisition date, POSIX seconds
        "u": str(int(now)),                # system date
        "p": "0",                          # password hash: none
        "r": {"none": "n", "fast": "f", "best": "b"}[compression],
    }[key]


def _header_record(fields, meta, acquired, now, compression, version):
    values = "\t".join(_header_value(k, meta, acquired, now, compression)
                       for k in fields)
    return (f"{version}\r\nmain\r\n{chr(9).join(fields)}\r\n"
            f"{values}\r\n\r\n")


def build_header2(meta, acquired, now, compression="fast"):
    """EnCase 5 `header2`: UTF-16LE with a BOM, deflated."""
    text = _header_record(_HEADER2_FIELDS, meta, acquired, now, compression, 3)
    return zlib.compress(b"\xff\xfe" + text.encode("utf-16-le"), 9)


def build_header(meta, acquired, now, compression="fast"):
    """Legacy ASCII `header`, kept for readers that only understand EnCase 4."""
    text = _header_record(_HEADER_FIELDS, meta, acquired, now, compression, 1)
    return zlib.compress(text.encode("ascii", "replace"), 9)


def parse_header(data):
    """Decompress a header/header2 section back into a dict of EWF fields."""
    try:
        raw = zlib.decompress(data)
    except zlib.error:
        return {}
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", "replace")
    else:
        text = raw.decode("latin-1", "replace")
    lines = text.replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if line.startswith("c\t") or line.startswith("a\t"):
            keys = line.split("\t")
            values = lines[i + 1].split("\t") if i + 1 < len(lines) else []
            return dict(zip(keys, values))
    return {}


def _volume(media_type, media_flags, chunk_count, sectors_per_chunk,
            bytes_per_sector, sector_count, cylinders, heads, spt,
            compression, error_granularity, guid):
    body = bytearray(VOLUME_SIZE - 4)
    body[0] = media_type
    struct.pack_into("<III", body, 4, chunk_count, sectors_per_chunk,
                     bytes_per_sector)
    struct.pack_into("<Q", body, 16, sector_count)
    struct.pack_into("<III", body, 24, cylinders, heads, spt)
    body[36] = media_flags
    body[52] = compression
    struct.pack_into("<I", body, 56, error_granularity)
    body[64:80] = guid
    return bytes(body) + struct.pack("<I", zlib.adler32(bytes(body)) & 0xFFFFFFFF)


def _error2(errors):
    """`error2` section: the sector ranges that could not be read.

    Without it, zero-filled bad sectors are indistinguishable from genuine
    zeros to anything reading the image. Entries are 32-bit, so ranges beyond
    2^32 sectors cannot be represented and are dropped.
    """
    usable = [(first, count) for first, count in errors if first < 1 << 32]
    head = struct.pack("<I", len(usable)) + b"\x00" * 512
    head += struct.pack("<I", zlib.adler32(head) & 0xFFFFFFFF)
    body = b"".join(struct.pack("<II", first, min(count, 0xFFFFFFFF))
                    for first, count in usable)
    return head + body + struct.pack("<I", zlib.adler32(body) & 0xFFFFFFFF)


ERROR2_HEADER_SIZE = 520


def _table(entries, base_offset):
    head = struct.pack("<IIQI", len(entries), 0, base_offset, 0)
    head += struct.pack("<I", zlib.adler32(head) & 0xFFFFFFFF)
    body = b"".join(struct.pack("<I", e) for e in entries)
    return head + body + struct.pack("<I", zlib.adler32(body) & 0xFFFFFFFF)


# ------------------------------------------------------------------- writer

class EwfWriter:
    """Streaming E01 writer. Feed bytes with write(), then close()."""

    def __init__(self, base, media_size, sector_size=512, *, meta=None,
                 compression="fast", segment_size=DEFAULT_SEGMENT_SIZE,
                 sectors_per_chunk=DEFAULT_SECTORS_PER_CHUNK,
                 media_type="fixed", physical=True, geometry=None,
                 error_granularity=None, acquired=None, created=None,
                 guid=None, workers=None, _resume=None):
        if compression not in COMPRESSION:
            raise ValueError(f"unknown compression: {compression}")
        self.base = base
        self.sector_size = sector_size
        self.sectors_per_chunk = sectors_per_chunk
        self.chunk_size = sector_size * sectors_per_chunk
        # EWF stores whole sectors; a trailing partial sector is zero-padded.
        self.media_size = -(-media_size // sector_size) * sector_size
        self.sector_count = self.media_size // sector_size
        self.chunk_count = -(-self.media_size // self.chunk_size)
        self.compression = compression
        self.level = _ZLIB_LEVEL[compression]
        # Floor keeps a segment able to hold at least a couple of chunks.
        self.segment_size = max(segment_size, self.chunk_size * 4) if segment_size else 0
        self.meta = dict(meta or {})
        self.media_type = MEDIA_TYPE.get(media_type, MEDIA_TYPE["fixed"])
        self.media_flags = MEDIA_FLAG_IMAGE | (MEDIA_FLAG_PHYSICAL if physical else 0)
        self.geometry = geometry or (self.sector_count // (255 * 63), 255, 63)
        self.error_granularity = error_granularity or sectors_per_chunk
        self.acquired = acquired or time.time()
        # `created` is EWF's system date; the set identifier must be shared by
        # every segment of one acquisition. Both are injectable so an image can
        # be reproduced byte for byte.
        self.created = created or time.time()
        self.guid = guid or uuid.uuid4().bytes

        self.segments = []
        self._buf = bytearray()
        self._written = 0            # media bytes accepted from the caller
        self._chunks_done = 0
        self._f = None
        self._segment_number = 0
        self._group = None           # (descriptor offset, base offset, entries)
        self._md5 = self._sha1 = None
        self._errors = []            # [(first sector, sector count)]
        self.trimmed = 0             # bytes dropped from an interrupted tail
        self._closed = False
        workers = default_workers() if workers is None else max(1, int(workers))
        self.workers = workers if self.level else 1
        self._pending = []           # chunks awaiting parallel compression
        self._batch = self.workers * 4
        self._pool = None
        if self.workers > 1:
            from concurrent.futures import ThreadPoolExecutor
            self._pool = ThreadPoolExecutor(max_workers=self.workers,
                                            thread_name_prefix="mlane-deflate")
        if _resume is None:
            self._open_segment()
        else:
            self._reopen_segment(_resume)

    @classmethod
    def resume(cls, base, media_size, sector_size=512, **kwargs):
        """Reopen an unfinished set and continue writing where it stopped.

        Returns (writer, resume_offset). Everything the format fixes at
        creation time — compression, chunk size, media geometry, the set
        identifier — is taken from the existing image rather than the caller,
        so the continued segments stay consistent with the ones already on
        disk. A tail left half-written by a hard kill is trimmed first.
        """
        first = segment_name(base, 1)
        if not os.path.exists(first):
            raise EwfError(f"nothing to resume: {first} does not exist")
        with EwfReader(first) as existing:
            if existing.complete:
                raise EwfError("this image is already complete")
            if not existing.sector_count:
                raise EwfError("existing image has no readable volume section")
            wanted = -(-media_size // existing.sector_size) * existing.sector_size
            if existing.size != wanted:
                raise EwfError(
                    f"source is {wanted} bytes but the image was started for "
                    f"{existing.size}; refusing to mix two acquisitions")
            if sector_size and existing.sector_size != sector_size:
                raise EwfError(
                    f"sector size changed: image says {existing.sector_size}, "
                    f"source says {sector_size}")
            state = {
                "segments": list(existing.segments),
                "chunks": existing.chunks_present,
                "tail": existing.tail_offset,
            }
            names = {v: k for k, v in MEDIA_TYPE.items()}
            levels = {v: k for k, v in COMPRESSION.items()}
            kwargs.update(
                sectors_per_chunk=existing.sectors_per_chunk,
                compression=levels.get(existing.compression, "fast"),
                media_type=names.get(existing.media_type, "fixed"),
                physical=bool(existing.media_flags & MEDIA_FLAG_PHYSICAL),
                geometry=existing.geometry,
                error_granularity=existing.error_granularity,
                guid=existing.set_identifier,
            )
        writer = cls(base, media_size, existing.sector_size, _resume=state,
                     **kwargs)
        return writer, state["chunks"] * writer.chunk_size

    def _reopen_segment(self, state):
        self.segments = list(state["segments"])
        self._segment_number = len(self.segments)
        self._chunks_done = state["chunks"]
        self._written = self._chunks_done * self.chunk_size
        if self._written > self.media_size:
            raise EwfError("existing image already holds more than the source")
        path = self.segments[-1]
        tail = state["tail"]
        self.trimmed = max(0, os.path.getsize(path) - tail)
        self._f = open(path, "r+b")
        if self.trimmed:
            # Bytes after the last intact section are an unreferenced fragment
            # of an interrupted write; nothing points at them.
            self._f.truncate(tail)
        self._f.seek(tail)

    # -- segment plumbing ---------------------------------------------------

    def _open_segment(self):
        self._segment_number += 1
        path = segment_name(self.base, self._segment_number)
        self._f = open(path, "wb")
        self.segments.append(path)
        self._f.write(SIGNATURE + struct.pack("<BHH", 1, self._segment_number, 0))
        if self._segment_number == 1:
            header2 = build_header2(self.meta, self.acquired, self.created,
                                    self.compression)
            self._section(b"header2", header2)
            self._section(b"header2", header2)
            self._section(b"header", build_header(self.meta, self.acquired,
                                                  self.created, self.compression))
            self._section(b"disk" if self.media_flags & MEDIA_FLAG_PHYSICAL
                          else b"volume", self._volume_section())
        else:
            self._section(b"data", self._volume_section())

    def _volume_section(self):
        cyl, heads, spt = self.geometry
        return _volume(self.media_type, self.media_flags, self.chunk_count,
                       self.sectors_per_chunk, self.sector_size,
                       self.sector_count, cyl, heads, spt,
                       COMPRESSION[self.compression], self.error_granularity,
                       self.guid)

    def _section(self, section_type, data=b""):
        offset = self._f.tell()
        size = DESCRIPTOR_SIZE + len(data)
        self._f.write(_descriptor(section_type, offset + size, size))
        self._f.write(data)

    def _terminator(self, section_type):
        offset = self._f.tell()
        # The closing section of a segment points at itself.
        self._f.write(_descriptor(section_type, offset, DESCRIPTOR_SIZE))

    def _close_segment(self, final):
        self._drain()
        self._flush_group()
        if final:
            if self._errors:
                self._section(b"error2", _error2(self._errors))
            self._section(b"digest", self._digest_section())
            self._section(b"hash", self._hash_section())
            self._terminator(b"done")
        else:
            self._terminator(b"next")
        self._f.close()
        self._f = None

    # -- chunk groups -------------------------------------------------------

    def _start_group(self):
        descriptor_offset = self._f.tell()
        self._f.write(b"\x00" * DESCRIPTOR_SIZE)   # patched by _flush_group
        self._group = (descriptor_offset, self._f.tell(), [])

    def _flush_group(self):
        if not self._group:
            return
        descriptor_offset, base_offset, entries = self._group
        self._group = None
        end = self._f.tell()
        self._f.seek(descriptor_offset)
        self._f.write(_descriptor(b"sectors", end, end - descriptor_offset))
        self._f.seek(end)
        table = _table(entries, base_offset)
        self._section(b"table", table)
        self._section(b"table2", table)

    def _pack(self, data):
        """Encode one chunk: deflated, or verbatim plus a trailing adler32.

        Pure function of `data` — safe to run on a worker thread.
        """
        data = bytes(data)
        if self.level:
            packed = zlib.compress(data, self.level)
            if len(packed) < len(data):
                return packed, True
        return data + struct.pack("<I", zlib.adler32(data) & 0xFFFFFFFF), False

    def _write_chunk(self, data):
        self._emit(*self._pack(data))

    def _emit(self, packed, compressed):
        """Append an already-encoded chunk. Single-threaded: owns the file
        position, the table entries and the segment rollover."""
        if self._group is None:
            self._start_group()
        _, base_offset, entries = self._group
        offset = self._f.tell() - base_offset
        if offset > 0x7FFFFFFF:
            raise EwfError("chunk group exceeded the 2 GiB table addressing limit")
        entries.append(offset | (0x80000000 if compressed else 0))
        self._f.write(packed)
        self._chunks_done += 1

        if len(entries) >= MAX_TABLE_ENTRIES:
            self._flush_group()
        if self.segment_size and self._f.tell() >= self.segment_size:
            self._flush_group()
            if self._chunks_done < self.chunk_count:
                self._close_segment(final=False)
                self._open_segment()

    # -- digests ------------------------------------------------------------

    def set_hashes(self, md5=None, sha1=None):
        """Digests of the acquired data, written into the hash/digest sections."""
        self._md5 = md5
        self._sha1 = sha1

    def set_errors(self, ranges):
        """Unreadable sector ranges as (first sector, count), recorded in the
        `error2` section so the image itself carries its own defect list."""
        self._errors = [(int(first), int(count)) for first, count in ranges]

    def _hash_section(self):
        md5 = self._md5 or b"\x00" * 16
        body = md5[:16].ljust(16, b"\x00") + b"\x00" * 16
        return body + struct.pack("<I", zlib.adler32(body) & 0xFFFFFFFF)

    def _digest_section(self):
        md5 = self._md5 or b"\x00" * 16
        sha1 = self._sha1 or b"\x00" * 20
        body = (md5[:16].ljust(16, b"\x00") + sha1[:20].ljust(20, b"\x00")
                + b"\x00" * 40)
        return body + struct.pack("<I", zlib.adler32(body) & 0xFFFFFFFF)

    # -- public API ---------------------------------------------------------

    def write(self, data):
        if self._closed:
            raise EwfError("writer is closed")
        if self._written + len(data) > self.media_size:
            raise EwfError(
                f"more data than the declared media size: "
                f"{self._written + len(data)} > {self.media_size} bytes")
        self._buf += data
        self._written += len(data)
        while len(self._buf) >= self.chunk_size:
            self._queue(bytes(self._buf[:self.chunk_size]))
            del self._buf[:self.chunk_size]
        return len(data)

    def _queue(self, chunk):
        if self._pool is None:
            self._write_chunk(chunk)
            return
        self._pending.append(chunk)
        if len(self._pending) >= self._batch:
            self._drain()

    def _drain(self):
        """Compress the queued chunks in parallel, then write them in order."""
        if not self._pending:
            return
        pending, self._pending = self._pending, []
        for packed, compressed in self._pool.map(self._pack, pending):
            self._emit(packed, compressed)

    def abort(self):
        """Close an interrupted acquisition *without* the digest/hash/done
        sections, so the set reads back as incomplete instead of looking like a
        finished image whose tail happens to be zeros. The trailing sub-chunk
        remainder is dropped; whole chunks already read are kept."""
        if self._closed:
            return self.segments
        while len(self._buf) >= self.chunk_size:
            self._queue(bytes(self._buf[:self.chunk_size]))
            del self._buf[:self.chunk_size]
        self._buf = bytearray()
        self._drain()
        self._flush_group()
        self._f.close()
        self._f = None
        self._closed = True
        self._shutdown_pool()
        return self.segments

    def _shutdown_pool(self):
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None

    def close(self):
        if self._closed:
            return self.segments
        # Pad the tail out to a whole sector, exactly as EnCase/FTK do.
        if self._written < self.media_size:
            self._buf += b"\x00" * (self.media_size - self._written)
            self._written = self.media_size
        while len(self._buf) >= self.chunk_size:
            self._queue(bytes(self._buf[:self.chunk_size]))
            del self._buf[:self.chunk_size]
        self._drain()
        if self._buf:
            self._write_chunk(bytes(self._buf))
            self._buf = bytearray()
        self._close_segment(final=True)
        self._closed = True
        self._shutdown_pool()
        return self.segments

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ------------------------------------------------------------------- reader

class EwfReader:
    """Reads an E01 set: media size, metadata, stored digests, chunk data."""

    def __init__(self, path, workers=None):
        self.path = path
        self.segments = glob_segments(path)
        self._fds = {}
        self.workers = default_workers() if workers is None else max(1, int(workers))
        self._pool = None
        self.sector_size = 512
        self.sectors_per_chunk = DEFAULT_SECTORS_PER_CHUNK
        self.chunk_size = DEFAULT_SECTORS_PER_CHUNK * 512
        self.sector_count = 0
        self.size = 0
        self.chunk_count = 0
        self.media_type = None
        self.media_flags = 0
        self.compression = None
        self.error_granularity = DEFAULT_SECTORS_PER_CHUNK
        self.set_identifier = None
        self.tail_offset = 0         # end of the last intact section
        self.geometry = (0, 0, 0)
        self.metadata = {}
        self.stored_md5 = None
        self.stored_sha1 = None
        self._chunks = []            # (segment index, offset, size, compressed)
        self.corrupt_chunks = []     # [(chunk number, reason)]
        self.read_errors = []        # [(first sector, count)] from `error2`
        self.complete = False        # set when a 'done' section closes the set
        try:
            self._parse()
        except BaseException:
            self.close()
            raise

    def _fd(self, index):
        fd = self._fds.get(index)
        if fd is None:
            fd = os.open(self.segments[index], os.O_RDONLY)
            self._fds[index] = fd
        return fd

    @property
    def chunks_present(self):
        """Chunks actually referenced by a table — what a resume can build on."""
        return len(self._chunks)

    def _parse(self):
        last_section = None
        for index, path in enumerate(self.segments):
            fd = self._fd(index)
            if pread(fd, 8, 0) != SIGNATURE:
                raise EwfError(f"{path}: not an EWF/E01 segment")
            offset = 13
            self.tail_offset = offset
            sectors_start = sectors_end = 0
            while True:
                descriptor = pread(fd, DESCRIPTOR_SIZE, offset)
                if len(descriptor) < DESCRIPTOR_SIZE:
                    break
                stype = descriptor[:16].split(b"\x00", 1)[0]
                next_offset = _u64(descriptor, 16)
                size = _u64(descriptor, 24)
                data_offset = offset + DESCRIPTOR_SIZE
                if not stype:
                    break       # zeroed descriptor: the intact tail stops here
                # libewf writes the closing next/done section with size 0;
                # we write 76. Both are read, so normalise before measuring.
                self.tail_offset = offset + max(size, DESCRIPTOR_SIZE)

                if stype in (b"volume", b"disk", b"data"):
                    self._read_volume(pread(fd, VOLUME_SIZE, data_offset))
                elif stype in (b"header", b"header2") and not self.metadata:
                    self.metadata = parse_header(
                        pread(fd, size - DESCRIPTOR_SIZE, data_offset))
                elif stype == b"sectors":
                    sectors_start, sectors_end = data_offset, offset + size
                elif stype == b"table":     # table2 is a verbatim duplicate
                    self._chunks.extend(self._read_table(
                        fd, index, data_offset, sectors_start, sectors_end))
                elif stype == b"error2":
                    self._read_error2(fd, data_offset)
                elif stype == b"digest":
                    body = pread(fd, 36, data_offset)
                    self.stored_md5 = body[:16].hex()
                    self.stored_sha1 = body[16:36].hex()
                elif stype == b"hash":
                    body = pread(fd, 16, data_offset)
                    if not self.stored_md5:
                        self.stored_md5 = body.hex()
                last_section = stype
                if next_offset in (0, offset) or next_offset <= offset:
                    break
                offset = next_offset
        self.complete = last_section == b"done"
        if self.stored_md5 == "0" * 32:
            self.stored_md5 = None
        if self.stored_sha1 == "0" * 40:
            self.stored_sha1 = None

    def _read_volume(self, body):
        if len(body) < 64:
            return
        self.media_type = body[0]
        self.chunk_count = _u32(body, 4)
        self.sectors_per_chunk = _u32(body, 8) or DEFAULT_SECTORS_PER_CHUNK
        self.sector_size = _u32(body, 12) or 512
        self.sector_count = _u64(body, 16)
        self.geometry = (_u32(body, 24), _u32(body, 28), _u32(body, 32))
        self.media_flags = body[36]
        self.compression = body[52]
        self.error_granularity = _u32(body, 56) or DEFAULT_SECTORS_PER_CHUNK
        self.set_identifier = bytes(body[64:80])
        self.chunk_size = self.sectors_per_chunk * self.sector_size
        self.size = self.sector_count * self.sector_size

    def _read_table(self, fd, index, data_offset, sectors_start, sectors_end):
        head = pread(fd, 24, data_offset)
        count = _u32(head, 0)
        base = _u64(head, 8)
        if not count:
            return []
        raw = pread(fd, count * 4, data_offset + 24)
        offsets = []
        for i in range(count):
            value = _u32(raw, i * 4)
            offsets.append(((value & 0x7FFFFFFF) + base, bool(value & 0x80000000)))
        chunks = []
        for i, (start, compressed) in enumerate(offsets):
            end = offsets[i + 1][0] if i + 1 < count else (sectors_end or start)
            chunks.append((index, start, max(end - start, 0), compressed))
        return chunks

    def chunk_length(self, number):
        """Payload length of a chunk once decoded (the last one may be short)."""
        if not self.size:
            return self.chunk_size
        return max(0, min(self.chunk_size, self.size - number * self.chunk_size))

    def _corrupt(self, number, reason):
        self.corrupt_chunks.append((number, reason))

    def _read_error2(self, fd, data_offset):
        head = pread(fd, ERROR2_HEADER_SIZE, data_offset)
        count = _u32(head, 0)
        if not count:
            return
        raw = pread(fd, count * 8, data_offset + ERROR2_HEADER_SIZE)
        for i in range(count):
            self.read_errors.append((_u32(raw, i * 8), _u32(raw, i * 8 + 4)))

    def read_chunk(self, number):
        """Decode one chunk. Damaged chunks are zero-filled and recorded in
        `corrupt_chunks` rather than raising, so a verification pass over a
        failing image still completes and reports a hash mismatch."""
        index, offset, size, compressed = self._chunks[number]
        want = self.chunk_length(number)
        raw = pread(self._fd(index), size or self.chunk_size + 4, offset)
        if compressed:
            try:
                data = zlib.decompress(raw)
            except zlib.error:
                try:
                    data = zlib.decompressobj().decompress(raw, self.chunk_size)
                except zlib.error:
                    self._corrupt(number, "decompression failed")
                    return b"\x00" * want
        else:
            data = raw[:want]
            stored = raw[want:want + 4]
            if len(stored) == 4 and _u32(stored) != (zlib.adler32(data) & 0xFFFFFFFF):
                self._corrupt(number, "checksum mismatch")
        if len(data) < want:
            self._corrupt(number, "truncated")
            data += b"\x00" * (want - len(data))
        return data[:want]

    def read(self, offset, length):
        if offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        out = bytearray()
        position = offset
        while len(out) < length:
            number = position // self.chunk_size
            if number >= len(self._chunks):
                break
            chunk = self.read_chunk(number)
            start = position - number * self.chunk_size
            take = min(len(chunk) - start, length - len(out))
            if take <= 0:
                break
            out += chunk[start:start + take]
            position += take
        return bytes(out)

    def stream(self, block_size=1 << 20, limit=None):
        """Yield the media contents in order, honouring the recorded size.

        Chunks are inflated on a thread pool (zlib drops the GIL) but always
        emitted in order, so the byte stream is identical to a serial read.
        `limit` stops after that many bytes — used to rebuild hash state when
        resuming an interrupted acquisition.
        """
        remaining = self.size if limit is None else max(0, min(limit, self.size))
        pool = self._inflate_pool()
        window = max(1, self.workers * 8)
        number = 0
        batch = bytearray()
        while remaining > 0 and number < len(self._chunks):
            upto = min(number + window, len(self._chunks))
            numbers = range(number, upto)
            decoded = (pool.map(self.read_chunk, numbers) if pool
                       else (self.read_chunk(n) for n in numbers))
            for chunk in decoded:
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                remaining -= len(chunk)
                batch += chunk
                if len(batch) >= block_size:
                    yield bytes(batch)
                    batch = bytearray()
            number = upto
        if batch:
            yield bytes(batch)
        if remaining > 0:
            # Table entries ran out before the recorded media size: report the
            # gap as zeros so digests still cover the full declared length.
            self._corrupt(number, f"missing {remaining} trailing bytes")
            yield b"\x00" * remaining

    def _inflate_pool(self):
        if self._pool is None and self.workers > 1:
            from concurrent.futures import ThreadPoolExecutor
            self._pool = ThreadPoolExecutor(max_workers=self.workers,
                                            thread_name_prefix="mlane-inflate")
        return self._pool

    def close(self):
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None
        for fd in self._fds.values():
            _close_fd(fd)
        self._fds.clear()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
