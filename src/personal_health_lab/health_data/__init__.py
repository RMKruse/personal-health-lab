"""Typed canonical health values shared by import, storage, and read projections."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class CanonicalHealthType(StrEnum):
    ACTIVE_ENERGY = "active_energy"
    APPLE_RESTING_HEART_RATE = "apple_resting_heart_rate"
    BODY_MASS = "body_mass"


class CanonicalUnit(StrEnum):
    KILOCALORIE = "kcal"
    BEATS_PER_MINUTE = "count/min"
    KILOGRAM = "kg"


class AnalysisFreshness(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class DataQualityStatus(StrEnum):
    REVIEWED = "reviewed"
    PROVISIONAL = "provisional"


class DataStatusReasonCode(StrEnum):
    OPEN_REVIEW_CASE = "open_review_case"
    PASSIVE_COVERAGE_GAP = "passive_coverage_gap"


@dataclass(frozen=True, slots=True)
class AnalysisDataStatusReason:
    code: DataStatusReasonCode
    evidence_ids: tuple[str, ...]


class ModelMaturityStatus(StrEnum):
    EXPLORATORY = "exploratory"
    ROBUST = "robust"


class ReproducibilityStatus(StrEnum):
    REPRODUCIBLE = "reproducible"
    LOCAL_DEVELOPMENT = "local_development"
    NOT_RECORDED = "not_recorded"


class ModelMaturityCriterionCode(StrEnum):
    MINIMUM_OBSERVATIONS = "minimum_observations"
    ROBUST_OBSERVATIONS = "robust_observations"
    INPUT_COMPLETENESS = "input_completeness"
    FEATURE_DEPENDENCY = "feature_dependency"
    BOOTSTRAP_SUCCESS_RATE = "bootstrap_success_rate"
    OUTCOME_VARIATION = "outcome_variation"
    TIME_SERIES_CONTINUITY = "time_series_continuity"


@dataclass(frozen=True, slots=True)
class ModelMaturityCriterion:
    code: ModelMaturityCriterionCode
    passed: bool
    observed_value: float | str
    threshold: float | str


@dataclass(frozen=True, slots=True)
class _MeasurementId:
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("Messungs-ID darf nicht leer sein.")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class LogicalMeasurementId(_MeasurementId):
    pass


@dataclass(frozen=True, slots=True)
class MeasurementVersionId(_MeasurementId):
    pass


@dataclass(frozen=True, slots=True)
class HealthProvenance:
    source_name: str
    source_version: str
    device: str
    original_value: float
    original_unit: str
    strong_source_id_hash: str | None = None

    def __post_init__(self) -> None:
        if self.strong_source_id_hash is not None and (
            len(self.strong_source_id_hash) != 64
            or not set(self.strong_source_id_hash) <= set("0123456789abcdef")
        ):
            raise ValueError("Starke Quellen-ID muss ein SHA-256-Wert sein.")


@dataclass(frozen=True, slots=True)
class CanonicalHealthRecord:
    logical_measurement_id: LogicalMeasurementId
    measurement_version_id: MeasurementVersionId
    data_type: CanonicalHealthType
    unit: CanonicalUnit
    value: float
    source_start: datetime
    source_end: datetime
    source_updated_at: datetime
    measurement_local_day: date
    provenance: HealthProvenance

    def __post_init__(self) -> None:
        expected_unit = {
            CanonicalHealthType.ACTIVE_ENERGY: CanonicalUnit.KILOCALORIE,
            CanonicalHealthType.APPLE_RESTING_HEART_RATE: CanonicalUnit.BEATS_PER_MINUTE,
            CanonicalHealthType.BODY_MASS: CanonicalUnit.KILOGRAM,
        }[self.data_type]
        if self.unit is not expected_unit or not math.isfinite(self.value):
            raise ValueError("Ungültiger kanonischer Gesundheitswert.")
        if any(
            timestamp.tzinfo is None
            for timestamp in (self.source_start, self.source_end, self.source_updated_at)
        ):
            raise ValueError("Quellzeitpunkte müssen eine Zeitzone enthalten.")
        if self.measurement_local_day != self.source_start.date():
            raise ValueError("Messlokaler Kalendertag passt nicht zum Quellzeitpunkt.")


@dataclass(frozen=True, slots=True)
class DailyHealthValue:
    day: date
    value: float
    source_starts: tuple[datetime, ...]
    source_names: tuple[str, ...]
    measurement_version_ids: tuple[MeasurementVersionId, ...] = ()
    source_updated_ats: tuple[datetime, ...] = ()
    source_versions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DailyHealthSeries:
    data_type: CanonicalHealthType
    unit: CanonicalUnit
    values: tuple[DailyHealthValue, ...]


__all__ = [
    "AnalysisDataStatusReason",
    "AnalysisFreshness",
    "CanonicalHealthRecord",
    "CanonicalHealthType",
    "CanonicalUnit",
    "DailyHealthSeries",
    "DailyHealthValue",
    "DataQualityStatus",
    "DataStatusReasonCode",
    "HealthProvenance",
    "LogicalMeasurementId",
    "MeasurementVersionId",
    "ModelMaturityCriterion",
    "ModelMaturityCriterionCode",
    "ModelMaturityStatus",
    "ReproducibilityStatus",
]
