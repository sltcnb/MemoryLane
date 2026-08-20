"""Output sinks, so one acquisition path can drive a terminal or a TUI.

`cmd_acquire` does the work and reports through a Reporter; the console
reporter prints, and the TUI reporter paints a curses screen. Keeping a single
acquisition implementation matters more here than in most tools: two code paths
would be two chances to get evidence handling subtly different.
"""

import sys

from .progress import Progress


class Reporter:
    """Default sink: silent, and progress goes nowhere."""

    def info(self, text):
        """A normal line of output; may be suppressed when quiet."""

    def warn(self, text):
        """Something the operator must see even when quiet."""

    def error(self, text):
        """A failure. Callers include their own `error:` prefix."""

    def result(self, acquisition):
        """Called once with the finished Acquisition record."""

    def progress(self, label, total):
        return Progress(label, total, enabled=False)


class ConsoleReporter(Reporter):
    def __init__(self, quiet=False):
        self.quiet = quiet
        self.acquisition = None

    def info(self, text):
        if not self.quiet:
            print(text)

    def warn(self, text):
        print(text, file=sys.stderr)

    def error(self, text):
        print(text, file=sys.stderr)

    def result(self, acquisition):
        self.acquisition = acquisition

    def progress(self, label, total):
        return Progress(label, total, enabled=not self.quiet)
