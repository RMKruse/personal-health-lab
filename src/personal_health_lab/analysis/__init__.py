"""Fixed V0.4 analysis definitions."""

from dataclasses import dataclass
from enum import StrEnum

from personal_health_lab.storage import AnalysisDefinitionId


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
    POINTWISE_AND_STUDENTIZED_SIMULTANEOUS_BAND = (
        "pointwise_and_studentized_simultaneous_band"
    )
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


__all__ = [
    "AnalysisBootstrapMethod",
    "AnalysisDefinition",
    "AnalysisDiagnostic",
    "AnalysisInput",
    "AnalysisIntervalMethod",
    "AnalysisMethodFacts",
    "AnalysisResultFamily",
    "AssociationMeasure",
    "BootstrapBlockLengthFacts",
    "BootstrapMethodFacts",
    "BootstrapSampleCountBasis",
    "LagContrast",
    "LagMaturityFacts",
    "LagProfileMethodFacts",
    "OutcomeAssociationMaturityFacts",
    "OutcomeAssociationMethodFacts",
    "TrendUncertainty",
    "WeightCoreMaturityFacts",
    "WeightCoreMethodFacts",
    "WeightTrendKernel",
    "WeightWindowMethodFacts",
    "analysis_definitions",
]
