"""Built-in resting-heart-rate analysis operation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import fmean, pstdev
from types import MappingProxyType
from typing import Literal
from uuid import uuid4

from personal_health_lab.health_data import CanonicalHealthType, DailyHealthSeries
from personal_health_lab.storage import (
    AnalysisDefinitionId as AnalysisDefinitionId,
)
from personal_health_lab.storage import (
    AnalysisResultId as AnalysisResultId,
)
from personal_health_lab.storage import (
    AnalysisRunId as AnalysisRunId,
)
from personal_health_lab.storage import (
    AssociationDirection,
    AssociationEstimate,
    DataMode,
    LocalStore,
    OperationId,
    RestingHeartRateAnalysisResult,
    SnapshotId,
    StoreBusyError,
)


@dataclass(frozen=True, slots=True)
class _AnalysisDefinition:
    ridge_penalty: float
    minimum_observations: int


_DEFINITIONS = MappingProxyType(
    {
        AnalysisDefinitionId("lag-signal-v1"): _AnalysisDefinition(
            ridge_penalty=1.0,
            minimum_observations=30,
        )
    }
)


@dataclass(frozen=True, slots=True)
class AnalysisExecution:
    operation_id: OperationId
    analysis_run_id: AnalysisRunId
    status: Literal["completed", "insufficient_data", "store_busy"]
    snapshot_id: SnapshotId | None
    analysis_definition_id: AnalysisDefinitionId
    model_maturity: Literal["exploratory", "robust"] | None
    result_id: AnalysisResultId | None
    diagnostics: tuple[str, ...] = ()


class AnalysisError(RuntimeError):
    """The built-in analysis could not be completed safely."""


def _solve(matrix: list[list[float]], values: list[float]) -> list[float]:
    augmented = [[*row, value] for row, value in zip(matrix, values, strict=True)]
    for column in range(len(augmented)):
        pivot = max(range(column, len(augmented)), key=lambda row: abs(augmented[row][column]))
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        if abs(divisor) < 1e-12:
            raise ArithmeticError("Distributed-Lag-Modell ist numerisch singulär.")
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(len(augmented)):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column], strict=True)
            ]
    return [row[-1] for row in augmented]


def _direction(value: float) -> AssociationDirection:
    if value < 0:
        return AssociationDirection.NEGATIVE
    if value > 0:
        return AssociationDirection.POSITIVE
    return AssociationDirection.ZERO


def _fit(
    series: tuple[DailyHealthSeries, ...],
    snapshot_id: SnapshotId,
    definition_id: AnalysisDefinitionId,
    definition: _AnalysisDefinition,
    start_date: date | None,
    end_date: date | None,
) -> RestingHeartRateAnalysisResult | None:
    daily = {item.data_type: {value.day: value.value for value in item.values} for item in series}
    active = daily.get(CanonicalHealthType.ACTIVE_ENERGY, {})
    resting = daily.get(CanonicalHealthType.APPLE_RESTING_HEART_RATE, {})
    rows = [
        ([active[day - timedelta(days=lag)] for lag in range(1, 8)], value)
        for day, value in sorted(resting.items())
        if (start_date is None or day >= start_date)
        and (end_date is None or day <= end_date)
        and all(day - timedelta(days=lag) in active for lag in range(1, 8))
    ]
    if len(rows) < definition.minimum_observations:
        return None
    columns = list(zip(*(features for features, _ in rows), strict=True))
    means = [fmean(column) for column in columns]
    deviations = [pstdev(column) for column in columns]
    if any(value == 0 for value in deviations):
        return None
    design = [
        [(value - means[index]) / deviations[index] for index, value in enumerate(features)]
        for features, _ in rows
    ]
    outcomes = [value for _, value in rows]
    outcome_mean = fmean(outcomes)
    centered_outcomes = [value - outcome_mean for value in outcomes]
    gram = [
        [
            sum(row[left] * row[right] for row in design)
            + (definition.ridge_penalty if left == right else 0.0)
            for right in range(7)
        ]
        for left in range(7)
    ]
    projected = [
        sum(row[column] * outcome for row, outcome in zip(design, centered_outcomes, strict=True))
        for column in range(7)
    ]
    coefficients = [
        standardized / deviation
        for standardized, deviation in zip(_solve(gram, projected), deviations, strict=True)
    ]
    personal_sd = pstdev(active.values())

    def estimate(lag_days: int | None, coefficient: float) -> AssociationEstimate:
        return AssociationEstimate(
            lag_days=lag_days,
            direction=_direction(coefficient),
            estimate_per_100_kcal=coefficient * 100.0,
            estimate_per_personal_standard_deviation=coefficient * personal_sd,
        )

    return RestingHeartRateAnalysisResult(
        snapshot_id=snapshot_id,
        analysis_definition_id=definition_id,
        personal_standard_deviation_kcal=personal_sd,
        lag_associations=tuple(
            estimate(lag, coefficient) for lag, coefficient in enumerate(coefficients, start=1)
        ),
        cumulative_association=estimate(None, sum(coefficients)),
    )


def run_resting_hr_analysis(
    *,
    root: Path,
    mode: DataMode,
    analysis_definition_id: AnalysisDefinitionId,
    start_date: date | None,
    end_date: date | None,
) -> AnalysisExecution:
    definition = _DEFINITIONS.get(analysis_definition_id)
    if definition is None:
        raise ValueError("Unbekannte eingebaute Analysedefinition.")
    operation_id = OperationId(str(uuid4()))
    run_id = AnalysisRunId(str(uuid4()))
    try:
        store = LocalStore.open_writer(root, mode)
    except StoreBusyError:
        return AnalysisExecution(
            operation_id, run_id, "store_busy", None, analysis_definition_id, None, None
        )
    except Exception as error:
        raise AnalysisError("Ruhepulsanalyse konnte nicht gestartet werden.") from error
    try:
        input_start = None if start_date is None else start_date - timedelta(days=7)
        snapshot_id, series = store.load_analysis_input(input_start, end_date)
        if snapshot_id is None:
            return AnalysisExecution(
                operation_id,
                run_id,
                "insufficient_data",
                None,
                analysis_definition_id,
                None,
                None,
                ("no_snapshot",),
            )
        result = _fit(
            series,
            snapshot_id,
            analysis_definition_id,
            definition,
            start_date,
            end_date,
        )
        if result is None:
            return AnalysisExecution(
                operation_id,
                run_id,
                "insufficient_data",
                snapshot_id,
                analysis_definition_id,
                None,
                None,
                ("insufficient_complete_days",),
            )
        result_id = AnalysisResultId(str(uuid4()))
        store.persist_resting_hr_analysis(
            operation_id=operation_id,
            analysis_run_id=run_id,
            result_id=result_id,
            result=result,
            start_date=start_date,
            end_date=end_date,
        )
        return AnalysisExecution(
            operation_id,
            run_id,
            "completed",
            snapshot_id,
            analysis_definition_id,
            "exploratory",
            result_id,
        )
    except Exception as error:
        raise AnalysisError("Ruhepulsanalyse konnte nicht abgeschlossen werden.") from error
    finally:
        store.close()


__all__ = [
    "AnalysisDefinitionId",
    "AnalysisError",
    "AnalysisExecution",
    "AnalysisResultId",
    "AnalysisRunId",
    "run_resting_hr_analysis",
]
