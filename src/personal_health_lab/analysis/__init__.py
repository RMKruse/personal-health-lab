"""Fixed V0.4 analysis definitions and run lifecycle."""

import hashlib
import json
import math
import os
import subprocess
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import numpy as np

from personal_health_lab.storage import (
    ActivityDerivationRecord,
    AnalysisDataStatusReason,
    AnalysisDefinitionId,
    AnalysisJsonlArtifact,
    AnalysisProvenance,
    AnalysisResultId,
    AnalysisRunConfiguration,
    AnalysisRunFact,
    AnalysisRunId,
    AnalysisRunPublication,
    CanonicalHealthType,
    CanonicalSleepCategory,
    CanonicalUnit,
    ContextRevisionId,
    DataQualityStatus,
    DataStatusReasonCode,
    LocalStore,
    MeasurementVersionId,
    MedicationRevisionId,
    ModelMaturityStatus,
    OperationId,
    PlausibilityRuleRecord,
    ReproducibilityStatus,
    ReviewCaseId,
    SnapshotId,
    StoredActivityDayValue,
    StoredAnalysisSourceEvidence,
    StoredMeasurement,
    StoredOutcomeContextDay,
    StoredWorkout,
    StoreError,
)

_PROJECT_ROOT = Path(__file__).parents[3]


class AnalysisResultFamily(StrEnum):
    RHR_ACTIVITY_LAG_1_7 = "rhr_activity_lag_1_7"
    RHR_ACTIVITY_LAG_1_30 = "rhr_activity_lag_1_30"
    WEIGHT_CORE = "weight_core"
    RHR_WEIGHT_ASSOCIATION = "rhr_weight_association"


class AnalysisInput(StrEnum):
    ACTIVE_ENERGY = "active_energy"
    TRAINING_TIME = "training_time"
    STEPS = "steps"
    WALKING_RUNNING_DISTANCE = "walking_running_distance"
    WORKOUT_DURATION_BY_TYPE = "workout_duration_by_type"
    WORKOUT_ENERGY_BY_TYPE = "workout_energy_by_type"
    CALENDAR = "calendar"
    ANNUAL_SEASONALITY = "annual_seasonality"
    WEEKDAY = "weekday"
    OUTCOME_DAY_CONTEXT = "outcome_day_context"
    PREFERRED_DAILY_WEIGHT = "preferred_daily_weight"
    NUTRITION_DAY_V1 = "nutrition-day-v1"
    RESTING_ENERGY_DAY_V1 = "resting-energy-day-allocation/v1"
    APPLE_RESTING_HEART_RATE = "apple_resting_heart_rate"


class AnalysisDiagnostic(StrEnum):
    INPUT = "input"
    MISSINGNESS = "missingness"
    SUPPORT = "support"
    FIT = "fit"
    RESIDUAL = "residual"
    PREDICTION = "prediction"
    INFLUENCE = "influence"
    SENSITIVITY = "sensitivity"
    STRUCTURAL_BREAK = "structural_break"
    BOOTSTRAP = "bootstrap"


class AnalysisMaturityCriterionCode(StrEnum):
    AUGMENTED_CONDITION_NUMBER = "augmented_condition_number"
    BLOCKED_PREDICTION_GAIN = "blocked_prediction_gain"
    BOOTSTRAP_SUCCESS = "bootstrap_success"
    CALENDAR_DAYS = "calendar_days"
    COMMON_COMPLETE_FRACTION = "common_complete_fraction"
    CONTEXT_SENSITIVITY = "context_sensitivity"
    EFFECTIVE_BLOCKS = "effective_blocks"
    FULL_RANK = "full_rank"
    INPUT_COMPLETENESS = "input_completeness"
    LJUNG_BOX = "ljung_box"
    MAXIMUM_COMMON_GAP_DAYS = "maximum_common_gap_days"
    MAXIMUM_GAP_DAYS = "maximum_gap_days"
    MAXIMUM_OBSERVED_GAP_DAYS = "maximum_observed_gap_days"
    MEAN_BAND_HALFWIDTH = "mean_band_halfwidth"
    MINIMUM_FIT_ROWS = "minimum_fit_rows"
    MODEL_ANCHORS = "model_anchors"
    PAIR_DENSITY = "pair_density"
    POSITIVE_TRAINING_DAYS = "positive_training_days"
    RESIDUAL_ACF = "residual_acf"
    RHR_MEASUREMENT_ERROR = "rhr_measurement_error"
    RIDGE_SENSITIVITY = "ridge_sensitivity"
    STRUCTURAL_BREAK_OR_SOURCE_CHANGE = "structural_break_or_source_change"
    UNPENALIZED_CONDITION_NUMBER = "unpenalized_condition_number"
    WEIGHT_MEASUREMENT_ERROR = "weight_measurement_error"


class AnalysisBootstrapMethod(StrEnum):
    MOVING_BLOCK_FULL_REFIT = "moving_block_full_refit"
    PAIRED_CIRCULAR_RESIDUAL_MOVING_BLOCK_FULL_REFIT = (
        "paired_circular_residual_moving_block_full_refit"
    )


class AnalysisIntervalMethod(StrEnum):
    POINTWISE_AND_STUDENTIZED_SIMULTANEOUS_BAND = "pointwise_and_studentized_simultaneous_band"
    POINTWISE_AND_SIMULTANEOUS_FAMILY_BAND = "pointwise_and_simultaneous_family_band"
    FISHER_Z_BIAS_CORRECTED_STUDENTIZED_SIMULTANEOUS_BAND = (
        "fisher_z_bias_corrected_studentized_simultaneous_band"
    )


class BootstrapSampleCountBasis(StrEnum):
    FIT_ROWS = "fit_rows"
    OBSERVED_WEIGHT_DAYS = "observed_weight_days"
    PAIRED_DAYS = "paired_days"


class AnalysisBootstrapVariant(StrEnum):
    PRIMARY = "primary"
    SENSITIVITY = "sensitivity"


class AssociationMeasure(StrEnum):
    PEARSON_LOCAL_SLOPES = "pearson_local_slopes"
    PEARSON_PAIRED_DEVIATIONS = "pearson_paired_deviations"


class WeightTrendKernel(StrEnum):
    TRIANGULAR_LOCAL_LINEAR = "triangular_local_linear"


class TrendUncertainty(StrEnum):
    NO_CALIBRATED_INTERVAL = "no_calibrated_interval"


class WeightTrendSupportStatus(StrEnum):
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class WeightTrendFailureReason(StrEnum):
    INSUFFICIENT_LOCAL_SUPPORT = "insufficient_local_support"
    SINGULAR_LOCAL_FIT = "singular_local_fit"
    OUTSIDE_OBSERVED_SUPPORT = "outside_observed_support"


class WeightModelFamily(StrEnum):
    ENERGY_COMPONENTS = "energy_components"
    ENERGY_MACROS = "energy_macros"


@dataclass(frozen=True, slots=True)
class BootstrapBlockLengthFacts:
    sample_count_basis: BootstrapSampleCountBasis
    minimum_days: int | None = None
    horizon_divisor: int | None = None
    sample_count_root: int = 3
    rounds_up: bool = True
    takes_maximum: bool = True
    sensitivity_factor: int = 2


@dataclass(frozen=True, slots=True)
class BootstrapMethodFacts:
    method: AnalysisBootstrapMethod
    interval_method: AnalysisIntervalMethod
    block_length: BootstrapBlockLengthFacts
    successful_refits: int = 2_000
    maximum_attempts: int = 2_020
    confidence_level: float = 0.95


@dataclass(frozen=True, slots=True)
class LagContrast:
    start_day: int
    end_day: int


@dataclass(frozen=True, slots=True)
class LagMaturityFacts:
    minimum_fit_rows: int
    minimum_input_completeness: float
    minimum_effective_blocks: int
    minimum_positive_training_days: int
    maximum_unpenalized_condition_number: float
    maximum_augmented_condition_number: float
    maximum_residual_acf: float
    maximum_ljung_box: float
    maximum_context_sensitivity_bpm_per_sd: float
    maximum_gap_days: int
    requires_full_rank: bool = True


@dataclass(frozen=True, slots=True)
class LagProfileMethodFacts:
    inputs: tuple[AnalysisInput, ...]
    lag_start_day: int
    contrasts: tuple[LagContrast, ...]
    lag_basis_nodes: int
    smoothing_penalty: float
    ridge_penalty: float
    simultaneous_critical_floor: float
    bootstrap: BootstrapMethodFacts
    diagnostics: tuple[AnalysisDiagnostic, ...]
    maturity: LagMaturityFacts


@dataclass(frozen=True, slots=True)
class WeightWindowMethodFacts:
    window_days: int
    energy_components_ridge_penalty: float
    energy_macros_ridge_penalty: float
    energy_components_absolute_bias_floor: float
    energy_macros_absolute_bias_floor: float


@dataclass(frozen=True, slots=True)
class WeightCoreMaturityFacts:
    minimum_common_complete_fraction: float = 0.35
    maximum_common_gap_days: int = 35
    minimum_model_anchors: int = 100
    maximum_unpenalized_condition_number: float = 1_000.0
    maximum_ridge_sensitivity_kg_per_week_per_sd: float = 0.025
    maximum_residual_acf: float = 0.8
    minimum_blocked_prediction_gain: float = -0.5
    maximum_mean_band_halfwidth_kg_per_week_per_sd: float = 0.25
    requires_full_rank: bool = True
    requires_primary_and_sensitivity_bootstraps: bool = True


@dataclass(frozen=True, slots=True)
class WeightCoreMethodFacts:
    inputs: tuple[AnalysisInput, ...]
    trend_kernel: WeightTrendKernel
    trend_uncertainty: TrendUncertainty
    windows: tuple[WeightWindowMethodFacts, ...]
    minimum_local_observations_floor: int
    minimum_local_observations_fraction: float
    minimum_scaled_pivot: float
    minimum_model_anchor_floor: int
    minimum_model_anchor_fraction: float
    energy_components_family_multiplier: float
    energy_macros_family_multiplier: float
    bootstrap: BootstrapMethodFacts
    diagnostics: tuple[AnalysisDiagnostic, ...]
    maturity: WeightCoreMaturityFacts


@dataclass(frozen=True, slots=True)
class OutcomeAssociationMaturityFacts:
    minimum_calendar_days: int = 365
    minimum_pair_density: float = 0.40
    maximum_observed_gap_days: int = 18
    maximum_residual_acf: float = 0.55
    maximum_rhr_measurement_error_sd: float = 0.35
    maximum_weight_measurement_error_sd: float = 0.084
    rejects_structural_break_or_source_change: bool = True


@dataclass(frozen=True, slots=True)
class OutcomeAssociationMethodFacts:
    inputs: tuple[AnalysisInput, ...]
    decomposition_kernel: WeightTrendKernel
    trend_measure: AssociationMeasure
    deviation_measure: AssociationMeasure
    minimum_pairs: int
    bootstrap: BootstrapMethodFacts
    diagnostics: tuple[AnalysisDiagnostic, ...]
    maturity: OutcomeAssociationMaturityFacts


type AnalysisMethodFacts = (
    LagProfileMethodFacts | WeightCoreMethodFacts | OutcomeAssociationMethodFacts
)


@dataclass(frozen=True, slots=True)
class AnalysisDefinition:
    analysis_definition_id: AnalysisDefinitionId
    definition_version: int
    result_family: AnalysisResultFamily
    horizon_days: tuple[int, ...]
    method_facts: AnalysisMethodFacts

    def __post_init__(self) -> None:
        if self.definition_version <= 0:
            raise ValueError("Analysedefinitionsversion muss positiv sein.")
        if not self.horizon_days or tuple(sorted(set(self.horizon_days))) != self.horizon_days:
            raise ValueError("Analysezeithorizonte müssen positiv, eindeutig und sortiert sein.")
        if self.horizon_days[0] <= 0:
            raise ValueError("Analysezeithorizonte müssen positiv sein.")
        expected_method_type = {
            AnalysisResultFamily.RHR_ACTIVITY_LAG_1_7: LagProfileMethodFacts,
            AnalysisResultFamily.RHR_ACTIVITY_LAG_1_30: LagProfileMethodFacts,
            AnalysisResultFamily.WEIGHT_CORE: WeightCoreMethodFacts,
            AnalysisResultFamily.RHR_WEIGHT_ASSOCIATION: OutcomeAssociationMethodFacts,
        }[self.result_family]
        if not isinstance(self.method_facts, expected_method_type):
            raise ValueError("Ergebnisfamilie und Methodenfakten passen nicht zusammen.")


@dataclass(frozen=True, slots=True)
class AnalysisReuseCandidate:
    analysis_run_id: AnalysisRunId
    result_ref: AnalysisResultId


@dataclass(frozen=True, slots=True)
class RunAnalysisPlan:
    analysis_definition: AnalysisDefinition
    requested_start_date: date | None
    requested_end_date: date | None
    eligible_start_date: date | None
    eligible_end_date: date | None
    schema_version: str
    base_snapshot_ref: SnapshotId | None
    reuse_candidate: AnalysisReuseCandidate | None


class AnalysisMissingness(StrEnum):
    OBSERVED = "observed"
    PARTIAL = "partial"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"


class AnalysisMissingnessReason(StrEnum):
    NO_OBSERVATION = "no_observation"
    EXCLUDED_OR_UNRESOLVED = "excluded_or_unresolved"
    CONFLICTING_VALUES = "conflicting_values"
    INPUT_NOT_AVAILABLE = "input_not_available"
    PARTIAL_OBSERVATION = "partial_observation"


class AnalysisScalingStatus(StrEnum):
    OBSERVED = "observed"
    INSUFFICIENT_OBSERVATIONS = "insufficient_observations"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class AnalysisSourceEvidence:
    measurement_version_id: MeasurementVersionId
    source_name: str
    source_version: str
    source_updated_at: datetime
    is_selected: bool
    disposition: str | None


class AnalysisSourceRecordKind(StrEnum):
    MEASUREMENT_VERSION = "measurement_version"
    CONTEXT_REVISION = "context_revision"
    MEDICATION_REVISION = "medication_revision"


@dataclass(frozen=True, slots=True)
class AnalysisSourceRecordEvidence:
    source_record_kind: AnalysisSourceRecordKind
    source_record_id: MeasurementVersionId | ContextRevisionId | MedicationRevisionId
    source_name: str
    source_version: str
    source_updated_at: datetime
    is_selected: bool
    disposition: str | None

    def __post_init__(self) -> None:
        expected_type = {
            AnalysisSourceRecordKind.MEASUREMENT_VERSION: MeasurementVersionId,
            AnalysisSourceRecordKind.CONTEXT_REVISION: ContextRevisionId,
            AnalysisSourceRecordKind.MEDICATION_REVISION: MedicationRevisionId,
        }[self.source_record_kind]
        if not isinstance(self.source_record_id, expected_type):
            raise ValueError("Quellenart und opake Quellen-ID widersprechen sich.")


@dataclass(frozen=True, slots=True)
class AnalysisInputValue:
    day: date
    input_id: AnalysisInput
    component: str | None
    unit: CanonicalUnit | None
    value: float | None
    missingness: AnalysisMissingness
    source_evidence: tuple[AnalysisSourceEvidence, ...] = ()
    data_quality_fact_ids: tuple[ReviewCaseId, ...] = ()
    missingness_reason: AnalysisMissingnessReason | None = None
    quality_status: DataQualityStatus = DataQualityStatus.REVIEWED
    source_records: tuple[AnalysisSourceRecordEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class AnalysisScaling:
    input_id: AnalysisInput
    component: str | None
    population_standard_deviation: float | None
    status: AnalysisScalingStatus


@dataclass(frozen=True, slots=True)
class AnalysisInputBundle:
    analysis_run_id: AnalysisRunId
    snapshot_id: SnapshotId
    analysis_definition_id: AnalysisDefinitionId
    start_date: date
    end_date: date
    calendar: tuple[date, ...]
    values: tuple[AnalysisInputValue, ...]
    rule_versions: tuple[str, ...]
    data_quality_fact_ids: tuple[ReviewCaseId, ...]
    scalings: tuple[AnalysisScaling, ...]
    activity_coverage_incomplete: bool = False
    schema_version: int = 2


_INPUT_KEYS = {
    "activity_coverage_incomplete",
    "analysis_definition_id",
    "analysis_period",
    "analysis_run_id",
    "calendar",
    "data_quality_fact_ids",
    "input_schema_version",
    "rule_versions",
    "scalings",
    "snapshot_id",
    "values",
}
_INPUT_VALUE_KEYS = {
    "component",
    "data_quality_fact_ids",
    "day",
    "input_id",
    "measurement_version_ids",
    "missingness",
    "missingness_reason",
    "quality_status",
    "source_evidence",
    "source_records",
    "unit",
    "value",
}
_SOURCE_EVIDENCE_KEYS = {
    "disposition",
    "is_selected",
    "measurement_version_id",
    "source_name",
    "source_updated_at",
    "source_version",
}
_SOURCE_RECORD_KEYS = {
    "disposition",
    "is_selected",
    "source_name",
    "source_record_id",
    "source_record_kind",
    "source_updated_at",
    "source_version",
}
_SCALING_KEYS = {"component", "input_id", "population_standard_deviation", "status"}
_RESULT_LIST_FIELDS = {
    AnalysisResultFamily.RHR_ACTIVITY_LAG_1_7: {
        "bootstrap_facts",
        "contrasts",
        "diagnostics",
        "lag_estimates",
        "maturity_criteria",
    },
    AnalysisResultFamily.RHR_ACTIVITY_LAG_1_30: {
        "bootstrap_facts",
        "contrasts",
        "diagnostics",
        "lag_estimates",
        "maturity_criteria",
    },
    AnalysisResultFamily.WEIGHT_CORE: {
        "bootstrap_facts",
        "diagnostics",
        "maturity_criteria",
        "models",
        "predictions",
        "trends",
    },
    AnalysisResultFamily.RHR_WEIGHT_ASSOCIATION: {
        "associations",
        "bootstrap_facts",
        "diagnostics",
        "maturity_criteria",
    },
}
_RESULT_PRIMARY_FIELDS = {
    AnalysisResultFamily.RHR_ACTIVITY_LAG_1_7: "lag_estimates",
    AnalysisResultFamily.RHR_ACTIVITY_LAG_1_30: "lag_estimates",
    AnalysisResultFamily.WEIGHT_CORE: "trends",
    AnalysisResultFamily.RHR_WEIGHT_ASSOCIATION: "associations",
}
_RESULT_ITEM_KEYS = {
    "lag_estimates": {
        "estimate_bpm_per_natural_scale",
        "estimate_bpm_per_personal_sd",
        "feature_id",
        "lag_day",
        "natural_scale",
        "natural_unit",
        "pointwise_interval",
        "simultaneous_band",
    },
    "contrasts": {
        "end_day",
        "estimate_bpm_per_natural_scale",
        "estimate_bpm_per_personal_sd",
        "feature_id",
        "natural_scale",
        "natural_unit",
        "pointwise_interval",
        "simultaneous_band",
        "start_day",
    },
    "trends": {
        "day",
        "failure_reason",
        "level_kg",
        "local_support",
        "numerical_pivot",
        "rate_kg_per_week",
        "support_status",
        "window_days",
    },
    "models": {
        "blocked_prediction_gain",
        "coefficient",
        "estimate_kg_per_week_per_natural_scale",
        "estimate_kg_per_week_per_personal_sd",
        "feature_id",
        "model_family",
        "natural_scale",
        "natural_unit",
        "pointwise_interval",
        "simultaneous_band",
        "window_days",
    },
    "predictions": {
        "day",
        "model_family",
        "observed_kg_per_week",
        "predicted_kg_per_week",
        "residual_kg_per_week",
        "window_days",
    },
    "associations": {
        "estimate",
        "measure",
        "paired_days",
        "pointwise_interval",
        "simultaneous_band",
        "window_days",
    },
    "bootstrap_facts": {
        "attempts",
        "block_length",
        "failure_counts",
        "quantile_stability",
        "seed",
        "successful_refits",
        "variant",
    },
    "diagnostics": {"code", "facts"},
    "maturity_criteria": {"code", "observed_value", "passed", "threshold"},
}


@dataclass(frozen=True, slots=True)
class AnalysisExecution:
    operation_id: OperationId
    analysis_run_id: AnalysisRunId
    snapshot_id: SnapshotId
    analysis_definition_id: AnalysisDefinitionId
    diagnostics: tuple[str, ...]
    provenance: AnalysisProvenance
    status: Literal["completed", "insufficient_data", "unstable"]
    result_id: AnalysisResultId | None = None
    model_maturity: ModelMaturityStatus | None = None


type AnalysisFactValue = (
    str
    | int
    | float
    | bool
    | None
    | tuple[AnalysisFactValue, ...]
    | tuple[tuple[str, AnalysisFactValue], ...]
)


@dataclass(frozen=True, slots=True)
class AnalysisResultInterval:
    lower: float
    upper: float


@dataclass(frozen=True, slots=True)
class AnalysisLagEstimate:
    feature_id: str
    lag_day: int
    natural_scale: float
    natural_unit: CanonicalUnit
    estimate_bpm_per_natural_scale: float
    estimate_bpm_per_personal_sd: float
    pointwise_interval: AnalysisResultInterval | None
    simultaneous_band: AnalysisResultInterval | None


@dataclass(frozen=True, slots=True)
class AnalysisLagContrastEstimate:
    feature_id: str
    start_day: int
    end_day: int
    natural_scale: float
    natural_unit: CanonicalUnit
    estimate_bpm_per_natural_scale: float
    estimate_bpm_per_personal_sd: float
    pointwise_interval: AnalysisResultInterval | None
    simultaneous_band: AnalysisResultInterval | None


@dataclass(frozen=True, slots=True)
class AnalysisBootstrapFacts:
    variant: AnalysisBootstrapVariant
    seed: int
    block_length: int
    attempts: int
    successful_refits: int
    failure_counts: tuple[tuple[str, int], ...]
    quantile_stability: float | None


@dataclass(frozen=True, slots=True)
class AnalysisResultDiagnostic:
    code: AnalysisDiagnostic
    facts: tuple[tuple[str, AnalysisFactValue], ...]


@dataclass(frozen=True, slots=True)
class AnalysisResultMaturityCriterion:
    code: AnalysisMaturityCriterionCode
    passed: bool
    observed_value: float | str
    threshold: float | str


@dataclass(frozen=True, slots=True)
class WeightTrendEstimate:
    day: date
    window_days: int
    level_kg: float | None
    rate_kg_per_week: float | None
    local_support: int
    numerical_pivot: float | None
    support_status: WeightTrendSupportStatus
    failure_reason: WeightTrendFailureReason | None


@dataclass(frozen=True, slots=True)
class WeightModelEstimate:
    window_days: int
    model_family: WeightModelFamily
    feature_id: str
    coefficient: float | None
    natural_scale: float
    natural_unit: CanonicalUnit
    estimate_kg_per_week_per_natural_scale: float | None
    estimate_kg_per_week_per_personal_sd: float | None
    pointwise_interval: AnalysisResultInterval
    simultaneous_band: AnalysisResultInterval
    blocked_prediction_gain: float | None


@dataclass(frozen=True, slots=True)
class WeightPrediction:
    day: date
    window_days: int
    model_family: WeightModelFamily
    observed_kg_per_week: float | None
    predicted_kg_per_week: float | None
    residual_kg_per_week: float | None


@dataclass(frozen=True, slots=True)
class RhrWeightAssociationEstimate:
    window_days: int
    measure: AssociationMeasure
    estimate: float | None
    paired_days: int
    pointwise_interval: AnalysisResultInterval
    simultaneous_band: AnalysisResultInterval


@dataclass(frozen=True, slots=True)
class AnalysisLagResultValues:
    lag_estimates: tuple[AnalysisLagEstimate, ...]
    contrasts: tuple[AnalysisLagContrastEstimate, ...]
    bootstrap_facts: tuple[AnalysisBootstrapFacts, ...]
    diagnostics: tuple[AnalysisResultDiagnostic, ...]
    maturity_criteria: tuple[AnalysisResultMaturityCriterion, ...]


@dataclass(frozen=True, slots=True)
class WeightCoreResultValues:
    trends: tuple[WeightTrendEstimate, ...]
    models: tuple[WeightModelEstimate, ...]
    predictions: tuple[WeightPrediction, ...]
    bootstrap_facts: tuple[AnalysisBootstrapFacts, ...]
    diagnostics: tuple[AnalysisResultDiagnostic, ...]
    maturity_criteria: tuple[AnalysisResultMaturityCriterion, ...]


@dataclass(frozen=True, slots=True)
class RhrWeightAssociationResultValues:
    associations: tuple[RhrWeightAssociationEstimate, ...]
    bootstrap_facts: tuple[AnalysisBootstrapFacts, ...]
    diagnostics: tuple[AnalysisResultDiagnostic, ...]
    maturity_criteria: tuple[AnalysisResultMaturityCriterion, ...]


type AnalysisResultValues = (
    AnalysisLagResultValues | WeightCoreResultValues | RhrWeightAssociationResultValues
)


def _result_items(record: dict[str, object], name: str) -> tuple[dict[str, object], ...]:
    values = record.get(name)
    if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
        raise ValueError("Analyseergebnisliste ist ungültig.")
    return tuple(cast(dict[str, object], value) for value in values)


def _result_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Analyseergebnistext ist ungültig.")
    return value


def _result_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("Analyseergebniszahl ist ungültig.")
    return value


def _result_float(value: object, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("Analyseergebniswert ist ungültig.")
    return float(value)


def _result_interval(value: object) -> AnalysisResultInterval:
    if not isinstance(value, dict) or set(value) != {"lower", "upper"}:
        raise ValueError("Analyseergebnisintervall ist ungültig.")
    lower = _result_float(value["lower"])
    upper = _result_float(value["upper"])
    assert lower is not None and upper is not None
    if lower > upper:
        raise ValueError("Analyseergebnisintervall ist umgekehrt.")
    return AnalysisResultInterval(lower, upper)


def _result_optional_interval(value: object) -> AnalysisResultInterval | None:
    return None if value is None else _result_interval(value)


def _freeze_analysis_fact(value: object) -> AnalysisFactValue:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return tuple(_freeze_analysis_fact(item) for item in value)
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return tuple(
            (key, _freeze_analysis_fact(item))
            for key, item in sorted(cast(dict[str, object], value).items())
        )
    raise ValueError("Analyseergebnisfakt ist ungültig.")


def _result_support_lists(
    record: dict[str, object],
) -> tuple[
    tuple[AnalysisBootstrapFacts, ...],
    tuple[AnalysisResultDiagnostic, ...],
    tuple[AnalysisResultMaturityCriterion, ...],
]:
    bootstrap = tuple(
        AnalysisBootstrapFacts(
            AnalysisBootstrapVariant(_result_string(item["variant"])),
            _result_int(item["seed"]),
            _result_int(item["block_length"]),
            _result_int(item["attempts"]),
            _result_int(item["successful_refits"]),
            tuple(
                sorted(
                    (_result_string(code), _result_int(count))
                    for code, count in cast(dict[str, object], item["failure_counts"]).items()
                )
            ),
            _result_float(item["quantile_stability"], optional=True),
        )
        for item in _result_items(record, "bootstrap_facts")
    )
    diagnostics = tuple(
        AnalysisResultDiagnostic(
            AnalysisDiagnostic(_result_string(item["code"])),
            cast(
                tuple[tuple[str, AnalysisFactValue], ...],
                _freeze_analysis_fact(item["facts"]),
            ),
        )
        for item in _result_items(record, "diagnostics")
    )
    maturity = tuple(
        AnalysisResultMaturityCriterion(
            AnalysisMaturityCriterionCode(_result_string(item["code"])),
            cast(bool, item["passed"]),
            cast(float | str, item["observed_value"]),
            cast(float | str, item["threshold"]),
        )
        for item in _result_items(record, "maturity_criteria")
    )
    return bootstrap, diagnostics, maturity


def load_analysis_result_values(
    store: LocalStore,
    analysis_run_id: AnalysisRunId,
    result_id: AnalysisResultId,
    definition: AnalysisDefinition,
) -> tuple[int, AnalysisResultValues]:
    artifact = store.load_analysis_result_artifact(
        analysis_run_id, result_id, definition.analysis_definition_id
    )
    record = json.loads(artifact.payload)
    if (
        not isinstance(record, dict)
        or record.get("analysis_run_id") != str(analysis_run_id)
        or record.get("analysis_result_id") != str(result_id)
        or record.get("analysis_definition_id") != str(definition.analysis_definition_id)
        or record.get("result_family") != definition.result_family.value
        or record.get("result_schema_version") != artifact.schema_version
    ):
        raise ValueError("Analyseergebnisreferenzen sind inkohärent.")
    typed = cast(dict[str, object], record)
    content = dict(typed)
    del content["analysis_run_id"]
    del content["analysis_result_id"]
    if (
        hashlib.sha256(
            json.dumps(content, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        != artifact.content_hash
    ):
        raise ValueError("Analyseergebnis verletzt den Inhaltshash.")
    bootstrap, diagnostics, maturity = _result_support_lists(typed)
    if definition.result_family in {
        AnalysisResultFamily.RHR_ACTIVITY_LAG_1_7,
        AnalysisResultFamily.RHR_ACTIVITY_LAG_1_30,
    }:
        values: AnalysisResultValues = AnalysisLagResultValues(
            tuple(
                AnalysisLagEstimate(
                    _result_string(item["feature_id"]),
                    _result_int(item["lag_day"]),
                    cast(float, _result_float(item["natural_scale"])),
                    CanonicalUnit(_result_string(item["natural_unit"])),
                    cast(float, _result_float(item["estimate_bpm_per_natural_scale"])),
                    cast(float, _result_float(item["estimate_bpm_per_personal_sd"])),
                    _result_optional_interval(item["pointwise_interval"]),
                    _result_optional_interval(item["simultaneous_band"]),
                )
                for item in _result_items(typed, "lag_estimates")
            ),
            tuple(
                AnalysisLagContrastEstimate(
                    _result_string(item["feature_id"]),
                    _result_int(item["start_day"]),
                    _result_int(item["end_day"]),
                    cast(float, _result_float(item["natural_scale"])),
                    CanonicalUnit(_result_string(item["natural_unit"])),
                    cast(float, _result_float(item["estimate_bpm_per_natural_scale"])),
                    cast(float, _result_float(item["estimate_bpm_per_personal_sd"])),
                    _result_optional_interval(item["pointwise_interval"]),
                    _result_optional_interval(item["simultaneous_band"]),
                )
                for item in _result_items(typed, "contrasts")
            ),
            bootstrap,
            diagnostics,
            maturity,
        )
    elif definition.result_family is AnalysisResultFamily.WEIGHT_CORE:
        values = WeightCoreResultValues(
            tuple(
                WeightTrendEstimate(
                    date.fromisoformat(_result_string(item["day"])),
                    _result_int(item["window_days"]),
                    _result_float(item["level_kg"], optional=True),
                    _result_float(item["rate_kg_per_week"], optional=True),
                    _result_int(item["local_support"]),
                    _result_float(item["numerical_pivot"], optional=True),
                    WeightTrendSupportStatus(_result_string(item["support_status"])),
                    None
                    if item["failure_reason"] is None
                    else WeightTrendFailureReason(_result_string(item["failure_reason"])),
                )
                for item in _result_items(typed, "trends")
            ),
            tuple(
                WeightModelEstimate(
                    _result_int(item["window_days"]),
                    WeightModelFamily(_result_string(item["model_family"])),
                    _result_string(item["feature_id"]),
                    _result_float(item["coefficient"], optional=True),
                    cast(float, _result_float(item["natural_scale"])),
                    CanonicalUnit(_result_string(item["natural_unit"])),
                    _result_float(item["estimate_kg_per_week_per_natural_scale"], optional=True),
                    _result_float(item["estimate_kg_per_week_per_personal_sd"], optional=True),
                    _result_interval(item["pointwise_interval"]),
                    _result_interval(item["simultaneous_band"]),
                    _result_float(item["blocked_prediction_gain"], optional=True),
                )
                for item in _result_items(typed, "models")
            ),
            tuple(
                WeightPrediction(
                    date.fromisoformat(_result_string(item["day"])),
                    _result_int(item["window_days"]),
                    WeightModelFamily(_result_string(item["model_family"])),
                    _result_float(item["observed_kg_per_week"], optional=True),
                    _result_float(item["predicted_kg_per_week"], optional=True),
                    _result_float(item["residual_kg_per_week"], optional=True),
                )
                for item in _result_items(typed, "predictions")
            ),
            bootstrap,
            diagnostics,
            maturity,
        )
    else:
        values = RhrWeightAssociationResultValues(
            tuple(
                RhrWeightAssociationEstimate(
                    _result_int(item["window_days"]),
                    AssociationMeasure(_result_string(item["measure"])),
                    _result_float(item["estimate"], optional=True),
                    _result_int(item["paired_days"]),
                    _result_interval(item["pointwise_interval"]),
                    _result_interval(item["simultaneous_band"]),
                )
                for item in _result_items(typed, "associations")
            ),
            bootstrap,
            diagnostics,
            maturity,
        )
    return artifact.schema_version, values


_LAG_INPUTS = (
    AnalysisInput.APPLE_RESTING_HEART_RATE,
    AnalysisInput.ACTIVE_ENERGY,
    AnalysisInput.TRAINING_TIME,
    AnalysisInput.STEPS,
    AnalysisInput.WALKING_RUNNING_DISTANCE,
    AnalysisInput.WORKOUT_DURATION_BY_TYPE,
    AnalysisInput.WORKOUT_ENERGY_BY_TYPE,
    AnalysisInput.CALENDAR,
    AnalysisInput.ANNUAL_SEASONALITY,
    AnalysisInput.WEEKDAY,
    AnalysisInput.OUTCOME_DAY_CONTEXT,
)
_LAG_DIAGNOSTICS = (
    AnalysisDiagnostic.INPUT,
    AnalysisDiagnostic.MISSINGNESS,
    AnalysisDiagnostic.FIT,
    AnalysisDiagnostic.RESIDUAL,
    AnalysisDiagnostic.SENSITIVITY,
    AnalysisDiagnostic.BOOTSTRAP,
)
_LAG_BOOTSTRAP = BootstrapMethodFacts(
    AnalysisBootstrapMethod.MOVING_BLOCK_FULL_REFIT,
    AnalysisIntervalMethod.POINTWISE_AND_STUDENTIZED_SIMULTANEOUS_BAND,
    BootstrapBlockLengthFacts(BootstrapSampleCountBasis.FIT_ROWS, horizon_divisor=3),
)
_WEIGHT_BOOTSTRAP = BootstrapMethodFacts(
    AnalysisBootstrapMethod.MOVING_BLOCK_FULL_REFIT,
    AnalysisIntervalMethod.POINTWISE_AND_SIMULTANEOUS_FAMILY_BAND,
    BootstrapBlockLengthFacts(
        BootstrapSampleCountBasis.OBSERVED_WEIGHT_DAYS,
        horizon_divisor=3,
    ),
)

_DEFINITIONS = (
    AnalysisDefinition(
        AnalysisDefinitionId("rhr-activity-lag-1-7-v1"),
        1,
        AnalysisResultFamily.RHR_ACTIVITY_LAG_1_7,
        (7,),
        LagProfileMethodFacts(
            _LAG_INPUTS,
            1,
            (LagContrast(1, 7),),
            7,
            3.0,
            1.0,
            5.708,
            _LAG_BOOTSTRAP,
            _LAG_DIAGNOSTICS,
            LagMaturityFacts(100, 0.60, 18, 10, 55.0, 55.0, 0.35, 5.0, 0.5, 35),
        ),
    ),
    AnalysisDefinition(
        AnalysisDefinitionId("rhr-activity-lag-1-30-v1"),
        1,
        AnalysisResultFamily.RHR_ACTIVITY_LAG_1_30,
        (30,),
        LagProfileMethodFacts(
            _LAG_INPUTS,
            1,
            (LagContrast(1, 7), LagContrast(8, 30), LagContrast(1, 30)),
            9,
            300.0,
            1.0,
            6.454,
            _LAG_BOOTSTRAP,
            _LAG_DIAGNOSTICS,
            LagMaturityFacts(120, 0.65, 11, 10, 500.0, 450.0, 0.60, 28.0, 0.5, 14),
        ),
    ),
    AnalysisDefinition(
        AnalysisDefinitionId("weight-core-7-14-30-90-v1"),
        1,
        AnalysisResultFamily.WEIGHT_CORE,
        (7, 14, 30, 90),
        WeightCoreMethodFacts(
            (
                AnalysisInput.PREFERRED_DAILY_WEIGHT,
                AnalysisInput.NUTRITION_DAY_V1,
                AnalysisInput.ACTIVE_ENERGY,
                AnalysisInput.RESTING_ENERGY_DAY_V1,
            ),
            WeightTrendKernel.TRIANGULAR_LOCAL_LINEAR,
            TrendUncertainty.NO_CALIBRATED_INTERVAL,
            (
                WeightWindowMethodFacts(7, 100.0, 100.0, 0.059519, 0.132232),
                WeightWindowMethodFacts(14, 100.0, 100.0, 0.043473, 0.142449),
                WeightWindowMethodFacts(30, 100.0, 100.0, 0.044135, 0.197123),
                WeightWindowMethodFacts(90, 30.0, 1.0, 0.045717, 0.105168),
            ),
            4,
            0.22,
            1e-6,
            3,
            0.12,
            1.454414,
            1.120367,
            _WEIGHT_BOOTSTRAP,
            (
                AnalysisDiagnostic.INPUT,
                AnalysisDiagnostic.MISSINGNESS,
                AnalysisDiagnostic.SUPPORT,
                AnalysisDiagnostic.FIT,
                AnalysisDiagnostic.RESIDUAL,
                AnalysisDiagnostic.PREDICTION,
                AnalysisDiagnostic.SENSITIVITY,
                AnalysisDiagnostic.BOOTSTRAP,
            ),
            WeightCoreMaturityFacts(),
        ),
    ),
    AnalysisDefinition(
        AnalysisDefinitionId("rhr-weight-association-7-14-30-90-v1"),
        1,
        AnalysisResultFamily.RHR_WEIGHT_ASSOCIATION,
        (7, 14, 30, 90),
        OutcomeAssociationMethodFacts(
            (
                AnalysisInput.APPLE_RESTING_HEART_RATE,
                AnalysisInput.PREFERRED_DAILY_WEIGHT,
            ),
            WeightTrendKernel.TRIANGULAR_LOCAL_LINEAR,
            AssociationMeasure.PEARSON_LOCAL_SLOPES,
            AssociationMeasure.PEARSON_PAIRED_DEVIATIONS,
            4,
            BootstrapMethodFacts(
                AnalysisBootstrapMethod.PAIRED_CIRCULAR_RESIDUAL_MOVING_BLOCK_FULL_REFIT,
                AnalysisIntervalMethod.FISHER_Z_BIAS_CORRECTED_STUDENTIZED_SIMULTANEOUS_BAND,
                BootstrapBlockLengthFacts(
                    BootstrapSampleCountBasis.PAIRED_DAYS,
                    minimum_days=3,
                ),
            ),
            (
                AnalysisDiagnostic.INPUT,
                AnalysisDiagnostic.MISSINGNESS,
                AnalysisDiagnostic.FIT,
                AnalysisDiagnostic.INFLUENCE,
                AnalysisDiagnostic.STRUCTURAL_BREAK,
                AnalysisDiagnostic.SENSITIVITY,
                AnalysisDiagnostic.BOOTSTRAP,
            ),
            OutcomeAssociationMaturityFacts(),
        ),
    ),
)


def analysis_definitions() -> tuple[AnalysisDefinition, ...]:
    return _DEFINITIONS


def plan_analysis(
    analysis_definition_id: AnalysisDefinitionId,
    start_date: date | None,
    end_date: date | None,
    schema_version: str,
    snapshot_id: SnapshotId | None,
    available_start_date: date | None,
    available_end_date: date | None,
    reuse_candidate: AnalysisReuseCandidate | None = None,
) -> RunAnalysisPlan:
    definition = next(
        item for item in _DEFINITIONS if item.analysis_definition_id == analysis_definition_id
    )
    eligible_start_date: date | None = None
    eligible_end_date: date | None = None
    if available_start_date is not None and available_end_date is not None:
        eligible_start = max(
            value for value in (available_start_date, start_date) if value is not None
        )
        eligible_end = min(value for value in (available_end_date, end_date) if value is not None)
        if eligible_start <= eligible_end:
            eligible_start_date, eligible_end_date = eligible_start, eligible_end
    return RunAnalysisPlan(
        definition,
        start_date,
        end_date,
        eligible_start_date,
        eligible_end_date,
        schema_version,
        snapshot_id,
        reuse_candidate,
    )


_MEASUREMENT_INPUTS = {
    "active_energy": (AnalysisInput.ACTIVE_ENERGY, None),
    "apple_exercise_time": (AnalysisInput.TRAINING_TIME, None),
    "apple_resting_heart_rate": (AnalysisInput.APPLE_RESTING_HEART_RATE, None),
    "body_mass": (AnalysisInput.PREFERRED_DAILY_WEIGHT, None),
    "dietary_energy_consumed": (AnalysisInput.NUTRITION_DAY_V1, "energy"),
    "dietary_protein": (AnalysisInput.NUTRITION_DAY_V1, "protein"),
    "dietary_carbohydrates": (AnalysisInput.NUTRITION_DAY_V1, "carbohydrates"),
    "dietary_fat_total": (AnalysisInput.NUTRITION_DAY_V1, "total_fat"),
    "step_count": (AnalysisInput.STEPS, None),
    "walking_running_distance": (AnalysisInput.WALKING_RUNNING_DISTANCE, None),
}
_FIXED_INPUT_COMPONENTS = {
    AnalysisInput.NUTRITION_DAY_V1: ("energy", "protein", "carbohydrates", "total_fat"),
}
_MEASUREMENT_BACKED_INPUTS = {item[0] for item in _MEASUREMENT_INPUTS.values()}
_ACTIVITY_INPUTS = {
    AnalysisInput.ACTIVE_ENERGY,
    AnalysisInput.TRAINING_TIME,
    AnalysisInput.STEPS,
    AnalysisInput.WALKING_RUNNING_DISTANCE,
}
_WORKOUT_INPUTS = {
    AnalysisInput.WORKOUT_DURATION_BY_TYPE,
    AnalysisInput.WORKOUT_ENERGY_BY_TYPE,
}
_POINT_INPUTS = {
    AnalysisInput.APPLE_RESTING_HEART_RATE,
    AnalysisInput.PREFERRED_DAILY_WEIGHT,
}
_BUILTIN_RULE_VERSIONS = {
    AnalysisInput.PREFERRED_DAILY_WEIGHT: "preferred-daily-weight/v1",
    AnalysisInput.NUTRITION_DAY_V1: "nutrition-day-v1",
    AnalysisInput.RESTING_ENERGY_DAY_V1: "resting-energy-day-allocation/v1",
}


def _calendar_values(day: date, input_id: AnalysisInput) -> tuple[tuple[str, float], ...]:
    if input_id is AnalysisInput.CALENDAR:
        return (("linear_day", float(day.toordinal())),)
    if input_id is AnalysisInput.ANNUAL_SEASONALITY:
        angle = 2.0 * math.pi * (day.timetuple().tm_yday - 1) / 365.2425
        return (("sine", math.sin(angle)), ("cosine", math.cos(angle)))
    if input_id is AnalysisInput.WEEKDAY:
        return tuple(
            (name, float(day.weekday() == index))
            for index, name in enumerate(
                ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
            )
        )
    return ()


def _source_evidence(
    measurements: tuple[StoredMeasurement, ...],
) -> tuple[AnalysisSourceEvidence, ...]:
    return tuple(
        AnalysisSourceEvidence(
            item.measurement_version_id,
            item.source_name,
            item.source_version,
            item.source_updated_at,
            item.is_selected,
            item.disposition,
        )
        for item in measurements
    )


def _workout_source_evidence(
    workouts: tuple[StoredWorkout, ...],
) -> tuple[AnalysisSourceEvidence, ...]:
    return tuple(
        AnalysisSourceEvidence(
            item.workout_version_id,
            item.source_name,
            item.source_version,
            item.source_updated_at,
            item.is_selected,
            item.disposition,
        )
        for item in workouts
    )


def _measurement_value(
    day: date,
    input_id: AnalysisInput,
    component: str | None,
    measurements: tuple[StoredMeasurement, ...],
) -> AnalysisInputValue:
    quality_ids = tuple(
        sorted(
            {case for item in measurements for case in item.review_case_ids},
            key=str,
        )
    )
    quality_status = DataQualityStatus.PROVISIONAL if quality_ids else DataQualityStatus.REVIEWED
    if not measurements:
        return AnalysisInputValue(
            day,
            input_id,
            component,
            None,
            None,
            AnalysisMissingness.MISSING,
            missingness_reason=AnalysisMissingnessReason.NO_OBSERVATION,
        )
    eligible = tuple(
        item
        for item in measurements
        if item.is_selected
        and item.disposition in {"included_source", "included_correction"}
        and item.effective_value is not None
    )
    if not eligible:
        return AnalysisInputValue(
            day,
            input_id,
            component,
            measurements[0].unit,
            None,
            AnalysisMissingness.MISSING,
            _source_evidence(measurements),
            quality_ids,
            AnalysisMissingnessReason.EXCLUDED_OR_UNRESOLVED,
            quality_status,
        )
    if input_id in _POINT_INPUTS:
        latest = max(item.source_start for item in eligible)
        used = tuple(item for item in eligible if item.source_start == latest)
        distinct = {item.effective_value for item in used}
        if len(distinct) != 1:
            return AnalysisInputValue(
                day,
                input_id,
                component,
                used[0].unit,
                None,
                AnalysisMissingness.AMBIGUOUS,
                _source_evidence(used),
                quality_ids,
                AnalysisMissingnessReason.CONFLICTING_VALUES,
                quality_status,
            )
        value = next(iter(distinct))
    else:
        used = eligible
        value = sum(item.effective_value or 0.0 for item in used)
    return AnalysisInputValue(
        day,
        input_id,
        component,
        used[0].unit,
        value,
        AnalysisMissingness.OBSERVED,
        _source_evidence(used),
        quality_ids,
        quality_status=quality_status,
    )


def _workout_value(
    day: date,
    input_id: AnalysisInput,
    component: str | None,
    workouts: tuple[StoredWorkout, ...],
) -> AnalysisInputValue:
    unit = (
        CanonicalUnit.MINUTE
        if input_id is AnalysisInput.WORKOUT_DURATION_BY_TYPE
        else CanonicalUnit.KILOCALORIE
    )
    if not workouts and component is not None:
        return AnalysisInputValue(
            day,
            input_id,
            component,
            unit,
            0.0,
            AnalysisMissingness.OBSERVED,
        )
    quality_ids = tuple(
        sorted({case for item in workouts for case in item.review_case_ids}, key=str)
    )
    quality_status = DataQualityStatus.PROVISIONAL if quality_ids else DataQualityStatus.REVIEWED
    eligible = tuple(
        item
        for item in workouts
        if item.is_selected
        and (item.disposition is None or item.disposition.startswith("included"))
    )
    if not eligible:
        return AnalysisInputValue(
            day,
            input_id,
            component,
            unit,
            None,
            AnalysisMissingness.MISSING,
            _workout_source_evidence(workouts),
            quality_ids,
            (
                AnalysisMissingnessReason.EXCLUDED_OR_UNRESOLVED
                if workouts
                else AnalysisMissingnessReason.NO_OBSERVATION
            ),
            quality_status,
        )
    if input_id is AnalysisInput.WORKOUT_DURATION_BY_TYPE:
        values = tuple(
            item.effective_duration_minutes
            if item.effective_duration_minutes is not None
            else item.reported_duration_minutes
            if item.reported_duration_minutes is not None
            else (item.source_end - item.source_start).total_seconds() / 60
            for item in eligible
        )
    else:
        values = tuple(
            item.active_energy_kilocalories
            for item in eligible
            if item.active_energy_kilocalories is not None
        )
    if not values:
        return AnalysisInputValue(
            day,
            input_id,
            component,
            unit,
            None,
            AnalysisMissingness.MISSING,
            _workout_source_evidence(eligible),
            quality_ids,
            AnalysisMissingnessReason.NO_OBSERVATION,
            quality_status,
        )
    partial = len(values) != len(eligible)
    return AnalysisInputValue(
        day,
        input_id,
        component,
        unit,
        sum(values),
        AnalysisMissingness.PARTIAL if partial else AnalysisMissingness.OBSERVED,
        _workout_source_evidence(eligible),
        quality_ids,
        AnalysisMissingnessReason.PARTIAL_OBSERVATION if partial else None,
        quality_status=quality_status,
    )


def _activity_value(
    day: date,
    input_id: AnalysisInput,
    activity: StoredActivityDayValue | None,
    measurements: tuple[StoredMeasurement, ...],
) -> AnalysisInputValue:
    if activity is None:
        quality_ids = tuple(
            sorted(
                {case for item in measurements for case in item.review_case_ids},
                key=str,
            )
        )
        return AnalysisInputValue(
            day,
            input_id,
            None,
            None if not measurements else measurements[0].unit,
            None,
            AnalysisMissingness.MISSING,
            _source_evidence(measurements),
            quality_ids,
            (
                AnalysisMissingnessReason.EXCLUDED_OR_UNRESOLVED
                if measurements
                else AnalysisMissingnessReason.NO_OBSERVATION
            ),
            (DataQualityStatus.PROVISIONAL if quality_ids else DataQualityStatus.REVIEWED),
        )
    contributor_ids = set(activity.measurement_version_ids)
    contributors = tuple(
        item for item in measurements if item.measurement_version_id in contributor_ids
    )
    quality_ids = tuple(
        sorted(
            {case for item in contributors for case in item.review_case_ids},
            key=str,
        )
    )
    return AnalysisInputValue(
        day,
        input_id,
        None,
        activity.unit,
        activity.effective_value,
        AnalysisMissingness.OBSERVED,
        _source_evidence(contributors),
        quality_ids,
        quality_status=(DataQualityStatus.PROVISIONAL if quality_ids else activity.quality_status),
    )


_ILLNESS_SEVERITY = {None: 0.0, "mild": 1.0, "moderate": 2.0, "severe": 3.0}
_STRESS_DEVIATION = {
    "very_low": -2.0,
    "low": -1.0,
    "average": 0.0,
    "high": 1.0,
    "very_high": 2.0,
}
_CONTEXT_RULE_VERSIONS = (
    "sleep-episode/v1",
    "sleep-night/v1",
    "daily-context/v1",
    "medication-context/v1",
    "outcome-context-encoding/v1",
    "analysis-complete-case/v1",
)


def _context_measurement_source_evidence(
    sources: tuple[StoredAnalysisSourceEvidence, ...],
) -> tuple[AnalysisSourceEvidence, ...]:
    return tuple(
        AnalysisSourceEvidence(
            source.source_record_id,
            source.source_name,
            source.source_version,
            source.source_updated_at,
            True,
            source.disposition,
        )
        for source in sources
        if isinstance(source.source_record_id, MeasurementVersionId)
    )


def _context_source_records(
    sources: tuple[StoredAnalysisSourceEvidence, ...],
) -> tuple[AnalysisSourceRecordEvidence, ...]:
    return tuple(
        AnalysisSourceRecordEvidence(
            (
                AnalysisSourceRecordKind.MEASUREMENT_VERSION
                if isinstance(source.source_record_id, MeasurementVersionId)
                else AnalysisSourceRecordKind.CONTEXT_REVISION
                if isinstance(source.source_record_id, ContextRevisionId)
                else AnalysisSourceRecordKind.MEDICATION_REVISION
            ),
            source.source_record_id,
            source.source_name,
            source.source_version,
            source.source_updated_at,
            True,
            source.disposition,
        )
        for source in sources
    )


def _outcome_context_values(
    day: date,
    context: StoredOutcomeContextDay | None,
    medication_segments: dict[MedicationRevisionId, float],
) -> tuple[AnalysisInputValue, ...]:
    if context is None:
        return tuple(
            AnalysisInputValue(
                day,
                AnalysisInput.OUTCOME_DAY_CONTEXT,
                component,
                unit,
                None,
                AnalysisMissingness.MISSING,
                missingness_reason=AnalysisMissingnessReason.INPUT_NOT_AVAILABLE,
            )
            for component, unit in (
                ("sleep_duration_minutes", CanonicalUnit.MINUTE),
                ("sleep_observation_status", None),
                ("illness_severity", None),
                ("stress_deviation", None),
                ("stress_origin_observed", None),
                ("medication_regime_segment", None),
                ("medication_deviation", None),
                ("medication_as_needed_intake", None),
            )
        )

    sleep_status = context.sleep_status
    if sleep_status == "observed":
        boundaries = sorted(
            {
                value
                for interval in context.sleep_intervals
                for value in (interval.start, interval.end)
            }
        )
        asleep_categories = {
            CanonicalSleepCategory.ASLEEP_UNSPECIFIED,
            CanonicalSleepCategory.ASLEEP_CORE,
            CanonicalSleepCategory.ASLEEP_DEEP,
            CanonicalSleepCategory.ASLEEP_REM,
        }
        for left, right in pairwise(boundaries):
            active = {
                interval.category
                for interval in context.sleep_intervals
                if interval.start <= left and interval.end >= right
            }
            asleep = active & asleep_categories
            if (
                not active
                or len(asleep) > 1
                or (asleep and CanonicalSleepCategory.AWAKE in active)
            ):
                sleep_status = "partial"
                break
    sleep_quality_status = (
        DataQualityStatus.PROVISIONAL
        if sleep_status == "partial"
        else context.sleep_quality_status
    )
    sleep_missingness = (
        AnalysisMissingness.PARTIAL
        if sleep_status == "partial"
        else AnalysisMissingness.MISSING
        if context.sleep_minutes is None
        else AnalysisMissingness.OBSERVED
    )
    sleep_reason = (
        AnalysisMissingnessReason.PARTIAL_OBSERVATION
        if sleep_status == "partial"
        else AnalysisMissingnessReason.NO_OBSERVATION
        if context.sleep_minutes is None
        else None
    )
    illness_known = context.illness_origin != "unknown"
    stress_known = context.stress_origin != "unknown"
    medication_known = context.medication_regime_revision_id is not None
    values = (
        (
            "sleep_duration_minutes",
            CanonicalUnit.MINUTE,
            context.sleep_minutes,
            sleep_missingness,
            sleep_reason,
            sleep_quality_status,
            context.sleep_source_evidence,
        ),
        (
            "sleep_observation_status",
            None,
            {"unobserved": 0.0, "partial": 1.0, "observed": 2.0}[sleep_status],
            AnalysisMissingness.OBSERVED,
            None,
            sleep_quality_status,
            context.sleep_source_evidence,
        ),
        (
            "illness_severity",
            None,
            _ILLNESS_SEVERITY.get(context.illness_severity) if illness_known else None,
            AnalysisMissingness.OBSERVED if illness_known else AnalysisMissingness.MISSING,
            None if illness_known else AnalysisMissingnessReason.NO_OBSERVATION,
            context.context_quality_status,
            context.context_source_evidence,
        ),
        (
            "stress_deviation",
            None,
            _STRESS_DEVIATION.get(context.stress_level)
            if stress_known and context.stress_level is not None
            else None,
            AnalysisMissingness.OBSERVED if stress_known else AnalysisMissingness.MISSING,
            None if stress_known else AnalysisMissingnessReason.NO_OBSERVATION,
            context.context_quality_status,
            context.context_source_evidence,
        ),
        (
            "stress_origin_observed",
            None,
            float(context.stress_origin == "observed") if stress_known else None,
            AnalysisMissingness.OBSERVED if stress_known else AnalysisMissingness.MISSING,
            None if stress_known else AnalysisMissingnessReason.NO_OBSERVATION,
            context.context_quality_status,
            context.context_source_evidence,
        ),
        (
            "medication_regime_segment",
            None,
            medication_segments.get(context.medication_regime_revision_id)
            if medication_known and context.medication_regime_revision_id is not None
            else None,
            AnalysisMissingness.OBSERVED if medication_known else AnalysisMissingness.MISSING,
            None if medication_known else AnalysisMissingnessReason.NO_OBSERVATION,
            context.medication_quality_status,
            context.medication_source_evidence,
        ),
        (
            "medication_deviation",
            None,
            float(context.medication_deviation) if medication_known else None,
            AnalysisMissingness.OBSERVED if medication_known else AnalysisMissingness.MISSING,
            None if medication_known else AnalysisMissingnessReason.NO_OBSERVATION,
            context.medication_quality_status,
            context.medication_source_evidence,
        ),
        (
            "medication_as_needed_intake",
            None,
            float(context.medication_as_needed_intake) if medication_known else None,
            AnalysisMissingness.OBSERVED if medication_known else AnalysisMissingness.MISSING,
            None if medication_known else AnalysisMissingnessReason.NO_OBSERVATION,
            context.medication_quality_status,
            context.medication_source_evidence,
        ),
    )
    return tuple(
        AnalysisInputValue(
            day,
            AnalysisInput.OUTCOME_DAY_CONTEXT,
            component,
            unit,
            value,
            missingness,
            _context_measurement_source_evidence(sources),
            missingness_reason=reason,
            quality_status=quality_status,
            source_records=_context_source_records(sources),
        )
        for component, unit, value, missingness, reason, quality_status, sources in values
    )


def build_analysis_input_bundle(
    plan: RunAnalysisPlan,
    analysis_run_id: AnalysisRunId,
    measurements: tuple[StoredMeasurement, ...],
    workouts: tuple[StoredWorkout, ...] = (),
    activity_values: tuple[StoredActivityDayValue, ...] = (),
    outcome_context_days: tuple[StoredOutcomeContextDay, ...] = (),
    activity_coverage_incomplete: bool = False,
    plausibility_rules: tuple[PlausibilityRuleRecord, ...] = (),
    activity_derivation: ActivityDerivationRecord | None = None,
) -> AnalysisInputBundle:
    if (
        plan.base_snapshot_ref is None
        or plan.eligible_start_date is None
        or plan.eligible_end_date is None
    ):
        raise ValueError("Analyseplan besitzt keinen ausführbaren Eingangszeitraum.")
    calendar = tuple(
        plan.eligible_start_date + timedelta(days=offset)
        for offset in range((plan.eligible_end_date - plan.eligible_start_date).days + 1)
    )
    grouped: dict[tuple[date, AnalysisInput, str | None], list[StoredMeasurement]] = {}
    for measurement in measurements:
        mapped = _MEASUREMENT_INPUTS.get(measurement.data_type.value)
        if mapped is not None:
            input_id, component = mapped
            grouped.setdefault((measurement.measurement_local_day, input_id, component), []).append(
                measurement
            )
    included_workouts = tuple(
        item
        for item in workouts
        if item.is_selected
        and (item.disposition is None or item.disposition.startswith("included"))
    )
    positive_days_by_type: dict[str, set[date]] = {}
    for workout in included_workouts:
        duration = (
            workout.effective_duration_minutes
            if workout.effective_duration_minutes is not None
            else workout.reported_duration_minutes
            if workout.reported_duration_minutes is not None
            else (workout.source_end - workout.source_start).total_seconds() / 60
        )
        if duration > 0:
            positive_days_by_type.setdefault(workout.original_activity_type, set()).add(
                workout.measurement_local_day
            )
    analytical_type = {
        workout_type: workout_type if len(days) >= 10 else "other"
        for workout_type, days in positive_days_by_type.items()
    }
    workout_components = tuple(sorted(set(analytical_type.values())))
    workouts_by_day_and_type: dict[tuple[date, str], list[StoredWorkout]] = {}
    for workout in workouts:
        component = analytical_type.get(workout.original_activity_type, "other")
        workouts_by_day_and_type.setdefault(
            (workout.measurement_local_day, component), []
        ).append(workout)
    activity_by_day_and_input = {
        (item.day, _MEASUREMENT_INPUTS[item.data_type.value][0]): item for item in activity_values
    }
    context_by_day = {item.day: item for item in outcome_context_days}
    medication_segments = {
        revision_id: float(index)
        for index, revision_id in enumerate(
            dict.fromkeys(
                item.medication_regime_revision_id
                for item in outcome_context_days
                if item.medication_regime_revision_id is not None
            )
        )
    }
    required = plan.analysis_definition.method_facts.inputs
    values: list[AnalysisInputValue] = []
    for day in calendar:
        for input_id in required:
            deterministic = _calendar_values(day, input_id)
            if deterministic:
                values.extend(
                    AnalysisInputValue(
                        day,
                        input_id,
                        component,
                        None,
                        value,
                        AnalysisMissingness.OBSERVED,
                    )
                    for component, value in deterministic
                )
                continue
            if input_id in _WORKOUT_INPUTS:
                components: tuple[str | None, ...] = workout_components or (None,)
                values.extend(
                    _workout_value(
                        day,
                        input_id,
                        component,
                        tuple(workouts_by_day_and_type.get((day, component), ()))
                        if component is not None
                        else (),
                    )
                    for component in components
                )
                continue
            if input_id in _ACTIVITY_INPUTS:
                values.append(
                    _activity_value(
                        day,
                        input_id,
                        activity_by_day_and_input.get((day, input_id)),
                        tuple(grouped.get((day, input_id, None), ())),
                    )
                )
                continue
            if input_id is AnalysisInput.OUTCOME_DAY_CONTEXT:
                values.extend(
                    _outcome_context_values(
                        day,
                        context_by_day.get(day),
                        medication_segments,
                    )
                )
                continue
            if input_id not in _MEASUREMENT_BACKED_INPUTS:
                values.append(
                    AnalysisInputValue(
                        day,
                        input_id,
                        None,
                        None,
                        None,
                        AnalysisMissingness.MISSING,
                        missingness_reason=AnalysisMissingnessReason.INPUT_NOT_AVAILABLE,
                    )
                )
                continue
            components = _FIXED_INPUT_COMPONENTS.get(input_id, (None,))
            values.extend(
                _measurement_value(
                    day,
                    input_id,
                    component,
                    tuple(grouped.get((day, input_id, component), ())),
                )
                for component in components
            )
    scale_keys = sorted(
        {(item.input_id, item.component) for item in values},
        key=lambda item: (item[0].value, item[1] or ""),
    )
    scalings: list[AnalysisScaling] = []
    for input_id, component in scale_keys:
        scalings.append(
            AnalysisScaling(
                input_id,
                component,
                None,
                AnalysisScalingStatus.UNAVAILABLE,
            )
        )
    quality_ids = tuple(
        sorted(
            {fact_id for item in measurements for fact_id in item.review_case_ids}
            | {fact_id for item in workouts for fact_id in item.review_case_ids},
            key=str,
        )
    )
    rule_versions = ["healthkit-identity/v3", "healthkit-canonical/v3"]
    rule_versions.extend(
        f"plausibility/{rule.data_type}/{rule.version_id}" for rule in plausibility_rules
    )
    if activity_derivation is not None and any(item in _ACTIVITY_INPUTS for item in required):
        rule_versions.extend(
            (
                activity_derivation.version_id,
                activity_derivation.source_classifier_version,
            )
        )
    if any(item in _WORKOUT_INPUTS for item in required):
        rule_versions.append("analytical-workout-type/v1")
    if AnalysisInput.OUTCOME_DAY_CONTEXT in required:
        rule_versions.extend(_CONTEXT_RULE_VERSIONS)
    rule_versions.extend(
        version for input_id, version in _BUILTIN_RULE_VERSIONS.items() if input_id in required
    )
    return AnalysisInputBundle(
        analysis_run_id,
        plan.base_snapshot_ref,
        plan.analysis_definition.analysis_definition_id,
        plan.eligible_start_date,
        plan.eligible_end_date,
        calendar,
        tuple(values),
        tuple(sorted(set(rule_versions))),
        quality_ids,
        tuple(scalings),
        activity_coverage_incomplete,
    )


def _bundle_payload(bundle: AnalysisInputBundle, *, include_run_id: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "activity_coverage_incomplete": bundle.activity_coverage_incomplete,
        "analysis_definition_id": str(bundle.analysis_definition_id),
        "analysis_period": {
            "start_date": bundle.start_date.isoformat(),
            "end_date": bundle.end_date.isoformat(),
        },
        "calendar": [day.isoformat() for day in bundle.calendar],
        "data_quality_fact_ids": [str(item) for item in bundle.data_quality_fact_ids],
        "input_schema_version": bundle.schema_version,
        "rule_versions": list(bundle.rule_versions),
        "scalings": [
            {
                "component": item.component,
                "input_id": item.input_id.value,
                "population_standard_deviation": item.population_standard_deviation,
                "status": item.status.value,
            }
            for item in bundle.scalings
        ],
        "snapshot_id": str(bundle.snapshot_id),
        "values": [
            {
                "component": item.component,
                "data_quality_fact_ids": [str(value) for value in item.data_quality_fact_ids],
                "day": item.day.isoformat(),
                "input_id": item.input_id.value,
                "measurement_version_ids": [
                    str(evidence.measurement_version_id) for evidence in item.source_evidence
                ],
                "missingness": item.missingness.value,
                "missingness_reason": (
                    None if item.missingness_reason is None else item.missingness_reason.value
                ),
                "quality_status": item.quality_status.value,
                "source_evidence": [
                    {
                        "disposition": evidence.disposition,
                        "is_selected": evidence.is_selected,
                        "measurement_version_id": str(evidence.measurement_version_id),
                        "source_name": evidence.source_name,
                        "source_updated_at": evidence.source_updated_at.isoformat(),
                        "source_version": evidence.source_version,
                    }
                    for evidence in item.source_evidence
                ],
                "source_records": [
                    {
                        "disposition": source.disposition,
                        "is_selected": source.is_selected,
                        "source_name": source.source_name,
                        "source_record_id": str(source.source_record_id),
                        "source_record_kind": source.source_record_kind.value,
                        "source_updated_at": source.source_updated_at.isoformat(),
                        "source_version": source.source_version,
                    }
                    for source in item.source_records
                ],
                "unit": None if item.unit is None else item.unit.value,
                "value": item.value,
            }
            for item in bundle.values
        ],
    }
    if include_run_id:
        payload["analysis_run_id"] = str(bundle.analysis_run_id)
    return payload


def _artifact_rows(artifact: AnalysisJsonlArtifact) -> tuple[dict[str, object], ...]:
    if (
        artifact.schema_version <= 0
        or artifact.size_bytes != len(artifact.payload)
        or artifact.sha256 != hashlib.sha256(artifact.payload).hexdigest()
        or len(artifact.content_hash) != 64
        or any(character not in "0123456789abcdef" for character in artifact.content_hash)
        or not artifact.payload.endswith(b"\n")
    ):
        raise StoreError("Analyseartefakt verletzt Größen- oder Hashvertrag.")
    try:
        rows = tuple(json.loads(row) for row in artifact.payload.decode().splitlines())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StoreError("Analyseartefakt verletzt das JSONL-Schema.") from error
    if (
        len(rows) != artifact.row_count
        or not rows
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise StoreError("Analyseartefakt verletzt das JSONL-Schema.")
    return cast(tuple[dict[str, object], ...], rows)


def _validate_input_artifact(
    artifact: AnalysisJsonlArtifact, provenance: AnalysisProvenance
) -> None:
    rows = _artifact_rows(artifact)
    if (
        artifact.schema_id != "analysis-input-bundle"
        or artifact.schema_version != 2
        or len(rows) != 1
    ):
        raise StoreError("Unbekanntes Analyseeingangsschema.")
    record = rows[0]
    period = record.get("analysis_period")
    calendar = record.get("calendar")
    values = record.get("values")
    scalings = record.get("scalings")
    rule_versions = record.get("rule_versions")
    quality_ids = record.get("data_quality_fact_ids")
    if (
        set(record) != _INPUT_KEYS
        or record.get("input_schema_version") != 2
        or record.get("analysis_run_id") != str(provenance.analysis_run_id)
        or record.get("snapshot_id") != str(provenance.snapshot_id)
        or record.get("analysis_definition_id") != str(provenance.analysis_definition_id)
        or not isinstance(record.get("activity_coverage_incomplete"), bool)
        or not isinstance(period, dict)
        or set(period) != {"start_date", "end_date"}
        or not isinstance(calendar, list)
        or not isinstance(values, list)
        or not isinstance(scalings, list)
        or not isinstance(rule_versions, list)
        or not all(isinstance(item, str) for item in rule_versions)
        or not isinstance(quality_ids, list)
        or not all(isinstance(item, str) for item in quality_ids)
    ):
        raise StoreError("Analyseeingang verletzt das Schema.")
    try:
        start = date.fromisoformat(str(period["start_date"]))
        end = date.fromisoformat(str(period["end_date"]))
        days = tuple(date.fromisoformat(str(item)) for item in calendar)
    except ValueError as error:
        raise StoreError("Analyseeingang verletzt das Datumsschema.") from error
    if start > end or days != tuple(sorted(set(days))):
        raise StoreError("Analyseeingang verletzt das Kalenderraster.")
    for value in values:
        if not isinstance(value, dict) or set(value) != _INPUT_VALUE_KEYS:
            raise StoreError("Analysewert verletzt das Schema.")
        evidence = value["source_evidence"]
        source_records = value["source_records"]
        numeric = value["value"]
        if (
            not isinstance(value["day"], str)
            or not isinstance(value["input_id"], str)
            or not value["input_id"]
            or (value["component"] is not None and not isinstance(value["component"], str))
            or (
                value["unit"] is not None
                and value["unit"] not in {item.value for item in CanonicalUnit}
            )
            or (
                numeric is not None
                and (
                    not isinstance(numeric, (int, float))
                    or isinstance(numeric, bool)
                    or not math.isfinite(numeric)
                )
            )
            or value["missingness"] not in {item.value for item in AnalysisMissingness}
            or (
                value["missingness_reason"] is not None
                and value["missingness_reason"]
                not in {item.value for item in AnalysisMissingnessReason}
            )
            or value["quality_status"] not in {item.value for item in DataQualityStatus}
            or not isinstance(evidence, list)
            or not isinstance(source_records, list)
            or not isinstance(value["measurement_version_ids"], list)
            or not all(isinstance(item, str) for item in value["measurement_version_ids"])
            or not isinstance(value["data_quality_fact_ids"], list)
            or not all(isinstance(item, str) for item in value["data_quality_fact_ids"])
        ):
            raise StoreError("Analysewert verletzt das Schema.")
        try:
            date.fromisoformat(value["day"])
        except ValueError as error:
            raise StoreError("Analysewert verletzt das Datumsschema.") from error
        for source in evidence:
            if (
                not isinstance(source, dict)
                or set(source) != _SOURCE_EVIDENCE_KEYS
                or not isinstance(source["measurement_version_id"], str)
                or not isinstance(source["source_name"], str)
                or not isinstance(source["source_version"], str)
                or not isinstance(source["source_updated_at"], str)
                or not isinstance(source["is_selected"], bool)
                or (
                    source["disposition"] is not None and not isinstance(source["disposition"], str)
                )
            ):
                raise StoreError("Analysequellenbeleg verletzt das Schema.")
            try:
                if datetime.fromisoformat(source["source_updated_at"]).tzinfo is None:
                    raise ValueError
            except ValueError as error:
                raise StoreError("Analysequellenbeleg verletzt das Zeitschema.") from error
        for source in source_records:
            source_kind = source.get("source_record_kind") if isinstance(source, dict) else None
            source_id = source.get("source_record_id") if isinstance(source, dict) else None
            if (
                not isinstance(source, dict)
                or set(source) != _SOURCE_RECORD_KEYS
                or source["source_record_kind"]
                not in {item.value for item in AnalysisSourceRecordKind}
                or not isinstance(source["source_record_id"], str)
                or not source["source_record_id"]
                or (
                    source_kind == AnalysisSourceRecordKind.MEASUREMENT_VERSION.value
                    and (
                        len(cast(str, source_id)) != 64
                        or not set(cast(str, source_id)) <= set("0123456789abcdef")
                    )
                )
                or (
                    source_kind
                    in {
                        AnalysisSourceRecordKind.CONTEXT_REVISION.value,
                        AnalysisSourceRecordKind.MEDICATION_REVISION.value,
                    }
                    and (
                        len(cast(str, source_id)) != 32
                        or not set(cast(str, source_id)) <= set("0123456789abcdef")
                    )
                )
                or not isinstance(source["source_name"], str)
                or not isinstance(source["source_version"], str)
                or not isinstance(source["source_updated_at"], str)
                or not isinstance(source["is_selected"], bool)
                or (
                    source["disposition"] is not None
                    and not isinstance(source["disposition"], str)
                )
            ):
                raise StoreError("Analysequellenreferenz verletzt das Schema.")
            try:
                if datetime.fromisoformat(source["source_updated_at"]).tzinfo is None:
                    raise ValueError
            except ValueError as error:
                raise StoreError("Analysequellenreferenz verletzt das Zeitschema.") from error
    for scaling in scalings:
        if not isinstance(scaling, dict) or set(scaling) != _SCALING_KEYS:
            raise StoreError("Analyseskalierung verletzt das Schema.")
        standard_deviation = scaling["population_standard_deviation"]
        if (
            not isinstance(scaling["input_id"], str)
            or not scaling["input_id"]
            or (scaling["component"] is not None and not isinstance(scaling["component"], str))
            or (
                standard_deviation is not None
                and (
                    not isinstance(standard_deviation, (int, float))
                    or isinstance(standard_deviation, bool)
                    or not math.isfinite(standard_deviation)
                    or standard_deviation <= 0
                )
            )
            or scaling["status"] not in {item.value for item in AnalysisScalingStatus}
        ):
            raise StoreError("Analyseskalierung verletzt das Schema.")
    content = dict(record)
    del content["analysis_run_id"]
    if (
        hashlib.sha256(
            json.dumps(content, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        != artifact.content_hash
    ):
        raise StoreError("Analyseeingang verletzt den Inhaltshash.")


def _validate_input_publication(publication: AnalysisRunPublication) -> None:
    _validate_input_artifact(publication.input_artifact, publication.provenance)


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _validate_result_item(field: str, item: object) -> None:
    if not isinstance(item, dict) or set(item) != _RESULT_ITEM_KEYS[field]:
        raise StoreError("Analyseergebnis verletzt das geschlossene Familienschema.")
    if not _is_json_value(item):
        raise StoreError("Analyseergebnis enthält keinen endlichen JSON-Wert.")
    code = item.get("code")
    if field == "diagnostics" and code not in {value.value for value in AnalysisDiagnostic}:
        raise StoreError("Analyseergebnis enthält einen unbekannten Diagnosecode.")
    if field == "maturity_criteria" and code not in {
        value.value for value in AnalysisMaturityCriterionCode
    }:
        raise StoreError("Analyseergebnis enthält einen unbekannten Reifekriteriumscode.")
    if field == "bootstrap_facts" and item.get("variant") not in {
        value.value for value in AnalysisBootstrapVariant
    }:
        raise StoreError("Analyseergebnis enthält eine unbekannte Bootstrap-Variante.")
    if field in {"models", "predictions"} and item.get("model_family") not in {
        value.value for value in WeightModelFamily
    }:
        raise StoreError("Analyseergebnis enthält eine unbekannte Modellfamilie.")
    if field == "trends" and (
        item.get("support_status") not in {value.value for value in WeightTrendSupportStatus}
        or item.get("failure_reason")
        not in {None, *(value.value for value in WeightTrendFailureReason)}
    ):
        raise StoreError("Analyseergebnis enthält einen unbekannten Trendstatus.")
    for key, value in item.items():
        if key in {
            "code",
            "feature_id",
            "measure",
            "model_family",
            "natural_unit",
            "support_status",
            "variant",
        }:
            if not isinstance(value, str) or not value:
                raise StoreError("Analyseergebnis verletzt das geschlossene Familienschema.")
        elif key in {
            "lag_day",
            "start_day",
            "end_day",
            "window_days",
            "attempts",
            "block_length",
            "seed",
            "successful_refits",
            "paired_days",
            "local_support",
        }:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise StoreError("Analyseergebnis verletzt das geschlossene Familienschema.")
        elif (
            (key == "passed" and not isinstance(value, bool))
            or (
                key in {"observed_value", "threshold"}
                and (
                    not isinstance(value, (int, float, str))
                    or isinstance(value, bool)
                    or (isinstance(value, float) and not math.isfinite(value))
                    or (isinstance(value, str) and not value)
                )
            )
            or (
                key == "failure_counts"
                and (
                    not isinstance(value, dict)
                    or not all(
                        isinstance(code, str)
                        and code
                        and isinstance(count, int)
                        and not isinstance(count, bool)
                        and count >= 0
                        for code, count in value.items()
                    )
                )
            )
            or (
                key == "facts"
                and (
                    not isinstance(value, dict)
                    or not all(isinstance(name, str) and name for name in value)
                )
            )
        ):
            raise StoreError("Analyseergebnis verletzt das geschlossene Familienschema.")
        elif (
            key
            in {
                "blocked_prediction_gain",
                "coefficient",
                "estimate",
                "estimate_bpm_per_natural_scale",
                "estimate_bpm_per_personal_sd",
                "estimate_kg_per_week_per_natural_scale",
                "estimate_kg_per_week_per_personal_sd",
                "level_kg",
                "natural_scale",
                "numerical_pivot",
                "observed_kg_per_week",
                "predicted_kg_per_week",
                "quantile_stability",
                "rate_kg_per_week",
                "residual_kg_per_week",
            }
            and value is not None
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                raise StoreError("Analyseergebnis verletzt das geschlossene Familienschema.")
        elif key in {"pointwise_interval", "simultaneous_band"}:
            if value is None and field in {"lag_estimates", "contrasts"}:
                continue
            if not isinstance(value, dict) or set(value) != {"lower", "upper"}:
                raise StoreError("Analyseergebnis verletzt das geschlossene Familienschema.")
            bounds = tuple(value.values())
            if any(
                not isinstance(bound, (int, float))
                or isinstance(bound, bool)
                or not math.isfinite(bound)
                for bound in bounds
            ):
                raise StoreError("Analyseergebnis verletzt das geschlossene Familienschema.")
            interval = cast(dict[str, float], value)
            if interval["lower"] > interval["upper"]:
                raise StoreError("Analyseergebnisintervall ist umgekehrt.")
        elif key == "day":
            if not isinstance(value, str):
                raise StoreError("Analyseergebnis verletzt das Datumsschema.")
            try:
                date.fromisoformat(value)
            except ValueError as error:
                raise StoreError("Analyseergebnis verletzt das Datumsschema.") from error


def _validate_result_artifact(publication: AnalysisRunPublication) -> None:
    artifact = publication.result_artifact
    result_id = publication.provenance.result_id
    try:
        family = AnalysisResultFamily(str(publication.result_family))
    except ValueError as error:
        raise StoreError("Unbekannte Analyseergebnisfamilie.") from error
    definition = next(
        (
            item
            for item in analysis_definitions()
            if item.analysis_definition_id == publication.provenance.analysis_definition_id
        ),
        None,
    )
    if (
        artifact is None
        or result_id is None
        or definition is None
        or definition.result_family is not family
        or artifact.schema_id != f"analysis-result-{family.value}"
        or artifact.schema_version != 1
    ):
        raise StoreError("Unbekanntes Analyseergebnisschema.")
    rows = _artifact_rows(artifact)
    list_fields = _RESULT_LIST_FIELDS[family]
    expected_keys = list_fields | {
        "analysis_definition_id",
        "analysis_result_id",
        "analysis_run_id",
        "result_family",
        "result_schema_version",
    }
    if len(rows) != 1:
        raise StoreError("Analyseergebnis muss genau einen JSONL-Datensatz enthalten.")
    record = rows[0]
    if (
        set(record) != expected_keys
        or record.get("analysis_run_id") != str(publication.provenance.analysis_run_id)
        or record.get("analysis_result_id") != str(result_id)
        or record.get("analysis_definition_id") != str(definition.analysis_definition_id)
        or record.get("result_family") != family.value
        or record.get("result_schema_version") != 1
        or any(not isinstance(record.get(field), list) for field in list_fields)
        or not record.get(_RESULT_PRIMARY_FIELDS[family])
    ):
        raise StoreError("Analyseergebnis verletzt das geschlossene Familienschema.")
    for field in list_fields:
        for item in cast(list[object], record[field]):
            _validate_result_item(field, item)
    content = dict(record)
    del content["analysis_run_id"]
    del content["analysis_result_id"]
    if (
        hashlib.sha256(
            json.dumps(content, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        != artifact.content_hash
    ):
        raise StoreError("Analyseergebnis verletzt den Inhaltshash.")


def _publish_analysis_run(store: LocalStore, publication: AnalysisRunPublication) -> None:
    _validate_input_artifact(publication.input_artifact, publication.provenance)
    if publication.result_artifact is not None:
        _validate_result_artifact(publication)
    staging = store.prepare_analysis_staging(publication.provenance.analysis_run_id)
    paths = [(staging / "input.jsonl", publication.input_artifact.payload)]
    if publication.result_artifact is not None:
        result = staging / "result"
        result.mkdir()
        paths.append((result / "result.jsonl", publication.result_artifact.payload))
    for path, payload in paths:
        with path.open("xb") as artifact:
            artifact.write(payload)
            artifact.flush()
            os.fsync(artifact.fileno())
    store.persist_analysis_run(publication)


def _reproduction_facts() -> tuple[str, bool, str | None, str]:
    def git(*arguments: str) -> bytes:
        return subprocess.run(
            ("git", *arguments), cwd=_PROJECT_ROOT, check=True, capture_output=True
        ).stdout

    code_commit = git("rev-parse", "HEAD").decode("ascii").strip()
    tracked_diff = git("diff", "--binary", "HEAD")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z")
    diff_hasher = hashlib.sha256(tracked_diff)
    for relative in sorted(path for path in untracked.split(b"\0") if path):
        diff_hasher.update(b"\0" + relative + b"\0")
        path = _PROJECT_ROOT / os.fsdecode(relative)
        diff_hasher.update(os.readlink(path).encode() if path.is_symlink() else path.read_bytes())
    environment_hash = hashlib.sha256((_PROJECT_ROOT / "uv.lock").read_bytes()).hexdigest()
    return (
        code_commit,
        bool(tracked_diff or untracked),
        diff_hasher.hexdigest() if tracked_diff or untracked else None,
        environment_hash,
    )


def derive_current_analysis_reproducibility(
    store: LocalStore, facts: tuple[AnalysisRunFact, ...]
) -> tuple[ReproducibilityStatus, ...]:
    current_development: tuple[str, bool, str | None, str] | None = None
    statuses: list[ReproducibilityStatus] = []
    for fact in facts:
        available = store.analysis_run_material_is_available(fact)
        try:
            if fact.code_dirty:
                if current_development is None:
                    current_development = _reproduction_facts()
                available = available and current_development == (
                    fact.code_commit,
                    True,
                    fact.code_diff_hash,
                    fact.environment_lock_hash,
                )
            else:
                lock = subprocess.run(
                    ("git", "show", f"{fact.code_commit}:uv.lock"),
                    cwd=_PROJECT_ROOT,
                    check=True,
                    capture_output=True,
                ).stdout
                available = (
                    available
                    and fact.code_diff_hash is None
                    and hashlib.sha256(lock).hexdigest() == fact.environment_lock_hash
                )
        except (OSError, subprocess.CalledProcessError):
            available = False
        statuses.append(
            ReproducibilityStatus.LOCAL_DEVELOPMENT
            if available and fact.code_dirty
            else ReproducibilityStatus.REPRODUCIBLE
            if available
            else ReproducibilityStatus.NOT_RECORDED
        )
    return tuple(statuses)


_LAG_FEATURE_INPUTS = _ACTIVITY_INPUTS | _WORKOUT_INPUTS
_LAG_NATURAL_SCALES = {
    AnalysisInput.ACTIVE_ENERGY: (100.0, CanonicalUnit.KILOCALORIE),
    AnalysisInput.TRAINING_TIME: (10.0, CanonicalUnit.MINUTE),
    AnalysisInput.STEPS: (1_000.0, CanonicalUnit.COUNT),
    AnalysisInput.WALKING_RUNNING_DISTANCE: (1.0, CanonicalUnit.KILOMETER),
    AnalysisInput.WORKOUT_DURATION_BY_TYPE: (10.0, CanonicalUnit.MINUTE),
    AnalysisInput.WORKOUT_ENERGY_BY_TYPE: (100.0, CanonicalUnit.KILOCALORIE),
}


@dataclass(frozen=True, slots=True)
class _LagPointOutcome:
    bundle: AnalysisInputBundle
    status: Literal["completed", "insufficient_data", "unstable"]
    diagnostics: tuple[str, ...]
    lag_estimates: tuple[dict[str, object], ...] = ()
    contrasts: tuple[dict[str, object], ...] = ()
    result_diagnostics: tuple[dict[str, object], ...] = ()


def _feature_id(input_id: AnalysisInput, component: str | None) -> str:
    return input_id.value if component is None else f"{input_id.value}:{component}"


def _lag_basis(horizon: int, nodes: int) -> np.ndarray:
    lags = np.arange(horizon, dtype=float)
    knots = np.linspace(0.0, horizon - 1.0, nodes)
    identity = np.eye(nodes)
    return np.column_stack(
        [np.interp(lags, knots, identity[:, index]) for index in range(nodes)]
    )


def _with_scalings(
    bundle: AnalysisInputBundle,
    feature_keys: tuple[tuple[AnalysisInput, str | None], ...],
    deviations: np.ndarray | None,
) -> AnalysisInputBundle:
    positions = {key: index for index, key in enumerate(feature_keys)}
    scalings = tuple(
        AnalysisScaling(
            scaling.input_id,
            scaling.component,
            (
                None
                if deviations is None
                or not math.isfinite(float(deviations[positions[key]]))
                or deviations[positions[key]] <= 0
                else float(deviations[positions[key]])
            ),
            (
                AnalysisScalingStatus.INSUFFICIENT_OBSERVATIONS
                if deviations is None
                or not math.isfinite(float(deviations[positions[key]]))
                or deviations[positions[key]] <= 0
                else AnalysisScalingStatus.OBSERVED
            ),
        )
        if (key := (scaling.input_id, scaling.component)) in positions
        else scaling
        for scaling in bundle.scalings
    )
    return replace(bundle, scalings=scalings)


def _with_context_scalings(
    bundle: AnalysisInputBundle,
    context_columns: tuple[str, ...],
    context_deviations: tuple[float, ...],
) -> AnalysisInputBundle:
    scalings = bundle.scalings + tuple(
        AnalysisScaling(
            AnalysisInput.OUTCOME_DAY_CONTEXT,
            component,
            deviation,
            AnalysisScalingStatus.OBSERVED,
        )
        for component, deviation in zip(
            context_columns, context_deviations, strict=True
        )
    )
    return replace(bundle, scalings=scalings)


@dataclass(frozen=True, slots=True)
class _LagVariantFit:
    status: Literal["completed", "insufficient_data", "unstable"]
    diagnostics: tuple[str, ...]
    feature_keys: tuple[tuple[AnalysisInput, str | None], ...]
    rows: tuple[int, ...] = ()
    deviations: np.ndarray | None = None
    estimates: np.ndarray | None = None
    context_columns: tuple[str, ...] = ()
    context_deviations: tuple[float, ...] = ()
    matrix_columns: int = 0
    rank: int = 0


def _short_lag_variant_fit(
    bundle: AnalysisInputBundle,
    method: LagProfileMethodFacts,
    context_lags: tuple[int, ...],
) -> _LagVariantFit:
    horizon = 7
    values = {(item.day, item.input_id, item.component): item for item in bundle.values}
    feature_keys = tuple(
        sorted(
            {
                (item.input_id, item.component)
                for item in bundle.values
                if item.input_id in _LAG_FEATURE_INPUTS
                and (item.input_id not in _WORKOUT_INPUTS or item.component is not None)
            },
            key=lambda item: (item[0].value, item[1] or ""),
        )
    )
    context_keys = tuple(
        sorted(
            {
                item.component
                for item in bundle.values
                if item.input_id is AnalysisInput.OUTCOME_DAY_CONTEXT and item.component is not None
            }
        )
    )
    medication_segments = tuple(
        sorted(
            {
                int(item.value)
                for item in bundle.values
                if item.input_id is AnalysisInput.OUTCOME_DAY_CONTEXT
                and item.component == "medication_regime_segment"
                and item.value is not None
            }
        )
    )
    context_specs = tuple(
        (lag, component, segment)
        for lag in context_lags
        for component in context_keys
        for segment in (
            medication_segments[1:]
            if component == "medication_regime_segment"
            else (None,)
        )
    )
    rows: list[int] = []
    lagged_rows: list[list[list[float]]] = []
    context_rows: list[list[float]] = []
    outcomes: list[float] = []
    for outcome_index in range(horizon, len(bundle.calendar)):
        outcome = values.get(
            (
                bundle.calendar[outcome_index],
                AnalysisInput.APPLE_RESTING_HEART_RATE,
                None,
            )
        )
        lagged_values = [
            [
                values.get((bundle.calendar[outcome_index - lag], input_id, component))
                for input_id, component in feature_keys
            ]
            for lag in range(1, horizon + 1)
        ]
        context_by_key = {
            (lag, component): values.get(
                (
                    bundle.calendar[outcome_index - lag],
                    AnalysisInput.OUTCOME_DAY_CONTEXT,
                    component,
                )
            )
            for lag in context_lags
            for component in context_keys
        }
        complete = (
            outcome is not None
            and outcome.value is not None
            and math.isfinite(outcome.value)
            and all(
                item is not None and item.value is not None and math.isfinite(item.value)
                for lag in lagged_values
                for item in lag
            )
            and all(
                item is not None and item.value is not None and math.isfinite(item.value)
                for item in context_by_key.values()
            )
        )
        if complete:
            rows.append(outcome_index)
            outcomes.append(cast(float, cast(AnalysisInputValue, outcome).value))
            lagged_rows.append(
                [
                    [cast(float, cast(AnalysisInputValue, item).value) for item in lag]
                    for lag in lagged_values
                ]
            )
            context_rows.append(
                [
                    float(
                        cast(
                            AnalysisInputValue,
                            context_by_key[(lag, component)],
                        ).value
                        == segment
                    )
                    if segment is not None
                    else cast(
                        float,
                        cast(AnalysisInputValue, context_by_key[(lag, component)]).value,
                    )
                    for lag, component, segment in context_specs
                ]
            )
    if not rows or not feature_keys:
        return _LagVariantFit(
            "insufficient_data",
            ("insufficient_complete_rows",),
            feature_keys,
        )
    lagged = np.asarray(lagged_rows, dtype=float)
    deviations = np.std(lagged, axis=(0, 1))
    if np.any(~np.isfinite(deviations)) or np.any(deviations <= 0):
        return _LagVariantFit(
            "insufficient_data",
            ("insufficient_activity_scaling",),
            feature_keys,
            tuple(rows),
            deviations,
        )
    means = np.mean(lagged, axis=(0, 1))
    standardized = (lagged - means) / deviations
    basis = _lag_basis(horizon, method.lag_basis_nodes)
    exposure_design = np.concatenate(
        [standardized[:, :, feature] @ basis for feature in range(len(feature_keys))],
        axis=1,
    )
    context_names = tuple(
        (
            f"{component}={segment}@day-{lag}"
            if segment is not None
            else f"{component}@day-{lag}"
        )
        for lag, component, segment in context_specs
    )
    context_design = np.empty((len(rows), 0))
    context_columns: tuple[str, ...] = ()
    context_deviations: tuple[float, ...] = ()
    if context_names:
        raw_context = np.asarray(context_rows, dtype=float)
        raw_deviations = np.std(raw_context, axis=0)
        selected = np.isfinite(raw_deviations) & (raw_deviations > 0)
        context_design = (
            raw_context[:, selected] - np.mean(raw_context[:, selected], axis=0)
        ) / raw_deviations[selected]
        context_columns = tuple(
            name for name, included in zip(context_names, selected, strict=True) if included
        )
        context_deviations = tuple(float(value) for value in raw_deviations[selected])
    ordinals = np.asarray([bundle.calendar[index].toordinal() for index in rows], dtype=float)
    weekdays = np.asarray([bundle.calendar[index].weekday() for index in rows])
    day_deviation = float(np.std(ordinals))
    if not math.isfinite(day_deviation) or day_deviation <= 0:
        return _LagVariantFit(
            "unstable",
            ("rank_deficient",),
            feature_keys,
            tuple(rows),
            deviations,
            context_columns=context_columns,
            context_deviations=context_deviations,
        )
    scaled_day = (ordinals - np.mean(ordinals)) / day_deviation
    nuisance = (
        np.ones(len(rows)),
        scaled_day,
        np.sin(2.0 * np.pi * ordinals / 365.2425),
        np.cos(2.0 * np.pi * ordinals / 365.2425),
        *((weekdays == weekday).astype(float) for weekday in range(6)),
    )
    matrix = np.column_stack((exposure_design, *nuisance, context_design))
    rank = int(np.linalg.matrix_rank(matrix))
    if rank < matrix.shape[1]:
        return _LagVariantFit(
            "unstable",
            ("rank_deficient",),
            feature_keys,
            tuple(rows),
            deviations,
            context_columns=context_columns,
            context_deviations=context_deviations,
            matrix_columns=matrix.shape[1],
            rank=rank,
        )
    second_difference = np.diff(np.eye(horizon), n=2, axis=0) @ basis
    per_feature_penalty = np.vstack(
        (
            math.sqrt(method.smoothing_penalty) * second_difference,
            math.sqrt(method.ridge_penalty) * basis,
        )
    )
    penalty = np.zeros((len(feature_keys) * len(per_feature_penalty), matrix.shape[1]))
    for feature in range(len(feature_keys)):
        column = feature * method.lag_basis_nodes
        row = feature * len(per_feature_penalty)
        penalty[
            row : row + len(per_feature_penalty),
            column : column + method.lag_basis_nodes,
        ] = per_feature_penalty
    augmented = np.vstack((matrix, penalty))
    target = np.concatenate((np.asarray(outcomes), np.zeros(len(penalty))))
    coefficients, _, _, _ = np.linalg.lstsq(augmented, target, rcond=None)
    transform = np.zeros((len(feature_keys) * horizon, matrix.shape[1]))
    for feature in range(len(feature_keys)):
        transform[
            feature * horizon : (feature + 1) * horizon,
            feature * method.lag_basis_nodes : (feature + 1) * method.lag_basis_nodes,
        ] = basis
    estimates = (transform @ coefficients).reshape(len(feature_keys), horizon)
    if np.any(~np.isfinite(estimates)):
        return _LagVariantFit(
            "unstable",
            ("non_finite_point_estimate",),
            feature_keys,
            tuple(rows),
            deviations,
            context_columns=context_columns,
            context_deviations=context_deviations,
            matrix_columns=matrix.shape[1],
            rank=rank,
        )
    return _LagVariantFit(
        "completed",
        ("completed_point_estimate",),
        feature_keys,
        tuple(rows),
        deviations,
        estimates,
        context_columns,
        context_deviations,
        matrix.shape[1],
        rank,
    )


def _short_lag_point_fit(
    bundle: AnalysisInputBundle, method: LagProfileMethodFacts
) -> _LagPointOutcome:
    if bundle.analysis_definition_id != AnalysisDefinitionId("rhr-activity-lag-1-7-v1"):
        raise ValueError("Kurzfristiger Fit verlangt die 1-7-Definition.")
    variants = {
        "primary": (0,),
        "context_days_0_2": (0, 1, 2),
        "without_context": (),
    }
    fits = {
        name: _short_lag_variant_fit(bundle, method, context_lags)
        for name, context_lags in variants.items()
    }
    primary = fits["primary"]
    scaled_bundle = _with_context_scalings(
        _with_scalings(bundle, primary.feature_keys, primary.deviations),
        primary.context_columns,
        primary.context_deviations,
    )
    scaled_bundle = replace(
        scaled_bundle,
        scalings=scaled_bundle.scalings
        + tuple(
            AnalysisScaling(
                input_id,
                f"variant:{name}:{component or 'value'}",
                (
                    None
                    if fit.deviations is None
                    or not math.isfinite(float(fit.deviations[index]))
                    or fit.deviations[index] <= 0
                    else float(fit.deviations[index])
                ),
                (
                    AnalysisScalingStatus.INSUFFICIENT_OBSERVATIONS
                    if fit.deviations is None
                    or not math.isfinite(float(fit.deviations[index]))
                    or fit.deviations[index] <= 0
                    else AnalysisScalingStatus.OBSERVED
                ),
            )
            for name, fit in fits.items()
            if name != "primary"
            for index, (input_id, component) in enumerate(fit.feature_keys)
        )
        + tuple(
            AnalysisScaling(
                AnalysisInput.OUTCOME_DAY_CONTEXT,
                f"variant:{name}:{component}",
                deviation,
                AnalysisScalingStatus.OBSERVED,
            )
            for name, fit in fits.items()
            if name != "primary"
            for component, deviation in zip(
                fit.context_columns, fit.context_deviations, strict=True
            )
        ),
    )
    for name, fit in fits.items():
        if fit.status != "completed":
            diagnostics = (
                fit.diagnostics
                if name == "primary"
                else tuple(f"{name}:{item}" for item in fit.diagnostics)
            )
            return _LagPointOutcome(scaled_bundle, fit.status, diagnostics)
    assert primary.deviations is not None and primary.estimates is not None
    lag_estimates: list[dict[str, object]] = []
    contrasts: list[dict[str, object]] = []
    for feature, (input_id, component) in enumerate(primary.feature_keys):
        feature_id = _feature_id(input_id, component)
        natural_scale, natural_unit = _LAG_NATURAL_SCALES[input_id]
        personal_sd = float(primary.deviations[feature])
        for lag, estimate in enumerate(primary.estimates[feature], start=1):
            natural_estimate = float(estimate) * natural_scale / personal_sd
            if not math.isfinite(natural_estimate):
                return _LagPointOutcome(
                    scaled_bundle,
                    "unstable",
                    ("non_finite_point_estimate",),
                )
            lag_estimates.append(
                {
                    "estimate_bpm_per_natural_scale": natural_estimate,
                    "estimate_bpm_per_personal_sd": float(estimate),
                    "feature_id": feature_id,
                    "lag_day": lag,
                    "natural_scale": natural_scale,
                    "natural_unit": natural_unit.value,
                    "pointwise_interval": None,
                    "simultaneous_band": None,
                }
            )
        for contrast in method.contrasts:
            estimate = sum(
                float(value)
                for value in primary.estimates[feature, contrast.start_day - 1 : contrast.end_day]
            )
            natural_estimate = estimate * natural_scale / personal_sd
            if not math.isfinite(estimate) or not math.isfinite(natural_estimate):
                return _LagPointOutcome(
                    scaled_bundle,
                    "unstable",
                    ("non_finite_point_estimate",),
                )
            contrasts.append(
                {
                    "end_day": contrast.end_day,
                    "estimate_bpm_per_natural_scale": natural_estimate,
                    "estimate_bpm_per_personal_sd": estimate,
                    "feature_id": feature_id,
                    "natural_scale": natural_scale,
                    "natural_unit": natural_unit.value,
                    "pointwise_interval": None,
                    "simultaneous_band": None,
                    "start_day": contrast.start_day,
                }
            )
    context_components = tuple(
        sorted(
            {
                item.component
                for item in bundle.values
                if item.input_id is AnalysisInput.OUTCOME_DAY_CONTEXT and item.component is not None
            }
        )
    )
    activity_scalings = {
        name: {
            _feature_id(*key): float(cast(np.ndarray, fit.deviations)[index])
            for index, key in enumerate(fit.feature_keys)
        }
        for name, fit in fits.items()
    }
    cumulative = {
        name: {
            _feature_id(*key): float(np.sum(cast(np.ndarray, fit.estimates)[index]))
            for index, key in enumerate(fit.feature_keys)
        }
        for name, fit in fits.items()
        if name != "primary"
    }
    result_diagnostics: tuple[dict[str, object], ...] = (
        {
            "code": AnalysisDiagnostic.FIT.value,
            "facts": {
                "basis_nodes": method.lag_basis_nodes,
                "context_columns": primary.context_columns,
                "feature_count": len(primary.feature_keys),
                "fit_rows": len(primary.rows),
                "matrix_columns": primary.matrix_columns,
                "rank": primary.rank,
                "ridge_penalty": method.ridge_penalty,
                "smoothing_penalty": method.smoothing_penalty,
                "variant": "primary",
            },
        },
        {
            "code": AnalysisDiagnostic.INPUT.value,
            "facts": {
                "activity_coverage_incomplete": bundle.activity_coverage_incomplete,
                "activity_scalings": activity_scalings,
                "context_components": context_components,
                "context_rule_versions": _CONTEXT_RULE_VERSIONS,
                "context_scalings": {
                    name: dict(zip(fit.context_columns, fit.context_deviations, strict=True))
                    for name, fit in fits.items()
                },
                "context_variants": tuple(variants),
            },
        },
        {
            "code": AnalysisDiagnostic.MISSINGNESS.value,
            "facts": {
                "context_missing_values": {
                    component: sum(
                        item.input_id is AnalysisInput.OUTCOME_DAY_CONTEXT
                        and item.component == component
                        and item.value is None
                        for item in bundle.values
                    )
                    for component in context_components
                },
                "input_missing_values": {
                    _feature_id(input_id, component): sum(
                        item.input_id is input_id
                        and item.component == component
                        and item.value is None
                        for item in bundle.values
                    )
                    for input_id, component in {
                        (item.input_id, item.component) for item in bundle.values
                    }
                },
                "input_missingness": {
                    _feature_id(input_id, component): {
                        state.value: sum(
                            item.input_id is input_id
                            and item.component == component
                            and item.missingness is state
                            for item in bundle.values
                        )
                        for state in AnalysisMissingness
                    }
                    for input_id, component in {
                        (item.input_id, item.component) for item in bundle.values
                    }
                },
                "fit_days": {
                    name: tuple(bundle.calendar[index].isoformat() for index in fit.rows)
                    for name, fit in fits.items()
                },
            },
        },
        {
            "code": AnalysisDiagnostic.SENSITIVITY.value,
            "facts": {
                "cumulative_bpm_per_personal_sd": cumulative,
                "fit_rows": {name: len(fit.rows) for name, fit in fits.items()},
            },
        },
    )
    return _LagPointOutcome(
        scaled_bundle,
        "completed",
        ("completed_point_estimate",),
        tuple(lag_estimates),
        tuple(contrasts),
        result_diagnostics,
    )


def execute_analysis_run(store: LocalStore, plan: RunAnalysisPlan) -> AnalysisExecution:
    if plan.base_snapshot_ref is None:
        raise ValueError("Analyseplan besitzt keinen Snapshot.")
    operation_id = OperationId(uuid4().hex)
    run_id = AnalysisRunId(uuid4().hex)
    required = plan.analysis_definition.method_facts.inputs
    required_data_types = tuple(
        CanonicalHealthType(data_type)
        for data_type, (input_id, _) in _MEASUREMENT_INPUTS.items()
        if input_id in required
    )
    snapshot_id, measurements = store.load_analysis_measurements(
        plan.base_snapshot_ref,
        plan.eligible_start_date,
        plan.eligible_end_date,
        required_data_types,
    )
    uses_workouts = any(item in _WORKOUT_INPUTS for item in required)
    workout_snapshot_id, workouts = (
        store.load_workouts(
            plan.base_snapshot_ref,
            plan.eligible_start_date,
            plan.eligible_end_date,
        )
        if uses_workouts
        else (plan.base_snapshot_ref, ())
    )
    uses_activity = any(item in _ACTIVITY_INPUTS for item in required)
    activity_snapshot_id, activity_values, activity_coverage_incomplete = (
        store.load_analysis_activity_inputs(
            plan.base_snapshot_ref,
            plan.eligible_start_date,
            plan.eligible_end_date,
        )
        if uses_activity
        else (plan.base_snapshot_ref, (), False)
    )
    uses_context = AnalysisInput.OUTCOME_DAY_CONTEXT in required
    context_snapshot_id, outcome_context_days = (
        store.load_analysis_outcome_context_inputs(
            plan.base_snapshot_ref,
            plan.eligible_start_date,
            plan.eligible_end_date,
        )
        if uses_context
        else (plan.base_snapshot_ref, ())
    )
    if (
        snapshot_id != plan.base_snapshot_ref
        or workout_snapshot_id != plan.base_snapshot_ref
        or activity_snapshot_id != plan.base_snapshot_ref
        or context_snapshot_id != plan.base_snapshot_ref
    ):
        raise ValueError("Analyseeingang hat sich geändert.")
    bundle = build_analysis_input_bundle(
        plan,
        run_id,
        measurements,
        workouts=workouts,
        activity_values=activity_values,
        outcome_context_days=outcome_context_days,
        activity_coverage_incomplete=activity_coverage_incomplete,
        plausibility_rules=tuple(
            rule
            for rule in store.load_plausibility_rule_versions()
            if rule.data_type in {item.value for item in required_data_types}
        ),
        activity_derivation=store.load_activity_derivation_version(plan.base_snapshot_ref),
    )
    if plan.analysis_definition.result_family is AnalysisResultFamily.RHR_ACTIVITY_LAG_1_7:
        method = plan.analysis_definition.method_facts
        assert isinstance(method, LagProfileMethodFacts)
        point = _short_lag_point_fit(bundle, method)
        bundle = point.bundle
        status = point.status
        diagnostics = point.diagnostics
    else:
        point = None
        status = "insufficient_data"
        diagnostics = (
            "insufficient_data",
            f"calendar_days={len(bundle.calendar)}",
            f"observed_values={sum(item.value is not None for item in bundle.values)}",
        )
    artifact_payload = (
        json.dumps(
            _bundle_payload(bundle, include_run_id=True), separators=(",", ":"), sort_keys=True
        ).encode()
        + b"\n"
    )
    input_content_hash = hashlib.sha256(
        json.dumps(
            _bundle_payload(bundle, include_run_id=False),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    artifact = AnalysisJsonlArtifact(
        "analysis-input-bundle",
        bundle.schema_version,
        input_content_hash,
        hashlib.sha256(artifact_payload).hexdigest(),
        len(artifact_payload),
        1,
        artifact_payload,
        _validate_input_publication,
    )
    configuration = AnalysisRunConfiguration(
        plan.analysis_definition.analysis_definition_id,
        plan.requested_start_date,
        plan.requested_end_date,
        plan.schema_version,
    )
    code_commit, code_dirty, code_diff_hash, environment_hash = _reproduction_facts()
    result_id = AnalysisResultId(uuid4().hex) if status == "completed" else None
    provenance = AnalysisProvenance(
        run_id,
        result_id,
        plan.base_snapshot_ref,
        plan.analysis_definition.analysis_definition_id,
        configuration.content_hash,
        plan.schema_version,
        code_commit,
        code_dirty,
        code_diff_hash,
        environment_hash,
    )
    result_artifact: AnalysisJsonlArtifact | None = None
    if result_id is not None and point is not None:
        result_record: dict[str, object] = {
            "analysis_definition_id": str(plan.analysis_definition.analysis_definition_id),
            "analysis_result_id": str(result_id),
            "analysis_run_id": str(run_id),
            "bootstrap_facts": [],
            "contrasts": list(point.contrasts),
            "diagnostics": list(point.result_diagnostics),
            "lag_estimates": list(point.lag_estimates),
            "maturity_criteria": [],
            "result_family": plan.analysis_definition.result_family.value,
            "result_schema_version": 1,
        }
        result_payload = (
            json.dumps(result_record, separators=(",", ":"), sort_keys=True).encode() + b"\n"
        )
        result_content = dict(result_record)
        del result_content["analysis_run_id"]
        del result_content["analysis_result_id"]
        result_artifact = AnalysisJsonlArtifact(
            f"analysis-result-{plan.analysis_definition.result_family.value}",
            1,
            hashlib.sha256(
                json.dumps(result_content, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest(),
            hashlib.sha256(result_payload).hexdigest(),
            len(result_payload),
            1,
            result_payload,
            _validate_result_artifact,
        )
    provisional_evidence = tuple(
        sorted(
            {
                str(evidence.measurement_version_id)
                for item in bundle.values
                if item.quality_status is DataQualityStatus.PROVISIONAL
                for evidence in item.source_evidence
            }
            | {
                str(source.source_record_id)
                for item in bundle.values
                if item.quality_status is DataQualityStatus.PROVISIONAL
                for source in item.source_records
            }
        )
    )
    data_status_reasons = (
        (
            AnalysisDataStatusReason(
                DataStatusReasonCode.OPEN_REVIEW_CASE,
                tuple(str(item) for item in bundle.data_quality_fact_ids),
            ),
        )
        if bundle.data_quality_fact_ids
        else ()
    ) + (
        (
            AnalysisDataStatusReason(
                DataStatusReasonCode.PASSIVE_COVERAGE_GAP,
                ("activity-coverage/v1",),
            ),
        )
        if bundle.activity_coverage_incomplete
        else ()
    ) + (
        (
            AnalysisDataStatusReason(
                DataStatusReasonCode.PROVISIONAL_INPUT_QUALITY,
                provisional_evidence,
            ),
        )
        if provisional_evidence
        else ()
    )
    _publish_analysis_run(
        store,
        AnalysisRunPublication(
            operation_id=operation_id,
            provenance=provenance,
            start_date=plan.eligible_start_date,
            end_date=plan.eligible_end_date,
            configuration=configuration,
            input_artifact=artifact,
            diagnostics=diagnostics,
            data_status=(
                DataQualityStatus.PROVISIONAL if data_status_reasons else DataQualityStatus.REVIEWED
            ),
            data_status_reasons=data_status_reasons,
            status=status,
            result_family=(
                plan.analysis_definition.result_family.value
                if result_artifact is not None
                else None
            ),
            result_artifact=result_artifact,
            model_maturity=(
                ModelMaturityStatus.EXPLORATORY if result_artifact is not None else None
            ),
        ),
    )
    return AnalysisExecution(
        operation_id,
        run_id,
        plan.base_snapshot_ref,
        plan.analysis_definition.analysis_definition_id,
        diagnostics,
        provenance,
        status,
        result_id,
        ModelMaturityStatus.EXPLORATORY if result_artifact is not None else None,
    )


__all__ = [
    "AnalysisBootstrapMethod",
    "AnalysisDefinition",
    "AnalysisDiagnostic",
    "AnalysisExecution",
    "AnalysisInput",
    "AnalysisInputBundle",
    "AnalysisInputValue",
    "AnalysisIntervalMethod",
    "AnalysisMaturityCriterionCode",
    "AnalysisMethodFacts",
    "AnalysisMissingness",
    "AnalysisMissingnessReason",
    "AnalysisResultFamily",
    "AnalysisReuseCandidate",
    "AnalysisScaling",
    "AnalysisScalingStatus",
    "AnalysisSourceEvidence",
    "AssociationMeasure",
    "BootstrapBlockLengthFacts",
    "BootstrapMethodFacts",
    "BootstrapSampleCountBasis",
    "LagContrast",
    "LagMaturityFacts",
    "LagProfileMethodFacts",
    "OutcomeAssociationMaturityFacts",
    "OutcomeAssociationMethodFacts",
    "RunAnalysisPlan",
    "TrendUncertainty",
    "WeightCoreMaturityFacts",
    "WeightCoreMethodFacts",
    "WeightTrendKernel",
    "WeightWindowMethodFacts",
    "analysis_definitions",
    "build_analysis_input_bundle",
    "execute_analysis_run",
    "plan_analysis",
]
