"""Format-independent view of a recording, as every reader returns it.

A recording is a list of blocks: continuous pieces with their own start time
(LabChart blocks; Neuralynx segments between pauses). All channels of a block
share one sampling rate and are stored as integer counts with a fixed scale,
so they can be written to EDF without loss. A block may belong to a group of
channels recorded together but analyzed apart (Neuralynx: one animal).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np


@dataclass(frozen=True)
class Channel:
    label: str
    lsb: float                      # physical value of one count, in `unit`
    unit: str                       # "V"
    range_counts: Optional[int]     # nominal full scale in counts (LabChart: range / lsb), if known
    highpass_hz: Optional[float] = None   # hardware filters, if the file says
    lowpass_hz: Optional[float] = None
    reference: Optional[str] = None       # recording reference, if the file says
    source: dict = field(default_factory=dict, compare=False)   # format-specific details


@dataclass(frozen=True)
class Comment:
    sample: int                     # sample index from the start of the block
    channel: Optional[str]          # channel label; None = all channels
    text: str


@dataclass
class Block:
    index: int                      # 1-based, as LabChart numbers its blocks (within its group)
    start: dt.datetime              # local time, timezone-aware when the format stores the offset
    srate: float
    n_samples: int
    channels: list[Channel]
    comments: list[Comment] = field(default_factory=list)
    # read(channel index, start sample, stop sample) -> integer counts
    read: Callable[[int, int, int], np.ndarray] = None
    group: str = ""                 # e.g. the animal of a multi-animal Neuralynx session
    source: dict = field(default_factory=dict)   # format-specific details (recording.json)


@dataclass
class Recording:
    path: Path
    format: str                     # "adicht", "neuralynx"
    blocks: list[Block]
    close: Callable[[], None] = lambda: None
    name: str = ""                  # base of the output names (default: the file name without extension)

    def stem(self, block: Block) -> str:
        """<name>[_<group>][_blockNN]: block number only when the group has several blocks."""
        stem = self.name or self.path.stem
        if block.group:
            stem += f"_{block.group}"
        if sum(b.group == block.group for b in self.blocks) > 1:
            stem += f"_block{block.index:02d}"
        return stem

    def __enter__(self) -> "Recording":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
