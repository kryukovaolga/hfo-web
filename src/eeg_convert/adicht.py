"""LabChart .adicht reader: the only module that imports adichtion (pinned version).

Supported: LabChart for Windows files, raw int16
channels, one sampling rate per block, no units conversion. Anything else is refused
with a clear message rather than converted wrongly.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from .recording import Block, Channel, Comment, Recording

log = logging.getLogger(__name__)

CACHE_BYTES = 32 << 20


def _setup_float(xml: str, tag: str) -> Optional[float]:
    # values as LabChart writes them, with the PC's decimal separator: "0,3 ", "1000 "
    m = re.search(rf"<{tag}[^>]*>\s*([0-9.,]+)", xml)
    return float(m.group(1).replace(",", ".")) if m else None


def open_adicht(path: str | Path) -> Recording:
    try:
        from adichtion import AdichtFile
    except ImportError as e:
        raise ImportError("reading .adicht needs adichtion: pip install 'hfo-port[adicht]'") from e

    path = Path(path)
    # sequential reading: a large block cache only costs memory
    f = AdichtFile(path, cache_bytes=CACHE_BYTES)
    # Per-block min/max summaries are for adichtion's viewer. Not needed here; their numpy
    # reduceat with int64 indices also fails on 32-bit WebAssembly (Pyodide). Private
    # method of the pinned version (1.1.0): recheck when upgrading.
    f._remember_summary = lambda *args: None
    try:
        if f.kind != "adicht":
            raise ValueError(f"{path.name}: LabChart for Mac documents are not supported")
        titles = f.channel_titles
        with_data = [c for c in range(f.n_channels) if f.has_data(c)]
        for c in set(range(f.n_channels)) - set(with_data):
            log.warning("%s: channel %d (%r) has no data, skipped", path.name, c + 1, titles[c])

        blocks = []
        for r in f.records:
            channels = []
            for c in with_data:
                where = f"{path.name}, block {r.index + 1}, channel {titles[c]!r}"
                info = f.channel_record(r.index, c)
                if info is None or info.dtype != "int16":
                    raise ValueError(f"{where}: calculated or non-int16 channel, not supported")
                if r.samples_per_tick(c) != 1:
                    raise ValueError(f"{where}: sampling rate differs from the block's, not supported")
                if r.value_label(c) != "V":
                    raise ValueError(f"{where}: units conversion ({r.value_label(c)!r}) not supported")
                lsb = info.units.lsb
                rng = f.range_volts(c, r.index)
                xml = info.setup.xml if info.setup else ""
                channels.append(Channel(
                    label=titles[c], lsb=lsb, unit="V",
                    range_counts=round(rng / lsb) if rng else None,
                    highpass_hz=_setup_float(xml, "InputHighPassKnee"),
                    lowpass_hz=_setup_float(xml, "InputLowPassKnee")))

            comments = [Comment(sample=r.tick_to_sample(cm.tick, with_data[0]),
                                channel=titles[cm.channel] if cm.channel >= 0 else None,
                                text=cm.text)
                        for cm in r.comments]

            def read(i, s0, s1, r=r):
                return r.read_raw(with_data[i], s0, s1)

            blocks.append(Block(index=r.index + 1, start=r.start_local, srate=r.tick_rate_hz,
                                n_samples=r.ticks, channels=channels, comments=comments, read=read))
    except Exception:
        f.close()
        raise
    return Recording(path=path, format="adicht", blocks=blocks, close=f.close)
