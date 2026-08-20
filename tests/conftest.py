import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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

    Scoped to the file's own descriptors, so reading back the image written
    from it still works — which is what a real dying source looks like.
    """
    def install(path, bad_lbas, sector_size=512, fail_times=None):
        target = os.path.abspath(str(path))
        state = {"fds": set(), "attempts": 0}
        real_open, real_pread, real_close = os.open, os.pread, os.close

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
        monkeypatch.setattr(os, "pread", fake_pread)
        monkeypatch.setattr(os, "close", fake_close)
        return state

    return install
