"""Neuralynx continuous recordings (.ncs, one file per channel) + Persyst comments (.lay).

Input: a session folder, a .zip of one, or one .ncs file of it (= its folder). The
.ncs format is open: a 16 KB text header, then 1044-byte records (uint64 timestamp in
µs, uint32 channel, uint32 sampling rate, uint32 number of valid samples, 512 int16).

- Channels are grouped by the part of their name before the last space ("R1 CA1"
  -> group "R1", label "CA1"): one group per animal of a multi-animal session.
  Names without a space form one group.
- Blocks: a new block starts where the timestamps jump (pause in the recording).
  Records with fewer than 512 valid samples (e.g. the first one of a cut file) count
  only their valid samples. All channels of a group must have the same timeline.
- Samples are kept as stored (int16 counts x ADBitVolts), also when the input was
  inverted in hardware (InputInverted, noted in recording.json).
- Comments: the [Comments] section of the Persyst layout files (.lay) of the session,
  times in seconds from the start of the recording; they go to every group (they
  are not tied to a channel).
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import re
import zipfile
from pathlib import Path
from typing import BinaryIO, Callable

import numpy as np

from .recording import Block, Channel, Comment, Recording

log = logging.getLogger(__name__)

HEADER_BYTES = 16384
RECORD = np.dtype([("ts", "<u8"), ("ch", "<u4"), ("fs", "<u4"), ("n", "<u4"), ("s", "<i2", 512)])
META = np.dtype([("ts", "<u8"), ("ch", "<u4"), ("fs", "<u4"), ("n", "<u4")])
GAP_TOLERANCE_US = 2        # timestamps are integers in µs; anything beyond rounding is a pause


def parse_header(raw: bytes) -> dict[str, str]:
    text = raw[:HEADER_BYTES].split(b"\0", 1)[0].decode("latin-1")
    out = {}
    for line in text.splitlines():
        m = re.match(r"-(\S+)\s*(.*)$", line.strip())
        if m:
            v = m.group(2).strip()
            out[m.group(1)] = v[1:-1] if len(v) > 1 and v[0] == v[-1] == '"' else v
    return out


def parse_lay_comments(text: str) -> list[tuple[float, float, str]]:
    """(time s, duration s, text) from the [Comments] section of a Persyst .lay file."""
    out, section = [], ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].lower()
        elif section == "comments" and line:
            parts = line.split(",", 4)
            if len(parts) == 5:
                out.append((float(parts[0]), float(parts[1]), parts[4]))
    return out


class _Source:
    """The files of a session, from a folder or a zip."""

    def __init__(self, path: Path):
        self.path = path
        if path.is_file() and path.suffix.lower() == ".zip":
            self.zip = zipfile.ZipFile(path)
            names = [n for n in self.zip.namelist() if not n.endswith("/")]
            self.name = path.stem
        else:
            self.zip = None
            folder = path if path.is_dir() else path.parent
            names = [str(p) for p in sorted(folder.iterdir()) if p.is_file()]
            self.name = folder.name
        self.ncs = sorted(n for n in names if n.lower().endswith(".ncs"))
        self.lay = sorted(n for n in names if n.lower().endswith(".lay"))
        if not self.ncs:
            raise ValueError(f"{path.name}: no Neuralynx .ncs files found")

    def open(self, name: str) -> BinaryIO:
        return self.zip.open(name) if self.zip else open(name, "rb")

    def read_text(self, name: str) -> str:
        with self.open(name) as f:
            return f.read().decode("latin-1")

    def close(self) -> None:
        if self.zip:
            self.zip.close()


class _Channel:
    """One .ncs file: header, record timeline, and sample reading (forward or seek)."""

    def __init__(self, src: _Source, name: str):
        self.src, self.name = src, name
        with src.open(name) as f:
            self.header = parse_header(f.read(HEADER_BYTES))
            meta = []
            while chunk := f.read(RECORD.itemsize * 4096):
                n = len(chunk) // RECORD.itemsize
                meta.append(np.frombuffer(chunk[:n * RECORD.itemsize], RECORD)[list(META.names)].copy())
        self.meta = np.concatenate(meta) if meta else np.zeros(0, META)
        self._f, self._pos = None, None

    def records(self, r0: int, r1: int) -> np.ndarray:
        """Records [r0, r1) (seek on files; forward reading on zip members, reopened if needed)."""
        size = RECORD.itemsize
        if self._f is None or (self.src.zip and self._pos > r0):
            if self._f:
                self._f.close()
            self._f, self._pos = self.src.open(self.name), 0
            self._f.seek(HEADER_BYTES)
        if not self.src.zip:
            self._f.seek(HEADER_BYTES + r0 * size)
        elif r0 > self._pos:
            self._f.read((r0 - self._pos) * size)
        buf = self._f.read((r1 - r0) * size)
        self._pos = r1
        return np.frombuffer(buf, RECORD)

    def close(self) -> None:
        if self._f:
            self._f.close()


def _split(name: str) -> tuple[str, str]:
    return tuple(name.rsplit(" ", 1)) if " " in name.strip() else ("", name.strip())


def _block_bounds(meta: np.ndarray, fs: float) -> list[tuple[int, int]]:
    """Record ranges [r0, r1) without timestamp jumps."""
    expected = meta["ts"][:-1].astype(np.int64) + np.round(meta["n"][:-1] / fs * 1e6).astype(np.int64)
    jumps = np.nonzero(np.abs(meta["ts"][1:].astype(np.int64) - expected) > GAP_TOLERANCE_US)[0] + 1
    edges = [0, *jumps.tolist(), len(meta)]
    return list(zip(edges[:-1], edges[1:]))


def _reader(chans: list[_Channel], r0: int, r1: int) -> Callable[[int, int, int], np.ndarray]:
    """read(channel, s0, s1) over the valid samples of records [r0, r1)."""
    n = chans[0].meta["n"][r0:r1].astype(np.int64)
    first = np.concatenate([[0], np.cumsum(n)])          # first sample of each record in the block

    def read(i: int, s0: int, s1: int) -> np.ndarray:
        if s1 <= s0:
            return np.zeros(0, np.int16)
        a = int(np.searchsorted(first, s0, side="right")) - 1
        b = int(np.searchsorted(first, s1, side="left"))
        recs = chans[i].records(r0 + a, r0 + b)
        valid = np.concatenate([r["s"][:k] for r, k in zip(recs, n[a:b])]) if (n[a:b] != 512).any() \
            else recs["s"].reshape(-1)
        return valid[s0 - first[a]:s1 - first[a]]

    return read


def _session(h: dict) -> str:
    """Name of the acquisition folder (Cheetah writes one per recording), from the header."""
    parts = re.split(r"[\\/]", h.get("OriginalFileName", ""))
    return parts[-2] if len(parts) > 1 else ""


def _start_time(ts_us: int) -> dt.datetime:
    # Cheetah timestamps read as wall-clock time of the acquisition computer (to verify:
    # docs on the reference of Neuralynx timestamps vary between versions)
    return dt.datetime(1970, 1, 1) + dt.timedelta(microseconds=int(ts_us))


def open_neuralynx(path: str | Path) -> Recording:
    path = Path(path)
    src = _Source(path)
    chans: list[_Channel] = []
    try:
        chans = [_Channel(src, n) for n in src.ncs]
        comments = [c for lay in src.lay for c in parse_lay_comments(src.read_text(lay))]
        groups: dict[str, list[_Channel]] = {}
        for c in chans:
            name = c.header.get("AcqEntName") or Path(c.name).stem
            c.group, c.label = _split(name)
            groups.setdefault(c.group, []).append(c)

        blocks = []
        for group, members in groups.items():
            ref = members[0]
            fs = float(ref.header["SamplingFrequency"])
            where = f"{src.name}, group {group!r}" if group else src.name
            for c in members[1:]:
                if float(c.header["SamplingFrequency"]) != fs:
                    raise ValueError(f"{where}: channels at different sampling rates, not supported")
                if not (np.array_equal(c.meta["ts"], ref.meta["ts"]) and np.array_equal(c.meta["n"], ref.meta["n"])):
                    raise ValueError(f"{where}: {c.label} and {ref.label} have different record timelines")
            channels = []
            for c in members:
                h = c.header
                lsb = float(h["ADBitVolts"])
                hp = float(h["DspLowCutFrequency"]) if h.get("DSPLowCutFilterEnabled") == "True" else None
                lp = float(h["DspHighCutFrequency"]) if h.get("DSPHighCutFilterEnabled") == "True" else None
                channels.append(Channel(
                    label=c.label, lsb=lsb, unit="V",
                    range_counts=min(32767, round(float(h["InputRange"]) * 1e-6 / lsb)) if h.get("InputRange") else None,
                    highpass_hz=hp, lowpass_hz=lp, reference=h.get("ReferenceChannel"),
                    source={k: h.get(k) for k in ("AcqEntName", "ADChannel", "ReferenceChannel", "InputRange",
                                                   "InputInverted", "ADBitVolts", "DspFilterDelay_µs")}))
            ts0 = int(ref.meta["ts"][0])
            bounds = _block_bounds(ref.meta, fs)
            for k, (r0, r1) in enumerate(bounds, 1):
                n = int(ref.meta["n"][r0:r1].sum())
                t_start, t_end = int(ref.meta["ts"][r0]), int(ref.meta["ts"][r0]) + n / fs * 1e6
                block_comments = [Comment(sample=int(round((ts0 + t * 1e6 - t_start) / 1e6 * fs)), channel=None, text=text)
                                  for t, _d, text in comments if t_start <= ts0 + t * 1e6 < t_end]
                blocks.append(Block(
                    index=k, start=_start_time(t_start), srate=fs, n_samples=n, channels=channels,
                    comments=block_comments, read=_reader(members, r0, r1), group=group,
                    source={"Format": "Neuralynx .ncs", "Session": _session(ref.header), "FirstTimestamp_us": t_start,
                            "Records": r1 - r0, "PartialRecords": int((ref.meta["n"][r0:r1] != 512).sum()),
                            "Application": ref.header.get("ApplicationName"),
                            "AcquisitionSystem": ref.header.get("AcquisitionSystem"),
                            "CommentsFrom": [Path(l).name for l in src.lay],
                            "StartTimeNote": "first record timestamp read as local wall-clock time (to verify)"}))
            if len(bounds) > 1:
                log.warning("%s: %d blocks (pauses in the recording)", where, len(bounds))
    except Exception:
        for c in chans:
            c.close()
        src.close()
        raise

    def close():
        for c in chans:
            c.close()
        src.close()

    return Recording(path=path, format="neuralynx", blocks=blocks, close=close, name=src.name)
