"""Small re-implementations of MATLAB functions whose exact semantics matter."""

from __future__ import annotations

import math

import numpy as np


def findpeaks_locs(y: np.ndarray, min_height: float | None = None) -> np.ndarray:
    """0-based locations returned by MATLAB findpeaks(y, 'MinPeakHeight', h).

    MATLAB rules: endpoints are never peaks; on a plateau the first sample is
    the peak; MinPeakHeight keeps peaks strictly greater than h.
    (MinPeakDistance below 1 sample, as used by HFOApp, has no effect.)
    """
    y = np.asarray(y, dtype=np.float64)
    if y.size < 3:
        return np.empty(0, dtype=np.int64)
    # keep the first of each run of equal values
    keep = np.concatenate([[True], y[1:] != y[:-1]])
    idx = np.flatnonzero(keep)
    v = y[idx]
    s = np.sign(np.diff(v))
    peaks = idx[1:-1][np.diff(s) < 0]
    if min_height is not None:
        peaks = peaks[y[peaks] > min_height]
    return peaks


def movmean_movstd(x: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """MATLAB movmean(x, k) and movstd(x, k) (default 'shrink' endpoints, N-1 normalization).

    Odd k: (k-1)/2 samples on each side. Even k: k/2 before, k/2 - 1 after.
    """
    x = np.asarray(x, dtype=np.float64)
    mu = np.empty_like(x)
    sd = np.empty_like(x)
    for sl, m, s in _moving_stats(x, k):
        mu[sl] = m
        sd[sl] = s
    return mu, sd


def moving_zscore(x, k: int, *, out=None, cumsums=None, shift: float | None = None,
                  chunk: int = 1 << 20) -> np.ndarray:
    """(x - movmean(x, k)) ./ movstd(x, k), block by block.

    x, out and cumsums (two arrays of len(x) + 1) may be memory-mapped files, so that
    nothing of signal length has to live in RAM. shift defaults to x.mean().
    """
    out = np.empty(len(x)) if out is None else out
    with np.errstate(invalid="ignore", divide="ignore"):
        for sl, m, s in _moving_stats(x, k, chunk, cumsums, shift):
            out[sl] = (np.asarray(x[sl], dtype=np.float64) - m) / s
    return out


def _moving_stats(x, k: int, chunk: int = 1 << 20, cumsums=None, shift: float | None = None):
    """Yield (slice, movmean, movstd) block by block.

    Cumulative sums of (x - shift) and (x - shift)^2 are built sequentially, block by block,
    with the running total carried over: the additions happen in the same order as a single
    np.cumsum, so results do not depend on the block size.
    """
    n = len(x)
    before = k // 2 if k % 2 == 0 else (k - 1) // 2
    after = k // 2 - 1 if k % 2 == 0 else (k - 1) // 2
    if shift is None:
        shift = float(np.asarray(x).mean())  # reduces cancellation in the sum-of-squares formula
    c1, c2 = cumsums if cumsums is not None else (np.empty(n + 1), np.empty(n + 1))
    c1[0] = c2[0] = 0.0
    for start in range(0, n, chunk):
        stop = min(start + chunk, n)
        t = np.asarray(x[start:stop], dtype=np.float64) - shift
        c1[start + 1:stop + 1] = np.cumsum(np.concatenate(([c1[start]], t)))[1:]
        np.multiply(t, t, out=t)
        c2[start + 1:stop + 1] = np.cumsum(np.concatenate(([c2[start]], t)))[1:]
    for start in range(0, n, chunk):
        i = np.arange(start, min(start + chunk, n))
        lo = np.maximum(i - before, 0)
        hi = np.minimum(i + after, n - 1) + 1
        cnt = (hi - lo).astype(np.float64)
        s1 = c1[hi] - c1[lo]
        s2 = c2[hi] - c2[lo]
        mean = s1 / cnt
        with np.errstate(invalid="ignore", divide="ignore"):
            var = (s2 - s1 * s1 / cnt) / (cnt - 1)
        var = np.where(cnt > 1, np.maximum(var, 0.0), 0.0)
        yield slice(i[0], i[-1] + 1), mean + shift, np.sqrt(var)


class _CumCursor:
    """Values c1[j], c2[j] of the cumulative sums used by _moving_stats, produced on demand
    for non-decreasing j by reading x forward; nothing of signal length is stored.

    Each value is built by the same sequential additions as the stored version (c[j + 1] =
    c[j] + t[j], with t = x - shift, and t * t for c2), so the values are identical.
    """

    def __init__(self, x, shift: float, chunk: int):
        self.x, self.shift, self.chunk, self.n = x, shift, chunk, len(x)
        self.base = 0                                    # j of c1[0] below
        self.c1 = np.zeros(1)
        self.c2 = np.zeros(1)

    def at(self, j: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        lo, hi = int(j.min()), int(j.max())
        if lo < self.base:
            raise ValueError("cumulative sums must be requested in increasing order")
        # drop values no longer needed, but keep the last one: the next sums start from it
        drop = min(lo - self.base, len(self.c1) - 1)
        self.c1, self.c2, self.base = self.c1[drop:], self.c2[drop:], self.base + drop
        parts1, parts2 = [self.c1], [self.c2]
        last = self.base + len(self.c1) - 1              # largest j available
        while last < hi:
            stop = min(last + max(self.chunk, hi - last), self.n)
            t = np.asarray(self.x[last:stop], dtype=np.float64) - self.shift
            parts1.append(np.cumsum(np.concatenate(([parts1[-1][-1]], t)))[1:])
            np.multiply(t, t, out=t)
            parts2.append(np.cumsum(np.concatenate(([parts2[-1][-1]], t)))[1:])
            last = stop
        if len(parts1) > 1:
            self.c1, self.c2 = np.concatenate(parts1), np.concatenate(parts2)
        return self.c1[j - self.base], self.c2[j - self.base]


def moving_zscore_stream(x, k: int, *, shift: float | None = None, chunk: int = 1 << 20):
    """get(start, stop) -> moving_zscore(x, k)[start:stop], for ranges requested in
    increasing order. Same operations, element by element, as moving_zscore, but the
    cumulative sums are produced while reading x instead of being stored (block processing
    of long recordings: no scratch arrays of signal length for z and the sums)."""
    n = len(x)
    before = k // 2 if k % 2 == 0 else (k - 1) // 2
    after = k // 2 - 1 if k % 2 == 0 else (k - 1) // 2
    if shift is None:
        shift = blocked_mean(x, chunk)
    lo_sums, hi_sums = _CumCursor(x, shift, chunk), _CumCursor(x, shift, chunk)

    def get(start: int, stop: int) -> np.ndarray:
        i = np.arange(start, stop)
        lo = np.maximum(i - before, 0)
        hi = np.minimum(i + after, n - 1) + 1
        cnt = (hi - lo).astype(np.float64)
        h1, h2 = hi_sums.at(hi)
        l1, l2 = lo_sums.at(lo)
        s1 = h1 - l1
        s2 = h2 - l2
        mean = s1 / cnt
        with np.errstate(invalid="ignore", divide="ignore"):
            var = (s2 - s1 * s1 / cnt) / (cnt - 1)
            var = np.where(cnt > 1, np.maximum(var, 0.0), 0.0)
            return (np.asarray(x[start:stop], dtype=np.float64) - (mean + shift)) / np.sqrt(var)
    return get


def blocked_mean(x, chunk: int = 1 << 22) -> float:
    """Mean of an array-like (possibly memory-mapped) read block by block."""
    n = len(x)
    total = 0.0
    for s in range(0, n, chunk):
        total += float(np.asarray(x[s:s + chunk], dtype=np.float64).sum())
    return total / n


def mean_std(x, chunk: int = 1 << 22) -> tuple[float, float]:
    """Mean and SD (N-1) like MATLAB zscore uses. Exact numpy formulas when x fits in one
    block, two block-wise passes otherwise."""
    n = len(x)
    if n <= chunk:
        a = np.asarray(x, dtype=np.float64)
        return float(a.mean()), float(a.std(ddof=1)) if n > 1 else 0.0
    m = blocked_mean(x, chunk)
    ss = 0.0
    for s in range(0, n, chunk):
        d = np.asarray(x[s:s + chunk], dtype=np.float64) - m
        ss += float(np.dot(d, d))
    return m, (ss / (n - 1)) ** 0.5


def zscore(x: np.ndarray) -> np.ndarray:
    """MATLAB zscore: N-1 normalization; a zero SD (e.g. a single sample) is replaced by 1."""
    x = np.asarray(x, dtype=np.float64)
    m, sd = mean_std(x, chunk=max(x.size, 1))
    return (x - m) / (sd if sd != 0 else 1.0)


def buffer_nodelay(n: int, length: int) -> list[tuple[int, int]]:
    """Epochs of buffer(1:n, length, 0, 'nodelay') as 0-based [start, stop) ranges.

    The zero padding of the last column is dropped, as HFOAutoDetectorHil does
    with epoch_idx(epoch_idx(:, x) > 0, x).
    """
    return [(s, min(s + length, n)) for s in range(0, n, length)]


def num2str(x: float) -> str:
    """MATLAB num2str for a real scalar."""
    if float(x).is_integer():
        return str(int(x))
    digits = max(int(math.floor(math.log10(abs(x)))) + 5, 5) if x != 0 else 5
    return f"{x:.{min(digits, 16)}g}"


def num2str_int_row(values) -> str:
    """MATLAB num2str for a row of integers, e.g. [302076 302164] -> '302076  302164'."""
    values = [int(v) for v in values]
    width = len(str(max(abs(v) for v in values))) + (1 if any(v < 0 for v in values) else 0) + 2
    return "".join(f"{v:{width}d}" for v in values).strip()
