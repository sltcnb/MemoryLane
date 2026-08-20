"""MemoryLane command-line interface."""

import argparse
import os
import sys
import time

from . import PRODUCT, __version__, collect, ewf, raw
from .hashing import DEFAULT as DEFAULT_HASHES, SUPPORTED, MultiHash
from .progress import Progress, human
from .report import Acquisition
from .source import (
    Source,
    SourceError,
    list_devices,
    mounted_volumes,
    whole_disk_of_device,
    whole_disk_of_path,
)
from .ui import ConsoleReporter

DEFAULT_BLOCK = 1 << 20
_SUFFIXES = (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10),
             ("T", 1 << 40), ("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10),
             ("B", 1))


def parse_size(text):
    text = str(text).strip().upper().replace(" ", "")
    if not text:
        raise ValueError("empty size")
    for suffix, multiplier in _SUFFIXES:
        if text.endswith(suffix):
            return int(float(text[:-len(suffix)] or 0) * multiplier)
    return int(float(text))


def strip_evidence_suffix(path):
    """out/img.E01 or out/img.001 -> out/img (so -o accepts either form)."""
    root, ext = os.path.splitext(path)
    low = ext.lower()
    if low[1:].isdigit() and len(low) == 4:
        return root
    if len(low) == 4 and low[1] in "esl" and low[2:].isdigit():
        return root
    if low in (".dd", ".raw", ".img", ".e01", ".ex01", ".s01", ".l01"):
        return root
    return path


def is_collection(path):
    """True for a MemoryLane logical-collection container."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as zf:
            return collect.MANIFEST_JSON in zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def open_image(path):
    """Return an EwfReader or RawReader based on the file's signature."""
    if not os.path.exists(path):
        raise SystemExit(f"error: {path}: no such file")
    with open(path, "rb") as f:
        magic = f.read(8)
    if magic == ewf.SIGNATURE:
        return ewf.EwfReader(path)
    if magic[:8] in (b"LVF\x09\x0d\x0a\xff\x00", b"EVF2\x0d\x0a\x81\x00"):
        raise SystemExit(f"error: {path}: EWF variant not supported (L01/Ex01)")
    return raw.RawReader(path)


def hash_stream(reader, algorithms, label, rep):
    hasher = MultiHash(algorithms)
    bar = rep.progress(label, getattr(reader, "size", 0))
    for block in reader.stream(DEFAULT_BLOCK):
        hasher.update(block)
        bar.advance(len(block))
    elapsed = bar.finish()
    hasher.close()
    return hasher, elapsed


# ------------------------------------------------------------------ acquire

def cmd_acquire(args, reporter=None):
    """Acquire `args.source` into evidence. Reports through `reporter`, so the
    CLI and the TUI drive exactly the same acquisition code."""
    rep = reporter or ConsoleReporter(quiet=args.quiet)
    try:
        source = Source(args.source, sector_size=args.sector_size,
                        retries=args.retries)
    except SourceError as e:
        rep.error(f"error: {e}")
        return 2

    with source:
        segment_size = parse_size(args.split) if args.split else 0
        # --single means "this exact filename"; otherwise -o is a basename and
        # the .E01 / .001 suffix belongs to us.
        single = args.format == "raw" and args.single and not segment_size
        base = args.output if single else strip_evidence_suffix(args.output)
        parent = os.path.dirname(os.path.abspath(base))
        os.makedirs(parent, exist_ok=True)

        if not args.force and _same_device(source, parent):
            rep.error(f"error: destination {parent} lives on the source "
                      "device; image to different media (override with "
                      "--force)")
            return 2

        if source.is_device:
            status = _check_mounts(source, args.force, args.quiet, rep)
            if status:
                return status

        try:
            algorithms = _hash_list(args.hash)
        except ValueError as e:
            rep.error(f"error: {e}")
            return 2

        meta = {
            "case_number": args.case_number,
            "evidence_number": args.evidence_number,
            "description": args.description,
            "examiner": args.examiner,
            "notes": args.notes,
            "software": PRODUCT,
            "os": _os_string(),
        }
        media_type = args.media_type
        if media_type == "auto":
            media_type = "removable" if source.removable else "fixed"

        first_segment = (ewf.segment_name(base, 1) if args.format == "e01"
                         else raw.segment_name(base, 1, single=single))
        existing = [p for p in (ewf.glob_segments(first_segment)
                                if args.format == "e01"
                                else raw.glob_segments(first_segment))
                    if os.path.exists(p)]
        if existing and not (args.force or args.resume):
            rep.error(f"error: {existing[0]} already exists "
                      f"({len(existing)} segment(s)); use --resume to continue "
                      "it or --force to overwrite")
            return 2
        if args.resume and not existing:
            rep.error(f"error: nothing to resume: {first_segment} does not "
                      "exist")
            return 2

        hasher = MultiHash(algorithms)
        started = time.time()
        offset = 0
        try:
            if args.resume:
                writer, offset = _reopen(args, base, source, segment_size, single)
            elif args.format == "e01":
                writer = ewf.EwfWriter(
                    base, source.size, source.sector_size, meta=meta,
                    compression=args.compress, segment_size=segment_size,
                    media_type=media_type,
                    physical=source.source_type != "Logical",
                    geometry=(source.cylinders, 255, 63), acquired=started,
                    workers=args.workers)
            else:
                writer = raw.RawWriter(base, segment_size=segment_size,
                                       single=single)
        except (ewf.EwfError, OSError) as e:
            hasher.close()
            rep.error(f"error: {e}")
            return 2
        total = writer.media_size if args.format == "e01" else source.size

        first = writer.segments[0]
        # A resumed set dictates its own compression and segment size, so
        # report what is actually in force rather than what was typed.
        in_use = getattr(writer, "compression", args.compress)
        split_in_use = writer.segment_size
        if args.resume and args.format == "e01" and in_use != args.compress:
            rep.warn(f"  note: continuing with the image's own {in_use} "
                     f"compression, not --compress {args.compress}")
        rep.info(f"{PRODUCT}")
        rep.info(f"  Source: {source.path}  ({human(source.size)}, "
                 f"{source.sector_count:,} x {source.sector_size} byte sectors)")
        rep.info(f"  Model:  {source.model}  [{source.interface}]")
        rep.info(f"  Output: {first}  ({args.format.upper()}"
                 + (f", {in_use} compression" if args.format == "e01" else "")
                 + (f", {human(split_in_use)} segments" if split_in_use
                    else ", unsplit")
                 + ")")

        if args.resume:
            if offset >= total:
                rep.error("error: the existing image already covers the whole "
                          "source; nothing left to acquire")
                writer.close()
                hasher.close()
                return 2
            trimmed = getattr(writer, "trimmed", 0)
            rep.info(f"  Resuming at {human(offset)} of {human(total)}"
                     + (f", trimmed {human(trimmed)} of interrupted tail"
                        if trimmed else ""))
            # Digest state cannot be carried across a restart, so rebuild it
            # from the bytes already committed to the image.
            rebuild = rep.progress("  Rehashing", offset)
            with open_image(first) as done_so_far:
                for block in done_so_far.stream(DEFAULT_BLOCK, limit=offset):
                    hasher.update(block)
                    rebuild.advance(len(block))
            rebuild.finish()
            if hasher.length != offset:
                rep.error(f"error: could only re-hash {hasher.length} of "
                          f"{offset} committed bytes; the existing image is "
                          "damaged")
                writer.close()
                hasher.close()
                return 2

        carried = offset
        bar = rep.progress("  Imaging", total)
        bar.done = offset
        block = max(args.block_size, source.sector_size)
        try:
            while offset < source.size:
                data = source.read(offset, min(block, source.size - offset))
                if not data:
                    break
                hasher.update(data)
                writer.write(data)
                offset += len(data)
                bar.advance(len(data))
            # E01 pads the tail to a whole sector; hash what actually lands
            # in the image so the stored digest verifies on read-back.
            remaining = total - offset
            while remaining > 0:
                step = min(remaining, 1 << 20)
                hasher.update(b"\x00" * step)
                remaining -= step
        except KeyboardInterrupt:
            bar.finish()
            hasher.close()
            # Leave the set explicitly unfinished rather than closing it out as
            # a complete image whose missing tail reads as zeros.
            writer.abort() if hasattr(writer, "abort") else writer.close()
            rep.error(f"\naborted at {human(offset)} of {human(total)}; "
                      "partial evidence left in place and marked incomplete")
            return 130
        elapsed = bar.finish()
        hasher.close()

        if args.format == "e01":
            writer.set_hashes(hasher.raw("md5"), hasher.raw("sha1"))
            writer.set_errors(source.bad_sectors.as_pairs())
        segments = writer.close()
        finished = time.time()

        report = Acquisition(source, first, meta)
        report.started, report.finished = started, finished
        report.segments = [os.path.abspath(s) for s in segments]
        report.hashes = hasher.digests()
        report.data_size = total
        report.sector_count = -(-total // source.sector_size)
        fresh = total - carried
        rate = fresh / elapsed if elapsed else 0
        rep.info(f"  Imaged {human(fresh)} in {elapsed:,.1f}s ({human(rate)}/s)"
                 + (f", {human(carried)} carried over" if carried else "")
                 + f" -> {len(segments)} segment(s)")
        for name, value in report.hashes.items():
            rep.info(f"    {name.upper():<7} {value}")
        if source.bad_sectors:
            rep.warn(f"  WARNING: {source.bad_sectors.count} unreadable "
                     "sector(s) replaced with zeros")

        status = 0
        if not args.no_verify:
            report.verify_started = time.time()
            with open_image(first) as reader:
                verifier, velapsed = hash_stream(reader, algorithms,
                                                 "  Verifying", rep)
                stored_md5 = getattr(reader, "stored_md5", None)
                if getattr(reader, "complete", True) is False:
                    report.errors.append("Image set has no closing 'done' section")
                for number, reason in getattr(reader, "corrupt_chunks", []):
                    report.errors.append(f"Damaged chunk {number}: {reason}")
            report.verify_finished = time.time()
            report.verify_hashes = verifier.digests()
            if stored_md5 and stored_md5 != report.verify_hashes.get("md5"):
                report.errors.append(
                    f"Stored E01 MD5 {stored_md5} does not match read-back")
            rep.info(f"  Verified in {velapsed:,.1f}s")
            for name, value in report.verify_hashes.items():
                ok = report.hashes.get(name) == value
                rep.info(f"    {name.upper():<7} {value} : "
                         f"{'verified' if ok else 'MISMATCH'}")
            if not report.verified or report.errors:
                status = 1

        txt = report.write(first + ".txt")
        report.summary_path = txt
        rep.info(f"  Summary: {txt}")
        rep.info("  " + ("VERIFIED" if report.verified
                         else "NOT VERIFIED" if report.verified is None
                         else "VERIFICATION FAILED"))
        rep.result(report)
        return status


def _reopen(args, base, source, segment_size, single):
    """Continue an interrupted acquisition; returns (writer, resume offset)."""
    if args.format == "e01":
        return ewf.EwfWriter.resume(
            base, source.size, source.sector_size, workers=args.workers,
            segment_size=segment_size)
    return raw.RawWriter.resume(base, segment_size=segment_size, single=single)


def _check_mounts(source, force, quiet, rep):
    """Imaging a mounted, writable volume yields a smeared image. Say so."""
    disk = whole_disk_of_device(source.path)
    mounts = mounted_volumes(disk)
    if not mounts:
        return 0
    writable = [m for m in mounts if not m[2]]
    listing = "\n".join(f"         {device} on {point}"
                         f"{' (read-only)' if readonly else ''}"
                         for device, point, readonly in mounts)
    node = f"/dev/{disk}" if disk else source.requested_path
    if writable and not force:
        unmount = (f"diskutil unmountDisk {node}" if sys.platform == "darwin"
                   else f"umount {writable[0][0]}")
        rep.error(f"error: {node} has volumes mounted for writing:\n{listing}"
                  f"\n       unmount first ({unmount}), or pass --force to "
                  "image a live filesystem anyway")
        return 2
    if writable:
        # Forced: the image will be inconsistent, so never stay quiet about it.
        rep.warn(f"WARNING: imaging {node} with {len(writable)} writable "
                 "volume(s) still mounted; the image will not be a consistent "
                 f"point-in-time copy:\n{listing}")
    elif not quiet:
        rep.warn(f"  note: {len(mounts)} read-only volume(s) mounted on "
                 f"{node}:\n{listing}")
    return 0


def _hash_list(text):
    if not text:
        return DEFAULT_HASHES
    names = [n.strip().lower() for n in text.split(",") if n.strip()]
    for name in names:
        if name not in SUPPORTED:
            raise ValueError(f"unsupported hash '{name}' "
                             f"(choose from {', '.join(SUPPORTED)})")
    # MD5 and SHA1 are what the E01 hash/digest sections carry; keep them.
    return list(dict.fromkeys(list(DEFAULT_HASHES) + names))


def _os_string():
    import platform
    return f"{platform.system()} {platform.release()}"


def _same_device(source, destination):
    """True when the output directory sits on the disk being imaged.

    Compared by whole-disk identity, not by device number: on macOS every disk
    shares a major number, and on Linux the destination filesystem lives on a
    partition whose device number never equals the whole disk's.
    """
    if not source.is_device:
        return False
    src = whole_disk_of_device(source.path)
    dst = whole_disk_of_path(destination)
    return bool(src and dst and src == dst)


# ------------------------------------------------------------------- verify

def cmd_verify(args):
    if is_collection(args.image):
        return _verify_collection(args)
    reader = open_image(args.image)
    with reader:
        algorithms = _hash_list(args.hash)
        stored = {}
        if getattr(reader, "stored_md5", None):
            stored["md5"] = reader.stored_md5
        if getattr(reader, "stored_sha1", None):
            stored["sha1"] = reader.stored_sha1
        if args.expected_md5:
            stored["md5"] = args.expected_md5.lower()
        if args.expected_sha1:
            stored["sha1"] = args.expected_sha1.lower()

        print(f"Verifying {args.image}  ({human(reader.size)}, "
              f"{len(reader.segments)} segment(s))")
        hasher, elapsed = hash_stream(reader, algorithms, "  Hashing",
                                      ConsoleReporter(quiet=args.quiet))
        digests = hasher.digests()
        failures = 0
        if getattr(reader, "complete", True) is False:
            print("  incomplete image: no closing 'done' section "
                  "(truncated set, or acquisition never finished)")
            failures += 1
        errors = getattr(reader, "read_errors", [])
        if errors:
            total = sum(count for _, count in errors)
            print(f"  image records {total:,} unreadable source sector(s) in "
                  f"{len(errors)} range(s) — those are zeros by design")
        corrupt = getattr(reader, "corrupt_chunks", [])
        for number, reason in corrupt[:10]:
            print(f"  chunk {number}: {reason}")
        if len(corrupt) > 10:
            print(f"  ... and {len(corrupt) - 10} further damaged chunk(s)")
        for name, value in digests.items():
            want = stored.get(name)
            if want is None:
                print(f"  {name.upper():<7} {value}   (no stored digest)")
            elif want == value:
                print(f"  {name.upper():<7} {value} : verified")
            else:
                failures += 1
                print(f"  {name.upper():<7} {value} : MISMATCH (stored {want})")
        print(f"  Read {human(hasher.length)} in {elapsed:,.1f}s")
        if corrupt:
            print(f"  {len(corrupt)} damaged chunk(s) zero-filled")
            failures += 1
        if not stored:
            print("  No stored digests to compare against.")
        else:
            print("  " + ("VERIFICATION FAILED" if failures else "VERIFIED"))
        return 1 if failures else 0


# ------------------------------------------------------------------ collect

def cmd_collect(args):
    """Targeted logical acquisition: named paths into one verified container."""
    target = args.output
    if not target.lower().endswith(".zip"):
        target += ".zip"
    parent = os.path.dirname(os.path.abspath(target))
    os.makedirs(parent, exist_ok=True)
    if os.path.exists(target) and not args.force:
        print(f"error: {target} already exists; use --force to overwrite",
              file=sys.stderr)
        return 2

    paths = list(args.paths)
    if args.from_file:
        try:
            with open(args.from_file, encoding="utf-8") as f:
                paths += [line.strip() for line in f
                          if line.strip() and not line.startswith("#")]
        except OSError as e:
            print(f"error: {args.from_file}: {e.strerror}", file=sys.stderr)
            return 2
    if not paths:
        print("error: nothing to collect", file=sys.stderr)
        return 2

    try:
        algorithms = _hash_list(args.hash)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    meta = {
        "case_number": args.case_number,
        "evidence_number": args.evidence_number,
        "description": args.description,
        "examiner": args.examiner,
        "notes": args.notes,
        "software": PRODUCT,
        "os": _os_string(),
    }
    max_size = parse_size(args.max_size) if args.max_size else 0
    started = time.time()
    if not args.quiet:
        print(f"{PRODUCT}")
        print(f"  Collecting {len(paths)} path(s) -> {target}  "
              f"({args.compress} compression)")

    planned = list(collect.walk(paths, excludes=args.exclude or (),
                                max_size=max_size,
                                follow_symlinks=args.follow_symlinks,
                                include_dirs=args.include_dirs))
    total = sum(_safe_size(p) for p, err in planned if err is None)
    bar = Progress("  Collecting", total, enabled=not args.quiet)
    collector = collect.Collector(target, compression=args.compress,
                                  follow_symlinks=args.follow_symlinks)
    for path, err in planned:
        collector.add(path, error=err, progress=bar)
    collector.close(meta)
    elapsed = bar.finish()
    finished = time.time()
    totals = collector.totals()

    if not args.quiet:
        print(f"  Collected {totals['objects']:,} object(s), "
              f"{human(totals['bytes'])} in {elapsed:,.1f}s")
        for kind, count in sorted(totals["by_type"].items()):
            print(f"    {kind + ':':<12}{count:,}")
        for problem in collector.errors[:10]:
            print(f"    ! {problem}")
        if len(collector.errors) > 10:
            print(f"    ! ... and {len(collector.errors) - 10} more")

    container = MultiHash(algorithms)
    with open(target, "rb") as f:
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            container.update(block)
    digests = container.digests()
    container.close()

    status = 0
    collected = sum(1 for e in collector.entries
                    if e.type == "file" and not e.error)
    if collector.errors and not collected:
        print("error: nothing was collected; every path failed",
              file=sys.stderr)
        status = 1
    verify_started = verify_finished = None
    problems = []
    checked = 0
    if not args.no_verify:
        verify_started = time.time()
        vbar = Progress("  Verifying", totals["bytes"], enabled=not args.quiet)
        checked, problems = collect.verify(target, progress=vbar)
        vbar.finish()
        verify_finished = time.time()
        if not args.quiet:
            print(f"  Verified {checked:,} file(s) against the manifest")
        for problem in problems[:10]:
            print(f"    ! {problem}", file=sys.stderr)
        if problems:
            status = 1

    summary = _collection_summary(target, meta, started, finished,
                                  verify_started, verify_finished, totals,
                                  digests, collector, checked, problems)
    with open(target + ".txt", "w", encoding="utf-8", newline="\r\n") as f:
        f.write(summary)
    if not args.quiet:
        for name, value in digests.items():
            print(f"    {name.upper():<7} {value}   (container)")
        print(f"  Summary: {target}.txt")
        if args.no_verify:
            verdict = "NOT VERIFIED"
        elif problems:
            verdict = "VERIFICATION FAILED"
        elif collector.errors:
            # Never let unreadable evidence hide behind a clean-looking result.
            verdict = (f"VERIFIED, with {len(collector.errors)} read error(s) "
                       "recorded in the manifest")
        else:
            verdict = "VERIFIED"
        print(f"  {verdict}")
    return status


def _safe_size(path):
    try:
        st = os.lstat(path)
        return st.st_size if os.path.isfile(path) else 0
    except OSError:
        return 0


def _collection_summary(target, meta, started, finished, verify_started,
                        verify_finished, totals, digests, collector, checked,
                        problems):
    """FTK-shaped summary, adapted to a logical collection."""
    from .report import RULE, _label, _stamp
    out = [f"Created By {PRODUCT}", ""]
    out.append("Case Information:")
    out.append(f"Acquired using: {PRODUCT}")
    out.append(f"Case Number: {meta.get('case_number', '')}")
    out.append(f"Evidence Number: {meta.get('evidence_number', '')}")
    out.append(f"Unique description: {meta.get('description', '')}")
    out.append(f"Examiner: {meta.get('examiner', '')}")
    out.append(f"Notes: {meta.get('notes', '')}")
    out += ["", RULE, "", f"Information for {os.path.abspath(target)}:", ""]
    out.append("Logical Evidentiary Item (Source) Information:")
    out.append("[Device Info]")
    out.append(" Source Type: Logical")
    out.append("[Collection]")
    out.append(f" Objects collected: {totals['objects']:,}")
    for kind, count in sorted(totals["by_type"].items()):
        out.append(f"  {kind}: {count:,}")
    out.append(f" Bytes collected: {totals['bytes']:,}")
    out.append(f" Read errors: {totals['errors']:,}")
    out.append("[Computed Hashes]")
    for name, value in digests.items():
        out.append(f" {_label(name)}{value}")
    out.append(" (hashes cover the container; per-file MD5/SHA1 are in "
               f"{collect.MANIFEST_CSV})")
    if collector.errors:
        out.append("[Read Errors]")
        for problem in collector.errors[:200]:
            out.append(f" {problem}")
        if len(collector.errors) > 200:
            out.append(f" ... and {len(collector.errors) - 200} more")
    out += ["", "Image Information:"]
    out.append(f" Acquisition started:   {_stamp(started)}")
    out.append(f" Acquisition finished:  {_stamp(finished)}")
    out.append(" Segment list:")
    out.append(f" {os.path.abspath(target)}")
    out += ["", "Image Verification Results:"]
    if verify_started is None:
        out.append(" Verification not performed.")
    else:
        out.append(f" Verification started:  {_stamp(verify_started)}")
        out.append(f" Verification finished: {_stamp(verify_finished)}")
        out.append(f" Files re-hashed against the manifest: {checked:,}")
        if problems:
            for problem in problems[:200]:
                out.append(f" MISMATCH {problem}")
        else:
            out.append(" All manifest digests : verified")
    out.append("")
    return "\n".join(out)


# ------------------------------------------------------------------- export

def cmd_export(args):
    """Write an image back out as raw (dd), the way ewfexport does."""
    reader = open_image(args.image)
    with reader:
        segment_size = parse_size(args.split) if args.split else 0
        single = not segment_size
        base = args.output if single else strip_evidence_suffix(args.output)
        parent = os.path.dirname(os.path.abspath(base))
        os.makedirs(parent, exist_ok=True)
        first = raw.segment_name(base, 1, single=single)
        if os.path.exists(first) and not args.force:
            print(f"error: {first} already exists; use --force to overwrite",
                  file=sys.stderr)
            return 2

        hasher = MultiHash(_hash_list(args.hash))
        bar = Progress("  Exporting", reader.size, enabled=not args.quiet)
        with raw.RawWriter(base, segment_size=segment_size, single=single) as w:
            for block in reader.stream(DEFAULT_BLOCK):
                w.write(block)
                hasher.update(block)
                bar.advance(len(block))
            segments = w.segments
        elapsed = bar.finish()
        hasher.close()

        stored = getattr(reader, "stored_md5", None)
        digests = hasher.digests()
        if not args.quiet:
            print(f"Exported {human(hasher.length)} in {elapsed:,.1f}s "
                  f"-> {len(segments)} file(s)")
            for segment in segments:
                print(f"  {segment}")
            for name, value in digests.items():
                print(f"  {name.upper():<7} {value}")
        if stored and stored != digests.get("md5"):
            print(f"error: export MD5 {digests.get('md5')} does not match the "
                  f"digest stored in the image ({stored})", file=sys.stderr)
            return 1
        if getattr(reader, "corrupt_chunks", []):
            print(f"error: {len(reader.corrupt_chunks)} damaged chunk(s) were "
                  "zero-filled in the export", file=sys.stderr)
            return 1
        if stored and not args.quiet:
            print("  MD5 matches the digest stored in the image")
    return 0


def _verify_collection(args):
    """Re-hash every member of a logical collection against its manifest."""
    import json
    import zipfile
    with zipfile.ZipFile(args.image) as zf:
        manifest = json.loads(zf.read(collect.MANIFEST_JSON))
    totals = manifest.get("totals", {})
    print(f"Verifying {args.image}  (logical collection, "
          f"{totals.get('objects', 0):,} object(s))")
    bar = Progress("  Hashing", totals.get("bytes", 0), enabled=not args.quiet)
    checked, problems = collect.verify(args.image, progress=bar)
    elapsed = bar.finish()
    for problem in problems[:20]:
        print(f"  ! {problem}")
    if len(problems) > 20:
        print(f"  ... and {len(problems) - 20} more")
    container = MultiHash(_hash_list(args.hash))
    with open(args.image, "rb") as f:
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            container.update(block)
    digests = container.digests()
    container.close()
    for name, value in digests.items():
        expected = {"md5": args.expected_md5, "sha1": args.expected_sha1}.get(name)
        if expected and expected.lower() != value:
            print(f"  {name.upper():<7} {value} : MISMATCH (expected "
                  f"{expected.lower()})")
            problems.append(f"container {name} mismatch")
        elif expected:
            print(f"  {name.upper():<7} {value} : verified")
        else:
            print(f"  {name.upper():<7} {value}   (container)")
    recorded = totals.get("errors", 0)
    print(f"  Re-hashed {checked:,} file(s) in {elapsed:,.1f}s")
    if recorded:
        print(f"  {recorded} read error(s) were recorded when this collection "
              "was made")
    print("  " + ("VERIFICATION FAILED" if problems else "VERIFIED"))
    return 1 if problems else 0


# --------------------------------------------------------------------- info

_MEDIA_NAMES = {0x00: "removable disk", 0x01: "fixed disk", 0x03: "optical disc",
                0x0e: "logical evidence", 0x10: "memory"}
_HEADER_LABELS = [("c", "Case number"), ("n", "Evidence number"),
                  ("a", "Description"), ("e", "Examiner"), ("t", "Notes"),
                  ("av", "Acquired with"), ("ov", "Operating system"),
                  ("m", "Acquisition date"), ("u", "System date")]


def _ewf_date(value):
    """EWF stores dates as POSIX seconds (EnCase 5+) or `Y M D H M S`
    (EnCase 4 / the FTK flavour libewf writes)."""
    import datetime
    try:
        if value.isdigit():
            return time.strftime("%a %b %d %H:%M:%S %Y",
                                 time.localtime(int(value)))
        parts = value.split()
        if len(parts) == 6:
            fields = [int(p) for p in parts]
            return datetime.datetime(*fields).strftime("%a %b %d %H:%M:%S %Y")
    except (ValueError, OverflowError):
        pass
    return value


def cmd_info(args):
    if is_collection(args.image):
        return _info_collection(args)
    reader = open_image(args.image)
    with reader:
        is_ewf = isinstance(reader, ewf.EwfReader)
        print(f"{args.image}")
        print(f"  Format:            {'EWF / E01' if is_ewf else 'raw (dd)'}")
        print(f"  Media size:        {human(reader.size)} ({reader.size:,} bytes)")
        if is_ewf:
            print(f"  Sector size:       {reader.sector_size}")
            print(f"  Sectors:           {reader.sector_count:,}")
            print(f"  Chunk size:        {reader.chunk_size:,} "
                  f"({reader.sectors_per_chunk} sectors)")
            print(f"  Chunks:            {reader.chunk_count:,}")
            print(f"  Media type:        "
                  f"{_MEDIA_NAMES.get(reader.media_type, reader.media_type)}")
            levels = {0: "none", 1: "fast", 2: "best"}
            print(f"  Compression:       "
                  f"{levels.get(reader.compression, reader.compression)}")
            print(f"  Geometry:          C/H/S {'/'.join(str(x) for x in reader.geometry)}")
            for key, label in _HEADER_LABELS:
                value = reader.metadata.get(key)
                if value:
                    if key in ("m", "u"):
                        value = _ewf_date(value)
                    print(f"  {label + ':':<18} {value}")
            if reader.read_errors:
                total = sum(count for _, count in reader.read_errors)
                print(f"  Read errors:       {total:,} sector(s) in "
                      f"{len(reader.read_errors)} range(s)")
                for first, count in reader.read_errors[:10]:
                    end = first + count - 1
                    print(f"                     sector "
                          f"{first}{'' if count == 1 else f'-{end}'}")
                if len(reader.read_errors) > 10:
                    print(f"                     ... and "
                          f"{len(reader.read_errors) - 10} more")
            print(f"  Stored MD5:        {reader.stored_md5 or '(none)'}")
            print(f"  Stored SHA1:       {reader.stored_sha1 or '(none)'}")
        print("  Segments:")
        for segment in reader.segments:
            print(f"    {segment}  ({human(os.path.getsize(segment))})")
    return 0


def _info_collection(args):
    import json
    import zipfile
    with zipfile.ZipFile(args.image) as zf:
        manifest = json.loads(zf.read(collect.MANIFEST_JSON))
    case = manifest.get("case", {})
    totals = manifest.get("totals", {})
    print(f"{args.image}")
    print(f"  Format:            MemoryLane logical collection "
          f"(manifest v{manifest.get('manifest_version')})")
    print(f"  Container size:    {human(os.path.getsize(args.image))}")
    print(f"  Objects:           {totals.get('objects', 0):,}")
    for kind, count in sorted(totals.get("by_type", {}).items()):
        print(f"    {kind + ':':<16} {count:,}")
    print(f"  Bytes collected:   {human(totals.get('bytes', 0))}")
    print(f"  Read errors:       {totals.get('errors', 0):,}")
    for key, label in (("case_number", "Case number"),
                       ("evidence_number", "Evidence number"),
                       ("description", "Description"), ("examiner", "Examiner"),
                       ("notes", "Notes"), ("software", "Acquired with"),
                       ("os", "Operating system")):
        if case.get(key):
            print(f"  {label + ':':<18} {case[key]}")
    return 0


# ------------------------------------------------------------------ devices

def cmd_devices(args):
    devices = list_devices()
    if not devices:
        print("No devices found (try running with elevated privileges).")
        return 1
    print(f"{'DEVICE':<22}{'SIZE':>12}  {'BUS':<14}{'RM':<4}MODEL")
    for d in devices:
        size = human(d["size"]) if d.get("size") else "?"
        print(f"{d['path']:<22}{size:>12}  {(d.get('interface') or '?'):<14}"
              f"{('yes' if d.get('removable') else 'no'):<4}{d.get('model') or ''}")
    return 0


# ---------------------------------------------------------------------- tui

def cmd_tui(args):
    from . import tui

    return tui.main()


# --------------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(
        prog="mlane",
        description="MemoryLane - forensic disk imager. Writes raw (dd) or "
                    "EWF/E01 evidence with FTK Imager-compatible output, "
                    "hashes the acquisition, verifies the result and drops the "
                    "familiar .txt acquisition summary next to the image.")
    p.add_argument("-V", "--version", action="version",
                   version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("acquire", aliases=["image"],
                       help="image a device or file to E01 / raw evidence")
    a.add_argument("source", help="block device or file (e.g. /dev/disk4, dump.bin)")
    a.add_argument("-o", "--output", required=True, metavar="PATH",
                   help="output basename; the .E01/.001 suffix is added for you")
    a.add_argument("-f", "--format", choices=("e01", "raw"), default="e01",
                   help="evidence format (default: e01)")
    a.add_argument("-c", "--compress", choices=("none", "fast", "best"),
                   default="fast", help="E01 compression (default: fast)")
    a.add_argument("-s", "--split", default="1500MB", metavar="SIZE",
                   help="segment size, e.g. 2GB / 650MB / 0 for none "
                        "(default: 1500MB, matching FTK Imager)")
    a.add_argument("--single", action="store_true",
                   help="with -f raw and -s 0, write one file named exactly "
                        "as -o instead of <name>.001")
    a.add_argument("--hash", metavar="LIST", default="md5,sha1",
                   help=f"hashes to compute ({', '.join(SUPPORTED)}); md5 and "
                        "sha1 are always included")
    a.add_argument("--no-verify", action="store_true",
                   help="skip the read-back verification pass")
    a.add_argument("--sector-size", type=int, metavar="N",
                   help="override the detected sector size")
    a.add_argument("--block-size", type=parse_size, default=DEFAULT_BLOCK,
                   metavar="SIZE", help="read block size (default: 1MB)")
    a.add_argument("--retries", type=int, default=2, metavar="N",
                   help="re-read attempts before a sector is written off as "
                        "bad and zero-filled (default: 2)")
    a.add_argument("--workers", type=int, metavar="N",
                   help="threads used to deflate E01 chunks "
                        f"(default: {ewf.default_workers()} here; 1 disables)")
    a.add_argument("--media-type", default="auto",
                   choices=("auto", "fixed", "removable", "optical", "memory"),
                   help="value recorded in the E01 volume section")
    a.add_argument("--case-number", default="", metavar="TEXT")
    a.add_argument("--evidence-number", default="", metavar="TEXT")
    a.add_argument("--description", default="", metavar="TEXT")
    a.add_argument("--examiner", default="", metavar="TEXT")
    a.add_argument("--notes", default="", metavar="TEXT")
    a.add_argument("--resume", action="store_true",
                   help="continue an interrupted acquisition into the existing "
                        "evidence set instead of starting over")
    a.add_argument("--force", action="store_true",
                   help="write even if the destination sits on the source device")
    a.add_argument("-q", "--quiet", action="store_true")
    a.set_defaults(func=cmd_acquire)

    v = sub.add_parser("verify", help="re-hash an image and check stored digests")
    v.add_argument("image", help="first segment (.E01 / .001) or a raw image")
    v.add_argument("--hash", metavar="LIST", default="md5,sha1")
    v.add_argument("--expected-md5", metavar="HEX")
    v.add_argument("--expected-sha1", metavar="HEX")
    v.add_argument("-q", "--quiet", action="store_true")
    v.set_defaults(func=cmd_verify)

    c = sub.add_parser("collect", aliases=["logical"],
                       help="targeted logical acquisition of chosen paths")
    c.add_argument("paths", nargs="*", help="files or directories to collect")
    c.add_argument("-o", "--output", required=True, metavar="PATH",
                   help="container to write (.zip is appended if missing)")
    c.add_argument("--from-file", metavar="FILE",
                   help="read additional paths from a file, one per line "
                        "(# comments allowed)")
    c.add_argument("-x", "--exclude", action="append", metavar="GLOB",
                   help="skip paths matching this glob; repeatable")
    c.add_argument("--max-size", metavar="SIZE",
                   help="skip files larger than this (e.g. 500MB)")
    c.add_argument("--follow-symlinks", action="store_true",
                   help="collect through symlinks instead of recording them")
    c.add_argument("--include-dirs", action="store_true",
                   help="record directories in the manifest as well as files")
    c.add_argument("-c", "--compress", choices=("none", "fast", "best"),
                   default="fast")
    c.add_argument("--hash", metavar="LIST", default="md5,sha1",
                   help="digests over the container itself")
    c.add_argument("--no-verify", action="store_true")
    c.add_argument("--case-number", default="", metavar="TEXT")
    c.add_argument("--evidence-number", default="", metavar="TEXT")
    c.add_argument("--description", default="", metavar="TEXT")
    c.add_argument("--examiner", default="", metavar="TEXT")
    c.add_argument("--notes", default="", metavar="TEXT")
    c.add_argument("--force", action="store_true")
    c.add_argument("-q", "--quiet", action="store_true")
    c.set_defaults(func=cmd_collect)

    e = sub.add_parser("export", aliases=["convert"],
                       help="write an image back out as raw (dd)")
    e.add_argument("image", help="first segment (.E01 / .001) or a raw image")
    e.add_argument("-o", "--output", required=True, metavar="PATH")
    e.add_argument("-s", "--split", default="0", metavar="SIZE",
                   help="split the output into segments of this size "
                        "(default: one file)")
    e.add_argument("--hash", metavar="LIST", default="md5,sha1")
    e.add_argument("--force", action="store_true")
    e.add_argument("-q", "--quiet", action="store_true")
    e.set_defaults(func=cmd_export)

    i = sub.add_parser("info", help="print image metadata (ewfinfo-style)")
    i.add_argument("image")
    i.set_defaults(func=cmd_info)

    t = sub.add_parser("tui", aliases=["ui"],
                       help="full-screen console interface")
    t.set_defaults(func=cmd_tui)

    d = sub.add_parser("devices", aliases=["list"],
                       help="list attached physical drives")
    d.set_defaults(func=cmd_devices)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except (ewf.EwfError, SourceError, collect.CollectionError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
