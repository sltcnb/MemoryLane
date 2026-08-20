"""Positional reads that also work on Windows.

`os.pread` is POSIX-only. Windows has no equivalent in the standard library,
so the fallback is seek-then-read — which is not atomic. That matters here:
the inflate pool issues concurrent reads against one descriptor, so an
unguarded seek+read would let one thread move the offset out from under
another. The fallback therefore serialises per descriptor. Only the read is
serialised; decompression still runs in parallel.
"""

import os
import threading

HAVE_PREAD = hasattr(os, "pread")

_locks = {}
_guard = threading.Lock()


def _lock_for(fd):
    lock = _locks.get(fd)
    if lock is None:
        with _guard:
            lock = _locks.setdefault(fd, threading.Lock())
    return lock


def pread(fd, length, offset):
    """Read `length` bytes at `offset` without disturbing the file position."""
    if HAVE_PREAD:
        # Looked up at call time so tests can substitute a failing reader.
        return os.pread(fd, length, offset)
    with _lock_for(fd):
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, length)


def forget(fd):
    """Drop the lock held for a descriptor that is being closed."""
    if not HAVE_PREAD:
        with _guard:
            _locks.pop(fd, None)


def close(fd):
    forget(fd)
    os.close(fd)
