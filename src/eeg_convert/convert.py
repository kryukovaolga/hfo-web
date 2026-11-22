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
}

ACCEPTS = tuple(READERS)        # pipeline stage: input file types


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
    key = path.suffix.lower() if fmt == "auto" else f".{fmt.lstrip('.').lower()}"
    if key not in READERS:
        raise ValueError(f"{path.name}: unsupported format {key!r} (supported: {', '.join(READERS)})")
    module, func = READERS[key].split(":")
    return getattr(importlib.import_module(module), func)(path)


def edf_stem(path: str | Path, block_index: int, n_blocks: int) -> str:
    """<name> for a single block, <name>_block01, _block02, ... otherwise."""
    stem = Path(path).stem
    return stem if n_blocks == 1 else f"{stem}_block{block_index:02d}"


def convert(path: str | Path, out_dir: str | Path, fmt: str = "auto",
            edf_opener: Optional[Callable[[Path], BinaryIO]] = None,
            progress: Optional[Callable[[int, float], None]] = None) -> list[BlockOutput]:
    """edf_opener(path) -> writable binary file: where the EDF goes instead of out_dir
    (the browser's private disk); the sidecars always go to out_dir.
    progress(block index, fraction of that block)."""
    path, out_dir = Path(path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    with open_recording(path, fmt) as rec:
        for block in rec.blocks:
            stem = edf_stem(path, block.index, len(rec.blocks))
            edf = out_dir / f"{stem}.edf"
            info = write_block(block, edf, recording_id=f"{path.name} block {block.index}",
                               fileobj=edf_opener(edf) if edf_opener else None,
                               progress=(lambda x, i=block.index: progress(i, x)) if progress else None)
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
            bids.write_tsv(out.channels, ["name", "type", "units", "sampling_frequency",
                                          "low_cutoff", "high_cutoff", "status"],
                           bids.channel_rows(block, info))
            bids.write_json(out.recording, bids.recording_json(rec, block, info))
            outputs.append(out)
    return outputs


def run(inputs: list[str | Path], params: dict, out_dir: str | Path) -> list[Path]:
    """Pipeline stage: vendor files in, EDF files out (sidecars next to them)."""
    fmt = params.get("format", "auto")
    edfs = []
    for p in inputs:
        edfs += [o.edf for o in convert(p, out_dir, fmt)]
    return edfs
