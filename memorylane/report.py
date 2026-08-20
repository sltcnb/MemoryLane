"""FTK Imager-style acquisition summary (`<image>.txt`).

Mirrors the layout AccessData FTK Imager drops next to its evidence files so
existing case templates, parsers and reviewers see exactly what they expect.
"""

import time

from . import PRODUCT

RULE = "-" * 62


def _stamp(when):
    return time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(when))


def _thousands(n):
    return f"{n:,}"


class Acquisition:
    """Everything the summary needs, filled in as the job progresses."""

    def __init__(self, source, image_path, meta):
        self.source = source
        self.image_path = image_path
        self.meta = meta
        self.started = None
        self.finished = None
        self.verify_started = None
        self.verify_finished = None
        self.segments = []
        self.hashes = {}
        self.verify_hashes = {}
        self.data_size = 0
        self.sector_count = 0
        self.errors = []

    @property
    def verified(self):
        """True when every computed digest matched on read-back."""
        if not self.verify_hashes:
            return None
        return all(self.verify_hashes.get(k) == v for k, v in self.hashes.items()
                   if k in self.verify_hashes)

    def render(self):
        m = self.meta
        src = self.source
        out = [f"Created By {PRODUCT}", ""]
        out.append("Case Information:")
        out.append(f"Acquired using: {PRODUCT}")
        out.append(f"Case Number: {m.get('case_number', '')}")
        out.append(f"Evidence Number: {m.get('evidence_number', '')}")
        out.append(f"Unique description: {m.get('description', '')}")
        out.append(f"Examiner: {m.get('examiner', '')}")
        out.append(f"Notes: {m.get('notes', '')}")
        out += ["", RULE, "", f"Information for {self.image_path}:", ""]

        out.append("Physical Evidentiary Item (Source) Information:")
        out.append("[Device Info]")
        out.append(f" Source Type: {src.source_type}")
        out.append("[Drive Geometry]")
        out.append(f" Cylinders: {_thousands(src.cylinders)}")
        out.append(" Tracks per Cylinder: 255")
        out.append(" Sectors per Track: 63")
        out.append(f" Bytes per Sector: {src.sector_size}")
        out.append(f" Sector Count: {_thousands(self.sector_count)}")
        out.append("[Physical Drive Information]")
        out.append(f" Drive Model: {src.model}")
        out.append(f" Drive Serial Number: {src.serial}")
        out.append(f" Drive Interface Type: {src.interface}")
        removable = src.removable
        out.append(" Removable drive: "
                   f"{'N/A' if removable is None else ('True' if removable else 'False')}")
        out.append(f" Source data size: {self.data_size // (1024 * 1024)} MB")
        out.append(f" Sector count:    {self.sector_count}")

        out.append("[Computed Hashes]")
        for name, value in self.hashes.items():
            out.append(f" {_label(name)}{value}")

        if src.bad_sectors:
            out.append("[Read Errors]")
            out.append(f" Bad sectors replaced with zeros: {src.bad_sectors.count}")
            for span in src.bad_sectors.format():
                out.append(f"  sector {span}")
        for error in self.errors:
            out.append(f" {error}")

        out += ["", "Image Information:"]
        out.append(f" Acquisition started:   {_stamp(self.started)}")
        out.append(f" Acquisition finished:  {_stamp(self.finished)}")
        out.append(" Segment list:")
        for segment in self.segments:
            out.append(f" {segment}")

        out.append("")
        out.append("Image Verification Results:")
        if not self.verify_hashes:
            out.append(" Verification not performed.")
        else:
            out.append(f" Verification started:  {_stamp(self.verify_started)}")
            out.append(f" Verification finished: {_stamp(self.verify_finished)}")
            for name, value in self.verify_hashes.items():
                status = "verified" if self.hashes.get(name) == value else "MISMATCH"
                out.append(f" {_label(name)}{value} : {status}")
        out.append("")
        return "\n".join(out)

    def write(self, path):
        with open(path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(self.render())
        return path


def _label(name):
    pretty = {"md5": "MD5", "sha1": "SHA1", "sha256": "SHA256",
              "sha512": "SHA512", "blake2b": "BLAKE2b"}.get(name, name.upper())
    return f"{pretty} checksum:".ljust(17)
