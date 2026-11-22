"""Pipeline stage entry point: EDF files in, HFOApp event files out.

A thin wrapper around pipeline.run + writer.write, the same calls as the hfo-port command.
"""

from __future__ import annotations

from pathlib import Path

from . import config, pipeline, writer

ACCEPTS = (".edf",)


def run(inputs: list[str | Path], params: dict, out_dir: str | Path) -> list[Path]:
    """params: config (YAML path; default HFOApp defaults), strict_hfoapp (bool),
    processing (dict overriding the config's processing section)."""
    cfg = config.load(params["config"]) if params.get("config") else config.from_dict({}, source="<defaults>")
    cfg.processing.update(params.get("processing", {}))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for edf in inputs:
        res = pipeline.run(edf, cfg)
        ev, sm = writer.output_paths(edf, out_dir)
        writer.write(res, ev, sm, strict=params.get("strict_hfoapp", False))
        outputs += [ev, sm]
    return outputs
