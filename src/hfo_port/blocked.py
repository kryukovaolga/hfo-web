"""Out-of-core (block) versions of the filtering steps, for recordings that do not fit in RAM.

Signals of full length live in memory-mapped scratch files (Workspace); RAM use depends on
the block length only (or, with Workspace.store_factory, arrays on another byte store).

Exactness with respect to the in-memory path (filters.py):
- filtfilt: the filter state is carried from block to block -> same operations as one call,
  except the mean removed before filtering, which is summed block by block (last-digit
  differences);
- envelope: FFT per block with overlapping margins, wrapped around at the ends like the
  global FFT -> tiny differences (relative error ~1e-7 or less with the default 120-s
  margins), largest near block edges.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

import numpy as np
from scipy import fft as sp_fft
from scipy import signal

from .filters import _coefficients, envelope
from .matlab import blocked_mean
from .scratch import ScratchArray

Reader = Callable[[int, int], np.ndarray]


class Workspace:
    """Scratch directory holding float64 memory-mapped arrays; removed on close.

    store_factory(name, nbytes) -> byte store (see scratch.py): when set, arrays are
    ScratchArray on those stores instead of memory maps (the browser sets it to its
    private-disk files, where memory maps are not available). Same values either way.
    """

    store_factory = None

    def __init__(self, tmp_dir: str | Path | None = None):
        self.path = Path(tempfile.mkdtemp(prefix="hfo-port-", dir=tmp_dir))
        self._arrays: list = []

    def array(self, name: str, n: int):
        if self.store_factory is not None:
            a = ScratchArray(self.store_factory(name, n * 8), n)
        else:
            a = np.memmap(self.path / f"{name}.f64", dtype=np.float64, mode="w+", shape=(n,))
        self._arrays.append(a)
        return a

    def close(self) -> None:
        # Arrays still referenced elsewhere stay valid on POSIX (the files are only unlinked).
        for a in self._arrays:
            if isinstance(a, ScratchArray):
                a.store.close()
        self._arrays.clear()
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def filtfilt_streamed(read: Reader, n: int, b, a, offset: float, out: np.ndarray,
                      block: int) -> np.ndarray:
    """filtfilt(b, a, x - offset) with odd padding of 3*(n_taps-1), x given by read(start, stop).

    out must have length n + 2*pad; the result is the view out[pad:pad + n].
    """
    pad = 3 * (max(len(a), len(b)) - 1)
    if n <= pad:
        raise ValueError(f"signal too short ({n} samples) for filter padding {pad}")
    assert len(out) == n + 2 * pad
    zi = signal.lfilter_zi(b, a)

    head = read(0, pad + 1) - offset
    tail = read(n - pad - 1, n) - offset
    left = 2 * head[:1] - head[pad:0:-1]
    right = 2 * tail[-1:] - tail[-2::-1]

    def pieces():
        yield left
        for s in range(0, n, block):
            yield read(s, min(s + block, n)) - offset
        yield right

    # forward pass
    z = zi * left[0]
    p = 0
    for piece in pieces():
        y, z = signal.lfilter(b, a, piece, zi=z)
        out[p:p + len(y)] = y
        p += len(y)
    # backward pass, from the end, in place
    z = zi * out[-1]
    total = len(out)
    for e in range(total, 0, -block):
        s = max(0, e - block)
        y, z = signal.lfilter(b, a, np.array(out[s:e][::-1]), zi=z)
        out[s:e] = y[::-1]
    return out[pad:pad + n]


def envelope_blocked(x: np.ndarray, out: np.ndarray, block: int, margin: int) -> np.ndarray:
    """abs(hilbert(x)) computed per block with `margin` samples of context on each side,
    taken circularly at the ends (the global FFT also treats the signal as periodic)."""
    n = len(x)
    if n <= block + 2 * margin:
        out[:] = envelope(np.asarray(x, dtype=np.float64))
        return out
    for s in range(0, n, block):
        e = min(s + block, n)
        length = sp_fft.next_fast_len(e - s + 2 * margin, real=True)
        lo = s - margin
        idx = np.arange(lo, lo + length) % n
        env = envelope(np.asarray(x[idx], dtype=np.float64))
        out[s:e] = env[margin:margin + e - s]
    return out


def hfo_filt_data_blocked(read: Reader, n: int, fs: float, ws: Workspace, block: int,
                          margin: int, bpfreq, bptype="fir", notch=False, notch_freq=60,
                          notch_type="fir", notch_bandwidth=4) -> tuple[np.ndarray, np.ndarray]:
    """Block version of filters.hfo_filt_data. Returns memory-mapped (filt, amp)."""
    b, a = _coefficients(fs, tuple(bpfreq), bptype, stop=False, n_samples=n)
    pad = 3 * (max(len(a), len(b)) - 1)
    mean = blocked_mean_reader(read, n, block)
    filt = filtfilt_streamed(read, n, b, a, mean, ws.array("bandpass", n + 2 * pad), block)

    if notch:
        # same notch-loop bug as HFOFiltData.m (see filters.hfo_filt_data)
        harmonics = np.arange(notch_freq, fs / 2 - notch_bandwidth + 1e-9, notch_freq)
        bs = (notch_freq - 0.5 * notch_bandwidth, notch_freq + 0.5 * notch_bandwidth)
        bn, an = _coefficients(fs, bs, notch_type, stop=True, n_samples=n)
        npad = 3 * (max(len(an), len(bn)) - 1)
        bufs = [ws.array("notch_a", n + 2 * npad), ws.array("notch_b", n + 2 * npad)]
        for k, _ in enumerate(harmonics):
            src = filt
            m = blocked_mean(src, block)
            filt = filtfilt_streamed(lambda s, e: np.asarray(src[s:e], dtype=np.float64), n,
                                     bn, an, m, bufs[k % 2], block)
            for s in range(0, n, block):  # band-stop adds the mean back
                filt[s:s + block] += m

    amp = envelope_blocked(filt, ws.array("envelope", n), block, margin)
    return filt, amp


def blocked_mean_reader(read: Reader, n: int, block: int) -> float:
    total = 0.0
    for s in range(0, n, block):
        total += float(read(s, min(s + block, n)).sum())
    return total / n
