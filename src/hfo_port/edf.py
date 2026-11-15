"""Streamed EDF reader, equivalent to FieldTrip's read_edf as used by HFOLoadData.

Channels are read one at a time through a memory map, so files larger than RAM
can be processed.

Matches read_edf.m:
- labels: trailing blanks removed (cellstr / deblank);
- start time: dd.mm.yy hh.mm.ss, two-digit year < 91 -> 20yy, else 19yy;
- samples scaled by Cal = (PhysMax - PhysMin) / (DigMax - DigMin) only.
  read_edf.m does NOT add the offset (PhysMin - Cal * DigMin); for symmetric
  ranges the offset is 0 anyway.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class EdfHeader:
    path: Path
    header_bytes: int
    n_records: int
    record_duration: float
    labels: list[str]
    samples_per_record: list[int]
    phys_min: np.ndarray
    phys_max: np.ndarray
    dig_min: np.ndarray
    dig_max: np.ndarray
    start: dt.datetime
    phys_dim: list[str] = field(default_factory=list)

    @property
    def n_channels(self) -> int:
        return len(self.labels)

    @property
    def calibration(self) -> np.ndarray:
        return (self.phys_max - self.phys_min) / (self.dig_max - self.dig_min)

    @property
    def srate(self) -> float:
        rates = {spr / self.record_duration for spr in self.samples_per_record}
        if len(rates) != 1:
            raise ValueError(f"channels with different sampling rates are not supported: {sorted(rates)}")
        return rates.pop()

    @property
    def n_samples(self) -> int:
        return self.n_records * self.samples_per_record[0]


def _fields(block: str, n: int, width: int) -> list[str]:
    return [block[i * width:(i + 1) * width] for i in range(n)]


def read_header(path: str | Path) -> EdfHeader:
    path = Path(path)
    with open(path, "rb") as f:
        fixed = f.read(256).decode("latin-1")
        ns = int(fixed[252:256])
        sig = f.read(ns * 256).decode("latin-1")

    pos = 0

    def take(width: int) -> list[str]:
        nonlocal pos
        out = _fields(sig[pos:pos + width * ns], ns, width)
        pos += width * ns
        return out

    labels = [s.rstrip() for s in take(16)]
    take(80)  # transducer
    phys_dim = [s.strip() for s in take(8)]
    phys_min = np.array([float(s) for s in take(8)])
    phys_max = np.array([float(s) for s in take(8)])
    dig_min = np.array([float(s) for s in take(8)])
    dig_max = np.array([float(s) for s in take(8)])
    take(80)  # prefiltering
    spr = [int(s) for s in take(8)]

    d, m, y = (int(x) for x in fixed[168:176].split("."))
    hh, mm, ss = (int(x) for x in fixed[176:184].split("."))
    y += 2000 if y < 91 else 1900

    n_records = int(fixed[236:244])
    if n_records < 0:
        raise ValueError("EDF with unknown number of records (-1) is not supported")

    return EdfHeader(
        path=path,
        header_bytes=int(fixed[184:192]),
        n_records=n_records,
        record_duration=float(fixed[244:252]),
        labels=labels,
        samples_per_record=spr,
        phys_min=phys_min,
        phys_max=phys_max,
        dig_min=dig_min,
        dig_max=dig_max,
        start=dt.datetime(y, m, d, hh, mm, ss),
        phys_dim=phys_dim,
    )


class EdfReader:
    """Read one channel at a time as float64 physical values."""

    def __init__(self, path: str | Path):
        self.header = read_header(path)
        h = self.header
        self._record_len = sum(h.samples_per_record)
        self._offsets = np.concatenate([[0], np.cumsum(h.samples_per_record)[:-1]])
        self._mm = np.memmap(h.path, dtype="<i2", mode="r", offset=h.header_bytes,
                             shape=(h.n_records, self._record_len))

    def read_channel(self, index: int) -> np.ndarray:
        h = self.header
        o = self._offsets[index]
        raw = self._mm[:, o:o + h.samples_per_record[index]]
        # one float64 allocation: (records x samples) block, then a flat view
        return np.multiply(raw, h.calibration[index], dtype=np.float64).reshape(-1)

    def read_range(self, index: int, start: int, stop: int) -> np.ndarray:
        """Samples [start, stop) of one channel; touches only the records involved."""
        h = self.header
        spr = h.samples_per_record[index]
        o = self._offsets[index]
        r0, r1 = start // spr, (stop - 1) // spr + 1
        raw = self._mm[r0:r1, o:o + spr]
        flat = np.multiply(raw, h.calibration[index], dtype=np.float64).reshape(-1)
        return flat[start - r0 * spr:stop - r0 * spr]

    def close(self) -> None:
        del self._mm

    def __enter__(self) -> "EdfReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
