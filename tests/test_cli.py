"""End-to-end CLI behaviour: acquire, verify, info, exit codes."""

import hashlib
import os

import pytest

from memorylane import ewf, raw
from memorylane.cli import main, parse_size, strip_evidence_suffix


def test_parse_size():
    assert parse_size("1500MB") == 1500 * 1024 * 1024
    assert parse_size("2GB") == 2 * 1024 * 1024 * 1024
    assert parse_size("1.5G") == 1610612736
    assert parse_size("650") == 650
    assert parse_size("0") == 0


def test_strip_evidence_suffix():
    assert strip_evidence_suffix("/c/img.E01") == "/c/img"
    assert strip_evidence_suffix("/c/img.001") == "/c/img"
    assert strip_evidence_suffix("/c/img.dd") == "/c/img"
    assert strip_evidence_suffix("/c/img") == "/c/img"
    assert strip_evidence_suffix("/c/case.2026") == "/c/case.2026"


def acquire(source, out, *extra):
    return main(["acquire", str(source), "-o", str(out), "-q", *extra])


def test_acquire_e01_verifies(tmp_path, evidence, capsys):
    path, data = evidence
    out = tmp_path / "case"
    assert acquire(path, out, "--case-number", "42", "--examiner", "nb") == 0
    assert (tmp_path / "case.E01").exists()
    assert (tmp_path / "case.E01.txt").exists()

    summary = (tmp_path / "case.E01.txt").read_text()
    assert hashlib.md5(data).hexdigest() in summary
    assert hashlib.sha1(data).hexdigest() in summary
    assert ": verified" in summary
    assert "Case Number: 42" in summary
    assert "Examiner: nb" in summary

    with ewf.EwfReader(str(tmp_path / "case.E01")) as r:
        assert b"".join(r.stream()) == data
        assert r.stored_md5 == hashlib.md5(data).hexdigest()


def test_acquire_raw_matches_byte_for_byte(tmp_path, evidence):
    path, data = evidence
    out = tmp_path / "raw"
    assert acquire(path, out, "-f", "raw", "-s", "1MB") == 0
    segments = sorted(p for p in os.listdir(tmp_path)
                  if p.startswith("raw.0") and not p.endswith(".txt"))
    assert segments == ["raw.001", "raw.002", "raw.003", "raw.004"]
    joined = b"".join((tmp_path / s).read_bytes() for s in segments)
    assert joined == data


def test_acquire_single_raw_file(tmp_path, evidence):
    path, data = evidence
    out = tmp_path / "one.dd"
    assert acquire(path, out, "-f", "raw", "-s", "0", "--single") == 0
    assert (tmp_path / "one.dd").read_bytes() == data


def test_extra_hash_reaches_the_summary(tmp_path, evidence):
    path, data = evidence
    assert acquire(path, tmp_path / "h", "--hash", "sha256") == 0
    summary = (tmp_path / "h.E01.txt").read_text()
    assert hashlib.sha256(data).hexdigest() in summary
    assert "SHA256 checksum:" in summary


def test_refuses_to_overwrite(tmp_path, evidence, capsys):
    path, _ = evidence
    assert acquire(path, tmp_path / "case") == 0
    assert acquire(path, tmp_path / "case") == 2
    assert "already exists" in capsys.readouterr().err
    assert acquire(path, tmp_path / "case", "--force") == 0


def test_verify_command(tmp_path, evidence, capsys):
    path, _ = evidence
    acquire(path, tmp_path / "case")
    assert main(["verify", str(tmp_path / "case.E01")]) == 0
    assert "VERIFIED" in capsys.readouterr().out


def test_verify_detects_tampering(tmp_path, evidence, capsys):
    path, _ = evidence
    acquire(path, tmp_path / "case")
    target = tmp_path / "case.E01"
    blob = bytearray(target.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    target.write_bytes(bytes(blob))
    assert main(["verify", str(target)]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "VERIFICATION FAILED" in out


def test_verify_expected_hash_for_raw(tmp_path, evidence, capsys):
    path, data = evidence
    acquire(path, tmp_path / "raw", "-f", "raw", "-s", "0")
    good = hashlib.md5(data).hexdigest()
    assert main(["verify", str(tmp_path / "raw.001"),
                 "--expected-md5", good]) == 0
    assert main(["verify", str(tmp_path / "raw.001"),
                 "--expected-md5", "0" * 32]) == 1


def test_info_command(tmp_path, evidence, capsys):
    path, data = evidence
    acquire(path, tmp_path / "case", "--description", "thumb drive")
    assert main(["info", str(tmp_path / "case.E01")]) == 0
    out = capsys.readouterr().out
    assert "EWF / E01" in out
    assert "thumb drive" in out
    assert f"{len(data):,} bytes" in out


def test_no_verify_skips_the_pass(tmp_path, evidence):
    path, _ = evidence
    assert acquire(path, tmp_path / "case", "--no-verify") == 0
    summary = (tmp_path / "case.E01.txt").read_text()
    assert "Verification not performed." in summary


def test_bad_hash_name_is_rejected(tmp_path, evidence, capsys):
    path, _ = evidence
    assert acquire(path, tmp_path / "case", "--hash", "crc32") == 2
    assert "unsupported hash" in capsys.readouterr().err


def test_missing_source(tmp_path, capsys):
    assert main(["acquire", str(tmp_path / "nope"), "-o",
                 str(tmp_path / "o"), "-q"]) == 2
    assert "cannot open" in capsys.readouterr().err


@pytest.mark.parametrize("compression", ["none", "fast", "best"])
def test_every_compression_level_verifies(tmp_path, evidence, compression):
    path, data = evidence
    out = tmp_path / compression
    assert acquire(path, out, "-c", compression) == 0
    with ewf.EwfReader(f"{out}.E01") as r:
        assert b"".join(r.stream()) == data


def test_export_back_to_raw(tmp_path, evidence, capsys):
    path, data = evidence
    acquire(path, tmp_path / "case")
    target = tmp_path / "restored.dd"
    assert main(["export", str(tmp_path / "case.E01"), "-o", str(target),
                 "-q"]) == 0
    assert target.read_bytes() == data


def test_export_split(tmp_path, evidence):
    path, data = evidence
    acquire(path, tmp_path / "case")
    assert main(["export", str(tmp_path / "case.E01"), "-o",
                 str(tmp_path / "out"), "-s", "1MB", "-q"]) == 0
    pieces = sorted(p for p in os.listdir(tmp_path) if p.startswith("out.0"))
    assert len(pieces) == 4
    assert b"".join((tmp_path / p).read_bytes() for p in pieces) == data


def test_export_refuses_to_overwrite(tmp_path, evidence, capsys):
    path, _ = evidence
    acquire(path, tmp_path / "case")
    target = tmp_path / "restored.dd"
    assert main(["export", str(tmp_path / "case.E01"), "-o", str(target),
                 "-q"]) == 0
    assert main(["export", str(tmp_path / "case.E01"), "-o", str(target),
                 "-q"]) == 2
    assert "already exists" in capsys.readouterr().err


def test_export_reports_damage(tmp_path, evidence, capsys):
    path, _ = evidence
    acquire(path, tmp_path / "case")
    source = tmp_path / "case.E01"
    blob = bytearray(source.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    source.write_bytes(bytes(blob))
    assert main(["export", str(source), "-o", str(tmp_path / "bad.dd"),
                 "-q"]) == 1
    assert "does not match" in capsys.readouterr().err


def test_raw_to_e01_conversion(tmp_path, evidence):
    """A raw image is just another source, so dd -> E01 works too."""
    path, data = evidence
    assert acquire(path, tmp_path / "r", "-f", "raw", "-s", "0",
                   "--single") == 0
    assert acquire(tmp_path / "r", tmp_path / "converted") == 0
    with ewf.EwfReader(str(tmp_path / "converted.E01")) as r:
        assert b"".join(r.stream()) == data


def test_ewf_date_formats():
    from memorylane.cli import _ewf_date
    assert _ewf_date("2026 8 20 11 52 4").startswith("Thu Aug 20 11:52:04 2026")
    assert "2026" in _ewf_date("1787220000")
    assert _ewf_date("not a date") == "not a date"
    assert _ewf_date("2026 13 40 99 99 99") == "2026 13 40 99 99 99"


def test_bad_media_is_recorded_everywhere(tmp_path, evidence, failing_media,
                                          capsys):
    """A defect must show up in the image, the summary and the console."""
    path, _ = evidence
    failing_media(path, {100, 101, 102, 103, 5000})
    out = tmp_path / "flaky"
    assert acquire(path, out, "--case-number", "BADMEDIA") == 0

    summary = (tmp_path / "flaky.E01.txt").read_text()
    assert "[Read Errors]" in summary
    assert "Bad sectors replaced with zeros: 5" in summary
    assert "sector 100-103" in summary
    assert "sector 5000" in summary

    with ewf.EwfReader(str(tmp_path / "flaky.E01")) as r:
        assert r.read_errors == [(100, 4), (5000, 1)]

    assert main(["info", str(tmp_path / "flaky.E01")]) == 0
    assert "Read errors:       5 sector(s) in 2 range(s)" in capsys.readouterr().out


def test_bad_media_still_verifies(tmp_path, evidence, failing_media):
    """Zero-filled defects are part of the image, so it must verify clean."""
    path, _ = evidence
    failing_media(path, {7})
    assert acquire(path, tmp_path / "flaky") == 0
    assert main(["verify", str(tmp_path / "flaky.E01"), "-q"]) == 0


def test_raw_bad_media_reaches_the_summary(tmp_path, evidence, failing_media):
    path, _ = evidence
    failing_media(path, {42})
    assert acquire(path, tmp_path / "r", "-f", "raw", "-s", "0") == 0
    assert "sector 42" in (tmp_path / "r.001.txt").read_text()


def test_retries_flag_is_honoured(tmp_path, evidence, failing_media):
    path, _ = evidence
    state = failing_media(path, {9}, fail_times=2)
    assert acquire(path, tmp_path / "r", "--retries", "5", "--no-verify") == 0
    with ewf.EwfReader(str(tmp_path / "r.E01")) as r:
        assert r.read_errors == []          # recovered, so nothing to record
    assert state["attempts"] == 3


def _fake_mounts(monkeypatch, mounts):
    monkeypatch.setattr("memorylane.cli.whole_disk_of_device", lambda p: "disk9")
    monkeypatch.setattr("memorylane.cli.mounted_volumes", lambda d: mounts)


def test_writable_mount_blocks_acquisition(tmp_path, evidence, monkeypatch,
                                           capsys):
    path, _ = evidence
    monkeypatch.setattr("memorylane.source._is_device", lambda p: True)
    _fake_mounts(monkeypatch, [("/dev/disk9s1", "/Volumes/EVID", False)])
    assert acquire(path, tmp_path / "blocked") == 2
    err = capsys.readouterr().err
    assert "mounted for writing" in err
    assert "/Volumes/EVID" in err
    assert not (tmp_path / "blocked.E01").exists()


def test_force_images_a_live_volume_but_shouts(tmp_path, evidence, monkeypatch,
                                               capsys):
    path, _ = evidence
    monkeypatch.setattr("memorylane.source._is_device", lambda p: True)
    _fake_mounts(monkeypatch, [("/dev/disk9s1", "/Volumes/EVID", False)])
    assert acquire(path, tmp_path / "forced", "--force") == 0
    # -q must not silence an evidence-quality warning.
    assert "not be a consistent" in capsys.readouterr().err


def test_read_only_mount_is_only_a_note(tmp_path, evidence, monkeypatch,
                                        capsys):
    path, _ = evidence
    monkeypatch.setattr("memorylane.source._is_device", lambda p: True)
    _fake_mounts(monkeypatch, [("/dev/disk9s1", "/Volumes/EVID", True)])
    assert acquire(path, tmp_path / "ro") == 0
    assert "WARNING" not in capsys.readouterr().err


def _interrupt_at(tmp_path, evidence, cut, extra=()):
    """Write a partial acquisition the way an aborted run leaves one."""
    path, data = evidence
    base = str(tmp_path / "job")
    kw = dict(compression="fast")
    kw.update(extra)
    w = ewf.EwfWriter(base, len(data), 512, **kw)
    w.write(data[:cut])
    w.abort()
    return base, data


def test_resume_completes_the_image(tmp_path, evidence):
    base, data = _interrupt_at(tmp_path, evidence, 1_000_000)
    with ewf.EwfReader(base + ".E01") as r:
        assert not r.complete
    assert main(["acquire", str(evidence[0]), "-o", base, "-q",
                 "--resume"]) == 0
    with ewf.EwfReader(base + ".E01") as r:
        assert r.complete
        assert b"".join(r.stream()) == data
        assert r.stored_md5 == hashlib.md5(data).hexdigest()
        assert r.stored_sha1 == hashlib.sha1(data).hexdigest()


def test_resume_across_segments(tmp_path, evidence):
    base, data = _interrupt_at(tmp_path, evidence, 2_000_000,
                               {"segment_size": 300_000})
    before = len(ewf.glob_segments(base + ".E01"))
    assert before > 2
    assert main(["acquire", str(evidence[0]), "-o", base, "-q",
                 "--resume"]) == 0
    with ewf.EwfReader(base + ".E01") as r:
        assert r.complete
        assert len(r.segments) >= before
        assert b"".join(r.stream()) == data


def test_resume_keeps_the_original_settings(tmp_path, evidence, capsys):
    """Compression and the set identifier are fixed by the existing segments."""
    base, data = _interrupt_at(tmp_path, evidence, 500_000,
                               {"compression": "best"})
    with ewf.EwfReader(base + ".E01") as r:
        identifier = r.set_identifier
    # Ask for a different compression; the image's own must win.
    assert main(["acquire", str(evidence[0]), "-o", base, "-q", "--resume",
                 "-c", "none"]) == 0
    assert "not --compress none" in capsys.readouterr().err
    with ewf.EwfReader(base + ".E01") as r:
        assert r.compression == ewf.COMPRESSION["best"]
        assert r.set_identifier == identifier
        assert b"".join(r.stream()) == data


def test_resume_refuses_a_finished_image(tmp_path, evidence, capsys):
    path, _ = evidence
    assert acquire(path, tmp_path / "done") == 0
    assert main(["acquire", str(path), "-o", str(tmp_path / "done"), "-q",
                 "--resume"]) == 2
    assert "already complete" in capsys.readouterr().err


def test_resume_refuses_a_different_source(tmp_path, evidence, capsys):
    base, _ = _interrupt_at(tmp_path, evidence, 500_000)
    other = tmp_path / "other.bin"
    other.write_bytes(b"\x00" * 999_424)
    assert main(["acquire", str(other), "-o", base, "-q", "--resume"]) == 2
    assert "refusing to mix two acquisitions" in capsys.readouterr().err


def test_resume_needs_something_to_resume(tmp_path, evidence, capsys):
    path, _ = evidence
    assert main(["acquire", str(path), "-o", str(tmp_path / "ghost"), "-q",
                 "--resume"]) == 2
    assert "nothing to resume" in capsys.readouterr().err


def test_resume_trims_a_hard_killed_tail(tmp_path, evidence):
    """A killed writer leaves an unreferenced fragment; it must be dropped."""
    base, data = _interrupt_at(tmp_path, evidence, 1_000_000)
    with open(base + ".E01", "ab") as f:
        f.write(b"\x00" * 76 + b"\xde\xad\xbe\xef" * 5000)
    assert main(["acquire", str(evidence[0]), "-o", base, "-q",
                 "--resume"]) == 0
    with ewf.EwfReader(base + ".E01") as r:
        assert r.complete
        assert not r.corrupt_chunks
        assert b"".join(r.stream()) == data


def test_resume_raw(tmp_path, evidence):
    path, data = evidence
    base = str(tmp_path / "r")
    with raw.RawWriter(base, segment_size=500_000) as w:
        w.write(data[:1_234_567])
    assert main(["acquire", str(path), "-o", base, "-q", "-f", "raw",
                 "-s", "500KB", "--resume"]) == 0
    with raw.RawReader(base + ".001") as r:
        assert r.size == len(data)
        assert b"".join(r.stream()) == data


def test_existing_set_suggests_resume(tmp_path, evidence, capsys):
    path, _ = evidence
    assert acquire(path, tmp_path / "case") == 0
    assert acquire(path, tmp_path / "case") == 2
    assert "--resume" in capsys.readouterr().err
