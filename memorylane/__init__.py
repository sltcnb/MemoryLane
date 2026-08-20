"""MemoryLane — forensic disk imager with FTK Imager-compatible output.

Acquires a bit-for-bit image of a block device or file to raw (dd / split raw)
or EWF (E01) evidence files, hashes the acquisition, verifies the written image
by reading it back, and writes the FTK Imager-style `.txt` acquisition summary
next to the evidence.
"""

__version__ = "0.1.0"
PRODUCT = f"MemoryLane {__version__}"
