"""Multi-algorithm streaming hasher.

FTK Imager computes MD5 and SHA1 over the acquired data; both go into the E01
hash/digest sections and into the acquisition summary. Extra algorithms can be
requested for the summary only.
"""

import hashlib

DEFAULT = ("md5", "sha1")
SUPPORTED = ("md5", "sha1", "sha256", "sha512", "blake2b")


# hashlib releases the GIL for buffers above a few KB, so running each
# algorithm on its own thread is real parallelism. Below this size the pool
# hand-off costs more than it saves.
THREAD_THRESHOLD = 1 << 16


class MultiHash:
    """Feed bytes once, get every requested digest out.

    With more than one algorithm the digests are computed concurrently; the
    result is identical either way, only faster.
    """

    def __init__(self, algorithms=DEFAULT, threads=True):
        names = []
        for name in algorithms:
            name = name.strip().lower()
            if not name:
                continue
            if name not in SUPPORTED:
                raise ValueError(f"unsupported hash algorithm: {name}")
            if name not in names:
                names.append(name)
        self.names = names
        self._h = {n: hashlib.new(n) for n in names}
        self.length = 0
        self._pool = None
        if threads and len(names) > 1:
            from concurrent.futures import ThreadPoolExecutor
            self._pool = ThreadPoolExecutor(max_workers=len(names),
                                            thread_name_prefix="mlane-hash")

    def update(self, data):
        self.length += len(data)
        if self._pool is None or len(data) < THREAD_THRESHOLD:
            for h in self._h.values():
                h.update(data)
        else:
            # Blocks until every digest has consumed this buffer, so the
            # algorithms stay in lockstep and the state order is preserved.
            list(self._pool.map(lambda h: h.update(data), self._h.values()))

    def digests(self):
        return {n: h.hexdigest() for n, h in self._h.items()}

    def get(self, name):
        h = self._h.get(name)
        return h.hexdigest() if h else None

    def raw(self, name):
        h = self._h.get(name)
        return h.digest() if h else None

    def close(self):
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
