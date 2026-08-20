"""EWF/E01 writer and reader tests, including on-disk structure validation."""

import hashlib
import os
import struct
import zlib

import pytest

from memorylane import ewf

# ----------------------------------------------------------- spec validator

def walk_sections(path, strict=True):
    """List (type, offset, size) for every section, validating as it goes.

    strict=False stops at EOF instead of demanding a terminator, which is what
    a deliberately unfinished (aborted) set looks like.
    """
    with open(path, "rb") as f:
        blob = f.read()
    assert blob[:8] == ewf.SIGNATURE, "bad segment signature"
    assert struct.unpack("<BHH", blob[8:13])[0] == 1
    offset = 13
    seen = []
    while True:
        descriptor = blob[offset:offset + 76]
        if len(descriptor) < 76:
            assert not strict, f"truncated descriptor at {offset}"
            break
        stored = struct.unpack("<I", descriptor[72:76])[0]
        assert stored == zlib.adler32(descriptor[:72]) & 0xFFFFFFFF, \
            f"descriptor checksum failed at {offset}"
        stype = descriptor[:16].split(b"\x00", 1)[0]
        next_offset, size = struct.unpack("<QQ", descriptor[16:32])
        assert size >= 76, f"section {stype} smaller than its descriptor"
        if offset + size > len(blob):
            assert not strict, f"section {stype} runs past EOF"
            seen.append((stype, offset, size))
            break
        seen.append((stype, offset, size))
        if next_offset == offset:                      # terminator
            assert offset + size == len(blob), "terminator is not last in file"
            break
        assert next_offset == offset + size, \
            f"section {stype} next-offset {next_offset} != {offset + size}"
        offset = next_offset
    return seen


def chunk_offset(path, number, skip=0):
    """File offset of a specific chunk's stored bytes.

    Corrupting a hard-coded offset is not safe: the header sections are
    zlib-compressed, so where the chunk data starts moves with the zlib
    version — that is exactly how one of these tests passed everywhere except
    ubuntu/3.10. Read the real position out of the table instead.
    """
    sectors_base = None
    for stype, offset, size in walk_sections(path):
        if stype == b"sectors":
            sectors_base = offset + 76
        elif stype == b"table" and sectors_base is not None:
            body_at = offset + 76
            with open(path, "rb") as f:
                f.seek(body_at)
                head = f.read(24)
                count = struct.unpack("<I", head[:4])[0]
                base = struct.unpack("<Q", head[8:16])[0]
                if number >= count:
                    number -= count            # this chunk is in a later group
                    sectors_base = None
                    continue
                entries = f.read(count * 4)
            entry = struct.unpack("<I", entries[number * 4:number * 4 + 4])[0]
            return base + (entry & 0x7FFFFFFF) + skip
    raise AssertionError(f"chunk {number} not found in {path}")


def flip_byte(path, offset):
    """Invert one byte, so the value always actually changes."""
    with open(path, "r+b") as f:
        f.seek(offset)
        original = f.read(1)
        f.seek(offset)
        f.write(bytes([original[0] ^ 0xFF]))


def noisy_chunk(data, chunk_size=32768):
    """Index of a chunk that is not all zeros.

    A damaged chunk is replaced with zeros, so corrupting an all-zero chunk
    leaves the output byte-identical and proves nothing.
    """
    for i in range(0, len(data) // chunk_size):
        if set(data[i * chunk_size:(i + 1) * chunk_size]) - {0}:
            return i
    raise AssertionError("no non-zero chunk in this fixture")


def test_segment_naming():
    assert ewf.segment_name("d", 1) == "d.E01"
    assert ewf.segment_name("d", 99) == "d.E99"
    assert ewf.segment_name("d", 100) == "d.EAA"
    assert ewf.segment_name("d", 126) == "d.EBA"
    with pytest.raises(ValueError):
        ewf.segment_name("d", 0)


def test_structure_matches_encase_layout(tmp_path, evidence):
    path, data = evidence
    base = str(tmp_path / "img")
    with ewf.EwfWriter(base, len(data), 512, compression="fast") as w:
        w.write(data)
    sections = [s[0] for s in walk_sections(base + ".E01")]
    assert sections[:4] == [b"header2", b"header2", b"header", b"disk"]
    assert sections[-1] == b"done"
    assert b"digest" in sections and b"hash" in sections
    # Chunk data always arrives as sectors + table + table2, in that order.
    for i, stype in enumerate(sections):
        if stype == b"sectors":
            assert sections[i + 1] == b"table"
            assert sections[i + 2] == b"table2"


@pytest.mark.parametrize("physical,expected", [(True, b"disk"), (False, b"volume")])
def test_physical_and_logical_use_the_right_section(tmp_path, physical, expected):
    base = str(tmp_path / f"img-{expected.decode()}")
    with ewf.EwfWriter(base, 4096, 512, media_type="removable",
                       physical=physical):
        pass
    assert [s[0] for s in walk_sections(base + ".E01")][3] == expected


def test_volume_section_fields(tmp_path, evidence):
    path, data = evidence
    base = str(tmp_path / "img")
    with ewf.EwfWriter(base, len(data), 512, compression="best",
                       media_type="removable", physical=False,
                       geometry=(4, 255, 63)):
        pass   # an empty acquisition still writes a complete, valid set
    with open(base + ".E01", "rb") as f:
        blob = f.read()
    for stype, offset, size in walk_sections(base + ".E01"):
        if stype == b"volume":
            body = blob[offset + 76:offset + size]
            assert size - 76 == ewf.VOLUME_SIZE
            assert struct.unpack("<I", body[-4:])[0] == \
                zlib.adler32(body[:-4]) & 0xFFFFFFFF
            assert body[0] == ewf.MEDIA_TYPE["removable"]
            assert struct.unpack("<I", body[4:8])[0] == len(data) // 32768
            assert struct.unpack("<I", body[8:12])[0] == 64      # sectors/chunk
            assert struct.unpack("<I", body[12:16])[0] == 512    # bytes/sector
            assert struct.unpack("<Q", body[16:24])[0] == len(data) // 512
            assert struct.unpack("<III", body[24:36]) == (4, 255, 63)
            assert body[36] == ewf.MEDIA_FLAG_IMAGE              # logical: no
            assert body[52] == ewf.COMPRESSION["best"]
            break
    else:
        pytest.fail("no volume section")


def test_table_checksums(tmp_path, evidence):
    path, data = evidence
    base = str(tmp_path / "img")
    with ewf.EwfWriter(base, len(data), 512) as w:
        w.write(data)
    with open(base + ".E01", "rb") as f:
        blob = f.read()
    tables = 0
    for stype, offset, size in walk_sections(base + ".E01"):
        if stype not in (b"table", b"table2"):
            continue
        tables += 1
        body = blob[offset + 76:offset + size]
        head, rest = body[:24], body[24:]
        assert struct.unpack("<I", head[20:24])[0] == \
            zlib.adler32(head[:20]) & 0xFFFFFFFF
        count = struct.unpack("<I", head[:4])[0]
        entries, trailer = rest[:count * 4], rest[count * 4:count * 4 + 4]
        assert struct.unpack("<I", trailer)[0] == \
            zlib.adler32(entries) & 0xFFFFFFFF
    assert tables >= 2


@pytest.mark.parametrize("compression", ["none", "fast", "best"])
def test_roundtrip(tmp_path, evidence, compression):
    path, data = evidence
    base = str(tmp_path / f"img-{compression}")
    with ewf.EwfWriter(base, len(data), 512, compression=compression) as w:
        w.write(data)
    with ewf.EwfReader(base + ".E01") as r:
        assert r.size == len(data)
        assert b"".join(r.stream()) == data
        assert r.read(1000, 4096) == data[1000:5096]
        assert not r.corrupt_chunks
        assert r.complete


def test_compression_actually_shrinks(tmp_path, evidence):
    path, data = evidence
    sizes = {}
    for compression in ("none", "best"):
        base = str(tmp_path / compression)
        with ewf.EwfWriter(base, len(data), 512, compression=compression) as w:
            w.write(data)
        sizes[compression] = os.path.getsize(base + ".E01")
    assert sizes["best"] < sizes["none"] * 0.8


def test_multi_segment(tmp_path, evidence):
    path, data = evidence
    base = str(tmp_path / "split")
    with ewf.EwfWriter(base, len(data), 512, segment_size=512 * 1024) as w:
        w.write(data)
        segments = w.segments
    assert len(segments) > 3
    for segment in segments:
        sections = [s[0] for s in walk_sections(segment)]
        assert sections[-1] in (b"next", b"done")
    assert [s[0] for s in walk_sections(segments[1])][0] == b"data"
    with ewf.EwfReader(segments[0]) as r:
        assert r.segments == segments
        assert b"".join(r.stream()) == data


def test_partial_sector_is_padded(tmp_path):
    data = b"\xa5" * 1000                       # not a multiple of 512
    base = str(tmp_path / "odd")
    with ewf.EwfWriter(base, len(data), 512) as w:
        w.write(data)
    with ewf.EwfReader(base + ".E01") as r:
        assert r.size == 1024
        out = b"".join(r.stream())
        assert out[:1000] == data
        assert out[1000:] == b"\x00" * 24


def test_stored_digests(tmp_path, evidence):
    path, data = evidence
    base = str(tmp_path / "hashed")
    w = ewf.EwfWriter(base, len(data), 512)
    w.write(data)
    w.set_hashes(hashlib.md5(data).digest(), hashlib.sha1(data).digest())
    w.close()
    with ewf.EwfReader(base + ".E01") as r:
        assert r.stored_md5 == hashlib.md5(data).hexdigest()
        assert r.stored_sha1 == hashlib.sha1(data).hexdigest()


def test_metadata_roundtrip(tmp_path):
    meta = {"case_number": "2026-042", "evidence_number": "001",
            "description": "USB key", "examiner": "N. Buisson",
            "notes": "sealed bag A17", "software": "MemoryLane 0.1.0",
            "os": "Darwin"}
    base = str(tmp_path / "meta")
    with ewf.EwfWriter(base, 4096, 512, meta=meta) as w:
        w.write(b"\x00" * 4096)
    with ewf.EwfReader(base + ".E01") as r:
        assert r.metadata["c"] == "2026-042"
        assert r.metadata["n"] == "001"
        assert r.metadata["a"] == "USB key"
        assert r.metadata["e"] == "N. Buisson"
        assert r.metadata["t"] == "sealed bag A17"
        assert r.metadata["av"] == "MemoryLane 0.1.0"


def test_corrupt_chunk_is_reported_not_raised(tmp_path, evidence):
    path, data = evidence
    base = str(tmp_path / "bad")
    with ewf.EwfWriter(base, len(data), 512, compression="fast") as w:
        w.write(data)
    target = noisy_chunk(data)
    flip_byte(base + ".E01", chunk_offset(base + ".E01", target, skip=8))
    with ewf.EwfReader(base + ".E01") as r:
        out = b"".join(r.stream())
        assert len(out) == r.size            # length preserved
        assert out != data                   # but the content is not trusted
        assert r.corrupt_chunks


def test_uncompressed_chunk_checksum_is_checked(tmp_path):
    data = os.urandom(65536)
    base = str(tmp_path / "nc")
    with ewf.EwfWriter(base, len(data), 512, compression="none") as w:
        w.write(data)
    flip_byte(base + ".E01", chunk_offset(base + ".E01", 0, skip=100))
    with ewf.EwfReader(base + ".E01") as r:
        b"".join(r.stream())
        assert r.corrupt_chunks
        assert r.corrupt_chunks[0][1] == "checksum mismatch"


def test_truncated_set_is_incomplete(tmp_path, evidence):
    path, data = evidence
    base = str(tmp_path / "cut")
    with ewf.EwfWriter(base, len(data), 512, segment_size=512 * 1024) as w:
        w.write(data)
        segments = w.segments
    os.truncate(segments[-1], 200)
    with ewf.EwfReader(segments[0]) as r:
        assert not r.complete
        assert len(b"".join(r.stream())) == r.size
        assert r.corrupt_chunks


def test_writer_refuses_extra_data(tmp_path):
    base = str(tmp_path / "over")
    w = ewf.EwfWriter(base, 4096, 512)
    w.write(b"\x00" * 4096)
    with pytest.raises(ewf.EwfError, match="more data than the declared"):
        w.write(b"\x00")
    w.close()


def test_writer_refuses_use_after_close(tmp_path):
    base = str(tmp_path / "closed")
    w = ewf.EwfWriter(base, 4096, 512)
    w.close()
    with pytest.raises(ewf.EwfError, match="writer is closed"):
        w.write(b"\x00")


def test_short_acquisition_is_zero_filled(tmp_path):
    """A source that ends early still yields a complete, declared-size image."""
    base = str(tmp_path / "short")
    with ewf.EwfWriter(base, 65536, 512) as w:
        w.write(b"\xab" * 1024)
    with ewf.EwfReader(base + ".E01") as r:
        out = b"".join(r.stream())
        assert len(out) == 65536
        assert out[:1024] == b"\xab" * 1024
        assert out[1024:] == b"\x00" * 64512
        assert r.complete


def test_abort_leaves_the_set_incomplete(tmp_path, evidence):
    path, data = evidence
    base = str(tmp_path / "aborted")
    w = ewf.EwfWriter(base, len(data), 512)
    w.write(data[:1_000_000])
    w.abort()
    sections = [s[0] for s in walk_sections(base + ".E01", strict=False)]
    assert sections[-1] == b"table2"
    assert b"done" not in sections
    assert b"digest" not in sections
    with ewf.EwfReader(base + ".E01") as r:
        assert not r.complete
        out = b"".join(r.stream())
        assert out[:983040] == data[:983040]        # whole chunks were kept
        assert r.corrupt_chunks                     # the rest is flagged missing


@pytest.mark.parametrize("segment_size", [0, 700_000])
def test_worker_count_does_not_change_the_bytes(tmp_path, evidence, segment_size):
    """Threaded deflate must produce exactly the single-threaded image."""
    path, data = evidence
    pinned = dict(acquired=1787000000, created=1787000001,
                  guid=b"\x11" * 16, segment_size=segment_size,
                  compression="fast")
    blobs = []
    for workers in (1, 2, 8):
        base = str(tmp_path / f"w{workers}-{segment_size}")
        with ewf.EwfWriter(base, len(data), 512, workers=workers, **pinned) as w:
            for i in range(0, len(data), 7919):        # ragged writes
                w.write(data[i:i + 7919])
            segments = w.segments
        blobs.append(b"".join(open(s, "rb").read() for s in segments))
        with ewf.EwfReader(segments[0]) as r:
            assert b"".join(r.stream()) == data
    assert blobs[0] == blobs[1] == blobs[2]


def test_error2_round_trip(tmp_path):
    ranges = [(100, 4), (5000, 1), (9000, 3)]
    base = str(tmp_path / "defects")
    w = ewf.EwfWriter(base, 1 << 20, 512)
    w.write(b"\x00" * (1 << 20))
    w.set_errors(ranges)
    w.close()
    sections = [s[0] for s in walk_sections(base + ".E01")]
    assert sections.index(b"error2") < sections.index(b"digest")
    with ewf.EwfReader(base + ".E01") as r:
        assert r.read_errors == ranges


def test_error2_section_is_well_formed(tmp_path):
    base = str(tmp_path / "defects")
    w = ewf.EwfWriter(base, 65536, 512)
    w.write(b"\x00" * 65536)
    w.set_errors([(7, 2)])
    w.close()
    with open(base + ".E01", "rb") as f:
        blob = f.read()
    for stype, offset, size in walk_sections(base + ".E01"):
        if stype != b"error2":
            continue
        body = blob[offset + 76:offset + size]
        head, rest = body[:ewf.ERROR2_HEADER_SIZE], body[ewf.ERROR2_HEADER_SIZE:]
        assert struct.unpack("<I", head[:4])[0] == 1
        assert struct.unpack("<I", head[-4:])[0] == \
            zlib.adler32(head[:-4]) & 0xFFFFFFFF
        assert struct.unpack("<II", rest[:8]) == (7, 2)
        assert struct.unpack("<I", rest[8:12])[0] == \
            zlib.adler32(rest[:8]) & 0xFFFFFFFF
        break
    else:
        pytest.fail("no error2 section")


def test_no_error2_when_the_read_was_clean(tmp_path):
    base = str(tmp_path / "clean")
    with ewf.EwfWriter(base, 65536, 512) as w:
        w.write(b"\x00" * 65536)
    assert b"error2" not in [s[0] for s in walk_sections(base + ".E01")]
    with ewf.EwfReader(base + ".E01") as r:
        assert r.read_errors == []


def test_error2_drops_unrepresentable_ranges(tmp_path):
    """32-bit entries cannot describe sectors past 2^32; they are dropped."""
    base = str(tmp_path / "huge")
    w = ewf.EwfWriter(base, 65536, 512)
    w.write(b"\x00" * 65536)
    w.set_errors([(10, 1), (1 << 33, 1)])
    w.close()
    with ewf.EwfReader(base + ".E01") as r:
        assert r.read_errors == [(10, 1)]


def test_windows_read_path_is_correct_under_threads(tmp_path, evidence,
                                                    monkeypatch):
    """Force the no-pread fallback and read concurrently.

    Windows has no os.pread, so the package seeks then reads. That pair is not
    atomic, and the inflate pool reads one descriptor from several threads, so
    this asserts the serialisation actually holds the offset steady.
    """
    from memorylane import _io

    path, data = evidence
    base = str(tmp_path / "winpath")
    with ewf.EwfWriter(base, len(data), 512, compression="fast",
                       segment_size=400_000) as w:
        w.write(data)
    monkeypatch.setattr(_io, "HAVE_PREAD", False)
    for _ in range(3):                   # repeat: races are not deterministic
        with ewf.EwfReader(base + ".E01", workers=8) as r:
            assert b"".join(r.stream()) == data
            assert not r.corrupt_chunks
            assert r.read(1234, 50_000) == data[1234:51_234]
