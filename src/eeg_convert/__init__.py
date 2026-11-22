"""Vendor recordings (LabChart .adicht; later Neuralynx) -> EDF per block + BIDS sidecars.

A pipeline stage of its own: reads vendor files, writes standard
files, knows nothing about HFO detection.
"""

__version__ = "0.1.0"
