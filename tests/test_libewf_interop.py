"""Cross-validation against libewf, the reference EWF implementation.

Skipped automatically when the libewf tools are not installed
(`brew install libewf` / `apt install ewf-tools`).
"""

import hashlib
import shutil
import subprocess

import pytest

from memorylane import ewf
from memorylane.cli import main

needs_libewf = pytest.mark.skipif(
    not (shutil.which("ewfverify") and shutil.which("ewfinfo")),
    reason="libewf tools not installed")
needs_ewfacquire = pytest.mark.skipif(
    not shutil.which("ewfacquire"), reason="ewfacquire not installed")


def run(*argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=300)


@needs_libewf
@pytest.mark.parametrize("compression", ["none", "fast", "best"])
@pytest.mark.parametrize("split", ["0", "1MB"])
def test_libewf_verifies_our_images(tmp_path, evidence, compression, split):
    path, data = evidence
    base = tmp_path / f"x-{compression}-{split}"
    assert main(["acquire", str(path), "-o", str(base), "-q",
                 "-c", compression, "-s", split, "--no-verify"]) == 0
    result = run("ewfverify", f"{base}.E01")
    assert "SUCCESS" in result.stdout, result.stdout + result.stderr
    assert hashlib.md5(data).hexdigest() in result.stdout


@needs_libewf
def test_libewf_reads_our_metadata(tmp_path, evidence):
    path, _ = evidence
    base = tmp_path / "meta"
    assert main(["acquire", str(path), "-o", str(base), "-q", "--no-verify",
                 "--case-number", "2026-042", "--evidence-number", "001",
                 "--description", "USB key", "--examiner", "N. Buisson",
                 "--notes", "bag A17", "--media-type", "removable"]) == 0
    out = run("ewfinfo", f"{base}.E01").stdout
    assert "2026-042" in out
    assert "USB key" in out
    assert "N. Buisson" in out
    assert "bag A17" in out
    assert "removable disk" in out
    assert "EnCase 5" in out          # the format we claim to write


@needs_libewf
def test_ewfexport_reproduces_the_source(tmp_path, evidence):
    path, data = evidence
    base = tmp_path / "exp"
    assert main(["acquire", str(path), "-o", str(base), "-q", "-s", "1MB",
                 "--no-verify"]) == 0
    target = tmp_path / "out"
    result = run("ewfexport", "-u", "-f", "raw", "-t", str(target),
                 "-o", "0", "-B", str(len(data)), f"{base}.E01")
    assert "SUCCESS" in result.stdout, result.stdout + result.stderr
    assert (tmp_path / "out.raw").read_bytes() == data


@needs_ewfacquire
@pytest.mark.parametrize("fmt", ["encase5", "encase6", "ftk"])
def test_we_read_libewf_images(tmp_path, evidence, fmt):
    """The reverse direction: libewf writes, MemoryLane reads."""
    path, data = evidence
    base = tmp_path / fmt
    result = run("ewfacquire", "-u", "-t", str(base), "-f", fmt,
                 "-c", "deflate:fast", "-S", "1MiB", "-b", "64",
                 "-C", "REV-1", "-D", "libewf-produced", "-E", "9",
                 "-e", "libewf", "-N", "reverse test",
                 "-m", "removable", "-M", "physical", str(path))
    assert "SUCCESS" in result.stdout, result.stdout + result.stderr
    with ewf.EwfReader(f"{base}.E01") as r:
        assert len(r.segments) > 1
        assert r.size == len(data)
        assert b"".join(r.stream()) == data
        assert r.stored_md5 == hashlib.md5(data).hexdigest()
        assert r.metadata["c"] == "REV-1"
        assert not r.corrupt_chunks
        assert r.complete
    assert main(["verify", f"{base}.E01", "-q"]) == 0


@needs_libewf
def test_libewf_reads_our_defect_list(tmp_path, evidence, failing_media):
    """The error2 section must be legible to a third-party reader."""
    path, _ = evidence
    failing_media(path, {100, 101, 102, 103, 5000, 7000, 7001, 7002})
    base = tmp_path / "defects"
    assert main(["acquire", str(path), "-o", str(base), "-q",
                 "--no-verify"]) == 0
    out = run("ewfinfo", "-e", f"{base}.E01").stdout
    assert "Read errors during acquiry" in out
    assert "total number: 3" in out
    assert "100 - 103 number: 4" in out
    assert "5000 - 5000 number: 1" in out
    assert "7000 - 7002 number: 3" in out
    assert "SUCCESS" in run("ewfverify", f"{base}.E01").stdout
