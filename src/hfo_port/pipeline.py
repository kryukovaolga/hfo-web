"""EDF + Config -> per-channel events (HFOApp automatic mode, all selected channels)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .detectors import Events, detect_hilbert, detect_hilbert_blocked
from .edf import EdfHeader, EdfReader

log = logging.getLogger(__name__)


@dataclass
class ChannelResult:
    label: str
    events: Events


@dataclass
class RunResult:
    header: EdfHeader
    labels: list[str]            # all channel labels after duplicate handling (EDF order)
    channels: list[ChannelResult]
    config: Config


def _labels(raw: list[str], policy: str) -> tuple[list[str], list[int]]:
    """Return (labels, indices to analyze).

    'hfoapp': HFOApp looks channels up by label (ismember -> first match), so a
              duplicated label is analyzed once, with the data of its first channel.
    'rename': duplicates become 'A1#2', 'A1#3', ... and every channel is analyzed.
    """
    if policy == "rename":
        seen: dict[str, int] = {}
        out = []
        for lab in raw:
            seen[lab] = seen.get(lab, 0) + 1
            out.append(lab if seen[lab] == 1 else f"{lab}#{seen[lab]}")
        return out, list(range(len(raw)))
    first = {}
    for i, lab in enumerate(raw):
        first.setdefault(lab, i)
    for lab in {l for l in raw if raw.count(l) > 1}:
        log.warning("duplicate channel label %r: only the first one is analyzed (HFOApp behavior)", lab)
    return list(raw), sorted(first.values())


def run(edf_path: str | Path, cfg: Config) -> RunResult:
    with EdfReader(edf_path) as reader:
        h = reader.header
        labels, to_analyze = _labels(h.labels, cfg.duplicate_labels)
        if cfg.channels != "all":
            wanted = list(cfg.channels)
            missing = set(wanted) - set(labels)
            if missing:
                raise ValueError(f"channels not found in EDF: {sorted(missing)}")
            to_analyze = [i for i in to_analyze if labels[i] in wanted]

        srate = h.srate
        n = h.n_samples
        p = cfg.params
        # HFOApp's default epoch is the "length" of the time range: diff(xrange) / srate,
        # i.e. (n - 1) samples, not n.
        epoch = (n - 1) / srate if p.get("epoch", "whole") == "whole" else float(p["epoch"])
        common = dict(bpfreq=cfg.bpfreq, bptype=cfg.bptype, notch=cfg.notch,
                      notch_freq=cfg.notch_freq, notch_type=cfg.notch_type,
                      notch_bandwidth=cfg.notch_bandwidth, epoch=epoch)

        if cfg.detector == "hfoapp_hilbert":
            det = dict(z_thresh=p["z_thresh"], peak_thresh=p["peak_thresh"],
                       nocycle_thresh=p["nocycle_thresh"], avg_method="mov")
        else:  # ripplelab_hilbert
            det = dict(z_thresh=p["sd_thresh"], peak_thresh=p["sd_thresh"],
                       dur_thresh=p["min_dur_ms"] / 1000, avg_method="epoch")

        proc = cfg.processing
        blocks = proc["mode"] == "blocks" or (
            proc["mode"] == "auto" and n > proc["max_whole_samples"])
        if blocks:
            block = int(round(proc["block_seconds"] * srate))
            margin = int(round(proc["margin_seconds"] * srate))
            log.info("block processing: %d-sample blocks, %d-sample envelope margins", block, margin)

        results = []
        for i in to_analyze:
            if blocks:
                ev = detect_hilbert_blocked(lambda s, e, i=i: reader.read_range(i, s, e), n,
                                            srate, block=block, margin=margin,
                                            tmp_dir=proc["tmp_dir"], **common, **det)
            else:
                # the signal is passed directly (no local name) so the detector can free it early
                ev = detect_hilbert(reader.read_channel(i), srate, **common, **det)
            log.info("%s: %d events", labels[i], len(ev))
            results.append(ChannelResult(labels[i], ev))

    return RunResult(header=h, labels=labels, channels=results, config=cfg)
