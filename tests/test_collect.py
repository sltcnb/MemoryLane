"""Targeted logical acquisition: container, manifest and verification."""

import hashlib
import json
import os
import stat
import zipfile

import pytest

from memorylane import collect
from memorylane.cli import main


@pytest.fixture
def tree(tmp_path):
    """A directory with the awkward cases a real collection meets."""
    root = tmp_path / "src"
    (root / "logs").mkdir(parents=True)
    (root / "docs" / "nested").mkdir(parents=True)
    (root / "logs" / "auth.log").write_bytes(b"authentication log\n" * 10)
    (root / "docs" / "report.pdf").write_bytes(bytes(range(256)) * 400)
    (root / "docs" / "nested" / "notes.txt").write_text("notes")
    (root / "scratch.tmp").write_text("disposable")
    try:
        (root / "docs" / "link").symlink_to("../logs/auth.log")
    except OSError:                      # Windows without developer mode
        pass
    return root


def collected_names(target):
    with zipfile.ZipFile(target) as zf:
        return {n for n in zf.namelist()
                if n not in (collect.MANIFEST_CSV, collect.MANIFEST_JSON)}


def manifest_of(target):
    with zipfile.ZipFile(target) as zf:
        return json.loads(zf.read(collect.MANIFEST_JSON))


def by_source(manifest):
    return {os.path.basename(e["source_path"]): e for e in manifest["files"]}


def test_archive_name_keeps_provenance():
    name = collect.archive_name("/var/log/auth.log")
    assert name.endswith("var/log/auth.log")
    assert not name.startswith("/")
    assert "\\" not in name
    if os.name == "nt":
        # The drive letter is part of the provenance and must be kept.
        assert name.split("/")[0].isalpha()
    else:
        assert name == "var/log/auth.log"


def test_collects_files_and_hashes_them(tmp_path, tree):
    target = str(tmp_path / "out.zip")
    assert main(["collect", str(tree), "-o", target, "-q"]) == 0
    entries = by_source(manifest_of(target))

    report = tree / "docs" / "report.pdf"
    record = entries["report.pdf"]
    assert record["type"] == "file"
    assert record["size"] == report.stat().st_size
    assert record["md5"] == hashlib.md5(report.read_bytes()).hexdigest()
    assert record["sha1"] == hashlib.sha1(report.read_bytes()).hexdigest()
    assert record["error"] is None

    with zipfile.ZipFile(target) as zf:
        stored = zf.read(collect.archive_name(str(report)))
    assert stored == report.read_bytes()


def test_symlinks_are_recorded_not_followed(tmp_path, tree):
    if not (tree / "docs" / "link").is_symlink():
        pytest.skip("symlinks unavailable on this platform")
    target = str(tmp_path / "out.zip")
    assert main(["collect", str(tree), "-o", target, "-q"]) == 0
    link = by_source(manifest_of(target))["link"]
    assert link["type"] == "symlink"
    assert link["symlink_target"] == "../logs/auth.log"
    assert collect.archive_name(str(tree / "docs" / "link")) not in \
        collected_names(target)


def test_excludes_and_size_cap(tmp_path, tree):
    target = str(tmp_path / "out.zip")
    assert main(["collect", str(tree), "-o", target, "-q", "-x", "*.tmp",
                 "--max-size", "1KB"]) == 0
    names = {os.path.basename(n) for n in collected_names(target)}
    assert "scratch.tmp" not in names          # excluded by glob
    assert "report.pdf" not in names           # over the size cap
    assert "auth.log" in names


def test_manifest_csv_matches_json(tmp_path, tree):
    target = str(tmp_path / "out.zip")
    main(["collect", str(tree), "-o", target, "-q"])
    with zipfile.ZipFile(target) as zf:
        csv_text = zf.read(collect.MANIFEST_CSV).decode()
    rows = csv_text.strip().splitlines()
    assert rows[0] == ",".join(collect.CSV_COLUMNS)
    assert len(rows) - 1 == len(manifest_of(target)["files"])


def test_case_metadata_reaches_manifest_and_summary(tmp_path, tree):
    target = str(tmp_path / "out.zip")
    assert main(["collect", str(tree), "-o", target, "-q",
                 "--case-number", "2026-042", "--evidence-number", "002",
                 "--description", "home subset", "--examiner", "N. Buisson",
                 "--notes", "bag A17"]) == 0
    case = manifest_of(target)["case"]
    assert case["case_number"] == "2026-042"
    assert case["examiner"] == "N. Buisson"

    summary = (tmp_path / "out.zip.txt").read_text()
    assert "Case Number: 2026-042" in summary
    assert "Unique description: home subset" in summary
    assert "Source Type: Logical" in summary
    assert "MD5 checksum:" in summary
    assert "All manifest digests : verified" in summary


@pytest.mark.skipif(os.name == "nt",
                    reason="chmod 000 does not deny reads on Windows")
def test_unreadable_file_is_recorded_not_dropped(tmp_path, tree):
    secret = tree / "logs" / "auth.log"
    secret.chmod(0o000)
    try:
        target = str(tmp_path / "out.zip")
        rc = main(["collect", str(tree), "-o", target, "-q"])
    finally:
        secret.chmod(0o644)
    assert rc == 0
    record = by_source(manifest_of(target))["auth.log"]
    assert record["error"] and "denied" in record["error"].lower()
    assert "[Read Errors]" in (tmp_path / "out.zip.txt").read_text()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX only")
def test_special_files_are_noted_but_not_read(tmp_path, tree):
    os.mkfifo(tree / "pipe")
    target = str(tmp_path / "out.zip")
    assert main(["collect", str(tree), "-o", target, "-q"]) == 0
    record = by_source(manifest_of(target))["pipe"]
    assert record["type"] == "special"
    assert "not a regular file" in record["error"]


def test_include_dirs(tmp_path, tree):
    target = str(tmp_path / "out.zip")
    main(["collect", str(tree), "-o", target, "-q", "--include-dirs"])
    kinds = manifest_of(target)["totals"]["by_type"]
    assert kinds.get("directory", 0) >= 3


def test_from_file_list(tmp_path, tree):
    listing = tmp_path / "paths.txt"
    listing.write_text(f"# targets\n{tree / 'logs' / 'auth.log'}\n"
                       f"{tree / 'docs' / 'nested' / 'notes.txt'}\n")
    target = str(tmp_path / "out.zip")
    assert main(["collect", "-o", target, "--from-file", str(listing),
                 "-q"]) == 0
    assert len(collected_names(target)) == 2


def test_verify_detects_tampering(tmp_path, tree):
    target = str(tmp_path / "out.zip")
    main(["collect", str(tree), "-o", target, "-q"])
    victim = collect.archive_name(str(tree / "docs" / "nested" / "notes.txt"))
    swapped = str(tmp_path / "swapped.zip")
    with zipfile.ZipFile(target) as zin, zipfile.ZipFile(swapped, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == victim:
                data = b"EVIL!"
            zout.writestr(item, data)
    checked, problems = collect.verify(swapped)
    assert problems and "md5 mismatch" in problems[0]
    assert main(["verify", swapped]) == 1


def test_verify_flags_a_stowaway(tmp_path, tree):
    target = str(tmp_path / "out.zip")
    main(["collect", str(tree), "-o", target, "-q"])
    extra = str(tmp_path / "extra.zip")
    with zipfile.ZipFile(target) as zin, zipfile.ZipFile(extra, "w") as zout:
        for item in zin.infolist():
            zout.writestr(item, zin.read(item.filename))
        zout.writestr("planted/evidence.txt", b"not in the manifest")
    _, problems = collect.verify(extra)
    assert any("not in the manifest" in p for p in problems)


def test_verify_needs_a_manifest(tmp_path):
    plain = str(tmp_path / "plain.zip")
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("a.txt", b"hello")
    with pytest.raises(collect.CollectionError):
        collect.verify(plain)


def test_info_reads_a_collection(tmp_path, tree, capsys):
    target = str(tmp_path / "out.zip")
    main(["collect", str(tree), "-o", target, "-q", "--case-number", "C-9"])
    assert main(["info", target]) == 0
    out = capsys.readouterr().out
    assert "logical collection" in out
    assert "C-9" in out


def test_refuses_to_overwrite(tmp_path, tree, capsys):
    target = str(tmp_path / "out.zip")
    assert main(["collect", str(tree), "-o", target, "-q"]) == 0
    assert main(["collect", str(tree), "-o", target, "-q"]) == 2
    assert "already exists" in capsys.readouterr().err
    assert main(["collect", str(tree), "-o", target, "-q", "--force"]) == 0


def test_missing_path_is_evidence_too(tmp_path, capsys):
    target = str(tmp_path / "out.zip")
    assert main(["collect", str(tmp_path / "ghost"), "-o", target, "-q"]) == 1
    assert "nothing was collected" in capsys.readouterr().err
    record = manifest_of(target)["files"][0]
    assert record["type"] == "missing"


def test_nothing_to_collect(tmp_path, capsys):
    assert main(["collect", "-o", str(tmp_path / "out.zip"), "-q"]) == 2
    assert "nothing to collect" in capsys.readouterr().err


@pytest.mark.parametrize("compression", ["none", "fast", "best"])
def test_every_compression_round_trips(tmp_path, tree, compression):
    target = str(tmp_path / f"{compression}.zip")
    assert main(["collect", str(tree), "-o", target, "-q",
                 "-c", compression]) == 0
    checked, problems = collect.verify(target)
    assert checked >= 3 and not problems
    report = tree / "docs" / "report.pdf"
    with zipfile.ZipFile(target) as zf:
        assert zf.read(collect.archive_name(str(report))) == report.read_bytes()


def test_output_gets_a_zip_extension(tmp_path, tree):
    assert main(["collect", str(tree), "-o", str(tmp_path / "noext"),
                 "-q"]) == 0
    assert (tmp_path / "noext.zip").exists()


def test_mode_and_ownership_are_recorded(tmp_path, tree):
    target = str(tmp_path / "out.zip")
    main(["collect", str(tree), "-o", target, "-q"])
    record = by_source(manifest_of(target))["notes.txt"]
    assert record["mode"].startswith("-")
    source = tree / "docs" / "nested" / "notes.txt"
    assert stat.filemode(source.stat().st_mode) == record["mode"]
    if hasattr(os, "getuid"):
        assert record["uid"] == os.getuid()
    else:
        assert record["uid"] is not None
