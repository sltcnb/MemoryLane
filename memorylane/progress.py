"""Terminal progress reporting for long acquisitions."""

import sys
import time


def human(n, unit="B"):
    for suffix in ("", "K", "M", "G", "T", "P"):
        if abs(n) < 1024 or suffix == "P":
            return f"{n:,.1f} {suffix}{unit}" if suffix else f"{n:,.0f} {unit}"
        n /= 1024.0


def clock(seconds):
    if seconds is None or seconds != seconds or seconds in (float("inf"),):
        return "--:--:--"
    seconds = int(max(seconds, 0))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class Progress:
    """A single self-refreshing status line; a no-op when not on a terminal."""

    def __init__(self, label, total, enabled=True, stream=None, width=28):
        self.label = label
        self.total = total or 0
        self.stream = stream or sys.stderr
        self.enabled = enabled and self.stream.isatty()
        self.width = width
        self.done = 0
        self.start = time.monotonic()
        self._last = 0.0

    def advance(self, count):
        self.done += count
        now = time.monotonic()
        if self.enabled and now - self._last >= 0.15:
            self._last = now
            self._draw(now)

    def _draw(self, now):
        elapsed = max(now - self.start, 1e-6)
        rate = self.done / elapsed
        fraction = (self.done / self.total) if self.total else 0.0
        filled = int(self.width * min(fraction, 1.0))
        bar = "#" * filled + "-" * (self.width - filled)
        eta = (self.total - self.done) / rate if rate and self.total else None
        line = (f"\r{self.label} [{bar}] {fraction * 100:5.1f}%  "
                f"{human(self.done)} / {human(self.total)}  "
                f"{human(rate)}/s  ETA {clock(eta)}")
        self.stream.write(line[:200].ljust(0))
        self.stream.flush()

    def finish(self):
        elapsed = time.monotonic() - self.start
        if self.enabled:
            self.stream.write("\r" + " " * 110 + "\r")
            self.stream.flush()
        return elapsed
