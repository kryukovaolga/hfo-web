"""Scratch arrays on a byte store, for block processing where memory maps are not available.

Block processing (blocked.py) keeps full-length float64 arrays in scratch
storage. Natively these are np.memmap files. In the browser a file cannot be memory-mapped,
but it can be read and written at an offset (OPFS sync access handles): ScratchArray gives
that kind of store the subset of the ndarray interface the block code uses, and every value
goes through unchanged (same float64 bytes), so results are identical to the memmap path.

Supported, as used by blocked.py, detectors.py and matlab.py:
- len(a), np.asarray(a);
- a[i] (int, negative allowed) -> float; a[i] = value;
- a[s:e] -> lazy view (no data read); views can be sliced again; a[s:e] = array or scalar;
- a[s:e:step] with step != 1 -> ndarray (read, then stepped);
- a[index_array] -> ndarray (read in contiguous runs);
- a[s:e] += value.

A store has read_into(offset, out: memoryview), write(offset, data: memoryview) and
close(); offsets in bytes. FileStore is the plain-file version (tests, and platforms
without memory maps).
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

_ITEM = 8  # float64


class FileStore:
    """Byte store on a regular file (seek + read/write); the file is deleted on close."""

    def __init__(self, path: str | Path, nbytes: int):
        self.path = Path(path)
        self._f = open(self.path, "w+b")
        self._f.truncate(nbytes)

    def read_into(self, offset: int, out: memoryview) -> None:
        self._f.seek(offset)
        if self._f.readinto(out) != len(out):
            raise OSError("short read from scratch file")

    def write(self, offset: int, data: memoryview) -> None:
        self._f.seek(offset)
        self._f.write(data)

    def close(self) -> None:
        self._f.close()
        try:
            os.remove(self.path)
        except OSError:
            pass


class ScratchArray:
    """1-D float64 array of length n stored in `store`, starting at element `start`."""

    def __init__(self, store, n: int, start: int = 0):
        self.store = store
        self.n = n
        self.start = start

    def __len__(self) -> int:
        return self.n

    # ---- raw access (indices relative to this view) ------------------------------------
    def _read(self, s: int, e: int) -> np.ndarray:
        out = np.empty(max(e - s, 0), dtype=np.float64)
        if out.size:
            self.store.read_into((self.start + s) * _ITEM, memoryview(out).cast("B"))
        return out

    def _write(self, s: int, data) -> None:
        data = np.ascontiguousarray(data, dtype=np.float64)
        if data.size:
            self.store.write((self.start + s) * _ITEM, memoryview(data).cast("B"))

    def _index(self, i) -> int:
        i = int(i)
        if i < 0:
            i += self.n
        if not 0 <= i < self.n:
            raise IndexError(f"index {i} out of range for length {self.n}")
        return i

    def _gather(self, idx: np.ndarray) -> np.ndarray:
        """Read arbitrary indices, one contiguous read per monotonic run without big gaps."""
        idx = np.asarray(idx, dtype=np.int64)
        idx = np.where(idx < 0, idx + self.n, idx)
        if idx.size and (idx.min() < 0 or idx.max() >= self.n):
            raise IndexError("index out of range")
        out = np.empty(idx.shape, dtype=np.float64)
        flat, res = idx.reshape(-1), out.reshape(-1)
        if flat.size == 0:
            return out
        d = np.diff(flat)
        cuts = np.flatnonzero((d < 0) | (d > 4096)) + 1
        for a, b in zip(np.concatenate([[0], cuts]), np.concatenate([cuts, [flat.size]])):
            seg = flat[a:b]
            lo, hi = int(seg.min()), int(seg.max()) + 1
            res[a:b] = self._read(lo, hi)[seg - lo]
        return out

    # ---- ndarray-like interface ---------------------------------------------------------
    def __array__(self, dtype=None, copy=None):
        a = self._read(0, self.n)
        return a if dtype is None else a.astype(dtype, copy=False)

    def __getitem__(self, key):
        if isinstance(key, slice):
            s, e, step = key.indices(self.n)
            if step == 1:
                return ScratchArray(self.store, max(e - s, 0), self.start + s)
            lo, hi = (s, e) if step > 0 else (e + 1, s + 1)
            return self._read(lo, hi)[::step] if step > 0 else self._read(lo, hi)[::-1][::-step]
        if isinstance(key, (np.ndarray, list)):
            return self._gather(np.asarray(key))
        i = self._index(key)
        return float(self._read(i, i + 1)[0])

    def __setitem__(self, key, value) -> None:
        if isinstance(key, slice):
            s, e, step = key.indices(self.n)
            if step != 1:
                raise NotImplementedError("strided assignment")
            if isinstance(value, ScratchArray):
                if value.store is self.store and value.start == self.start + s and value.n == e - s:
                    return  # a[s:e] += x already wrote through the view
                value = np.asarray(value)
            if np.ndim(value) == 0:
                value = np.full(max(e - s, 0), value, dtype=np.float64)
            if len(value) != e - s:
                raise ValueError(f"cannot assign {len(value)} values to a slice of {e - s}")
            self._write(s, value)
            return
        i = self._index(key)
        self._write(i, np.array([value], dtype=np.float64))

    def __iadd__(self, other):
        self._write(0, self._read(0, self.n) + other)
        return self
