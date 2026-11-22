"""Command line: eeg-convert FILE.adicht [...] -o OUT_DIR"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .convert import READERS, convert


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="eeg-convert", description=__doc__)
    ap.add_argument("inputs", nargs="+", help=f"recordings ({', '.join(READERS)})")
    ap.add_argument("-o", "--out-dir", help="output directory (default: next to each input)")
    ap.add_argument("--format", default="auto", help="input format (default: from the extension)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    for src in map(Path, args.inputs):
        t0 = time.perf_counter()
        try:
            outputs = convert(src, args.out_dir or src.parent, args.format)
        except (ValueError, ImportError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        for o in outputs:
            print(f"{o.edf}  ({o.info.n_records} records x {o.info.record_duration} s, "
                  f"{o.n_comments} comments)")
        print(f"{src.name}: {len(outputs)} block(s) in {time.perf_counter() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
