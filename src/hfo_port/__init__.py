"""Python port of HFOApp's automatic HFO detection.

Layers:
    core   -- filters, cycles, movstats, detectors: pure functions on arrays, no I/O
    io     -- edf (streamed reader), writer (HFOApp text files)
    glue   -- config, pipeline, cli
"""

__version__ = "0.1.0"
