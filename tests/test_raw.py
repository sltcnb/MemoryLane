"""Raw (dd) writer/reader tests."""

import os

from memorylane import raw


def test_split_segments(tmp_path, evidence):
    path, data = evidence
    base = str(tmp_path / "img")
    with raw.RawWriter(base, segment_size=1024 * 1024) as w:
        w.write(data)
        segments = w.segments
    assert [os.path.basename(s) for s in segments] == \
        ["img.001", "img.002", "img.003", "img.004"]
    assert sum(os.path.getsize(s) for s in segments) == len(data)
    assert os.path.getsize(segments[0]) == 1024 * 1024


def test_single_file(tmp_path, evidence):
    path, data = evidence
    target = str(tmp_path / "one.dd")
    with raw.RawWriter(target, single=True) as w:
        w.write(data)
        segments = w.segments
    assert segments == [target]
    assert open(target, "rb").read() == data


def test_reader_spans_segments(tmp_path, evidence):
    path, data = evidence
    base = str(tmp_path / "img")
    with raw.RawWriter(base, segment_size=100_000) as w:
        w.write(data)
    with raw.RawReader(base + ".001") as r:
        assert r.size == len(data)
        assert b"".join(r.stream()) == data
        # A read that straddles a segment boundary must still be contiguous.
        assert r.read(99_000, 4000) == data[99_000:103_000]
        assert r.read(len(data) - 10, 100) == data[-10:]


def test_write_in_small_pieces(tmp_path):
    base = str(tmp_path / "img")
    data = bytes(range(256)) * 40
    with raw.RawWriter(base, segment_size=1000) as w:
        for i in range(0, len(data), 37):
            w.write(data[i:i + 37])
        segments = w.segments
    assert b"".join(open(s, "rb").read() for s in segments) == data
    assert all(os.path.getsize(s) <= 1000 for s in segments)


def test_glob_stops_at_gap(tmp_path):
    for name in ("i.001", "i.002", "i.004"):
        (tmp_path / name).write_bytes(b"x")
    assert raw.glob_segments(str(tmp_path / "i.001")) == \
        [str(tmp_path / "i.001"), str(tmp_path / "i.002")]
