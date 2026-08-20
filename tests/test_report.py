"""The acquisition summary must keep FTK Imager's exact shape."""

import time

from memorylane.report import Acquisition


class FakeSource:
    source_type = "Physical"
    sector_size = 512
    cylinders = 1018
    model = "SanDisk Cruzer Blade"
    serial = "4C530001181205119344"
    interface = "USB"
    removable = True
    bad_sectors = None


def build(**kw):
    a = Acquisition(FakeSource(), "/cases/001/img.E01", {
        "case_number": "2026-042", "evidence_number": "001",
        "description": "USB key", "examiner": "N. Buisson", "notes": "bag A17"})
    a.started = a.finished = a.verify_started = a.verify_finished = time.time()
    a.segments = ["/cases/001/img.E01", "/cases/001/img.E02"]
    a.hashes = {"md5": "0" * 32, "sha1": "1" * 40}
    a.verify_hashes = dict(a.hashes)
    a.data_size = 8000 * 1024 * 1024
    a.sector_count = 16_384_000
    for key, value in kw.items():
        setattr(a, key, value)
    return a


def test_ftk_layout():
    text = build().render()
    for line in ("Case Information:",
                 "Case Number: 2026-042",
                 "Evidence Number: 001",
                 "Unique description: USB key",
                 "Examiner: N. Buisson",
                 "Notes: bag A17",
                 "Physical Evidentiary Item (Source) Information:",
                 "[Device Info]",
                 " Source Type: Physical",
                 "[Drive Geometry]",
                 " Cylinders: 1,018",
                 " Tracks per Cylinder: 255",
                 " Sectors per Track: 63",
                 " Bytes per Sector: 512",
                 " Sector Count: 16,384,000",
                 "[Physical Drive Information]",
                 " Drive Model: SanDisk Cruzer Blade",
                 " Drive Serial Number: 4C530001181205119344",
                 " Drive Interface Type: USB",
                 " Removable drive: True",
                 " Source data size: 8000 MB",
                 " Sector count:    16384000",
                 "[Computed Hashes]",
                 "Image Information:",
                 " Segment list:",
                 "Image Verification Results:"):
        assert line in text.splitlines(), f"missing line: {line!r}"
    assert text.startswith("Created By MemoryLane ")


def test_hash_columns_line_up():
    text = build().render()
    md5 = [x for x in text.splitlines() if x.startswith(" MD5 checksum:")][0]
    sha1 = [x for x in text.splitlines() if x.startswith(" SHA1 checksum:")][0]
    assert md5.index("0" * 32) == sha1.index("1" * 40) == 18


def test_verified_flag():
    assert build().verified is True
    assert build(verify_hashes={"md5": "9" * 32, "sha1": "1" * 40}).verified is False
    assert build(verify_hashes={}).verified is None


def test_mismatch_is_labelled():
    text = build(verify_hashes={"md5": "9" * 32, "sha1": "1" * 40}).render()
    assert ": MISMATCH" in text
    assert ": verified" in text


def test_crlf_on_disk(tmp_path):
    path = build().write(str(tmp_path / "img.E01.txt"))
    assert open(path, "rb").read().count(b"\r\n") > 20
