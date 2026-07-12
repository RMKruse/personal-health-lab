from enum import StrEnum


class DataMode(StrEnum):
    """Session-wide, immutable choice of physically isolated data store."""

    SYNTHETIC = "synthetic"
    REAL = "real"
