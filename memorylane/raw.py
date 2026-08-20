"""Raw (dd) evidence writer and reader, including FTK-style split segments.

FTK Imager's "Raw (dd)" output is a plain byte-for-byte copy, optionally split
into fixed-size segments named image.001, image.002, ... MemoryLane matches
that, and can also emit a single unsplit file.
"""

import os
import re

DEFAULT_SEGMENT_SIZE = 1500 * 1024 * 1024


def segment_name(base, number, digits=3, single=False):
    return base if single else f"{base}.{number:0{digits}d}"


def glob_segments(path):
    """Every .NNN segment belonging to the set that `path` starts."""
    m = re.match(r"^(.*)\.(\d{2,3})$", path)
    if not m:
        return [path]
    base, digits = m.group(1), len(m.group(2))
    segments, number = [], int(m.group(2))
    while True:
        candidate = f"{base}.{number:0{digits}d}"
        if not os.path.exists(candidate):
            break
        segments.append(candidate)
        number += 1
    return segments or [path]


class RawWriter:
    """Streaming raw writer that rolls over to a new segment on demand."""

    @classmethod
    def resume(cls, base, *, segment_size=DEFAULT_SEGMENT_SIZE, single=False,
               digits=3):
        """Reopen an existing raw set and append. Returns (writer, offset)."""
        first = segment_name(base, 1, digits, single)
        if not os.path.exists(first):
            raise FileNotFoundError(f"nothing to resume: {first} does not exist")
        segments = [first] if single else glob_segments(first)
        writer = cls(base, segment_size=segment_size, single=single,
                     digits=digits, _resume=segments)
        return writer, sum(os.path.getsize(p) for p in segments)

    def __init__(self, base, *, segment_size=DEFAULT_SEGMENT_SIZE, single=False,
                 digits=3, _resume=None):
        self.base = base
        self.segment_size = 0 if single else segment_size
        self.single = single
        self.digits = digits
        self.segments = []
        self._f = None
        self._in_segment = 0
        self._number = 0
        self._closed = False
        if _resume is None:
            self._open_segment()
        else:
            self.segments = list(_resume)
            self._number = len(self.segments)
            self._f = open(self.segments[-1], "r+b")
            self._f.seek(0, os.SEEK_END)
            self._in_segment = self._f.tell()

    def _open_segment(self):
        self._number += 1
        path = segment_name(self.base, self._number, self.digits, self.single)
        self._f = open(path, "wb")
        self.segments.append(path)
        self._in_segment = 0

    def write(self, data):
        view = memoryview(data)
        while view:
            if self.segment_size and self._in_segment >= self.segment_size:
                self._f.close()
                self._open_segment()
            take = len(view)
            if self.segment_size:
                take = min(take, self.segment_size - self._in_segment)
            self._f.write(view[:take])
            self._in_segment += take
            view = view[take:]
        return len(data)

    def close(self):
        if not self._closed and self._f:
            self._f.close()
            self._f = None
            self._closed = True
        return self.segments

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class RawReader:
    """Reads a raw image, transparently spanning .001/.002/... segments."""

    def __init__(self, path):
        self.path = path
        self.segments = glob_segments(path)
        self._map = []          # (start offset, size, path)
        total = 0
        for segment in self.segments:
            size = os.path.getsize(segment)
            self._map.append((total, size, segment))
            total += size
        self.size = total
        self._fds = {}

    def _fd(self, path):
        fd = self._fds.get(path)
        if fd is None:
            fd = os.open(path, os.O_RDONLY)
            self._fds[path] = fd
        return fd

    def read(self, offset, length):
        if offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        out = bytearray()
        position = offset
        while len(out) < length:
            for start, size, path in self._map:
                if start <= position < start + size:
                    take = min(length - len(out), start + size - position)
                    out += os.pread(self._fd(path), take, position - start)
                    position += take
                    break
            else:
                break
        return bytes(out)

    def stream(self, block_size=1 << 20, limit=None):
        """Yield the image in order; `limit` stops after that many bytes."""
        remaining = self.size if limit is None else max(0, min(limit, self.size))
        for _, size, path in self._map:
            if remaining <= 0:
                break
            fd = self._fd(path)
            position = 0
            while position < size and remaining > 0:
                want = min(block_size, size - position, remaining)
                data = os.pread(fd, want, position)
                if not data:
                    break
                position += len(data)
                remaining -= len(data)
                yield data

    def close(self):
        for fd in self._fds.values():
            os.close(fd)
        self._fds.clear()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
