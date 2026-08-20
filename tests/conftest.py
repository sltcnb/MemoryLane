import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows has no os.pread, so the package falls back to a locked seek+read.
# Setting MEMORYLANE_NO_PREAD=1 forces that path on POSIX too, which lets CI
# exercise the Windows read path on every platform instead of only on Windows.
if os.environ.get("MEMORYLANE_NO_PREAD") == "1":
    from memorylane import _io

    _io.HAVE_PREAD = False


@pytest.fixture
def evidence(tmp_path):
    """A 4 MiB pseudo-disk: compressible runs, random data and sparse zeros."""
    rnd = random.Random(1337)
    blocks = []
    for i in range(128):
        if i % 4 == 0:
            blocks.append(b"\x00" * 32768)
        elif i % 4 == 1:
            blocks.append((b"MFT_RECORD" * 3277)[:32768])
        else:
            blocks.append(rnd.randbytes(32768))
    data = b"".join(blocks)
    path = tmp_path / "evidence.bin"
    path.write_bytes(data)
    return path, data


@pytest.fixture
def failing_media(monkeypatch):
    """Make chosen sectors of one specific file unreadable.

    Patches the package's own positional-read seam rather than os.pread, so
    the simulation works in both modes: real pread on POSIX, and the locked
    seek+read fallback used on Windows.

    Scoped to the file's own descriptors, so reading back the image written
    from it still works — which is what a real dying source looks like.
    """
    def install(path, bad_lbas, sector_size=512, fail_times=None):
        from memorylane import _io

        target = os.path.abspath(str(path))
        state = {"fds": set(), "attempts": 0}
        real_open, real_close, real_pread = os.open, os.close, _io.pread

        def fake_open(p, *a, **kw):
            fd = real_open(p, *a, **kw)
            if os.path.abspath(os.fspath(p)) == target:
                state["fds"].add(fd)
            return fd

        def fake_close(fd):
            state["fds"].discard(fd)
            return real_close(fd)

        def fake_pread(fd, length, offset):
            if fd in state["fds"]:
                for lba in bad_lbas:
                    if offset <= lba * sector_size < offset + length:
                        state["attempts"] += 1
                        if fail_times is None or state["attempts"] <= fail_times:
                            raise OSError(5, "Input/output error")
            return real_pread(fd, length, offset)

        monkeypatch.setattr(os, "open", fake_open)
        monkeypatch.setattr(os, "close", fake_close)
        # Every module that did `from ._io import pread` holds its own
        # reference; rebind all of them so no read path escapes the simulation.
        monkeypatch.setattr(_io, "pread", fake_pread)
        for module in list(sys.modules.values()):
            if not getattr(module, "__name__", "").startswith("memorylane"):
                continue
            if getattr(module, "pread", None) is real_pread:
                monkeypatch.setattr(module, "pread", fake_pread)
        return state

    return install
