"""Write HFOApp's two text files (SaveResults in gui-src/HFOApp.m:5853).

Differences from HFOApp, on purpose:
- the header is written as text, not used as a printf format string, so Windows paths
  (backslashes) do not truncate it;
- unless strict=True, a block with all parameters used is inserted before the dashed
  separator and "Number of header lines" is updated;
- unless strict=True, three columns are appended to the events table: Onset(s) (from the
  first sample of the file), Duration(ms) ((end - start) / srate, as HFOApp's summary) and
  StartTime (file start + onset, ms). Appended, not inserted: HFOApp reloads an events file
  by column position (readtable, Var1..Var8, HFOApp.m:3805-3827), so the first 8 columns must not move.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import yaml

from . import __version__
from .config import EVENT_TYPES, as_dict
from .matlab import num2str, num2str_int_row
from .pipeline import RunResult

PREFIX = "HFOEvents_"


def output_paths(edf_path: str | Path, out_dir: str | Path | None = None) -> tuple[Path, Path]:
    edf_path = Path(edf_path)
    out_dir = Path(out_dir) if out_dir else edf_path.parent
    events = out_dir / f"{PREFIX}{edf_path.stem}.txt"
    return events, out_dir / f"{events.stem}_summary.txt"


def _fmt_time(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")  # display format truncates fractional seconds


def _f(x: float, spec: str) -> str:
    """printf %f like MATLAB (NaN/Inf spelled as MATLAB does)."""
    if math.isnan(x) or math.isinf(x):
        word = "NaN" if math.isnan(x) else ("Inf" if x > 0 else "-Inf")
        return f"{word:>{int(spec.split('.')[0])}}"
    return format(x, spec)


def header_lines(res: RunResult, strict: bool) -> list[str]:
    h = res.header
    n = h.n_samples
    srate = h.srate
    start = h.start
    end = start + dt.timedelta(seconds=(n - 1) / srate)
    g = res.config.header
    lines = [
        "File created by HFOApp. DO NOT EDIT.",
        "Number of header lines:  11",
        "Number of trials: 1",
        f"Data file: {Path(h.path).resolve()}",
        f"Time: {_fmt_time(start)} - {_fmt_time(end)}",
        f"Total number of channels: {h.n_channels}",
        f"Length of data: {num2str(n / srate)} s",
        f"Sampling rate: {num2str(srate)} Hz",
        f"Onset threshold: {num2str(g['onset'])}; Inclusion threshold: {num2str(g['inclusion'])}; "
        f"Number of cycles: {num2str(g['cycles'])}; Baseline: {g['baseline']}.",
    ]
    if not strict:
        params = yaml.safe_dump(as_dict(res.config), sort_keys=False, default_flow_style=None).splitlines()
        block = [f"# Parameters used (hfo-port {__version__}, config: {res.config.source})"]
        block += [f"#   {p}" for p in params]
        lines += block
        # +2: the dashed separator and the column header line
        lines[1] = f"Number of header lines:  {len(lines) + 2}"
    lines.append("-" * 100)
    return lines


def write(res: RunResult, events_path: Path, summary_path: Path, strict: bool = False) -> None:
    header = header_lines(res, strict)
    srate = res.header.srate
    evtype = res.config.event_type
    with_events = [c for c in res.channels if len(c.events)]

    # ---- events file
    max_len = max(len(evtype), 10)
    rows = [
        f"{'Trial':>5}, {'Channel':>7}, {'EventLocation(sample)':>21}, {'EventType':>{max_len}}, "
        f"{'AverageFreq(Hz)':>15}, {'PeakZscore':>10}, {'NoCycles':>8}, {'BPFreq':>12}"
    ]
    if not strict:
        rows[0] += f", {'Onset(s)':>12}, {'Duration(ms)':>12}, {'StartTime':>23}"
    for ch in with_events:
        ev = ch.events
        for k in range(len(ev)):
            row = (
                f"{1:5d}, {ch.label:>7}, {num2str_int_row(ev.location[k]):>21}, {evtype:>{max_len}}, "
                f"{_f(ev.avg_freq[k], '15.2f')}, {_f(ev.peak_zscore[k], '10.2f')}, "
                f"{_f(ev.no_cycles[k], '8.2f')}, {ev.bpfreq[0]:6.1f} {ev.bpfreq[1]:6.1f}"
            )
            if not strict:
                s, e = (int(x) for x in ev.location[k])
                onset = (s - 1) / srate  # locations are 1-based
                clock = res.header.start + dt.timedelta(milliseconds=round(onset * 1000))
                row += (f", {onset:12.4f}, {(e - s) / srate * 1000:12.2f}, "
                        f"{clock.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]:>23}")
            rows.append(row)
    events_path.write_text("\n".join(header + rows) + "\n", encoding="utf-8", newline="\n")

    # ---- summary file: one row per channel label that has events (labels in EDF order;
    # with duplicate labels HFOApp finds the first channel each time, so rows repeat)
    by_label = {}
    for ch in with_events:
        by_label.setdefault(ch.label, ch.events)
    t = EVENT_TYPES.index(evtype)
    srows = [
        f"{'NoTrial':>7}, {'Channel':>7}, {'GammaDur(s)':>11}, {'RippleDur(s)':>12}, "
        f"{'FastrippleDur(s)':>16}, {'UltrafastDur(s)':>15}, {'SpikeDur(s)':>11}, {'GammaNo':>7}, "
        f"{'RippleNo':>8}, {'FastrippleNo':>12}, {'UltrafastNo':>11}, {'SpikeNo':>7}"
    ]
    for label in res.labels:
        ev = by_label.get(label)
        if ev is None:
            continue
        dur = [0.0] * 5
        cnt = [0] * 5
        dur[t] = float(sum((e - s) / srate for s, e in ev.location))
        cnt[t] = len(ev)
        srows.append(
            f"{1:7d}, {label:>7}, {dur[0]:11.3f}, {dur[1]:12.3f}, {dur[2]:16.3f}, {dur[3]:15.3f}, "
            f"{dur[4]:11.3f}, {cnt[0]:7d}, {cnt[1]:8d}, {cnt[2]:12d}, {cnt[3]:11d}, {cnt[4]:7d}"
        )
    summary_path.write_text("\n".join(header + srows) + "\n", encoding="utf-8", newline="\n")
