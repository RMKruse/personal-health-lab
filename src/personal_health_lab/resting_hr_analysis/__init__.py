"""Built-in resting-heart-rate analysis operation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from random import Random
from statistics import correlation, fmean, pstdev
from types import MappingProxyType
from typing import Literal
from uuid import uuid4

from personal_health_lab.health_data import CanonicalHealthType, DailyHealthSeries
from personal_health_lab.storage import (
    AnalysisDefinitionId as AnalysisDefinitionId,
)
from personal_health_lab.storage import (
    AnalysisDiagnostics,
    AnalysisMethodology,
    AnalysisProvenance,
    AssociationDirection,
    AssociationEstimate,
    AssociationInterval,
    DataMode,
    LocalStore,
    OperationId,
    RestingHeartRateAnalysisResult,
    SnapshotId,
    StoreBusyError,
)
from personal_health_lab.storage import (
    AnalysisResultId as AnalysisResultId,
)
from personal_health_lab.storage import (
    AnalysisRunId as AnalysisRunId,
)

type _AnalysisRow = tuple[date, list[float], float]
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class _AnalysisDefinition:
    ridge_penalty: float
    minimum_observations: int
    robust_observations: int
    bootstrap_block_length: int
    bootstrap_resamples: int
    bootstrap_seed: int
    interval_level: float


_DEFINITIONS = MappingProxyType(
    {
        AnalysisDefinitionId("lag-signal-v1"): _AnalysisDefinition(
            ridge_penalty=1.0,
            minimum_observations=30,
            robust_observations=180,
            bootstrap_block_length=7,
            bootstrap_resamples=250,
            bootstrap_seed=20260713,
            interval_level=0.95,
        )
    }
)


@dataclass(frozen=True, slots=True)
class AnalysisExecution:
    operation_id: OperationId
    analysis_run_id: AnalysisRunId
    status: Literal["completed", "reused", "insufficient_data", "unstable", "store_busy"]
    snapshot_id: SnapshotId | None
    analysis_definition_id: AnalysisDefinitionId
    model_maturity: Literal["exploratory", "robust"] | None
    result_id: AnalysisResultId | None
    diagnostics: tuple[str, ...] = ()
    provenance: AnalysisProvenance | None = None


@dataclass(frozen=True, slots=True)
class _ReproductionContext:
    config_json: str
    config_hash: str
    config_schema_version: str
    code_commit: str
    code_dirty: bool
    code_diff_hash: str | None
    environment_lock_hash: str


class AnalysisError(RuntimeError):
    """The built-in analysis could not be completed safely."""


class AnalysisInputChanged(AnalysisError):
    """The active snapshot no longer matches the authorized analysis plan."""


class _InsufficientData(RuntimeError):
    pass


class _Unstable(RuntimeError):
    pass


def _reproduction_context(
    analysis_definition_id: AnalysisDefinitionId,
    start_date: date | None,
    end_date: date | None,
    config_schema_version: str,
) -> _ReproductionContext:
    config_json = json.dumps(
        {
            "analysis_definition_id": str(analysis_definition_id),
            "end_date": end_date.isoformat() if end_date else None,
            "schema_version": config_schema_version,
            "start_date": start_date.isoformat() if start_date else None,
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    def git(*arguments: str) -> bytes:
        return subprocess.run(
            ("git", *arguments),
            cwd=_PROJECT_ROOT,
            check=True,
            capture_output=True,
        ).stdout

    try:
        code_commit = git("rev-parse", "HEAD").decode("ascii").strip()
        tracked_diff = git("diff", "--binary", "HEAD")
        untracked = git("ls-files", "--others", "--exclude-standard", "-z")
        diff_hasher = hashlib.sha256(tracked_diff)
        for relative in sorted(path for path in untracked.split(b"\0") if path):
            diff_hasher.update(b"\0" + relative + b"\0")
            path = _PROJECT_ROOT / os.fsdecode(relative)
            if path.is_symlink():
                diff_hasher.update(os.readlink(path).encode())
            else:
                with path.open("rb") as untracked_file:
                    while chunk := untracked_file.read(1024 * 1024):
                        diff_hasher.update(chunk)
        environment_lock = (_PROJECT_ROOT / "uv.lock").read_bytes()
    except (OSError, subprocess.CalledProcessError, UnicodeError) as error:
        raise AnalysisError("Analyseidentitäten konnten nicht sicher erfasst werden.") from error

    return _ReproductionContext(
        config_json=config_json,
        config_hash=hashlib.sha256(config_json.encode()).hexdigest(),
        config_schema_version=config_schema_version,
        code_commit=code_commit,
        code_dirty=bool(tracked_diff or untracked),
        code_diff_hash=diff_hasher.hexdigest() if tracked_diff or untracked else None,
        environment_lock_hash=hashlib.sha256(environment_lock).hexdigest(),
    )


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


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    fraction = position - lower
    if fraction == 0:
        return ordered[lower]
    return ordered[lower] + fraction * (ordered[lower + 1] - ordered[lower])


def _coefficients(rows: list[_AnalysisRow], definition: _AnalysisDefinition) -> list[float]:
    columns = list(zip(*(features for _, features, _ in rows), strict=True))
    means = [fmean(column) for column in columns]
    deviations = [pstdev(column) for column in columns]
    if any(value == 0 for value in deviations):
        raise _Unstable("constant_predictor")
    design = [
        [(value - means[index]) / deviations[index] for index, value in enumerate(features)]
        for _, features, _ in rows
    ]
    outcomes = [value for _, _, value in rows]
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
    try:
        standardized_coefficients = _solve(gram, projected)
    except ArithmeticError as error:
        raise _Unstable("numerical_singularity") from error
    return [
        standardized / deviation
        for standardized, deviation in zip(standardized_coefficients, deviations, strict=True)
    ]


def _bootstrap(rows: list[_AnalysisRow], definition: _AnalysisDefinition) -> list[list[float]]:
    rng = Random(definition.bootstrap_seed)
    block_length = min(definition.bootstrap_block_length, len(rows))
    blocks: list[list[_AnalysisRow]] = []
    for start, (start_day, _, _) in enumerate(rows):
        end = start
        limit = start_day + timedelta(days=block_length)
        while end < len(rows) and rows[end][0] < limit:
            end += 1
        blocks.append(rows[start:end])
    fits: list[list[float]] = []
    for _ in range(definition.bootstrap_resamples):
        sample: list[_AnalysisRow] = []
        while len(sample) < len(rows):
            sample.extend(blocks[rng.randrange(len(blocks))])
        try:
            fits.append(_coefficients(sample[: len(rows)], definition))
        except (ArithmeticError, _Unstable):
            continue
    if len(fits) < definition.bootstrap_resamples * 0.95:
        raise _Unstable("bootstrap_failures")
    return fits


def _fit(
    series: tuple[DailyHealthSeries, ...],
    snapshot_id: SnapshotId,
    definition_id: AnalysisDefinitionId,
    definition: _AnalysisDefinition,
    start_date: date | None,
    end_date: date | None,
) -> RestingHeartRateAnalysisResult:
    daily = {item.data_type: {value.day: value.value for value in item.values} for item in series}
    active = daily.get(CanonicalHealthType.ACTIVE_ENERGY, {})
    resting = daily.get(CanonicalHealthType.APPLE_RESTING_HEART_RATE, {})
    rows = [
        (day, [active[day - timedelta(days=lag)] for lag in range(1, 8)], value)
        for day, value in sorted(resting.items())
        if (start_date is None or day >= start_date)
        and (end_date is None or day <= end_date)
        and all(day - timedelta(days=lag) in active for lag in range(1, 8))
    ]
    if len(rows) < definition.minimum_observations:
        raise _InsufficientData("insufficient_complete_days")
    outcomes = [value for _, _, value in rows]
    if pstdev(outcomes) == 0:
        raise _Unstable("constant_outcome")
    coefficients = _coefficients(rows, definition)
    bootstrap = _bootstrap(rows, definition)
    personal_sd = pstdev(active.values())
    alpha = (1.0 - definition.interval_level) / 2.0
    pointwise = [
        (_quantile(values, alpha), _quantile(values, 1.0 - alpha))
        for values in ([fit[index] for fit in bootstrap] for index in range(7))
    ]
    simultaneous_radius = _quantile(
        [
            max(abs(value - coefficients[index]) for index, value in enumerate(fit))
            for fit in bootstrap
        ],
        definition.interval_level,
    )
    columns = list(zip(*(features for _, features, _ in rows), strict=True))
    max_dependency = max(
        abs(correlation(columns[left], columns[right]))
        for left in range(7)
        for right in range(left + 1, 7)
    )
    maturity: Literal["exploratory", "robust"] = (
        "robust"
        if len(rows) >= definition.robust_observations and max_dependency < 0.98
        else "exploratory"
    )

    def interval(lower: float, upper: float) -> AssociationInterval:
        return AssociationInterval(
            lower_per_100_kcal=lower * 100.0,
            upper_per_100_kcal=upper * 100.0,
            lower_per_personal_standard_deviation=lower * personal_sd,
            upper_per_personal_standard_deviation=upper * personal_sd,
        )

    def estimate(
        lag_days: int | None,
        coefficient: float,
        interval_bounds: tuple[float, float],
        simultaneous_bounds: tuple[float, float] | None,
    ) -> AssociationEstimate:
        return AssociationEstimate(
            lag_days=lag_days,
            direction=_direction(coefficient),
            estimate_per_100_kcal=coefficient * 100.0,
            estimate_per_personal_standard_deviation=coefficient * personal_sd,
            pointwise_interval=interval(*interval_bounds),
            simultaneous_band=(
                None if simultaneous_bounds is None else interval(*simultaneous_bounds)
            ),
        )

    simultaneous = [
        (
            min(coefficient - simultaneous_radius, pointwise[index][0]),
            max(coefficient + simultaneous_radius, pointwise[index][1]),
        )
        for index, coefficient in enumerate(coefficients)
    ]
    guardrail: Literal["simultaneous_band_includes_zero", "simultaneous_band_excludes_zero"] = (
        "simultaneous_band_excludes_zero"
        if any(lower > 0 or upper < 0 for lower, upper in simultaneous)
        else "simultaneous_band_includes_zero"
    )
    diagnostics = AnalysisDiagnostics(
        complete_days=len(rows),
        feature_dependency="acceptable" if max_dependency < 0.98 else "high",
        bootstrap_successes=len(bootstrap),
        bootstrap_resamples=definition.bootstrap_resamples,
        model_readiness=maturity,
        association_guardrail=guardrail,
    )

    return RestingHeartRateAnalysisResult(
        snapshot_id=snapshot_id,
        analysis_definition_id=definition_id,
        personal_standard_deviation_kcal=personal_sd,
        lag_associations=tuple(
            estimate(lag, coefficient, pointwise[lag - 1], simultaneous[lag - 1])
            for lag, coefficient in enumerate(coefficients, start=1)
        ),
        cumulative_association=estimate(
            None,
            sum(coefficients),
            (
                _quantile([sum(fit) for fit in bootstrap], alpha),
                _quantile([sum(fit) for fit in bootstrap], 1.0 - alpha),
            ),
            None,
        ),
        model_maturity=maturity,
        diagnostics=diagnostics,
        methodology=AnalysisMethodology(
            ridge_penalty=definition.ridge_penalty,
            minimum_observations=definition.minimum_observations,
            robust_observations=definition.robust_observations,
            bootstrap_method="moving_block",
            block_length_days=definition.bootstrap_block_length,
            resample_count=definition.bootstrap_resamples,
            random_seed=definition.bootstrap_seed,
            interval_level=definition.interval_level,
        ),
    )


def run_resting_hr_analysis(
    *,
    root: Path,
    mode: DataMode,
    analysis_definition_id: AnalysisDefinitionId,
    start_date: date | None,
    end_date: date | None,
    config_schema_version: str,
    expected_snapshot_id: SnapshotId | None,
) -> AnalysisExecution:
    definition = _DEFINITIONS.get(analysis_definition_id)
    if definition is None:
        raise ValueError("Unbekannte eingebaute Analysedefinition.")
    context = _reproduction_context(
        analysis_definition_id,
        start_date,
        end_date,
        config_schema_version,
    )
    operation_id = OperationId(str(uuid4()))
    run_id = AnalysisRunId(str(uuid4()))
    result_id = AnalysisResultId(str(uuid4()))
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
        if snapshot_id != expected_snapshot_id:
            raise AnalysisInputChanged
        if snapshot_id is None:
            store.persist_analysis_receipt(
                operation_id=operation_id,
                analysis_run_id=run_id,
                status="insufficient_data",
                provenance=None,
                config_json=context.config_json,
                diagnostics=("no_snapshot",),
            )
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
        candidate = AnalysisProvenance(
            analysis_run_id=run_id,
            result_id=result_id,
            snapshot_id=snapshot_id,
            analysis_definition_id=analysis_definition_id,
            config_hash=context.config_hash,
            config_schema_version=context.config_schema_version,
            code_commit=context.code_commit,
            code_dirty=context.code_dirty,
            code_diff_hash=context.code_diff_hash,
            environment_lock_hash=context.environment_lock_hash,
        )
        reusable = store.find_reusable_resting_hr_analysis(candidate)
        if reusable is not None:
            provenance = reusable.provenance
            store.persist_analysis_receipt(
                operation_id=operation_id,
                analysis_run_id=provenance.analysis_run_id,
                status="reused",
                provenance=provenance,
                config_json=context.config_json,
                diagnostics=reusable.diagnostics,
            )
            return AnalysisExecution(
                operation_id,
                provenance.analysis_run_id,
                "reused",
                provenance.snapshot_id,
                provenance.analysis_definition_id,
                reusable.model_maturity,
                provenance.result_id,
                reusable.diagnostics,
                provenance,
            )
        result = _fit(
            series,
            snapshot_id,
            analysis_definition_id,
            definition,
            start_date,
            end_date,
        )
        result = replace(result, provenance=candidate)
        receipt_diagnostics = (
            f"model_readiness_{result.model_maturity}",
            result.diagnostics.association_guardrail,
        )
        store.persist_resting_hr_analysis(
            operation_id=operation_id,
            result=result,
            start_date=start_date,
            end_date=end_date,
            config_json=context.config_json,
            receipt_diagnostics=receipt_diagnostics,
        )
        store.persist_analysis_receipt(
            operation_id=operation_id,
            analysis_run_id=run_id,
            status="completed",
            provenance=candidate,
            config_json=context.config_json,
            diagnostics=receipt_diagnostics,
        )
        return AnalysisExecution(
            operation_id,
            run_id,
            "completed",
            snapshot_id,
            analysis_definition_id,
            result.model_maturity,
            result_id,
            receipt_diagnostics,
            candidate,
        )
    except _InsufficientData as error:
        diagnostics = (str(error),)
        failed_provenance = replace(candidate, result_id=None)
        store.persist_analysis_receipt(
            operation_id=operation_id,
            analysis_run_id=run_id,
            status="insufficient_data",
            provenance=failed_provenance,
            config_json=context.config_json,
            diagnostics=diagnostics,
        )
        return AnalysisExecution(
            operation_id,
            run_id,
            "insufficient_data",
            snapshot_id,
            analysis_definition_id,
            None,
            None,
            diagnostics,
            failed_provenance,
        )
    except _Unstable as error:
        diagnostics = (str(error),)
        failed_provenance = replace(candidate, result_id=None)
        store.persist_analysis_receipt(
            operation_id=operation_id,
            analysis_run_id=run_id,
            status="unstable",
            provenance=failed_provenance,
            config_json=context.config_json,
            diagnostics=diagnostics,
        )
        return AnalysisExecution(
            operation_id,
            run_id,
            "unstable",
            snapshot_id,
            analysis_definition_id,
            None,
            None,
            diagnostics,
            failed_provenance,
        )
    except AnalysisInputChanged:
        raise
    except Exception as error:
        raise AnalysisError("Ruhepulsanalyse konnte nicht abgeschlossen werden.") from error
    finally:
        store.close()


__all__ = [
    "AnalysisDefinitionId",
    "AnalysisError",
    "AnalysisExecution",
    "AnalysisInputChanged",
    "AnalysisProvenance",
    "AnalysisResultId",
    "AnalysisRunId",
    "run_resting_hr_analysis",
]
