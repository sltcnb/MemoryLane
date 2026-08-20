"""A small full-screen console interface for MemoryLane.

Deliberately thin: the form builds an ordinary command line, argparse parses
it, and `cmd_acquire` runs exactly as it does from the shell. Nothing about
evidence handling lives here, so the TUI cannot drift away from the CLI.

The model (Field, Form, device list) is plain Python and unit-tested; only the
drawing and key dispatch need a terminal.
"""

import os
import threading
import time

from .progress import clock, human
from .ui import Reporter

TITLE = "MemoryLane"


def tail(text, width):
    """Show the end of an over-long value: that is where a path differs."""
    text = str(text)
    if width <= 1 or len(text) <= width:
        return text
    return "<" + text[-(width - 1):]


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------- model

class Field:
    """One editable row of the form."""

    def __init__(self, key, label, value="", choices=None, kind="text",
                 hint=""):
        self.key = key
        self.label = label
        self.value = value
        self.choices = choices or []
        self.kind = kind
        self.hint = hint

    @property
    def display(self):
        if self.kind == "action":
            return "<press enter>"
        if self.kind == "bool":
            return "yes" if self.value else "no"
        return str(self.value)

    def cycle(self, step):
        if self.kind == "bool":
            self.value = not self.value
        elif self.kind == "choice" and self.choices:
            i = (self.choices.index(self.value) + step) % len(self.choices)
            self.value = self.choices[i]

    def type(self, char):
        if self.kind == "text":
            self.value += char

    def backspace(self):
        if self.kind == "text":
            self.value = self.value[:-1]


class Form:
    """The acquisition settings, and how they become a command line."""

    def __init__(self, source="", output=""):
        self.source = source
        self.fields = [
            Field("output", "Output base", output,
                  hint="path without the .E01 / .001 suffix"),
            Field("format", "Format", "e01", ["e01", "raw"], "choice"),
            Field("compress", "Compression", "fast", ["fast", "best", "none"],
                  "choice"),
            Field("split", "Segment size", "1500MB",
                  hint="0 for one unsplit file"),
            Field("hash", "Hashes", "md5,sha1",
                  hint="md5 and sha1 are always included"),
            Field("case_number", "Case number"),
            Field("evidence_number", "Evidence number"),
            Field("description", "Description"),
            Field("examiner", "Examiner"),
            Field("notes", "Notes"),
            Field("verify", "Verify after writing", True, kind="bool"),
            Field("resume", "Resume if unfinished", False, kind="bool"),
            Field("start", "Start acquisition", "", kind="action",
                  hint="enter here, or F5 from anywhere"),
        ]
        self.index = 0

    def __getitem__(self, key):
        for field in self.fields:
            if field.key == key:
                return field
        raise KeyError(key)

    @property
    def current(self):
        return self.fields[self.index]

    def move(self, step):
        self.index = (self.index + step) % len(self.fields)

    def problems(self):
        """Reasons this form cannot be submitted yet."""
        issues = []
        if not self.source:
            issues.append("no source selected")
        if not self["output"].value.strip():
            issues.append("output path is empty")
        return issues

    @property
    def on_action(self):
        return self.current.kind == "action"

    def to_argv(self):
        """Build the exact command line the CLI would take.

        Going through argparse rather than constructing a namespace by hand
        means the TUI inherits every default, choice and validation the CLI
        has, and cannot fall behind when a flag is added.
        """
        argv = ["acquire", self.source, "-o", self["output"].value.strip(),
                "-f", self["format"].value, "-s", self["split"].value.strip(),
                "--hash", self["hash"].value.strip()]
        if self["format"].value == "e01":
            argv += ["-c", self["compress"].value]
        for key, flag in (("case_number", "--case-number"),
                          ("evidence_number", "--evidence-number"),
                          ("description", "--description"),
                          ("examiner", "--examiner"),
                          ("notes", "--notes")):
            value = self[key].value.strip()
            if value:
                argv += [flag, value]
        if not self["verify"].value:
            argv.append("--no-verify")
        if self["resume"].value:
            argv.append("--resume")
        return argv

    def command_line(self):
        """The same thing as a copyable shell command."""
        def quote(part):
            return f'"{part}"' if " " in part else part
        return "mlane " + " ".join(quote(p) for p in self.to_argv())


def source_choices():
    """Attached drives, newest probe first, plus a manual entry."""
    from .source import list_devices

    rows = []
    try:
        for device in list_devices():
            rows.append({
                "path": device["path"],
                "label": device["path"],
                "size": device.get("size") or 0,
                "bus": device.get("interface") or "?",
                "model": device.get("model") or "",
                "removable": bool(device.get("removable")),
            })
    except Exception as e:                     # probing must never be fatal
        rows.append({"path": "", "label": f"(device probe failed: {e})",
                     "size": 0, "bus": "", "model": "", "removable": False})
    return rows


# ---------------------------------------------------------------- reporter

class TuiReporter(Reporter):
    """Collects output for the screen and lets the operator cancel."""

    def __init__(self, state):
        self.state = state

    def info(self, text):
        self.state.log(text)

    def warn(self, text):
        self.state.log(text, level="warn")

    def error(self, text):
        self.state.log(text, level="error")

    def result(self, acquisition):
        self.state.acquisition = acquisition

    def progress(self, label, total):
        return TuiProgress(self.state, label, total)


class TuiProgress:
    """Feeds the on-screen bar, and turns a cancel request into the same
    KeyboardInterrupt the CLI uses — so cancelling takes the tested abort
    path and leaves the evidence explicitly incomplete."""

    def __init__(self, state, label, total):
        self.state = state
        self.label = label.strip()
        self.total = total or 0
        self.done = 0
        self.start = time.monotonic()
        state.begin(self.label, self.total)

    def advance(self, count):
        self.done += count
        if self.state.cancel_requested:
            raise KeyboardInterrupt()
        now = time.monotonic()
        elapsed = max(now - self.start, 1e-6)
        self.state.update(self.done, self.done / elapsed)

    def finish(self):
        elapsed = time.monotonic() - self.start
        self.state.update(self.done, self.done / max(elapsed, 1e-6))
        return elapsed


class RunState:
    """Shared between the worker thread and the drawing loop."""

    def __init__(self):
        self.lock = threading.Lock()
        self.lines = []
        self.label = ""
        self.total = 0
        self.done = 0
        self.rate = 0.0
        self.cancel_requested = False
        self.finished = False
        self.status = None
        self.acquisition = None

    def log(self, text, level="info"):
        with self.lock:
            for line in str(text).splitlines():
                self.lines.append((line, level))
            del self.lines[:-400]

    def begin(self, label, total):
        with self.lock:
            self.label, self.total, self.done, self.rate = label, total, 0, 0.0

    def update(self, done, rate):
        with self.lock:
            self.done, self.rate = done, rate

    def snapshot(self):
        with self.lock:
            eta = ((self.total - self.done) / self.rate
                   if self.rate and self.total else None)
            return {"lines": list(self.lines), "label": self.label,
                    "total": self.total, "done": self.done, "rate": self.rate,
                    "eta": eta, "finished": self.finished,
                    "status": self.status}


def run_job(argv, state):
    """Run one acquisition in a worker thread."""
    from .cli import build_parser, cmd_acquire

    try:
        args = build_parser().parse_args(argv)
        state.status = cmd_acquire(args, TuiReporter(state))
    except SystemExit as e:                    # argparse rejected something
        state.log(f"error: invalid settings ({e})", level="error")
        state.status = 2
    except Exception as e:
        state.log(f"error: {e}", level="error")
        state.status = 2
    finally:
        state.finished = True


# ----------------------------------------------------------------- curses

def main(argv=None):
    try:
        import curses
    except ImportError:
        print("error: the TUI needs the curses module, which is not part of "
              "the standard library on Windows.\n"
              "       Install it with:  pip install windows-curses\n"
              "       Or use the command line:  mlane acquire --help")
        return 2
    return curses.wrapper(_App().loop)


class _App:
    """Screen state machine: pick a source, fill the form, watch it run."""

    def __init__(self):
        self.screen = "sources"
        self.sources = []
        self.cursor = 0
        self.manual = ""
        self.form = None
        self.state = None
        self.worker = None
        self.message = ""

    # -- drawing ---------------------------------------------------------

    def loop(self, stdscr):
        import curses

        curses.curs_set(0)
        # timeout() rather than nodelay(): with no delay ncurses cannot wait
        # for the rest of an escape sequence, so an arrow key arrives as a
        # bare ESC. A short escape delay keeps a real ESC press responsive.
        stdscr.timeout(80)
        stdscr.keypad(True)
        try:
            curses.set_escdelay(25)
        except (AttributeError, curses.error):
            pass
        try:
            curses.use_default_colors()
            for i, colour in enumerate((curses.COLOR_CYAN, curses.COLOR_YELLOW,
                                        curses.COLOR_RED, curses.COLOR_GREEN),
                                       start=1):
                curses.init_pair(i, colour, -1)
        except curses.error:
            pass
        self.sources = source_choices()
        while True:
            self.draw(stdscr)
            try:
                key = stdscr.getch()
            except curses.error:
                key = -1
            if key == -1:
                continue
            if self.handle(key, stdscr) == "quit":
                return 0

    def draw(self, stdscr):
        import curses

        stdscr.erase()
        height, width = stdscr.getmaxyx()

        def put(row, col, text, attr=0):
            if 0 <= row < height and col < width:
                stdscr.addnstr(row, col, text, max(0, width - col - 1), attr)

        from . import __version__
        put(0, 0, f" {TITLE} {__version__} ".ljust(width - 1),
            curses.A_REVERSE | curses.A_BOLD)
        if self.screen == "sources":
            self.draw_sources(put, height, width)
        elif self.screen == "form":
            self.draw_form(put, height, width)
        else:
            self.draw_run(put, height, width)
        if self.message:
            put(height - 2, 0, self.message[:width - 1],
                curses.color_pair(2))
        stdscr.refresh()

    def draw_sources(self, put, height, width):
        import curses

        put(2, 2, "Select a source to image:", curses.A_BOLD)
        row = 4
        for i, source in enumerate(self.sources):
            marker = "->" if i == self.cursor else "  "
            size = human(source["size"]) if source["size"] else ""
            flag = "removable" if source["removable"] else "fixed"
            text = (f"{marker} {source['label']:<20} {size:>11}  "
                    f"{source['bus']:<12} {flag:<10} {source['model']}")
            put(row, 2, text,
                curses.A_REVERSE if i == self.cursor else 0)
            row += 1
        row += 1
        marker = "->" if self.cursor == len(self.sources) else "  "
        typed = tail(self.manual, max(10, width - 22))
        put(row, 2, f"{marker} File or path: {typed}_",
            curses.A_REVERSE if self.cursor == len(self.sources) else 0)
        put(height - 3, 2,
            "up/down select   enter continue   r rescan   q quit",
            curses.color_pair(1))

    def draw_form(self, put, height, width):
        import curses

        put(2, 2, f"Source: {self.form.source}", curses.A_BOLD)
        row = 4
        value_col = 28                       # "-> " + 22-wide label + a space
        for i, field in enumerate(self.form.fields):
            selected = i == self.form.index
            marker = "->" if selected else "  "
            hint = f"({field.hint})" if selected and field.hint else ""
            # Reserve the hint's space first, so a long path cannot run into it.
            hint_col = width - len(hint) - 2 if hint else width
            room = max(12, hint_col - value_col - 2)
            shown = tail(field.display, room)
            put(row, 2, f"{marker} {field.label:<22} {shown}",
                curses.A_REVERSE if selected else 0)
            if hint:
                put(row, hint_col, hint, curses.color_pair(1))
            row += 1
        row += 1
        problems = self.form.problems()
        if problems:
            put(row, 2, "cannot start: " + "; ".join(problems),
                curses.color_pair(3))
        else:
            put(row, 2, self.form.command_line()[:width - 6],
                curses.color_pair(4))
        put(height - 3, 2,
            "up/down field   left/right toggle   type to edit   "
            "enter on Start (or F5) begins   esc back",
            curses.color_pair(1))

    def draw_run(self, put, height, width):
        import curses

        snap = self.state.snapshot()
        bar_width = max(10, min(40, width - 46))
        fraction = (snap["done"] / snap["total"]) if snap["total"] else 0
        filled = int(bar_width * min(fraction, 1.0))
        bar = "#" * filled + "-" * (bar_width - filled)
        put(2, 2, f"{snap['label'] or 'working':<12} [{bar}] "
                  f"{fraction * 100:5.1f}%  {human(snap['done'])}"
                  f" / {human(snap['total'])}", curses.A_BOLD)
        put(3, 2, f"{human(snap['rate'])}/s    ETA {clock(snap['eta'])}")
        row = 5
        visible = snap["lines"][-(height - row - 4):]
        for line, level in visible:
            colour = {"warn": curses.color_pair(2),
                      "error": curses.color_pair(3)}.get(level, 0)
            put(row, 2, line, colour)
            row += 1
        if snap["finished"]:
            verdict = ("finished, exit status 0" if snap["status"] == 0
                       else f"finished with problems (exit {snap['status']})")
            put(height - 3, 2,
                f"{verdict}   —   enter: back to sources   q: quit",
                curses.color_pair(4 if snap["status"] == 0 else 3))
        else:
            put(height - 3, 2, "c: cancel (leaves the evidence marked "
                               "incomplete)", curses.color_pair(1))

    # -- input -----------------------------------------------------------

    def handle(self, key, stdscr):
        import curses

        self.message = ""
        if self.screen == "sources":
            return self.keys_sources(key)
        if self.screen == "form":
            return self.keys_form(key, curses)
        return self.keys_run(key)

    def keys_sources(self, key):
        total = len(self.sources) + 1
        typing = self.cursor == len(self.sources)
        # ESC deliberately does not quit: a terminal that emits an escape
        # sequence ncurses does not recognise would otherwise end the session.
        if key == ord("q") and not typing:
            return "quit"
        if key == ord("r") and not typing:
            self.sources = source_choices()
            self.cursor = min(self.cursor, len(self.sources))
        elif key == 259 or (key == ord("k") and not typing):        # up
            self.cursor = (self.cursor - 1) % total
        elif key == 258 or (key == ord("j") and not typing):        # down
            self.cursor = (self.cursor + 1) % total
        elif key in (10, 13):                                       # enter
            source = (self.manual.strip() if typing
                      else self.sources[self.cursor]["path"])
            if not source:
                self.message = "pick a device or type a path first"
                return None
            suggestion = os.path.join(
                os.getcwd(), os.path.basename(source.rstrip("/")) or "image")
            self.form = Form(source, suggestion)
            self.screen = "form"
        elif typing:
            if key in (263, 127, 8):                                # backspace
                self.manual = self.manual[:-1]
            elif 32 <= key < 127:
                self.manual += chr(key)
        return None

    def keys_form(self, key, curses):
        field = self.form.current
        if key == 27:                                      # esc
            self.screen = "sources"
        elif key in (259,):
            self.form.move(-1)
        elif key in (258, 9):                              # down / tab
            self.form.move(1)
        elif key in (260,):                                # left
            field.cycle(-1)
        elif key in (261,):                                # right
            field.cycle(1)
        elif key == ord(" ") and field.kind != "text":
            field.cycle(1)
        elif key == 269 or (key in (10, 13) and self.form.on_action):
            problems = self.form.problems()
            if problems:
                self.message = "cannot start: " + "; ".join(problems)
                return None
            self.start()
        elif key in (10, 13):                              # enter: next field
            self.form.move(1)
        elif key in (263, 127, 8):
            field.backspace()
        elif 32 <= key < 127:
            field.type(chr(key))
        return None

    def keys_run(self, key):
        snap = self.state.snapshot()
        if snap["finished"]:
            if key in (ord("q"),):
                return "quit"
            if key in (10, 13, 27):
                self.screen = "sources"
                self.sources = source_choices()
        elif key in (ord("c"), 27):
            self.state.cancel_requested = True
            self.message = "cancelling..."
        return None

    def start(self):
        self.state = RunState()
        argv = self.form.to_argv()
        self.worker = threading.Thread(target=run_job, args=(argv, self.state),
                                       daemon=True)
        self.worker.start()
        self.screen = "run"
