"""Command line: hfo-port recording.edf -c config.yaml [-o out_dir] [--strict-hfoapp]"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import __version__, config, pipeline, writer


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hfo-port", description=__doc__)
    ap.add_argument("edf", help="input EDF file")
    ap.add_argument("-c", "--config", help="YAML configuration (default: HFOApp defaults)")
    ap.add_argument("-o", "--out-dir", help="output directory (default: next to the EDF file)")
    ap.add_argument("--strict-hfoapp", action="store_true",
                    help="write HFOApp's original 11-line header, without the parameter block")
    ap.add_argument("--processing", choices=["auto", "whole", "blocks"],
                    help="override processing.mode of the config (blocks: for recordings larger than RAM)")
    ap.add_argument("--tmp-dir", help="where block processing puts its scratch files (default: system temp)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    cfg = config.load(args.config) if args.config else config.from_dict({}, source="<defaults>")
    if args.processing:
        cfg.processing["mode"] = args.processing
    if args.tmp_dir:
        cfg.processing["tmp_dir"] = args.tmp_dir

    t0 = time.perf_counter()
    res = pipeline.run(args.edf, cfg)
    ev_path, sm_path = writer.output_paths(args.edf, args.out_dir)
    ev_path.parent.mkdir(parents=True, exist_ok=True)
    writer.write(res, ev_path, sm_path, strict=args.strict_hfoapp)
    n = sum(len(c.events) for c in res.channels)
    print(f"{n} events in {len(res.channels)} channels, {time.perf_counter() - t0:.1f} s")
    print(f"  {ev_path}\n  {sm_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
