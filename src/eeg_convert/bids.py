"""BIDS sidecars next to each EDF: events.tsv (+ .json), channels.tsv, recording.json.

Event table columns, shared by every stage (BIDS columns + datetime and source):
onset, duration, datetime, sample, channel, trial_type, source, value.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Iterable

from . import __version__
from .edf_write import UNIT, EdfInfo
from .recording import Block, Recording

EVENT_COLUMNS = ["onset", "duration", "datetime", "sample", "channel", "trial_type", "source", "value"]

# HED 8.4.0, Property/Data-property/Data-source-type/...
SOURCE_HED = {
    "manual": "Expert-annotation",
    "automatic": "Computed-feature",
    "device": "Instrument-measurement",
}

GENERATED_BY = {"Name": "eeg_convert", "Version": __version__,
                "Description": "vendor recording -> EDF per block + BIDS sidecars"}


def _cell(v) -> str:
    if v is None or v == "":
        return "n/a"
    # one row per line, tab-separated: no tabs or line breaks inside a cell
    if isinstance(v, float):
        return f"{v:g}"
    return " ".join(str(v).split()) if isinstance(v, str) else str(v)


def write_tsv(path: Path, columns: list[str], rows: Iterable[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\t".join(columns) + "\n")
        for r in rows:
            f.write("\t".join(_cell(r.get(c)) for c in columns) + "\n")


def write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _local_iso(start: dt.datetime, seconds: float) -> str:
    # local wall-clock time, as people read it; the offset is in recording.json
    return (start + dt.timedelta(seconds=seconds)).replace(tzinfo=None).isoformat(timespec="milliseconds")


def comment_rows(block: Block) -> list[dict]:
    rows = []
    for c in sorted(block.comments, key=lambda c: c.sample):
        onset = c.sample / block.srate
        rows.append(dict(onset=f"{onset:.6f}", duration="0", datetime=_local_iso(block.start, onset),
                         sample=c.sample, channel=c.channel, trial_type="comment", source="manual",
                         value=c.text))
    return rows


def events_sidecar(source_file: str) -> dict:
    return {
        "onset": {"Description": "Time of the event from the start of the EDF file", "Units": "s"},
        "duration": {"Description": "Duration of the event (0 for a comment)", "Units": "s"},
        "datetime": {"LongName": "Local date and time",
                     "Description": "onset as local wall-clock time, ISO 8601 with milliseconds "
                                    "(time-zone offset in the *_recording.json file)"},
        "sample": {"Description": "Sample index from the start of the EDF file (0-based)"},
        "channel": {"Description": "Channel the event belongs to; n/a = all channels"},
        "trial_type": {"Description": "Event category",
                       "Levels": {"comment": "comment written in the acquisition software"}},
        "source": {"Description": "Who or what produced the event",
                   "Levels": {"manual": "added by a person",
                              "automatic": "computed by a program",
                              "device": "produced by the recording hardware"},
                   "HED": SOURCE_HED},
        "value": {"Description": "Text of the comment, as written"},
        "GeneratedBy": [GENERATED_BY],
        "SourceFile": source_file,
    }


CHANNEL_COLUMNS = ["name", "type", "units", "sampling_frequency", "low_cutoff", "high_cutoff", "status"]


def channel_columns(block: Block) -> list[str]:
    """BIDS channels.tsv columns; `reference` only when the file states the references."""
    return CHANNEL_COLUMNS + (["reference"] if any(c.reference for c in block.channels) else [])


def channel_rows(block: Block, info: EdfInfo) -> list[dict]:
    # type (SEEG / ECOG / EEG) is not in the vendor file: left n/a, to fill in by hand
    return [dict(name=c.label, type=None, units=UNIT, sampling_frequency=f"{block.srate:g}",
                 low_cutoff=c.highpass_hz, high_cutoff=c.lowpass_hz, status="good", reference=c.reference)
            for c in block.channels]


def recording_json(rec: Recording, block: Block, info: EdfInfo) -> dict:
    out = {
        "SourceFile": rec.path.name,
        "SourceFormat": rec.format,
        **({"Group": block.group} if block.group else {}),
        "Block": block.index,
        "BlocksInSource": sum(b.group == block.group for b in rec.blocks),
        "RecordingStart": block.start.isoformat(timespec="microseconds"),
        "SamplingFrequency": block.srate,
        "Samples": block.n_samples,
        "DurationSeconds": block.n_samples / block.srate,
        "EDF": {
            "File": info.path.name,
            "RecordSamples": info.record_samples,
            "RecordDuration": info.record_duration,
            "Records": info.n_records,
            "PaddingSamples": info.padding,
            "Note": "EDF stores the start time to the second; RecordingStart is exact",
        },
        "Channels": [{"Label": s.label, "Unit": UNIT, "LSB": s.lsb,
                      "DigitalRange": [s.dig_min, s.dig_max], "PhysicalRange": [s.phys_min, s.phys_max],
                      "DataRange": [s.data_min, s.data_max], "ScaleExact": s.exact,
                      "ScaleRelativeError": s.relative_error,
                      **({"Source": c.source} if c.source else {})}
                     for s, c in zip(info.scales, block.channels)],
        "GeneratedBy": [GENERATED_BY],
    }
    if block.source:
        out["Source"] = block.source
    return out
