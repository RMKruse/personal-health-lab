"""Fixed V0.4 analysis definitions and run lifecycle."""

import hashlib
import json
import math
import os
import statistics
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from personal_health_lab.storage import (
    AnalysisDefinitionId,
    AnalysisProvenance,
    AnalysisResultId,
    AnalysisRunId,
    LocalStore,
    OperationId,
    SnapshotId,
    StoredMeasurement,
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


class AssociationMeasure(StrEnum):
    PEARSON_LOCAL_SLOPES = "pearson_local_slopes"
    PEARSON_PAIRED_DEVIATIONS = "pearson_paired_deviations"


class WeightTrendKernel(StrEnum):
    TRIANGULAR_LOCAL_LINEAR = "triangular_local_linear"


class TrendUncertainty(StrEnum):
    NO_CALIBRATED_INTERVAL = "no_calibrated_interval"


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
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class AnalysisSourceEvidence:
    measurement_version_id: str
    source_name: str
    source_version: str
    source_updated_at: datetime


@dataclass(frozen=True, slots=True)
class AnalysisInputValue:
    day: date
    input_id: AnalysisInput
    component: str | None
    unit: str | None
    value: float | None
    missingness: AnalysisMissingness
    source_evidence: tuple[AnalysisSourceEvidence, ...] = ()
    data_quality_fact_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AnalysisScaling:
    input_id: AnalysisInput
    component: str | None
    population_standard_deviation: float | None
    status: str


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
    data_quality_fact_ids: tuple[str, ...]
    scalings: tuple[AnalysisScaling, ...]
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class AnalysisExecution:
    operation_id: OperationId
    analysis_run_id: AnalysisRunId
    snapshot_id: SnapshotId
    analysis_definition_id: AnalysisDefinitionId
    diagnostics: tuple[str, ...]
    provenance: AnalysisProvenance


_LAG_INPUTS = (
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
    "active_energy": AnalysisInput.ACTIVE_ENERGY,
    "apple_exercise_time": AnalysisInput.TRAINING_TIME,
    "apple_resting_heart_rate": AnalysisInput.APPLE_RESTING_HEART_RATE,
    "body_mass": AnalysisInput.PREFERRED_DAILY_WEIGHT,
    "dietary_energy_consumed": AnalysisInput.NUTRITION_DAY_V1,
    "step_count": AnalysisInput.STEPS,
    "walking_running_distance": AnalysisInput.WALKING_RUNNING_DISTANCE,
}
_POINT_INPUTS = {
    AnalysisInput.APPLE_RESTING_HEART_RATE,
    AnalysisInput.PREFERRED_DAILY_WEIGHT,
}
_RULE_VERSIONS = (
    "healthkit-identity/v3",
    "healthkit-canonical/v3",
    "activity-derivation/v1",
    "preferred-daily-weight/v1",
    "nutrition-day-v1",
    "resting-energy-day-allocation/v1",
)


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
            str(item.measurement_version_id),
            item.source_name,
            item.source_version,
            item.source_updated_at,
        )
        for item in measurements
    )


def _measurement_value(
    day: date,
    input_id: AnalysisInput,
    measurements: tuple[StoredMeasurement, ...],
) -> AnalysisInputValue:
    quality_ids = tuple(
        sorted({str(case) for item in measurements for case in item.review_case_ids})
    )
    if not measurements:
        return AnalysisInputValue(day, input_id, None, None, None, AnalysisMissingness.MISSING)
    if input_id in _POINT_INPUTS:
        latest = max(item.source_start for item in measurements)
        used = tuple(item for item in measurements if item.source_start == latest)
        distinct = {item.effective_value for item in used}
        if len(distinct) != 1:
            return AnalysisInputValue(
                day,
                input_id,
                None,
                used[0].unit.value,
                None,
                AnalysisMissingness.AMBIGUOUS,
                _source_evidence(used),
                quality_ids,
            )
        value = next(iter(distinct))
    else:
        used = measurements
        value = sum(item.effective_value or 0.0 for item in used)
    return AnalysisInputValue(
        day,
        input_id,
        None,
        used[0].unit.value,
        value,
        AnalysisMissingness.OBSERVED,
        _source_evidence(used),
        quality_ids,
    )


def build_analysis_input_bundle(
    plan: RunAnalysisPlan,
    analysis_run_id: AnalysisRunId,
    measurements: tuple[StoredMeasurement, ...],
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
    grouped: dict[tuple[date, AnalysisInput], list[StoredMeasurement]] = {}
    for measurement in measurements:
        input_id = _MEASUREMENT_INPUTS.get(measurement.data_type.value)
        if (
            input_id is not None
            and measurement.is_selected
            and measurement.disposition in {"included_source", "included_correction"}
            and measurement.effective_value is not None
        ):
            grouped.setdefault((measurement.measurement_local_day, input_id), []).append(
                measurement
            )
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
            values.append(
                _measurement_value(day, input_id, tuple(grouped.get((day, input_id), ())))
            )
    scale_keys = sorted(
        {(item.input_id, item.component) for item in values},
        key=lambda item: (item[0].value, item[1] or ""),
    )
    scalings: list[AnalysisScaling] = []
    for input_id, component in scale_keys:
        observed = [
            item.value
            for item in values
            if item.input_id is input_id
            and item.component == component
            and item.value is not None
        ]
        scalings.append(
            AnalysisScaling(
                input_id,
                component,
                statistics.pstdev(observed) if len(observed) >= 2 else None,
                "observed" if len(observed) >= 2 else "insufficient_observations",
            )
        )
    quality_ids = tuple(
        sorted({fact_id for item in values for fact_id in item.data_quality_fact_ids})
    )
    return AnalysisInputBundle(
        analysis_run_id,
        plan.base_snapshot_ref,
        plan.analysis_definition.analysis_definition_id,
        plan.eligible_start_date,
        plan.eligible_end_date,
        calendar,
        tuple(values),
        _RULE_VERSIONS,
        quality_ids,
        tuple(scalings),
    )


def _bundle_payload(bundle: AnalysisInputBundle, *, include_run_id: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "analysis_definition_id": str(bundle.analysis_definition_id),
        "analysis_period": {
            "start_date": bundle.start_date.isoformat(),
            "end_date": bundle.end_date.isoformat(),
        },
        "calendar": [day.isoformat() for day in bundle.calendar],
        "data_quality_fact_ids": list(bundle.data_quality_fact_ids),
        "input_schema_version": bundle.schema_version,
        "rule_versions": list(bundle.rule_versions),
        "scalings": [
            {
                "component": item.component,
                "input_id": item.input_id.value,
                "population_standard_deviation": item.population_standard_deviation,
                "status": item.status,
            }
            for item in bundle.scalings
        ],
        "snapshot_id": str(bundle.snapshot_id),
        "values": [
            {
                "component": item.component,
                "data_quality_fact_ids": list(item.data_quality_fact_ids),
                "day": item.day.isoformat(),
                "input_id": item.input_id.value,
                "measurement_version_ids": [
                    evidence.measurement_version_id for evidence in item.source_evidence
                ],
                "missingness": item.missingness.value,
                "source_evidence": [
                    {
                        "measurement_version_id": evidence.measurement_version_id,
                        "source_name": evidence.source_name,
                        "source_updated_at": evidence.source_updated_at.isoformat(),
                        "source_version": evidence.source_version,
                    }
                    for evidence in item.source_evidence
                ],
                "unit": item.unit,
                "value": item.value,
            }
            for item in bundle.values
        ],
    }
    if include_run_id:
        payload["analysis_run_id"] = str(bundle.analysis_run_id)
    return payload


def _reproduction_facts(
    definition_id: AnalysisDefinitionId,
    start_date: date | None,
    end_date: date | None,
    schema_version: str,
) -> tuple[str, str, str, bool, str | None, str]:
    config_json = json.dumps(
        {
            "analysis_definition_id": str(definition_id),
            "end_date": None if end_date is None else end_date.isoformat(),
            "schema_version": schema_version,
            "start_date": None if start_date is None else start_date.isoformat(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )

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
        config_json,
        hashlib.sha256(config_json.encode()).hexdigest(),
        code_commit,
        bool(tracked_diff or untracked),
        diff_hasher.hexdigest() if tracked_diff or untracked else None,
        environment_hash,
    )


def execute_analysis_run(store: LocalStore, plan: RunAnalysisPlan) -> AnalysisExecution:
    if plan.base_snapshot_ref is None:
        raise ValueError("Analyseplan besitzt keinen Snapshot.")
    operation_id = OperationId(uuid4().hex)
    run_id = AnalysisRunId(uuid4().hex)
    snapshot_id, measurements = store.load_analysis_measurements(
        plan.base_snapshot_ref,
        plan.eligible_start_date,
        plan.eligible_end_date,
    )
    if snapshot_id != plan.base_snapshot_ref:
        raise ValueError("Analyseeingang hat sich geändert.")
    bundle = build_analysis_input_bundle(plan, run_id, measurements)
    content_json = json.dumps(
        _bundle_payload(bundle, include_run_id=False), separators=(",", ":"), sort_keys=True
    )
    artifact_json = json.dumps(
        _bundle_payload(bundle, include_run_id=True), separators=(",", ":"), sort_keys=True
    )
    config_json, config_hash, code_commit, code_dirty, code_diff_hash, environment_hash = (
        _reproduction_facts(
            plan.analysis_definition.analysis_definition_id,
            plan.requested_start_date,
            plan.requested_end_date,
            plan.schema_version,
        )
    )
    provenance = AnalysisProvenance(
        run_id,
        None,
        plan.base_snapshot_ref,
        plan.analysis_definition.analysis_definition_id,
        config_hash,
        plan.schema_version,
        code_commit,
        code_dirty,
        code_diff_hash,
        environment_hash,
        hashlib.sha256(content_json.encode()).hexdigest(),
    )
    observed = sum(item.value is not None for item in bundle.values)
    diagnostics = (
        "insufficient_data",
        f"calendar_days={len(bundle.calendar)}",
        f"observed_values={observed}",
    )
    store.persist_insufficient_analysis_run(
        operation_id=operation_id,
        provenance=provenance,
        start_date=plan.eligible_start_date,
        end_date=plan.eligible_end_date,
        config_json=config_json,
        input_json=artifact_json,
        diagnostics=diagnostics,
        data_status="provisional" if bundle.data_quality_fact_ids else "reviewed",
    )
    return AnalysisExecution(
        operation_id,
        run_id,
        plan.base_snapshot_ref,
        plan.analysis_definition.analysis_definition_id,
        diagnostics,
        provenance,
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
    "AnalysisMethodFacts",
    "AnalysisMissingness",
    "AnalysisResultFamily",
    "AnalysisReuseCandidate",
    "AnalysisScaling",
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
