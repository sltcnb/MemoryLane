# MemoryLane

[![CI](https://github.com/sltcnb/MemoryLane/actions/workflows/ci.yml/badge.svg)](https://github.com/sltcnb/MemoryLane/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Dependencies: stdlib only](https://img.shields.io/badge/deps-stdlib%20only-brightgreen)](pyproject.toml)

Forensic imager in the spirit of FTK Imager — and with the same output.
MemoryLane acquires a bit-for-bit image of a drive or file into **EWF/E01** or
**raw (dd)** evidence, does **targeted logical collection** of chosen paths,
hashes what it reads, verifies the result by reading it back, and drops the
familiar `image.E01.txt` acquisition summary next to the evidence.

The E01 writer is implemented from the format up: no libewf, no pyewf, no
compiled extensions. Pure Python 3.10+, stdlib only.

## Install

```sh
pip install -e .
# or run without installing:
python3 -m memorylane --help
```

## Usage

```sh
# image a USB drive to E01 with case metadata (the FTK default: 1500 MB
# segments, "fast" compression, MD5 + SHA1, verify after writing)
sudo mlane acquire /dev/disk4 -o /cases/2026-042/usb \
     --case-number 2026-042 --evidence-number 001 \
     --description "Kingston DataTraveler 32GB" \
     --examiner "N. Buisson" --notes "Seized 2026-08-20, bag A17"

# raw dd instead, split into 2 GB pieces, plus a SHA-256 for the report
sudo mlane acquire /dev/sdb -o /cases/2026-042/usb -f raw -s 2GB --hash sha256

# one unsplit dd file, maximum compression is irrelevant for raw
sudo mlane acquire /dev/sdb -o /cases/2026-042/usb.dd -f raw -s 0 --single

# continue an acquisition that was interrupted, instead of starting over
sudo mlane acquire /dev/disk4 -o /cases/2026-042/usb --resume

# targeted logical collection: the paths that matter, not the whole disk
mlane collect /var/log /Users/suspect/Documents -o /cases/2026-042/files \
      -x '*.iso' --max-size 500MB --case-number 2026-042

# or drive the whole thing from a console interface
mlane tui

# what is attached?
mlane devices

# re-verify evidence later against the digests stored inside it
mlane verify /cases/2026-042/usb.E01

# read the metadata (ewfinfo-style); works on collections too
mlane info /cases/2026-042/usb.E01

# turn E01 back into raw so it can be mounted or fed to another tool
mlane export /cases/2026-042/usb.E01 -o /tmp/usb.dd
```

Sources can be devices *or* files, so `acquire` doubles as a converter:
raw → E01, split → single, E01 → E01 with different compression.

### Acquisition

```
MemoryLane 0.1.0
  Source: thumb.bin  (32.0 MB, 65,536 x 512 byte sectors)
  Model:  Kingston DataTraveler 3.0  [USB]
  Output: case2026-042.E01  (E01, fast compression, 1.5 GB segments)
  Imaging [########################----]  86.1%  27.6 MB / 32.0 MB  118.5 MB/s  ETA 00:00:01
  Imaged 32.0 MB in 0.2s (183.7 MB/s) -> 1 segment(s)
    MD5     a0c233753c61b6765dde368f27e2c30a
    SHA1    3ea152d0bff0b6f6badd37a95ebe2b1c2b35ba26
  Verified in 0.1s
    MD5     a0c233753c61b6765dde368f27e2c30a : verified
    SHA1    3ea152d0bff0b6f6badd37a95ebe2b1c2b35ba26 : verified
  Summary: case2026-042.E01.txt
  VERIFIED
```

### The console interface

`mlane tui` is a small full-screen front end for operators who would rather not
remember flags. It lists the attached drives, takes the case details, and shows
the acquisition running.

```
 MemoryLane 0.1.0

  Select a source to image:

  -> /dev/disk0              465.9 GB  Apple Fabric fixed      APPLE SSD AP0512Z
     /dev/disk4              238.5 GB  USB          removable  Kingston DataTraveler 3.0

     File or path: _

  up/down select   enter continue   r rescan   q quit
```

```
 MemoryLane 0.1.0

  Source: /dev/disk4

     Output base            /cases/2026-042/usb
     Format                 e01
     Compression            fast
     Segment size           1500MB
     Hashes                 md5,sha1
     Case number            2026-042
     Evidence number        001
     Description
     Examiner               N. Buisson
     Notes
     Verify after writing   yes
     Resume if unfinished   no
  -> Start acquisition      <press enter>                  (enter here, or F5 from anywhere)

  mlane acquire /dev/disk4 -o /cases/2026-042/usb -f e01 -s 1500MB --hash md5,sha1 -c fast

  up/down field   left/right toggle   type to edit   enter on Start (or F5) begins   esc back
```

The form shows the equivalent command line as you fill it in, so the TUI
doubles as a way to learn the CLI — and it *is* that command line: the form
builds an argument list, argparse parses it, and the same `acquire` code runs.
There is no second acquisition path that could drift from the tested one.
Cancelling raises the same interrupt Ctrl-C does, so a cancelled job leaves the
evidence marked incomplete and `--resume` can pick it up.

On Windows the TUI needs `pip install windows-curses`; the command line does
not.

### The `.txt` summary

Written next to the first segment as `<image>.E01.txt` / `<image>.001.txt`,
line for line where FTK Imager puts it:

```
Created By MemoryLane 0.1.0

Case Information:
Acquired using: MemoryLane 0.1.0
Case Number: 2026-042
Evidence Number: 001
Unique description: Kingston DataTraveler 32GB
Examiner: N. Buisson
Notes: Seized 2026-08-20, bag A17

--------------------------------------------------------------

Information for /cases/2026-042/usb.E01:

Physical Evidentiary Item (Source) Information:
[Device Info]
 Source Type: Physical
[Drive Geometry]
 Cylinders: 3,824
 Tracks per Cylinder: 255
 Sectors per Track: 63
 Bytes per Sector: 512
 Sector Count: 61,440,000
[Physical Drive Information]
 Drive Model: Kingston DataTraveler 3.0
 Drive Serial Number: 60A44C4257A9F1B0C9A70C3E
 Drive Interface Type: USB
 Removable drive: True
 Source data size: 30000 MB
 Sector count:    61440000
[Computed Hashes]
 MD5 checksum:    a0c233753c61b6765dde368f27e2c30a
 SHA1 checksum:   3ea152d0bff0b6f6badd37a95ebe2b1c2b35ba26

Image Information:
 Acquisition started:   Thu Aug 20 14:05:11 2026
 Acquisition finished:  Thu Aug 20 14:31:48 2026
 Segment list:
 /cases/2026-042/usb.E01
 /cases/2026-042/usb.E02

Image Verification Results:
 Verification started:  Thu Aug 20 14:31:48 2026
 Verification finished: Thu Aug 20 14:44:02 2026
 MD5 checksum:    a0c233753c61b6765dde368f27e2c30a : verified
 SHA1 checksum:   3ea152d0bff0b6f6badd37a95ebe2b1c2b35ba26 : verified
```

## What it writes

| | |
|---|---|
| **EWF / E01** | EnCase 5/6 layout: `header2`/`header2`/`header`, `disk`\|`volume`, then `sectors`+`table`+`table2` groups, closed by `digest`, `hash`, `done`. 32 KiB chunks (64 sectors), deflate or stored, adler32 on every descriptor, table and uncompressed chunk. Case metadata lands in the header sections; MD5 and SHA1 land in the `hash` and `digest` sections. Segments roll over at 1500 MB by default and are named `.E01 … .E99, .EAA …`. |
| **Raw / dd** | Byte-for-byte copy, optionally split into `.001`, `.002`, … or written as a single file. |
| **Logical collection** | A ZIP container holding the collected files under their full source paths, plus `MemoryLane-manifest.csv` / `.json` recording per-file size, MD5, SHA1, full-resolution timestamps, mode, ownership, symlink targets and any read error. Verification re-reads every member and re-hashes it against the manifest. |
| **Summary** | `image.E01.txt` in FTK Imager's layout: case block, source geometry, drive identity, computed hashes, segment list, verification result. Collections get the same file, adapted to a logical source. |

### Verified against libewf

MemoryLane's E01 output is cross-checked against **libewf**, the reference EWF
implementation behind Autopsy, The Sleuth Kit and `ewfmount` — in both
directions:

- `ewfverify` reports **SUCCESS** on every combination of compression level
  (`none`/`fast`/`best`), split size and media type, and `ewfinfo` identifies
  the files as **EnCase 5** with all case metadata intact.
- `ewfexport` reproduces the original source byte for byte.
- In reverse, MemoryLane reads multi-segment images written by `ewfacquire` in
  `encase5`, `encase6` and `ftk` formats, matching their stored digests.

`tests/test_libewf_interop.py` runs all of it and skips itself when the libewf
tools are absent (`brew install libewf` / `apt install ewf-tools`).

## Evidence-handling behaviour

- **Read-only, always.** The source is opened `O_RDONLY`. MemoryLane never
  writes to the evidence device.
- **A mounted source is refused.** Imaging a volume that is still mounted for
  writing cannot produce a consistent point-in-time copy, so it stops and tells
  you what to unmount. APFS synthesized volumes and LVM logical volumes are
  traced back to the physical drive underneath, so the check is not fooled by
  an indirect mount. `--force` proceeds and warns loudly, even under `-q`.
- **Bad media doesn't stop the job.** A failed read is retried (`--retries`,
  default 2) sector by sector; unreadable sectors are zero-filled, counted,
  listed by LBA in a `[Read Errors]` block in the summary, **and recorded in
  the E01's own `error2` section** so the defect list travels with the image
  instead of living only in a text file beside it.
- **Interrupted jobs resume.** Ctrl-C closes the set without its `done`
  section, so it reads back as explicitly incomplete rather than as a finished
  image with a zero tail. `--resume` continues from the last committed chunk,
  rebuilding the digest state from what is already on disk and trimming any
  fragment a hard kill left behind. Compression, chunk size, geometry and the
  set identifier are taken from the existing segments, not from the flags.
- **Verification is a real read-back.** The written image is re-opened and
  re-hashed from disk, not from a buffer, and compared against both the
  acquisition hashes and the digest stored inside the E01.
- **Damage is reported, not raised.** A corrupt chunk is zero-filled and named
  in the output; `verify` exits non-zero rather than crashing, so a failing
  image still yields a usable report.
- **Truncated sets are detected.** An image without its closing `done` section
  is flagged as incomplete.
- **It won't overwrite evidence.** Existing segments and destinations on the
  source device are refused unless you pass `--force`.
- **Exit codes:** `0` verified, `1` verification failed or damage found,
  `2` usage/IO error, `130` interrupted (partial evidence is left in place).

## Options

```
mlane acquire SOURCE -o PATH
  -f, --format {e01,raw}      evidence format (default: e01)
  -c, --compress {none,fast,best}
  -s, --split SIZE            segment size, e.g. 2GB / 650MB / 0 (default 1500MB)
      --single                with -f raw -s 0: write exactly the -o filename
      --hash LIST             extra digests: sha256, sha512, blake2b
      --no-verify             skip the read-back pass
      --sector-size N         override sector-size detection
      --block-size SIZE       read block size (default 1MB)
      --workers N             threads deflating E01 chunks (default: cores-1)
      --retries N             re-read attempts per bad sector (default 2)
      --resume                continue an interrupted acquisition
      --media-type {auto,fixed,removable,optical,memory}
      --case-number / --evidence-number / --description / --examiner / --notes
      --force                 overwrite, or image onto the source device
  -q, --quiet
```

## Speed

Deflate, inflate and the MD5/SHA1 digests all run on thread pools — zlib and
hashlib release the GIL, so this is real parallelism. Measured end to end
through the CLI on a 12-core M-series laptop, 600 MB of *incompressible* data
(the worst case; real media compresses and goes faster):

| | acquire | acquire + verify |
|---|---|---|
| `-f raw` | 677 MB/s | 355 MB/s |
| `-c none` | 567 MB/s | 295 MB/s |
| `-c fast`, `--workers 1` | 96 MB/s | 82 MB/s |
| `-c fast`, `--workers 8` | 308 MB/s | 208 MB/s |

A standalone `mlane verify` of a compressed image runs at ~577 MB/s. Every
figure includes MD5 + SHA1 over the whole stream.

`--workers 1` restores single-threaded deflate; the bytes written are
byte-for-byte identical either way, which the test suite asserts.

## Targeted collection

`mlane collect` gathers named paths instead of a whole disk — the shape most
incident response actually needs.

```sh
mlane collect /var/log /etc /Users/suspect/Documents \
      -o /cases/2026-042/files \
      -x '*.iso' -x 'node_modules' --max-size 2GB \
      --case-number 2026-042 --examiner "N. Buisson"
```

Files land in a ZIP under their full source path (`var/log/auth.log`), so
provenance survives and nothing collides. Everything the ZIP format cannot
carry goes into `MemoryLane-manifest.csv` / `.json`: per-file MD5 and SHA1,
full-resolution mtime/atime/ctime, mode, uid/gid, symlink targets, and the
reason any file could not be read. Unreadable and missing paths are recorded as
evidence rather than silently skipped; symlinks are recorded, not followed,
unless you ask.

`mlane verify` re-reads every member, re-hashes it against the manifest, and
also flags anything present in the container but absent from the manifest.

The container is a plain ZIP on purpose: it opens anywhere, and every claim it
makes is independently checkable.

### Why not L01?

EnCase's logical evidence format was the obvious container, and MemoryLane does
not write it. The `ltree` section that carries the file tree is an undocumented
text grammar; the section header geometry was recoverable by probing libewf
(48 bytes, tree size at offset 16), but the tree grammar itself was not — every
structural variant tried left libewf's parser asking for one line past the end
of the input, independent of content. Fitting it by trial and error against one
reader's tolerance would produce a forensic container whose metadata encoding
nobody had verified, which is worse than not offering it. The ZIP container
above does the same job with claims that can be checked.

## Running as root

Whole-disk devices need privileges: `sudo` on macOS/Linux, an elevated shell on
Windows. On macOS MemoryLane automatically switches `/dev/diskN` to the much
faster raw character device `/dev/rdiskN`; unmount the volume first
(`diskutil unmountDisk /dev/diskN`) so nothing writes to it mid-acquisition. A
hardware write-blocker is still the right answer for real evidence.

```sh
sudo mlane acquire /dev/disk4 -o out/usb            # macOS
sudo mlane acquire /dev/sdb   -o out/usb            # Linux
mlane acquire \\.\PhysicalDrive1 -o out\usb         # Windows (admin shell)
```

### A note on Windows

`os.pread` is POSIX-only, so on Windows the package falls back to a seek+read
guarded by a per-descriptor lock — the inflate pool reads one descriptor from
several threads, and that pair is not atomic. CI runs the whole suite on
Windows *and* runs it on Linux and macOS with the fallback forced
(`MEMORYLANE_NO_PREAD=1`), so the Windows read path is covered everywhere.

What is still unproven on Windows is physical-device acquisition: CI has no
disks to image, so `\\.\PhysicalDrive` reads and the `Get-Disk` identity probe
have never run against real hardware. The mounted-source guard is also a no-op
there, since the volume-to-disk mapping is only implemented for macOS and
Linux. File and image sources are fully exercised.

## Tests

```sh
pytest -q
```

147 tests. The suite round-trips every compression level and segment layout,
validates the written E01 against the EWF structure spec (descriptor chain,
section order, every adler32), proves threaded and single-threaded output are
byte-identical, checks the summary against FTK's exact line format, simulates
failing media down to individual sectors and retry counts, exercises resume
across segments and after a hard kill, and asserts that tampered, truncated,
aborted and corrupt images — and tampered or stowaway-carrying collections —
are detected rather than silently accepted. With libewf installed it also
cross-validates against `ewfverify` / `ewfinfo` / `ewfacquire` / `ewfexport`,
including the `error2` defect list. The TUI is driven through a real
pseudo-terminal: the test types a path, fills the form, starts the job from
keystrokes and then reads the resulting E01 back byte for byte.

## License

MIT
