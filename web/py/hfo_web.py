"""Browser adapter for hfo_port (runs inside Pyodide, in a Web Worker).

The validated package src/hfo_port is used as is. Browser-specific parts:
- WebEdfReader: np.memmap cannot map a browser File, so the same records are read with
  seek/read (same int16 samples, same float64 scaling -> same numbers);
- scratch storage for block processing: instead of memory-mapped temporary files, the
  full-length arrays go to the browser's private disk (OPFS) through sync access handles
  opened by worker.js, wrapped by hfo_port.scratch.ScratchArray (same float64 bytes);
- progress messages for the page;
- LabChart .adicht files: converted by the validated package src/eeg_convert, the EDF
  written to the browser's private disk (HandleFile), then analyzed like any EDF.

Entry points: inspect(), run() and the plan / run_channels / write_results trio, and
inspect_adicht() / convert_adicht() (worker.js); config_from_yaml() (test/check.mjs).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np
import yaml

from hfo_port import __version__, blocked, config, pipeline, writer
from hfo_port.edf import EdfReader, read_header

CHUNK_BYTES = 32 << 20  # raw bytes read at a time

# WebAssembly has at most 4 GB of memory: whole-channel processing peaked at ~3 GB for
# 17M samples per channel and failed at 42M. Above 10M samples per channel (~42 min at
# 4 kHz) block processing is used, with its scratch arrays on disk (OPFS).
PROCESSING = {"mode": "auto", "max_whole_samples": 10_000_000}

# Names of the scratch arrays blocked.py asks for (worker.js opens one OPFS file each).
SCRATCH_NAMES = ("bandpass", "notch_a", "notch_b", "envelope")

# Scratch bytes read + written per sample of a channel in block processing without notch,
# measured (web/test/check.mjs); used only for the progress bar.
IO_BYTES_PER_SAMPLE = 75


class _Progress:
    """Tracks the current channel and the scratch I/O done in it; posts to the page."""

    def __init__(self, post, labels, to_analyze, n):
        self.post, self.labels, self.n = post, labels, n
        self.order = {ch: k for k, ch in enumerate(to_analyze)}
        self.channel, self.io, self.last = None, 0, 0.0

    def on_read(self, index):
        if index != self.channel:
            self.channel, self.io = index, 0
            self.send(0.0, force=True)

    def on_io(self, nbytes):
        self.io += nbytes
        self.send(min(self.io / (IO_BYTES_PER_SAMPLE * self.n), 0.99))

    def send(self, fraction, force=False):
        now = time.monotonic()
        if force or now - self.last > 0.5:
            self.last = now
            k = self.order.get(self.channel, 0)
            self.post(json.dumps({"progress": {
                "channel": self.labels[self.channel], "k": k, "of": len(self.order),
                "fraction": fraction}}))


_progress: _Progress | None = None


class WebEdfReader(EdfReader):
    """EdfReader without memory map: records are read with seek/read."""

    def __init__(self, path):
        self.header = read_header(path)
        h = self.header
        self._record_len = sum(h.samples_per_record)
        self._offsets = np.concatenate([[0], np.cumsum(h.samples_per_record)[:-1]])
        self._f = open(h.path, "rb")

    def _records(self, r0: int, r1: int) -> np.ndarray:
        self._f.seek(self.header.header_bytes + r0 * self._record_len * 2)
        buf = self._f.read((r1 - r0) * self._record_len * 2)
        return np.frombuffer(buf, dtype="<i2").reshape(r1 - r0, self._record_len)

    def read_range(self, index: int, start: int, stop: int) -> np.ndarray:
        if _progress is not None:
            _progress.on_read(index)
        h = self.header
        spr = h.samples_per_record[index]
        o = self._offsets[index]
        r0, r1 = start // spr, (stop - 1) // spr + 1
        out = np.empty((r1 - r0) * spr, dtype=np.float64)
        step = max(1, CHUNK_BYTES // (self._record_len * 2))
        for r in range(r0, r1, step):
            e = min(r + step, r1)
            raw = self._records(r, e)[:, o:o + spr]
            np.multiply(raw, h.calibration[index], out=out[(r - r0) * spr:(e - r0) * spr]
                        .reshape(e - r, spr), dtype=np.float64)
        return out[start - r0 * spr:stop - r0 * spr]

    def read_channel(self, index: int) -> np.ndarray:
        return self.read_range(index, 0, self.header.n_samples)

    def close(self) -> None:
        self._f.close()


pipeline.EdfReader = WebEdfReader


class HandleStore:
    """Byte store (see hfo_port.scratch) on an OPFS FileSystemSyncAccessHandle, or any JS
    object with the same read/write/truncate/flush methods (the Node test uses files)."""

    def __init__(self, handle, nbytes: int):
        from js import Object  # Pyodide only
        from pyodide.ffi import to_js
        self.handle = handle
        self._at = lambda offset: to_js({"at": offset}, dict_converter=Object.fromEntries)
        handle.truncate(nbytes)

    def read_into(self, offset: int, out: memoryview) -> None:
        from js import Uint8Array
        buf = Uint8Array.new(len(out))
        if self.handle.read(buf, self._at(offset)) != len(out):
            raise OSError("short read from scratch storage")
        buf.assign_to(out)
        if _progress is not None:
            _progress.on_io(len(out))

    def write(self, offset: int, data: memoryview) -> None:
        from js import Uint8Array
        buf = Uint8Array.new(len(data))
        buf.assign(data)
        self.handle.write(buf, self._at(offset))
        if _progress is not None:
            _progress.on_io(len(data))

    def close(self) -> None:
        self.handle.truncate(0)  # frees the disk space; the handle stays open for reuse
        self.handle.flush()


class _Relay(logging.Handler):
    """Forward the pipeline's log lines (one per channel) to the page."""

    def __init__(self, post):
        super().__init__(logging.INFO)
        self.post = post

    def emit(self, record):
        self.post(json.dumps({"level": record.levelname, "msg": record.getMessage()}))


def inspect(path: str) -> str:
    h = read_header(path)
    try:
        srate = h.srate
    except ValueError as e:
        srate = None
        problem = str(e)
    else:
        problem = None
    return json.dumps({
        "labels": h.labels,
        "srate": srate,
        "n_samples": h.n_samples,
        "duration_s": h.n_records * h.record_duration,
        "start": h.start.isoformat(sep=" "),
        "problem": problem,
    })


def config_from_yaml(text: str) -> str:
    """Parse and validate a YAML config with hfo_port.config; return it as a dict."""
    cfg = config.from_dict(yaml.safe_load(text) or {}, source="<yaml>")
    return json.dumps(_form(cfg))


def _form(cfg: config.Config) -> dict:
    d = config.as_dict(cfg)
    d.pop("processing")
    d["header"] = cfg.header
    return d


def scratch_bytes(n_samples: int, notch: bool) -> int:
    """Disk space block processing needs for one channel (freed after each channel)."""
    return 8 * (n_samples * (4 if notch else 2) + 100_000)


# ---- parallel channels: the page splits the channels of one file among several workers.
# plan() lists them, each worker runs run_channels() on its share, and one worker merges
# the results with write_results(). Channels are independent, so the files are the same as
# with run() on the whole file (checked by test/check.mjs --split).

def _config(cfg_json: str, processing_json: str | None = None) -> config.Config:
    d = json.loads(cfg_json)
    d["processing"] = json.loads(processing_json) if processing_json else PROCESSING
    return config.from_dict(d, source="web app")


def plan(path: str, cfg_json: str) -> str:
    """Labels of the channels that will be analyzed, in EDF order."""
    cfg = _config(cfg_json)
    labels, to_analyze = pipeline._labels(read_header(path).labels, cfg.duplicate_labels)
    if cfg.channels != "all":
        missing = set(cfg.channels) - set(labels)
        if missing:
            raise ValueError(f"channels not found in EDF: {sorted(missing)}")
        to_analyze = [i for i in to_analyze if labels[i] in cfg.channels]
    return json.dumps([labels[i] for i in to_analyze])


def run_channels(path: str, cfg_json: str, labels_json: str, post, scratch=None,
                 processing_json: str | None = None) -> str:
    """Events of some channels, as JSON (exact floats; NaN allowed, so the page must pass
    the text back to write_results() without parsing it)."""
    d = json.loads(cfg_json)
    d["channels"] = json.loads(labels_json)
    res = _run(path, json.dumps(d), post, scratch, processing_json)
    return json.dumps([{
        "label": c.label, "location": c.events.location.tolist(),
        "no_cycles": c.events.no_cycles.tolist(), "peak_zscore": c.events.peak_zscore.tolist(),
        "avg_freq": c.events.avg_freq.tolist(), "bpfreq": list(c.events.bpfreq),
    } for c in res.channels])


def write_results(path: str, cfg_json: str, strict: bool, out_dir: str, parts: list,
                  processing_json: str | None = None) -> str:
    """Merge the run_channels() outputs of one file and write HFOApp's two files."""
    from hfo_port.detectors import Events
    cfg = _config(cfg_json, processing_json)
    h = read_header(path)
    labels, _ = pipeline._labels(h.labels, cfg.duplicate_labels)
    order = json.loads(plan(path, cfg_json))
    found = {c["label"]: c for part in parts for c in json.loads(part)}
    channels = [pipeline.ChannelResult(lab, Events(
        location=np.array(found[lab]["location"], dtype=np.int64).reshape(-1, 2),
        no_cycles=np.array(found[lab]["no_cycles"], dtype=np.float64),
        peak_zscore=np.array(found[lab]["peak_zscore"], dtype=np.float64),
        avg_freq=np.array(found[lab]["avg_freq"], dtype=np.float64),
        bpfreq=tuple(found[lab]["bpfreq"]))) for lab in order]
    return _write(pipeline.RunResult(header=h, labels=labels, channels=channels, config=cfg),
                  path, strict, out_dir)


def _write(res, path: str, strict: bool, out_dir: str) -> str:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    ev_path, sm_path = writer.output_paths(path, out_dir)
    writer.write(res, ev_path, sm_path, strict=strict)
    return json.dumps({
        "events_name": ev_path.name, "events": ev_path.read_text(encoding="utf-8"),
        "summary_name": sm_path.name, "summary": sm_path.read_text(encoding="utf-8"),
        "n_events": int(sum(len(c.events) for c in res.channels)),
        "n_channels": len(res.channels),
    })


def _run(path, cfg_json, post, scratch, processing_json):
    """pipeline.run with log relay, progress and scratch storage set up."""
    global _progress
    handler = _Relay(post)
    log = logging.getLogger("hfo_port")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    # JS null arrives as pyodide.ffi.jsnull, not None (no private disk: scratch in memory)
    if scratch is not None and type(scratch).__name__ != "JsNull":
        blocked.Workspace.store_factory = staticmethod(
            lambda name, nbytes: HandleStore(getattr(scratch, name), nbytes))
    try:
        cfg = _config(cfg_json, processing_json)
        h = read_header(path)
        labels, to_analyze = pipeline._labels(h.labels, cfg.duplicate_labels)
        if cfg.channels != "all":
            to_analyze = [i for i in to_analyze if labels[i] in cfg.channels]
        _progress = _Progress(post, labels, to_analyze, h.n_samples)
        return pipeline.run(path, cfg)
    finally:
        _progress = None
        blocked.Workspace.store_factory = None
        log.removeHandler(handler)


def run(path: str, cfg_json: str, strict: bool, out_dir: str, post, scratch=None,
        processing_json: str | None = None) -> str:
    """Detect events in one EDF; write HFOApp's two files into out_dir (in-memory FS).

    scratch: JS object {name: sync access handle} for SCRATCH_NAMES; without it, block
    processing keeps its scratch files in memory (Emscripten's in-memory file system).
    processing_json: overrides PROCESSING (tests).
    """
    return _write(_run(path, cfg_json, post, scratch, processing_json), path, strict, out_dir)


# ---- LabChart .adicht: convert first, then the EDF goes through the usual path.
# The file is read in pieces: mmap would copy it whole into WebAssembly memory (4 GB max).

import eeg_convert.adicht

eeg_convert.adicht.READ_IN_PIECES = True

class HandleFile:
    """Writable, seekable binary file on a sync access handle (eeg_convert.edf_write)."""

    def __init__(self, handle):
        from js import Object  # Pyodide only
        from pyodide.ffi import to_js
        self.handle, self.pos = handle, 0
        self._at = lambda offset: to_js({"at": offset}, dict_converter=Object.fromEntries)
        handle.truncate(0)

    def write(self, data) -> int:
        from js import Uint8Array
        data = memoryview(data).cast("B")
        buf = Uint8Array.new(len(data))
        buf.assign(data)
        if self.handle.write(buf, self._at(self.pos)) != len(data):
            raise OSError("short write to the browser's disk")
        self.pos += len(data)
        return len(data)

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence != 0:
            raise ValueError("only absolute seeks")
        self.pos = offset
        return offset

    def tell(self) -> int:
        return self.pos

    def close(self) -> None:
        self.handle.flush()

    def __enter__(self) -> "HandleFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def inspect_adicht(path: str) -> str:
    """Blocks of a .adicht file, from its metadata only (fast)."""
    from eeg_convert.convert import edf_stem, open_recording
    try:
        with open_recording(path) as rec:
            n = len(rec.blocks)
            blocks = [{"edf": f"{edf_stem(path, b.index, n)}.edf", "labels": [c.label for c in b.channels],
                       "srate": b.srate, "n_samples": b.n_samples, "duration_s": b.n_samples / b.srate,
                       "start": b.start.isoformat(sep=" ", timespec="seconds"),
                       "n_comments": len(b.comments)} for b in rec.blocks]
    except ValueError as e:
        return json.dumps({"blocks": [], "problem": str(e)})
    return json.dumps({"blocks": blocks, "problem": None if blocks else "no blocks"})


def convert_adicht(path: str, handles, out_dir: str, post) -> str:
    """Convert a .adicht; EDF files to handles (JS object {edf name: sync access handle}),
    sidecars to out_dir (in-memory FS), returned as text with the EDF names."""
    from eeg_convert.convert import convert
    handler = _Relay(post)
    log = logging.getLogger("eeg_convert")
    log.addHandler(handler)
    last = [0.0]

    def progress(block, fraction):
        now = time.monotonic()
        if now - last[0] > 0.5 or fraction >= 1:
            last[0] = now
            post(json.dumps({"convert": {"block": block, "fraction": fraction}}))

    try:
        outputs = convert(path, out_dir, edf_opener=lambda p: HandleFile(getattr(handles, p.name)),
                          progress=progress)
    finally:
        log.removeHandler(handler)
    sidecars = lambda o: [o.events, o.events.with_suffix(".json"), o.channels, o.recording]
    return json.dumps([{
        "edf": o.edf.name, "n_comments": o.n_comments, "n_samples": o.info.n_records * o.info.record_samples,
        "sidecars": {p.name: p.read_text(encoding="utf-8") for p in sidecars(o)},
    } for o in outputs])


VERSION = __version__
