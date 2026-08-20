"""Drive the real curses screen through a pseudo-terminal.

The model tests cover the form; this covers the parts only a terminal
exercises — drawing, key dispatch and the worker thread — by running an actual
acquisition from keystrokes and checking the evidence lands on disk.
"""

import fcntl
import os
import re
import select
import struct
import subprocess
import sys
import termios
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "openpty"),
    reason="needs a POSIX pseudo-terminal")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ncurses puts the terminal in application-cursor mode (smkx) when keypad is
# enabled, so arrows arrive as ESC O x, not ESC [ x.
UP, DOWN, ENTER = b"\x1bOA", b"\x1bOB", b"\r"
BACKSPACE = b"\x7f"


class Screen:
    """A TUI running on a pty, with helpers to type and to wait for text."""

    def __init__(self, cwd):
        self.master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 40, 120, 0, 0))
        env = dict(os.environ, TERM="xterm", PYTHONPATH=ROOT)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "memorylane", "tui"],
            stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
            close_fds=True)
        os.close(slave)
        self.buffer = ""

    def read(self, timeout=0.4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                self.buffer += chunk.decode("utf-8", "replace")
        return self.buffer

    def wait_for(self, pattern, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read(0.2)
            if re.search(pattern, self.plain()):
                return True
        return False

    def plain(self):
        """Strip escape sequences so assertions read like the visible text."""
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][B0]|\x1b[=>]",
                      " ", self.buffer)

    def type(self, data, settle=0.25):
        os.write(self.master, data if isinstance(data, bytes)
                 else data.encode())
        time.sleep(settle)
        self.read(0.15)

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        finally:
            os.close(self.master)


@pytest.fixture
def screen(tmp_path):
    s = Screen(str(tmp_path))
    try:
        yield s
    finally:
        s.close()


def test_tui_starts_and_lists_sources(screen):
    assert screen.wait_for(r"MemoryLane", timeout=15)
    assert screen.wait_for(r"Select a source to image", timeout=15)
    assert "File or path" in screen.plain()
    assert "q quit" in screen.plain()


def test_tui_quits_cleanly(screen):
    assert screen.wait_for(r"Select a source", timeout=15)
    screen.type(b"q")
    assert screen.proc.wait(timeout=10) == 0


def goto_manual_entry(screen):
    """Move the selection onto the 'File or path' row."""
    for _ in range(25):
        if re.search(r"->\s+File or path", screen.plain()):
            return True
        screen.type(DOWN, settle=0.12)
    return False


def goto_form_row(screen, label):
    """Move the form selection onto the row with this label."""
    for _ in range(25):
        if re.search(rf"->\s+{re.escape(label)}", screen.plain()):
            return True
        screen.type(DOWN, settle=0.12)
    return False


def test_tui_acquires_an_image_from_keystrokes(screen, tmp_path):
    """The whole point: type a path, fill the form, watch it verify."""
    source = tmp_path / "evidence.bin"
    source.write_bytes(bytes(range(256)) * 4096)        # 1 MiB

    assert screen.wait_for(r"Select a source", timeout=20)
    assert goto_manual_entry(screen), screen.plain()[-1500:]
    screen.type(str(source))
    assert screen.wait_for(re.escape(source.name), timeout=10)
    screen.type(ENTER)

    assert screen.wait_for(r"Output base", timeout=15), screen.plain()[-1500:]
    screen.type(BACKSPACE * 250, settle=0.4)            # clear the suggestion
    target = tmp_path / "fromtui"
    screen.type(str(target))

    assert goto_form_row(screen, "Segment size")
    screen.type(BACKSPACE * 16, settle=0.3)
    screen.type("0")
    assert screen.wait_for(r"mlane acquire", timeout=10)

    assert goto_form_row(screen, "Start acquisition")
    screen.type(ENTER, settle=0.5)
    assert screen.wait_for(r"VERIFIED|finished", timeout=90), \
        screen.plain()[-2500:]

    assert (tmp_path / "fromtui.E01").exists()
    summary = (tmp_path / "fromtui.E01.txt").read_text()
    assert ": verified" in summary

    from memorylane import ewf
    with ewf.EwfReader(str(tmp_path / "fromtui.E01")) as r:
        assert b"".join(r.stream()) == source.read_bytes()


def test_tui_refuses_an_empty_output(screen, tmp_path):
    source = tmp_path / "e.bin"
    source.write_bytes(b"\x00" * 4096)
    assert screen.wait_for(r"Select a source", timeout=20)
    assert goto_manual_entry(screen)
    screen.type(str(source))
    screen.type(ENTER)
    assert screen.wait_for(r"Output base", timeout=15)
    screen.type(BACKSPACE * 250, settle=0.4)
    assert goto_form_row(screen, "Start acquisition")
    screen.type(ENTER, settle=0.5)
    assert screen.wait_for(r"cannot start", timeout=10)
    assert not list(tmp_path.glob("*.E01"))
