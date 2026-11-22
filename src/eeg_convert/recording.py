"""Format-independent view of a recording, as every reader returns it.

A recording is a list of blocks: continuous pieces with their own start time
(LabChart blocks; Neuralynx segments between pauses). All channels of a block
share one sampling rate and are stored as integer counts with a fixed scale,
so they can be written to EDF without loss.
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


@dataclass(frozen=True)
class Comment:
    sample: int                     # sample index from the start of the block
    channel: Optional[str]          # channel label; None = all channels
    text: str


@dataclass
class Block:
    index: int                      # 1-based, as LabChart numbers its blocks
    start: dt.datetime              # local time, timezone-aware when the format stores the offset
    srate: float
    n_samples: int
    channels: list[Channel]
    comments: list[Comment] = field(default_factory=list)
    # read(channel index, start sample, stop sample) -> integer counts
    read: Callable[[int, int, int], np.ndarray] = None


@dataclass
class Recording:
    path: Path
    format: str                     # "adicht", ...
    blocks: list[Block]
    close: Callable[[], None] = lambda: None

    def __enter__(self) -> "Recording":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
