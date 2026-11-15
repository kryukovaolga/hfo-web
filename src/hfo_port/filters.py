"""Band-pass / notch filtering and envelope, equivalent to HFOFiltData.m.

HFOFiltData calls the FieldTrip functions bundled with HFOApp (External/fieldtrip):
- ft_preproc_bandpassfilter: remove the mean, filter, mean NOT added back;
- ft_preproc_bandstopfilter: remove the mean, filter, mean added back;
- 'fir' : order N = 3 * fix(fs / f_low) (band-stop: rounded up to even),
          coefficients from MATLAB fir1 (Hamming window, scaled);
- 'but' : Butterworth order 4;
- direction 'twopass' -> FieldTrip's own filtfilt.m (on the path via addpath(genpath)):
  odd reflection of 3 * (n - 1) samples, steady-state initial conditions.
  This equals scipy.signal.filtfilt with padtype='odd' and padlen = 3 * (n - 1).
"""

from __future__ import annotations

import numpy as np
from scipy import fft as sp_fft
from scipy import signal


def fir1(order: int, wn: tuple[float, float], stop: bool = False) -> np.ndarray:
    """MATLAB fir1(order, wn[, 'stop']) with the default Hamming window and scaling."""
    return signal.firwin(order + 1, list(wn), pass_zero=stop, window="hamming", scale=True)


def _filtfilt(b: np.ndarray, a: np.ndarray, x: np.ndarray) -> np.ndarray:
    """scipy.signal.filtfilt(b, a, x, padtype='odd', padlen=3*(n-1)), same operations in the
    same order (identical results), but intermediate arrays are released as soon as possible:
    peak memory ~3 signal copies instead of ~6, which matters for multi-GB recordings."""
    pad = 3 * (max(len(a), len(b)) - 1)
    if x.size <= pad:
        raise ValueError(f"signal too short ({x.size} samples) for filter padding {pad}")
    zi = signal.lfilter_zi(b, a)
    ext = np.concatenate([2 * x[:1] - x[pad:0:-1], x, 2 * x[-1:] - x[-2:-(pad + 2):-1]])
    y, _ = signal.lfilter(b, a, ext, zi=zi * ext[0])
    del ext
    y, _ = signal.lfilter(b, a, y[::-1], zi=zi * y[-1])
    return y[::-1][pad:-pad]


def envelope(x: np.ndarray) -> np.ndarray:
    """abs(hilbert(x)) like MATLAB/FieldTrip hilbert.m (= scipy.signal.hilbert), computed in place."""
    n = x.size
    spec = sp_fft.fft(x)
    half = n // 2 if n % 2 == 0 else (n + 1) // 2
    spec[1:half] *= 2
    spec[n // 2 + 1 if n % 2 == 0 else half:] = 0
    return np.abs(sp_fft.ifft(spec, overwrite_x=True))


def _coefficients(fs: float, band: tuple[float, float], ftype: str, stop: bool, n_samples: int):
    fn = fs / 2
    wn = (min(band) / fn, max(band) / fn)
    if ftype == "fir":
        order = 3 * int(fs / band[0])  # fix() truncates towards zero
        if stop and order % 2 == 1:
            order += 1
        limit = (n_samples - 1) // 3
        if order > limit:
            order = n_samples // 3 - (2 if stop else 1)
            if stop and order % 2 == 1:
                order += 1
        return fir1(order, wn, stop=stop), np.array([1.0])
    if ftype == "iir":
        return signal.butter(4, wn, btype="bandstop" if stop else "bandpass")
    raise ValueError(f"unknown filter type {ftype!r} (expected 'fir' or 'iir')")


def bandpass(x: np.ndarray, fs: float, band: tuple[float, float], ftype: str = "fir") -> np.ndarray:
    b, a = _coefficients(fs, band, ftype, stop=False, n_samples=len(x))
    return _filtfilt(b, a, x - x.mean())


def bandstop(x: np.ndarray, fs: float, band: tuple[float, float], ftype: str = "fir") -> np.ndarray:
    b, a = _coefficients(fs, band, ftype, stop=True, n_samples=len(x))
    m = x.mean()
    return _filtfilt(b, a, x - m) + m


def hfo_filt_data(x: np.ndarray, fs: float, bpfreq: tuple[float, float], bptype: str = "fir",
                  notch: bool = False, notch_freq: float = 60, notch_type: str = "fir",
                  notch_bandwidth: float = 4) -> tuple[np.ndarray, np.ndarray]:
    """Return (filtered signal, Hilbert envelope), like [filt_vec, amp] = HFOFiltData(...)."""
    filt = bandpass(np.asarray(x, dtype=np.float64), fs, bpfreq, bptype)
    if notch:
        # Faithful to HFOFiltData.m: the loop runs once per harmonic but always
        # removes the base frequency (f(k) is never used): an HFOApp bug, kept on purpose.
        harmonics = np.arange(notch_freq, fs / 2 - notch_bandwidth + 1e-9, notch_freq)
        bs = (notch_freq - 0.5 * notch_bandwidth, notch_freq + 0.5 * notch_bandwidth)
        for _ in harmonics:
            filt = bandstop(filt, fs, bs, notch_type)
    amp = envelope(filt)
    return filt, amp
