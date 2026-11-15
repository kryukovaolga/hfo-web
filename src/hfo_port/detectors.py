"""HFO detectors (core layer: arrays in, events out, no I/O).

Implemented: Hilbert detector, in both variants exposed by HFOApp's Auto Detectors dialog
(gui-src/ConfigAutoDetector.m, Functions/HFOAutoDetectorHil.m):
- 'hfoapp_hilbert'    : avg_method 'mov', onset + inclusion thresholds + number of cycles;
- 'ripplelab_hilbert' : avg_method 'epoch', one SD threshold + minimum duration.
STE, SLL and MNI are not ported yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .blocked import Workspace, hfo_filt_data_blocked
from .filters import hfo_filt_data
from .matlab import (blocked_mean, buffer_nodelay, findpeaks_locs, mean_std, moving_zscore,
                     moving_zscore_stream)


@dataclass
class Events:
    """Events of one channel. Locations are 1-based inclusive sample indices, as in HFOApp."""
    location: np.ndarray = field(default_factory=lambda: np.empty((0, 2), dtype=np.int64))
    no_cycles: np.ndarray = field(default_factory=lambda: np.empty(0))
    peak_zscore: np.ndarray = field(default_factory=lambda: np.empty(0))
    avg_freq: np.ndarray = field(default_factory=lambda: np.empty(0))
    bpfreq: tuple[float, float] = (0.0, 0.0)

    def __len__(self) -> int:
        return len(self.location)


def estimate_nb_cycles(v: np.ndarray, srate: float) -> tuple[float, float]:
    """EstimateNbcycles.m: number of cycles and average frequency from positive peaks."""
    if v.size < 3 or not np.any(v > 0):
        return 0.0, 0.0
    locs = findpeaks_locs(v, min_height=0.0)
    if locs.size > 1:
        cycle_dur = np.mean(np.diff(locs))
        return v.size / cycle_dur, srate / cycle_dur
    return 0.0, 0.0


def _clusters(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """0-based inclusive onset/offset of runs of True."""
    ind = np.flatnonzero(mask)
    if ind.size == 0:
        return ind, ind
    breaks = np.flatnonzero(np.diff(ind) > 1)
    on = ind[np.concatenate([[0], breaks + 1])]
    off = ind[np.concatenate([breaks, [ind.size - 1]])]
    return on, off


def find_clusters(get, epochs, thresh: float, chunk: int):
    """Runs of z >= thresh inside each epoch, reading z block by block with get(start, stop).

    Returns (on, off, peak): 0-based inclusive global indices and the maximum z of each run.
    Runs never cross an epoch boundary (as in HFOAutoDetectorHil.m) but may cross block
    boundaries, so the result does not depend on the block size.
    """
    ons, offs, peaks = [], [], []
    for es, ee in epochs:
        open_on, open_peak = None, -np.inf
        for s in range(es, ee, chunk):
            e = min(s + chunk, ee)
            z = np.asarray(get(s, e))
            on, off = _clusters(z >= thresh)
            last = e - s - 1
            for k in range(on.size):
                pk = float(z[on[k]:off[k] + 1].max())
                if k == 0 and open_on is not None:
                    if on[0] == 0:  # continues the run left open by the previous block
                        pk = max(pk, open_peak)
                        if off[0] == last:
                            open_peak = pk
                            continue
                        ons.append(open_on); offs.append(s + off[0]); peaks.append(pk)
                        open_on = None
                        continue
                    ons.append(open_on); offs.append(s - 1); peaks.append(open_peak)
                    open_on = None
                if off[k] == last:
                    open_on, open_peak = s + on[k], pk
                else:
                    ons.append(s + on[k]); offs.append(s + off[k]); peaks.append(pk)
            if on.size == 0 and open_on is not None:
                ons.append(open_on); offs.append(s - 1); peaks.append(open_peak)
                open_on = None
        if open_on is not None:
            ons.append(open_on); offs.append(ee - 1); peaks.append(open_peak)
    return (np.array(ons, dtype=np.int64), np.array(offs, dtype=np.int64), np.array(peaks))


def _select_events(filt, srate, bpfreq, on, off, peakz, peak_thresh, nocycle_thresh,
                   dur_thresh) -> Events:
    keep = peakz >= peak_thresh
    if dur_thresh is not None:
        keep &= (off - on) / srate >= dur_thresh
    loc_rows, cyc, peaks, freqs = [], [], [], []
    for k in np.flatnonzero(keep):
        s, e = int(on[k]), int(off[k])  # 0-based inclusive
        nb, f = estimate_nb_cycles(np.asarray(filt[s:e + 1], dtype=np.float64), srate)
        if nocycle_thresh is not None and nb < nocycle_thresh:
            continue
        loc_rows.append((s + 1, e + 1))
        cyc.append(nb)
        peaks.append(peakz[k])
        freqs.append(f)
    return Events(
        location=np.array(loc_rows, dtype=np.int64).reshape(-1, 2),
        no_cycles=np.array(cyc),
        peak_zscore=np.array(peaks),
        avg_freq=np.array(freqs),
        bpfreq=(float(bpfreq[0]), float(bpfreq[1])),
    )


def _epochs(n: int, srate: float, epoch: float | None) -> tuple[int, list[tuple[int, int]]]:
    if epoch is None:
        return n, [(0, n)]
    epoch_len = int(round(epoch * srate))
    return epoch_len, buffer_nodelay(n, epoch_len)


def _zscore_getter(amp, epochs, epoch_len, avg_method, chunk, stream=False, shift=None):
    """Return get(start, stop) giving z for any range inside one epoch.

    stream=True (block processing): z is computed while reading amp, ranges in increasing
    order, nothing of signal length stored; same values as the stored version."""
    if avg_method == "mov":
        if stream:
            return moving_zscore_stream(amp, epoch_len, shift=shift, chunk=chunk)
        zz = moving_zscore(amp, epoch_len, shift=shift, chunk=chunk)
        return lambda s, e: zz[s:e]
    if avg_method != "epoch":
        raise ValueError(f"unknown avg_method {avg_method!r}")
    stats = {}
    for es, ee in epochs:  # MATLAB zscore per epoch
        m, sd = mean_std(amp[es:ee], chunk)
        stats[es] = (m, sd if sd != 0 else 1.0)
    starts = np.array([es for es, _ in epochs])

    def get(s, e):
        m, sd = stats[int(starts[np.searchsorted(starts, s, side="right") - 1])]
        return (np.asarray(amp[s:e], dtype=np.float64) - m) / sd
    return get


def detect_hilbert(v: np.ndarray, srate: float, *, bpfreq, bptype="fir", notch=False,
                   notch_freq=60, notch_type="fir", notch_bandwidth=4,
                   epoch: float | None, z_thresh: float, peak_thresh: float,
                   nocycle_thresh: float | None = None, dur_thresh: float | None = None,
                   avg_method: str = "mov") -> Events:
    """HFOAutoDetectorHil.m, whole signal in memory. epoch in seconds (None = whole signal),
    dur_thresh in seconds."""
    n = v.size
    epoch_len, epochs = _epochs(n, srate, epoch)
    filt, amp = hfo_filt_data(v, srate, tuple(bpfreq), bptype, notch, notch_freq, notch_type,
                              notch_bandwidth)
    del v  # raw signal no longer needed; frees memory when the caller passed it directly
    get = _zscore_getter(amp, epochs, epoch_len, avg_method, chunk=max(n, 1))
    on, off, peakz = find_clusters(get, epochs, z_thresh, chunk=max(n, 1))
    return _select_events(filt, srate, bpfreq, on, off, peakz, peak_thresh, nocycle_thresh,
                          dur_thresh)


def detect_hilbert_blocked(read, n: int, srate: float, *, block: int, margin: int,
                           tmp_dir=None, bpfreq, bptype="fir", notch=False, notch_freq=60,
                           notch_type="fir", notch_bandwidth=4, epoch: float | None,
                           z_thresh: float, peak_thresh: float,
                           nocycle_thresh: float | None = None, dur_thresh: float | None = None,
                           avg_method: str = "mov") -> Events:
    """Same as detect_hilbert, for signals that do not fit in memory.

    read(start, stop) returns the raw samples of the channel; the full-length filtered
    signal and envelope are memory-mapped scratch files in tmp_dir (removed afterwards);
    the z-score is computed while reading the envelope (moving_zscore_stream).
    """
    epoch_len, epochs = _epochs(n, srate, epoch)
    with Workspace(tmp_dir) as ws:
        filt, amp = hfo_filt_data_blocked(read, n, srate, ws, block, margin, bpfreq, bptype,
                                          notch, notch_freq, notch_type, notch_bandwidth)
        get = _zscore_getter(amp, epochs, epoch_len, avg_method, block, stream=True,
                             shift=blocked_mean(amp, block))
        on, off, peakz = find_clusters(get, epochs, z_thresh, block)
        return _select_events(filt, srate, bpfreq, on, off, peakz, peak_thresh,
                              nocycle_thresh, dur_thresh)
