"""Drive the real curses screen through a pseudo-terminal.

The model tests cover the form; this covers the parts only a terminal
exercises — drawing, key dispatch and the worker thread — by running an actual
acquisition from keystrokes and checking the evidence lands on disk.
"""

import os
import re
import select
import struct
import subprocess
import sys
import time

import pytest

# fcntl/termios do not exist on Windows, and a module-level import would fail
# during collection before any skip marker could apply.
fcntl = pytest.importorskip("fcntl", reason="needs a POSIX pseudo-terminal")
termios = pytest.importorskip("termios",
                              reason="needs a POSIX pseudo-terminal")
pytestmark = pytest.mark.skipif(not hasattr(os, "openpty"),
                                reason="needs a POSIX pseudo-terminal")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ncurses puts the terminal in application-cursor mode (smkx) when keypad is
# enabled, so arrows arrive as ESC O x, not ESC [ x.
UP, DOWN, ENTER = b"\x1bOA", b"\x1bOB", b"\r"
BACKSPACE = b"\x7f"


class Screen:
    """A TUI running on a pty, with helpers to type and to wait for text."""

    rows, cols = 40, 120

    def __init__(self, cwd):
        self.master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", self.rows, self.cols, 0, 0))
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
        """Replay the output into a character grid and return what is visible.

        Stripping escape sequences textually is not good enough: ncurses moves
        the cursor mid-line, so a sequence can land inside a word and split it.
        Replaying the moves reproduces the screen the operator sees.
        """
        grid = [[" "] * self.cols for _ in range(self.rows)]
        row = col = 0
        data, i = self.buffer, 0
        while i < len(data):
            ch = data[i]
            if ch == "\x1b":
                m = re.match(r"\x1b\[([0-9;]*)([A-Za-z@])", data[i:])
                if m:
                    params = [int(x) for x in m.group(1).split(";") if x]
                    cmd, n = m.group(2), (params[0] if params else 1)
                    if cmd == "H":
                        row = params[0] - 1 if params else 0
                        col = params[1] - 1 if len(params) > 1 else 0
                    elif cmd == "J":
                        grid = [[" "] * self.cols for _ in range(self.rows)]
                    elif cmd == "K":
                        for x in range(col, self.cols):
                            grid[row][x] = " "
                    elif cmd == "C":
                        col = min(self.cols - 1, col + n)
                    elif cmd == "D":
                        col = max(0, col - n)
                    elif cmd == "A":
                        row = max(0, row - n)
                    elif cmd == "B":
                        row = min(self.rows - 1, row + n)
                    elif cmd == "G":
                        col = params[0] - 1 if params else 0
                    elif cmd == "d":
                        row = params[0] - 1 if params else 0
                    i += m.end()
                    continue
                other = re.match(r"\x1b[()][B0]|\x1b[=>]|\x1b\][^\x07]*\x07"
                                 r"|\x1b[78M]", data[i:])
                i += other.end() if other else 1
                continue
            if ch == "\r":
                col = 0
            elif ch == "\n":
                row, col = min(row + 1, self.rows - 1), 0
            elif ch == "\x08":
                col = max(0, col - 1)
            elif 0 <= row < self.rows and 0 <= col < self.cols:
                grid[row][col] = ch
                col += 1
            i += 1
        return "\n".join("".join(line).rstrip() for line in grid)

    def type(self, data, settle=0.25):
        """Send input in small chunks: a slow runner redraws the whole screen
        between keystrokes, and one huge write can outrun it."""
        raw = data if isinstance(data, bytes) else data.encode()
        for i in range(0, len(raw), 16):
            os.write(self.master, raw[i:i + 16])
            time.sleep(0.02)
            self.read(0.01)
        time.sleep(settle)
        self.read(0.15)

    def type_into_field(self, text, timeout=20):
        """Type text and wait until the screen shows it, retrying the tail."""
        self.type(text)
        deadline = time.monotonic() + timeout
        needle = text[-24:]
        while time.monotonic() < deadline:
            if needle in self.plain():
                return True
            self.read(0.2)
        return False

    def diagnose(self, note=""):
        alive = self.proc.poll() is None
        tail = "\n".join(line for line in self.plain().splitlines()
                          if line.strip())[-2000:]
        return (f"{note}\nchild alive: {alive} (exit {self.proc.returncode})"
                f"\n--- screen ---\n{tail}")

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
    assert screen.type_into_field(str(source)), screen.diagnose("typing source")
    screen.type(ENTER)

    assert screen.wait_for(r"Output base", timeout=20), screen.diagnose("form")
    screen.type(BACKSPACE * 250, settle=0.4)            # clear the suggestion
    target = tmp_path / "fromtui"
    assert screen.type_into_field(str(target)), screen.diagnose("typing output")

    assert goto_form_row(screen, "Segment size"), screen.diagnose("segment row")
    screen.type(BACKSPACE * 16, settle=0.3)
    screen.type("0")
    assert screen.wait_for(r"mlane acquire", timeout=15), \
        screen.diagnose("command preview")

    assert goto_form_row(screen, "Start acquisition"), screen.diagnose("start row")
    screen.type(ENTER, settle=0.5)
    assert screen.wait_for(r"VERIFIED|finished", timeout=120), \
        screen.diagnose("waiting for the job to finish")

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
    assert goto_manual_entry(screen), screen.diagnose("manual entry")
    assert screen.type_into_field(str(source)), screen.diagnose("typing source")
    screen.type(ENTER)
    assert screen.wait_for(r"Output base", timeout=20), screen.diagnose("form")
    screen.type(BACKSPACE * 250, settle=0.4)
    assert goto_form_row(screen, "Start acquisition"), screen.diagnose("start")
    screen.type(ENTER, settle=0.5)
    assert screen.wait_for(r"cannot start", timeout=15), screen.diagnose("guard")
    assert not list(tmp_path.glob("*.E01"))
