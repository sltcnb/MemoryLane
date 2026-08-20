"""The TUI's model, which is where anything can actually go wrong.

Drawing is thin and needs a terminal; the form, its validation and the command
line it produces are plain Python and are tested here. test_tui_pty.py drives
the real screen.
"""

import pytest

from memorylane import tui
from memorylane.cli import build_parser


def test_field_kinds():
    text = tui.Field("n", "Name", "abc")
    text.type("d")
    assert text.value == "abcd"
    text.backspace()
    assert text.value == "abc"

    choice = tui.Field("c", "C", "a", ["a", "b", "c"], "choice")
    choice.cycle(1)
    assert choice.value == "b"
    choice.cycle(-1)
    assert choice.value == "a"
    choice.cycle(-1)
    assert choice.value == "c"          # wraps

    flag = tui.Field("v", "V", True, kind="bool")
    assert flag.display == "yes"
    flag.cycle(1)
    assert flag.value is False and flag.display == "no"
    flag.type("x")                      # typing must not corrupt a toggle
    assert flag.value is False


def test_form_moves_and_wraps():
    form = tui.Form("/dev/disk4", "/cases/img")
    assert form.index == 0
    form.move(-1)
    assert form.index == len(form.fields) - 1
    form.move(1)
    assert form.index == 0


def test_form_reports_what_is_missing():
    assert "no source selected" in tui.Form("", "/tmp/x").problems()
    assert "output path is empty" in tui.Form("/dev/sdb", "  ").problems()
    assert tui.Form("/dev/sdb", "/tmp/x").problems() == []


def test_argv_parses_as_a_real_command():
    """The form must never build a command line the CLI would reject."""
    form = tui.Form("/dev/disk4", "/cases/2026-042/usb")
    form["case_number"].value = "2026-042"
    form["examiner"].value = "N. Buisson"
    form["notes"].value = "bag A17"
    args = build_parser().parse_args(form.to_argv())
    assert args.command == "acquire"
    assert args.source == "/dev/disk4"
    assert args.output == "/cases/2026-042/usb"
    assert args.format == "e01"
    assert args.compress == "fast"
    assert args.split == "1500MB"
    assert args.case_number == "2026-042"
    assert args.examiner == "N. Buisson"
    assert args.notes == "bag A17"
    assert args.no_verify is False
    assert args.resume is False


def test_argv_reflects_every_toggle():
    form = tui.Form("/img.dd", "/out/x")
    form["format"].value = "raw"
    form["split"].value = "0"
    form["hash"].value = "sha256"
    form["verify"].value = False
    form["resume"].value = True
    args = build_parser().parse_args(form.to_argv())
    assert args.format == "raw"
    assert args.split == "0"
    assert args.hash == "sha256"
    assert args.no_verify is True
    assert args.resume is True


def test_empty_case_fields_are_omitted():
    form = tui.Form("/img.dd", "/out/x")
    argv = form.to_argv()
    assert "--case-number" not in argv
    assert "--notes" not in argv


def test_compression_only_offered_for_e01():
    form = tui.Form("/img.dd", "/out/x")
    form["format"].value = "raw"
    assert "-c" not in form.to_argv()
    form["format"].value = "e01"
    assert "-c" in form.to_argv()


def test_command_line_is_copyable():
    form = tui.Form("/dev/disk4", "/cases/my case/usb")
    form["examiner"].value = "N. Buisson"
    line = form.command_line()
    assert line.startswith("mlane acquire /dev/disk4")
    assert '"/cases/my case/usb"' in line
    assert '"N. Buisson"' in line


def test_run_state_collects_output():
    state = tui.RunState()
    state.log("one")
    state.log("two\nthree", level="warn")
    snap = state.snapshot()
    assert snap["lines"] == [("one", "info"), ("two", "warn"),
                             ("three", "warn")]


def test_run_state_bounds_its_log():
    state = tui.RunState()
    for i in range(1000):
        state.log(str(i))
    assert len(state.snapshot()["lines"]) == 400


def test_progress_reports_and_computes_eta():
    state = tui.RunState()
    bar = tui.TuiProgress(state, "  Imaging", 1000)
    bar.advance(250)
    snap = state.snapshot()
    assert snap["label"] == "Imaging"
    assert snap["done"] == 250
    assert snap["total"] == 1000
    assert snap["eta"] is not None


def test_cancel_raises_the_abort_path():
    """Cancelling must surface as KeyboardInterrupt, which is the tested
    abort path that leaves the evidence marked incomplete."""
    state = tui.RunState()
    bar = tui.TuiProgress(state, "Imaging", 100)
    bar.advance(10)
    state.cancel_requested = True
    with pytest.raises(KeyboardInterrupt):
        bar.advance(10)


def test_reporter_routes_by_level():
    state = tui.RunState()
    rep = tui.TuiReporter(state)
    rep.info("plain")
    rep.warn("careful")
    rep.error("broken")
    levels = [level for _, level in state.snapshot()["lines"]]
    assert levels == ["info", "warn", "error"]


def test_run_job_reports_a_bad_command(tmp_path):
    state = tui.RunState()
    tui.run_job(["acquire", str(tmp_path / "missing"), "-o",
                 str(tmp_path / "out"), "-q"], state)
    assert state.finished
    assert state.status == 2
    assert any("cannot open" in line for line, _ in state.snapshot()["lines"])


def test_run_job_acquires(tmp_path, evidence):
    path, data = evidence
    state = tui.RunState()
    form = tui.Form(str(path), str(tmp_path / "job"))
    form["split"].value = "0"
    tui.run_job(form.to_argv(), state)
    assert state.status == 0
    assert (tmp_path / "job.E01").exists()
    assert state.acquisition is not None
    assert state.acquisition.verified is True
    assert state.snapshot()["done"] > 0


def test_source_choices_never_raises(monkeypatch):
    monkeypatch.setattr("memorylane.source.list_devices",
                        lambda: (_ for _ in ()).throw(OSError("nope")))
    rows = tui.source_choices()
    assert rows and "probe failed" in rows[0]["label"]


def test_tail_shows_the_end_of_long_values():
    assert tui.tail("short", 20) == "short"           # fits: untouched
    assert tui.tail("abcdefghij", 10) == "abcdefghij"  # exactly fits
    clipped = tui.tail("/very/long/path/to/evidence.bin", 12)
    assert len(clipped) == 12
    assert clipped.startswith("<")
    assert clipped.endswith("evidence.bin"[-11:])
    assert tui.tail("x", 0) == "x"                     # degenerate width
