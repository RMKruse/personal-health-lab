"""Typed canonical health values shared by import, storage, and read projections."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class CanonicalHealthType(StrEnum):
    ACTIVE_ENERGY = "active_energy"
    APPLE_RESTING_HEART_RATE = "apple_resting_heart_rate"


class CanonicalUnit(StrEnum):
    KILOCALORIE = "kcal"
    BEATS_PER_MINUTE = "count/min"


@dataclass(frozen=True, slots=True)
class HealthProvenance:
    source_name: str
    source_version: str
    device: str
    original_value: float
    original_unit: str


@dataclass(frozen=True, slots=True)
class CanonicalHealthRecord:
    data_type: CanonicalHealthType
    unit: CanonicalUnit
    value: float
    source_start: datetime
    source_end: datetime
    measurement_local_day: date
    provenance: HealthProvenance

    def __post_init__(self) -> None:
        expected_unit = {
            CanonicalHealthType.ACTIVE_ENERGY: CanonicalUnit.KILOCALORIE,
            CanonicalHealthType.APPLE_RESTING_HEART_RATE: CanonicalUnit.BEATS_PER_MINUTE,
        }[self.data_type]
        if self.unit is not expected_unit or not math.isfinite(self.value):
            raise ValueError("Ungültiger kanonischer Gesundheitswert.")
        if self.source_start.tzinfo is None or self.source_end.tzinfo is None:
            raise ValueError("Quellzeitpunkte müssen eine Zeitzone enthalten.")
        if self.measurement_local_day != self.source_start.date():
            raise ValueError("Messlokaler Kalendertag passt nicht zum Quellzeitpunkt.")


@dataclass(frozen=True, slots=True)
class DailyHealthValue:
    day: date
    value: float
    source_starts: tuple[datetime, ...]
    source_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DailyHealthSeries:
    data_type: CanonicalHealthType
    unit: CanonicalUnit
    values: tuple[DailyHealthValue, ...]


__all__ = [
    "CanonicalHealthRecord",
    "CanonicalHealthType",
    "CanonicalUnit",
    "DailyHealthSeries",
    "DailyHealthValue",
    "HealthProvenance",
]
