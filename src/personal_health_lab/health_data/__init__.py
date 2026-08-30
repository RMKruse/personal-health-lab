"""Typed canonical health values shared by import, storage, and read projections."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class CanonicalHealthType(StrEnum):
    ACTIVE_ENERGY = "active_energy"
    APPLE_EXERCISE_TIME = "apple_exercise_time"
    APPLE_RESTING_HEART_RATE = "apple_resting_heart_rate"
    BODY_MASS = "body_mass"
    DIETARY_BIOTIN = "dietary_biotin"
    DIETARY_CAFFEINE = "dietary_caffeine"
    DIETARY_CALCIUM = "dietary_calcium"
    DIETARY_CARBOHYDRATES = "dietary_carbohydrates"
    DIETARY_CHLORIDE = "dietary_chloride"
    DIETARY_CHOLESTEROL = "dietary_cholesterol"
    DIETARY_CHROMIUM = "dietary_chromium"
    DIETARY_COPPER = "dietary_copper"
    DIETARY_ENERGY_CONSUMED = "dietary_energy_consumed"
    DIETARY_FAT_MONOUNSATURATED = "dietary_fat_monounsaturated"
    DIETARY_FAT_POLYUNSATURATED = "dietary_fat_polyunsaturated"
    DIETARY_FAT_SATURATED = "dietary_fat_saturated"
    DIETARY_FAT_TOTAL = "dietary_fat_total"
    DIETARY_FIBER = "dietary_fiber"
    DIETARY_FOLATE = "dietary_folate"
    DIETARY_IODINE = "dietary_iodine"
    DIETARY_IRON = "dietary_iron"
    DIETARY_MAGNESIUM = "dietary_magnesium"
    DIETARY_MANGANESE = "dietary_manganese"
    DIETARY_MOLYBDENUM = "dietary_molybdenum"
    DIETARY_NIACIN = "dietary_niacin"
    DIETARY_PANTOTHENIC_ACID = "dietary_pantothenic_acid"
    DIETARY_PHOSPHORUS = "dietary_phosphorus"
    DIETARY_POTASSIUM = "dietary_potassium"
    DIETARY_PROTEIN = "dietary_protein"
    DIETARY_RIBOFLAVIN = "dietary_riboflavin"
    DIETARY_SELENIUM = "dietary_selenium"
    DIETARY_SODIUM = "dietary_sodium"
    DIETARY_SUGAR = "dietary_sugar"
    DIETARY_THIAMIN = "dietary_thiamin"
    DIETARY_VITAMIN_A = "dietary_vitamin_a"
    DIETARY_VITAMIN_B12 = "dietary_vitamin_b12"
    DIETARY_VITAMIN_B6 = "dietary_vitamin_b6"
    DIETARY_VITAMIN_C = "dietary_vitamin_c"
    DIETARY_VITAMIN_D = "dietary_vitamin_d"
    DIETARY_VITAMIN_E = "dietary_vitamin_e"
    DIETARY_VITAMIN_K = "dietary_vitamin_k"
    DIETARY_WATER = "dietary_water"
    DIETARY_ZINC = "dietary_zinc"
    STEP_COUNT = "step_count"
    WALKING_RUNNING_DISTANCE = "walking_running_distance"


class CanonicalUnit(StrEnum):
    KILOCALORIE = "kcal"
    BEATS_PER_MINUTE = "count/min"
    COUNT = "count"
    KILOGRAM = "kg"
    KILOMETER = "km"
    MINUTE = "min"
    GRAM = "g"
    MILLILITER = "mL"


def canonical_unit_for(data_type: CanonicalHealthType) -> CanonicalUnit:
    if data_type in {
        CanonicalHealthType.ACTIVE_ENERGY,
        CanonicalHealthType.DIETARY_ENERGY_CONSUMED,
    }:
        return CanonicalUnit.KILOCALORIE
    if data_type is CanonicalHealthType.APPLE_RESTING_HEART_RATE:
        return CanonicalUnit.BEATS_PER_MINUTE
    if data_type is CanonicalHealthType.APPLE_EXERCISE_TIME:
        return CanonicalUnit.MINUTE
    if data_type is CanonicalHealthType.STEP_COUNT:
        return CanonicalUnit.COUNT
    if data_type is CanonicalHealthType.WALKING_RUNNING_DISTANCE:
        return CanonicalUnit.KILOMETER
    if data_type is CanonicalHealthType.BODY_MASS:
        return CanonicalUnit.KILOGRAM
    if data_type is CanonicalHealthType.DIETARY_WATER:
        return CanonicalUnit.MILLILITER
    return CanonicalUnit.GRAM


class ActivitySourceClass(StrEnum):
    WATCH = "watch"
    IPHONE = "iphone"
    OTHER = "other"
    UNKNOWN = "unknown"


def classify_activity_source(source_name: str, device: str) -> ActivitySourceClass:
    if (source_name, device) == ("Apple Watch", "Apple Watch"):
        return ActivitySourceClass.WATCH
    if (source_name, device) == ("iPhone", "iPhone"):
        return ActivitySourceClass.IPHONE
    if source_name or device:
        return ActivitySourceClass.OTHER
    return ActivitySourceClass.UNKNOWN


class AnalysisFreshness(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class DataQualityStatus(StrEnum):
    REVIEWED = "reviewed"
    PROVISIONAL = "provisional"


class DataStatusReasonCode(StrEnum):
    OPEN_REVIEW_CASE = "open_review_case"
    PASSIVE_COVERAGE_GAP = "passive_coverage_gap"
    PROVISIONAL_INPUT_QUALITY = "provisional_input_quality"


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


class CanonicalSleepCategory(StrEnum):
    IN_BED = "in_bed"
    AWAKE = "awake"
    ASLEEP_UNSPECIFIED = "asleep_unspecified"
    ASLEEP_CORE = "asleep_core"
    ASLEEP_DEEP = "asleep_deep"
    ASLEEP_REM = "asleep_rem"


@dataclass(frozen=True, slots=True)
class CanonicalSleepInterval:
    logical_measurement_id: LogicalMeasurementId
    measurement_version_id: MeasurementVersionId
    original_category: str
    canonical_category: CanonicalSleepCategory
    source_start: datetime
    source_end: datetime
    source_updated_at: datetime
    source_name: str
    source_version: str
    device: str
    strong_source_id_hash: str | None = None
    is_selected: bool = True

    def __post_init__(self) -> None:
        if not self.original_category or self.source_end < self.source_start:
            raise ValueError("Ungültiges kanonisches Schlafintervall.")
        if any(
            timestamp.tzinfo is None
            for timestamp in (self.source_start, self.source_end, self.source_updated_at)
        ):
            raise ValueError("Quellzeitpunkte müssen eine Zeitzone enthalten.")


@dataclass(frozen=True, slots=True)
class CanonicalWorkout:
    logical_workout_id: LogicalMeasurementId
    workout_version_id: MeasurementVersionId
    original_activity_type: str
    source_start: datetime
    source_end: datetime
    source_updated_at: datetime
    measurement_local_day: date
    provenance: HealthProvenance
    reported_duration_minutes: float | None = None
    distance_kilometers: float | None = None
    active_energy_kilocalories: float | None = None

    def __post_init__(self) -> None:
        if not self.original_activity_type or self.source_end < self.source_start:
            raise ValueError("Ungültige kanonische Trainingseinheit.")
        if (
            any(
                timestamp.tzinfo is None
                for timestamp in (self.source_start, self.source_end, self.source_updated_at)
            )
            or self.measurement_local_day != self.source_start.date()
        ):
            raise ValueError("Ungültige Trainingszeitpunkte.")
        if any(
            value is not None and not math.isfinite(value)
            for value in (
                self.reported_duration_minutes,
                self.distance_kilometers,
                self.active_energy_kilocalories,
            )
        ):
            raise ValueError("Ungültige Trainingswerte.")


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
    legacy_measurement_version_id: MeasurementVersionId | None = None

    def __post_init__(self) -> None:
        expected_unit = canonical_unit_for(self.data_type)
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
    "ActivitySourceClass",
    "AnalysisDataStatusReason",
    "AnalysisFreshness",
    "CanonicalHealthRecord",
    "CanonicalHealthType",
    "CanonicalSleepCategory",
    "CanonicalSleepInterval",
    "CanonicalUnit",
    "CanonicalWorkout",
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
    "canonical_unit_for",
    "classify_activity_source",
]
