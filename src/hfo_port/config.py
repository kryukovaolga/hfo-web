"""YAML configuration. Missing keys take HFOApp's defaults (Auto Detectors dialog)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

EVENT_TYPES = ("gamma", "ripple", "fastripple", "ultrafast", "spike")
DETECTORS = ("hfoapp_hilbert", "ripplelab_hilbert")

# Defaults from gui-src/ConfigAutoDetector.m (startupFcn) and the main window Settings tab.
DETECTOR_DEFAULTS: dict[str, dict[str, Any]] = {
    "hfoapp_hilbert": {"epoch": "whole", "z_thresh": 3, "peak_thresh": 5, "nocycle_thresh": 3},
    "ripplelab_hilbert": {"epoch": "whole", "sd_thresh": 5, "min_dur_ms": 10},
}

# Values HFOApp writes in the header line "Onset threshold ...; Baseline: ..." (main window
# Settings tab defaults). They do not affect automatic detection; they are only written in
# the output header, as HFOApp does.
GUI_HEADER_DEFAULTS = {"onset": 3, "inclusion": 5, "cycles": 3, "baseline": "5 s"}


@dataclass
class Config:
    detector: str = "hfoapp_hilbert"
    event_type: str = "ripple"
    bpfreq: tuple[float, float] = (80.0, 250.0)
    bptype: str = "fir"
    notch: bool = False
    notch_freq: float = 60.0
    notch_type: str = "fir"
    notch_bandwidth: float = 4.0
    channels: list[str] | str = "all"
    duplicate_labels: str = "hfoapp"  # 'hfoapp' (first channel only, like HFOApp) | 'rename'
    params: dict[str, Any] = field(default_factory=dict)
    header: dict[str, Any] = field(default_factory=lambda: dict(GUI_HEADER_DEFAULTS))
    processing: dict[str, Any] = field(default_factory=lambda: dict(PROCESSING_DEFAULTS))
    source: str = "<defaults>"


# How a channel is processed: 'whole' keeps it in memory (exact),
# 'blocks' streams it through memory-mapped scratch files, 'auto' picks blocks above
# max_whole_samples per channel (~5e7 samples ≈ 3.5 h at 4 kHz ≈ 4.5 GB peak).
PROCESSING_DEFAULTS = {"mode": "auto", "max_whole_samples": 50_000_000,
                       "block_seconds": 600, "margin_seconds": 120, "tmp_dir": None}

_TOP_KEYS = {"detector", "event_type", "bandpass", "notch", "channels", "montage",
             "duplicate_labels", "params", "header", "processing"}


def _check_keys(section: str, given: dict, allowed) -> None:
    unknown = set(given) - set(allowed)
    if unknown:
        raise ValueError(f"unknown key(s) in {section}: {sorted(unknown)}")


def from_dict(d: dict[str, Any], source: str = "<dict>") -> Config:
    d = d or {}
    _check_keys("config", d, _TOP_KEYS)
    cfg = Config(source=source)

    cfg.detector = d.get("detector", cfg.detector)
    if cfg.detector not in DETECTORS:
        raise ValueError(f"detector must be one of {DETECTORS} (STE/SLL/MNI not ported yet)")

    cfg.event_type = d.get("event_type", cfg.event_type)
    if cfg.event_type not in EVENT_TYPES:
        raise ValueError(f"event_type must be one of {EVENT_TYPES}")

    bp = d.get("bandpass", {}) or {}
    _check_keys("bandpass", bp, {"freq", "type"})
    lo, hi = bp.get("freq", cfg.bpfreq)
    cfg.bpfreq = (float(lo), float(hi))
    cfg.bptype = bp.get("type", cfg.bptype)

    nt = d.get("notch", {}) or {}
    _check_keys("notch", nt, {"enabled", "freq", "type", "bandwidth"})
    cfg.notch = bool(nt.get("enabled", cfg.notch))
    cfg.notch_freq = float(nt.get("freq", cfg.notch_freq))
    cfg.notch_type = nt.get("type", cfg.notch_type)
    cfg.notch_bandwidth = float(nt.get("bandwidth", cfg.notch_bandwidth))
    for name, val in (("bandpass.type", cfg.bptype), ("notch.type", cfg.notch_type)):
        if val not in ("fir", "iir"):
            raise ValueError(f"{name} must be 'fir' or 'iir'")

    cfg.channels = d.get("channels", cfg.channels)
    if d.get("montage", "none") not in ("none", None):
        raise ValueError("montage is not implemented yet; use 'none'")
    cfg.duplicate_labels = d.get("duplicate_labels", cfg.duplicate_labels)
    if cfg.duplicate_labels not in ("hfoapp", "rename"):
        raise ValueError("duplicate_labels must be 'hfoapp' or 'rename'")

    defaults = DETECTOR_DEFAULTS[cfg.detector]
    params = d.get("params", {}) or {}
    _check_keys(f"params ({cfg.detector})", params, defaults)
    cfg.params = {**defaults, **params}

    hdr = d.get("header", {}) or {}
    _check_keys("header", hdr, GUI_HEADER_DEFAULTS)
    cfg.header = {**GUI_HEADER_DEFAULTS, **hdr}

    proc = d.get("processing", {}) or {}
    _check_keys("processing", proc, PROCESSING_DEFAULTS)
    cfg.processing = {**PROCESSING_DEFAULTS, **proc}
    if cfg.processing["mode"] not in ("auto", "whole", "blocks"):
        raise ValueError("processing.mode must be 'auto', 'whole' or 'blocks'")
    return cfg


def load(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as f:
        return from_dict(yaml.safe_load(f), source=str(path))


def as_dict(cfg: Config) -> dict[str, Any]:
    """All parameters actually used, for the output header."""
    return {
        "detector": cfg.detector,
        "event_type": cfg.event_type,
        "bandpass": {"freq": list(cfg.bpfreq), "type": cfg.bptype},
        "notch": {"enabled": cfg.notch, "freq": cfg.notch_freq, "type": cfg.notch_type,
                  "bandwidth": cfg.notch_bandwidth},
        "channels": cfg.channels,
        "montage": "none",
        "duplicate_labels": cfg.duplicate_labels,
        "params": cfg.params,
        "processing": {k: v for k, v in cfg.processing.items() if k != "tmp_dir"},
    }
