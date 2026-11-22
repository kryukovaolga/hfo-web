"""Write one block as a plain EDF file, losslessly.

- Digital values are the stored integer counts, unchanged.
- Scale: physical (mV) = digital x lsb. EDF writes the scale as 8-character
  numbers, so the digital range is chosen to make it exact: the nominal range
  (LabChart's own export: +-32000 counts = +-10 mV) when every sample fits, else
  the smallest symmetric range that holds the data and has an exact 8-character
  physical value; only if none exists (a channel that hits -32768) the full int16
  range, with the physical limits rounded (relative scale error ~1e-7, reported).
- Data record length: the largest duration <= 1 s that divides the block exactly,
  so no padding; otherwise 1 s and the last record padded with zeros (reported).
- The header is rewritten at the end, once the range of every channel is known.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Optional

import numpy as np

from .recording import Block

UNIT = "mV"
TO_UNIT = {"V": 1e3}
CHUNK_SECONDS = 60


@dataclass
class ChannelScale:
    label: str
    dig_min: int
    dig_max: int
    phys_min: str
    phys_max: str
    exact: bool
    data_min: int
    data_max: int
    lsb: float                      # exact physical value of one count, in UNIT

    @property
    def relative_error(self) -> float:
        cal = (float(self.phys_max) - float(self.phys_min)) / (self.dig_max - self.dig_min)
        return abs(cal / self.lsb - 1)


@dataclass
class EdfInfo:
    path: Path
    record_samples: int
    record_duration: str
    n_records: int
    padding: int                    # zero samples added at the end of every channel
    scales: list[ChannelScale]


def num8(x: float) -> str:
    """Shortest text of at most 8 characters for x (EDF numeric field)."""
    for p in range(8, 0, -1):
        s = f"{x:.{p}g}"
        if len(s) <= 8:
            return s
    raise ValueError(f"{x} does not fit in 8 characters")


def _exact(s: str, x: float) -> bool:
    return math.isclose(float(s), x, rel_tol=1e-12, abs_tol=0)


def record_length(n: int, srate: float) -> tuple[int, str]:
    """(samples per record, duration text): largest exact divisor of n up to 1 s, else 1 s."""
    top = int(srate)
    for d in range(top, max(top // 4, 1) - 1, -1):
        if n % d:
            continue
        s = num8(d / srate)
        if d / float(s) == srate:
            return d, s
    return top, num8(top / srate)


def choose_scale(label: str, lsb_unit: float, nominal: int | None, lo: int, hi: int) -> ChannelScale:
    need = max(abs(lo), abs(hi))
    candidates = ([nominal] if nominal and need <= nominal <= 32767 else []) + list(range(max(need, 1), 32768))
    for d in candidates:
        x = d * lsb_unit
        s = num8(x)
        if _exact(s, x):
            return ChannelScale(label, -d, d, num8(-x), s, True, lo, hi, lsb_unit)
    return ChannelScale(label, -32768, 32767, num8(-32768 * lsb_unit), num8(32767 * lsb_unit), False,
                        lo, hi, lsb_unit)


def _field(value, width: int) -> bytes:
    s = str(value)
    if len(s) > width:
        raise ValueError(f"EDF header field too long ({width}): {s!r}")
    return s.ljust(width).encode("ascii")


def _header(block: Block, recording_id: str, rec_samples: int, rec_dur: str, n_records: int,
            scales: list[ChannelScale]) -> bytes:
    ns = len(block.channels)
    t = block.start
    out = [_field("0", 8), _field("X", 80), _field(recording_id[:80], 80),
           _field(t.strftime("%d.%m.%y"), 8), _field(t.strftime("%H.%M.%S"), 8),
           _field(256 * (ns + 1), 8), _field("", 44), _field(n_records, 8), _field(rec_dur, 8),
           _field(ns, 4)]
    chans = block.channels
    for c in chans:
        if len(c.label) > 16:
            raise ValueError(f"channel label longer than 16 characters: {c.label!r}")
    out += [_field(c.label, 16) for c in chans]
    out += [_field("", 80) for _ in chans]
    out += [_field(UNIT, 8) for _ in chans]
    out += [_field(s.phys_min, 8) for s in scales]
    out += [_field(s.phys_max, 8) for s in scales]
    out += [_field(s.dig_min, 8) for s in scales]
    out += [_field(s.dig_max, 8) for s in scales]
    out += [_field(_prefilter(c), 80) for c in chans]
    out += [_field(rec_samples, 8) for _ in chans]
    out += [_field("", 32) for _ in chans]
    return b"".join(out)


def _prefilter(c) -> str:
    parts = []
    if c.highpass_hz is not None:
        parts.append(f"HP:{c.highpass_hz:g}Hz")
    if c.lowpass_hz is not None:
        parts.append(f"LP:{c.lowpass_hz:g}Hz")
    return " ".join(parts)


def write_block(block: Block, path: str | Path, recording_id: str = "",
                fileobj: Optional[BinaryIO] = None,
                progress: Optional[Callable[[float], None]] = None) -> EdfInfo:
    """Write `block` to `path`, or to `fileobj` (seekable; closed at the end) when given:
    the browser passes a file on its private disk. progress(fraction) after each chunk."""
    path = Path(path)
    chans = block.channels
    ns, n = len(chans), block.n_samples
    rec_samples, rec_dur = record_length(n, block.srate)
    n_records = -(-n // rec_samples)
    lo = np.full(ns, np.iinfo(np.int64).max)
    hi = np.full(ns, np.iinfo(np.int64).min)
    placeholder = [ChannelScale(c.label, -32768, 32767, "0", "0", True, 0, 0, 0.0) for c in chans]
    head = _header(block, recording_id, rec_samples, rec_dur, n_records, placeholder)

    per_chunk = max(1, int(CHUNK_SECONDS * block.srate) // rec_samples)
    with fileobj if fileobj is not None else open(path, "wb") as f:
        f.write(head)
        for r0 in range(0, n_records, per_chunk):
            r1 = min(r0 + per_chunk, n_records)
            s0, s1 = r0 * rec_samples, min(r1 * rec_samples, n)
            buf = np.zeros((r1 - r0, ns, rec_samples), dtype="<i2")
            for i in range(ns):
                x = np.asarray(block.read(i, s0, s1))
                if x.size != s1 - s0:
                    raise ValueError(f"{chans[i].label}: read {x.size} samples, expected {s1 - s0}")
                lo[i] = min(lo[i], int(x.min()))
                hi[i] = max(hi[i], int(x.max()))
                if lo[i] < -32768 or hi[i] > 32767:
                    raise ValueError(f"{chans[i].label}: counts outside int16, not supported")
                padded = np.zeros((r1 - r0) * rec_samples, dtype="<i2")
                padded[:x.size] = x
                buf[:, i, :] = padded.reshape(r1 - r0, rec_samples)
            f.write(buf.tobytes())
            if progress:
                progress(r1 / n_records)

        scales = [choose_scale(c.label, c.lsb * TO_UNIT[c.unit], c.range_counts, int(lo[i]), int(hi[i]))
                  for i, c in enumerate(chans)]
        f.seek(0)
        f.write(_header(block, recording_id, rec_samples, rec_dur, n_records, scales))

    return EdfInfo(path=path, record_samples=rec_samples, record_duration=rec_dur, n_records=n_records,
                   padding=n_records * rec_samples - n, scales=scales)
