"""Convert one vendor file: one EDF + sidecars per block. Also the pipeline stage entry point."""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Optional

from . import bids
from .edf_write import EdfInfo, write_block
from .recording import Recording

log = logging.getLogger(__name__)

# extension -> "module:function" returning a Recording; imported only when needed,
# so a reader's optional dependency is only required for its own format
READERS = {
    ".adicht": "eeg_convert.adicht:open_adicht",
    ".ncs": "eeg_convert.neuralynx:open_neuralynx",      # a session folder, or one of its files
    ".zip": "eeg_convert.neuralynx:open_neuralynx",      # a zipped Neuralynx session folder
    "/": "eeg_convert.neuralynx:open_neuralynx",         # a folder
}

ACCEPTS = tuple(k for k in READERS if k != "/")     # pipeline stage: input file types


@dataclass
class BlockOutput:
    edf: Path
    events: Path
    channels: Path
    recording: Path
    info: EdfInfo
    n_comments: int


def open_recording(path: str | Path, fmt: str = "auto") -> Recording:
    path = Path(path)
    key = ("/" if path.is_dir() else path.suffix.lower()) if fmt == "auto" else f".{fmt.lstrip('.').lower()}"
    if key not in READERS:
        raise ValueError(f"{path.name}: unsupported format {key!r} (supported: {', '.join(READERS)})")
    module, func = READERS[key].split(":")
    return getattr(importlib.import_module(module), func)(path)


def convert(path: str | Path, out_dir: str | Path, fmt: str = "auto",
            edf_opener: Optional[Callable[[Path], BinaryIO]] = None,
            progress: Optional[Callable[[int, float], None]] = None, name: str = "") -> list[BlockOutput]:
    """edf_opener(path) -> writable binary file: where the EDF goes instead of out_dir
    (the browser's private disk); the sidecars always go to out_dir.
    progress(block number in the file, fraction of that block). name: base of the
    output names instead of the recording's own."""
    path, out_dir = Path(path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    with open_recording(path, fmt) as rec:
        rec.name = name or rec.name
        for k, block in enumerate(rec.blocks, 1):
            stem = rec.stem(block)
            edf = out_dir / f"{stem}.edf"
            info = write_block(block, edf, recording_id=f"{path.name} {block.group} block {block.index}".replace("  ", " "),
                               fileobj=edf_opener(edf) if edf_opener else None,
                               progress=(lambda x, k=k: progress(k, x)) if progress else None)
            for s in info.scales:
                if not s.exact:
                    log.warning("%s %s: data reach %d..%d counts, scale rounded to 8 characters "
                                "(relative error %.1e); digital values are exact",
                                edf.name, s.label, s.data_min, s.data_max, s.relative_error)
            if info.padding:
                log.warning("%s: last EDF record padded with %d zero samples", edf.name, info.padding)

            out = BlockOutput(edf=edf, events=out_dir / f"{stem}_events.tsv",
                              channels=out_dir / f"{stem}_channels.tsv",
                              recording=out_dir / f"{stem}_recording.json", info=info,
                              n_comments=len(block.comments))
            bids.write_tsv(out.events, bids.EVENT_COLUMNS, bids.comment_rows(block))
            bids.write_json(out.events.with_suffix(".json"), bids.events_sidecar(path.name))
            bids.write_tsv(out.channels, bids.channel_columns(block), bids.channel_rows(block, info))
            bids.write_json(out.recording, bids.recording_json(rec, block, info))
            outputs.append(out)
    return outputs


def run(inputs: list[str | Path], params: dict, out_dir: str | Path) -> list[Path]:
    """Pipeline stage: vendor files in, EDF files out (sidecars next to them)."""
    fmt = params.get("format", "auto")
    edfs, sessions = [], []
    for p in map(Path, inputs):
        if p.suffix.lower() == ".ncs":      # one Neuralynx session per folder, not per channel file
            if p.parent in sessions:
                continue
            sessions.append(p.parent)
            p = p.parent
        edfs += [o.edf for o in convert(p, out_dir, fmt)]
    return edfs
