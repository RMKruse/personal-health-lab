"""PROTOTYPE — calibrate V0.4 distributed-lag choices; never import in production.

Run once with:
    uv run python prototypes/lag_calibration_102.py --mode full
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path

import numpy as np

VERSION = "v0.4-lag-calibration-1"
PROFILES = ("null", "smooth", "sharp", "delayed", "sign_change")
NO_MAXIMUM = 1e12


@dataclass(frozen=True)
class Candidate:
    basis_size: int
    smooth_penalty: float
    ridge_penalty: float


@dataclass(frozen=True)
class Scenario:
    horizon: int
    profile: str
    days: int
    feature_correlation: float
    overlap_ar: float
    gap_days: int
    context_lag: int
    noise_sd: float
    residual_ar: float
    training_support: float
    seed: int


@dataclass
class Data:
    day: np.ndarray
    exposures: np.ndarray
    outcome: np.ndarray
    context: np.ndarray
    truth: np.ndarray
    positive_training_days: int


@dataclass
class Fit:
    beta: np.ndarray
    se: np.ndarray
    residuals: np.ndarray
    complete_rows: int
    unpenalized_condition: float
    augmented_condition: float
    rank: int
    effective_df: float
    max_residual_acf: float
    ljung_box_per_lag: float
    cumulative: np.ndarray


def lag_basis(horizon: int, size: int) -> np.ndarray:
    lag = np.arange(horizon, dtype=float)
    knots = np.linspace(0.0, horizon - 1.0, size)
    eye = np.eye(size)
    return np.column_stack([np.interp(lag, knots, eye[:, index]) for index in range(size)])


def truth_profile(name: str, horizon: int) -> np.ndarray:
    lag = np.arange(1, horizon + 1, dtype=float)
    if name == "null":
        raw = np.zeros(horizon)
    elif name == "smooth":
        raw = -np.exp(-(lag - 1.0) / max(2.0, horizon / 5.0))
    elif name == "sharp":
        raw = -(
            (lag >= max(2, round(horizon * 0.12))) & (lag <= max(3, round(horizon * 0.28)))
        ).astype(float)
    elif name == "delayed":
        center = horizon * 0.58
        raw = -np.exp(-0.5 * ((lag - center) / max(1.0, horizon * 0.09)) ** 2)
    elif name == "sign_change":
        raw = np.exp(-0.5 * ((lag - horizon * 0.72) / max(1.0, horizon * 0.10)) ** 2)
        raw -= np.exp(-0.5 * ((lag - horizon * 0.22) / max(1.0, horizon * 0.10)) ** 2)
    else:
        raise ValueError(name)
    if np.any(raw):
        raw *= 1.2 / np.sum(np.abs(raw))
    return np.stack((raw, np.zeros(horizon), raw * 0.4))


def ar_series(rng: np.random.Generator, innovations: np.ndarray, coefficient: float) -> np.ndarray:
    values = np.empty_like(innovations)
    values[0] = innovations[0]
    for index in range(1, len(values)):
        values[index] = coefficient * values[index - 1] + innovations[index]
    return values


def simulate(scenario: Scenario) -> Data:
    rng = np.random.default_rng(scenario.seed)
    burn = 120
    total = burn + scenario.horizon + scenario.days
    covariance = np.array(
        [[1.0, scenario.feature_correlation], [scenario.feature_correlation, 1.0]]
    )
    innovations = rng.multivariate_normal(np.zeros(2), covariance, total)
    continuous = np.column_stack(
        [ar_series(rng, innovations[:, index], scenario.overlap_ar) for index in range(2)]
    )
    continuous = (continuous - continuous.mean(axis=0)) / continuous.std(axis=0)
    training_latent = 0.45 * continuous[:, 0] + rng.normal(size=total)
    cutoff = np.quantile(training_latent, 1.0 - scenario.training_support)
    training_raw = np.where(training_latent >= cutoff, np.exp(0.25 * training_latent), 0.0)
    training = (training_raw - training_raw.mean()) / training_raw.std()
    exposures = np.column_stack((continuous, training))

    context_noise = ar_series(rng, rng.normal(size=total), 0.45)
    previous_activity = np.concatenate(([0.0], continuous[:-1, 0]))
    context = 0.65 * previous_activity + context_noise
    context = (context - context.mean()) / context.std()
    residual = ar_series(rng, rng.normal(0.0, scenario.noise_sd, total), scenario.residual_ar)
    truth = truth_profile(scenario.profile, scenario.horizon)
    outcome = np.full(total, np.nan)
    for day in range(scenario.horizon, total):
        lagged = exposures[day - np.arange(1, scenario.horizon + 1)]
        signal = float(np.sum(lagged.T * truth))
        context_day = max(0, day - scenario.context_lag)
        weekday = 0.18 * math.sin(2.0 * math.pi * day / 7.0)
        season = 0.35 * math.sin(2.0 * math.pi * day / 365.25)
        outcome[day] = (
            62.0 + signal + 0.75 * context[context_day] + weekday + season + residual[day]
        )

    start = burn
    stop = total
    day = np.arange(scenario.days + scenario.horizon)
    exposures = exposures[start - scenario.horizon : stop].copy()
    outcome = outcome[start - scenario.horizon : stop].copy()
    context = context[start - scenario.horizon : stop].copy()
    training_raw = training_raw[start - scenario.horizon : stop]
    if scenario.gap_days:
        width = min(scenario.gap_days, scenario.days // 4)
        outcome[
            scenario.horizon + scenario.days // 3 : scenario.horizon + scenario.days // 3 + width
        ] = np.nan
        exposures[
            scenario.horizon + scenario.days * 2 // 3 : scenario.horizon
            + scenario.days * 2 // 3
            + width,
            :,
        ] = np.nan
        context[
            scenario.horizon + scenario.days // 2 : scenario.horizon
            + scenario.days // 2
            + width // 2
        ] = np.nan
    return Data(
        day=day,
        exposures=exposures,
        outcome=outcome,
        context=context,
        truth=truth,
        positive_training_days=int(np.count_nonzero(training_raw[scenario.horizon :])),
    )


def design(
    data: Data, candidate: Candidate, context_lags: tuple[int, ...] = (0,)
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    horizon = data.truth.shape[1]
    basis = lag_basis(horizon, candidate.basis_size)
    rows: list[int] = []
    lagged_rows: list[np.ndarray] = []
    for day in range(horizon, len(data.day)):
        lagged = data.exposures[day - np.arange(1, horizon + 1)]
        if (
            np.isfinite(data.outcome[day])
            and np.all(np.isfinite(lagged))
            and all(np.isfinite(data.context[day - lag]) for lag in context_lags)
        ):
            rows.append(day)
            lagged_rows.append(lagged)
    if not rows:
        raise np.linalg.LinAlgError("no complete rows")
    lagged = np.stack(lagged_rows)
    exposure_design = np.concatenate(
        [lagged[:, :, feature] @ basis for feature in range(data.exposures.shape[1])], axis=1
    )
    selected = np.asarray(rows)
    scaled_day = (selected - selected.mean()) / max(1.0, selected.std())
    nuisance = [
        np.ones(len(selected)),
        scaled_day,
        np.sin(2.0 * np.pi * selected / 365.25),
        np.cos(2.0 * np.pi * selected / 365.25),
    ]
    nuisance.extend((selected % 7 == weekday).astype(float) for weekday in range(1, 7))
    nuisance.extend(data.context[selected - lag] for lag in context_lags)
    matrix = np.column_stack((exposure_design, *nuisance))
    return matrix, data.outcome[selected], selected, basis, lagged


def penalty_matrix(
    candidate: Candidate, basis: np.ndarray, features: int, columns: int
) -> np.ndarray:
    second_difference = np.diff(np.eye(len(basis)), n=2, axis=0) @ basis
    per_feature = np.vstack(
        (
            math.sqrt(candidate.smooth_penalty) * second_difference,
            math.sqrt(candidate.ridge_penalty) * basis,
        )
    )
    penalty = np.zeros((features * len(per_feature), columns))
    for feature in range(features):
        start = feature * candidate.basis_size
        row = feature * len(per_feature)
        penalty[row : row + len(per_feature), start : start + candidate.basis_size] = per_feature
    return penalty


def residual_diagnostics(residuals: np.ndarray, max_lag: int) -> tuple[float, float]:
    centered = residuals - residuals.mean()
    denominator = float(centered @ centered)
    if denominator <= 0.0:
        return 1.0, math.inf
    correlations = np.array(
        [float(centered[lag:] @ centered[:-lag] / denominator) for lag in range(1, max_lag + 1)]
    )
    n = len(centered)
    q = n * (n + 2.0) * np.sum(correlations**2 / (n - np.arange(1, max_lag + 1)))
    return float(np.max(np.abs(correlations))), float(q / max_lag)


def fit_matrix(
    matrix: np.ndarray,
    outcome: np.ndarray,
    selected: np.ndarray,
    basis: np.ndarray,
    candidate: Candidate,
    features: int,
) -> Fit:
    penalty = penalty_matrix(candidate, basis, features, matrix.shape[1])
    augmented = np.vstack((matrix, penalty))
    target = np.concatenate((outcome, np.zeros(len(penalty))))
    coefficients, _, rank, _ = np.linalg.lstsq(augmented, target, rcond=None)
    residuals = outcome - matrix @ coefficients
    xtx = matrix.T @ matrix
    inverse = np.linalg.pinv(xtx + penalty.T @ penalty)
    degrees = float(np.trace(inverse @ xtx))
    sigma2 = float(residuals @ residuals / max(1.0, len(outcome) - degrees))
    covariance = sigma2 * inverse @ xtx @ inverse
    transform = np.zeros((features * len(basis), matrix.shape[1]))
    for feature in range(features):
        transform[
            feature * len(basis) : (feature + 1) * len(basis),
            feature * candidate.basis_size : (feature + 1) * candidate.basis_size,
        ] = basis
    beta = (transform @ coefficients).reshape(features, len(basis))
    variance = np.diag(transform @ covariance @ transform.T).reshape(features, len(basis))
    max_acf, q_per_lag = residual_diagnostics(residuals, min(14, max(1, len(residuals) // 10)))
    return Fit(
        beta=beta,
        se=np.sqrt(np.maximum(variance, 1e-12)),
        residuals=residuals,
        complete_rows=len(outcome),
        unpenalized_condition=float(np.linalg.cond(matrix)),
        augmented_condition=float(np.linalg.cond(augmented)),
        rank=int(rank),
        effective_df=degrees,
        max_residual_acf=max_acf,
        ljung_box_per_lag=q_per_lag,
        cumulative=beta.sum(axis=1),
    )


def fit(
    data: Data, candidate: Candidate, context_lags: tuple[int, ...] = (0,)
) -> tuple[Fit, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    parts = design(data, candidate, context_lags)
    return fit_matrix(*parts[:4], candidate, data.exposures.shape[1]), parts


def bootstrap(
    parts: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    candidate: Candidate,
    features: int,
    block_length: int,
    count: int,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, int]:
    matrix, outcome, selected, basis, _ = parts
    rng = np.random.default_rng(seed)
    blocks: list[np.ndarray] = []
    for start in range(len(selected)):
        end = start + 1
        while (
            end < len(selected)
            and end - start < block_length
            and selected[end] == selected[end - 1] + 1
        ):
            end += 1
        blocks.append(np.arange(start, end))
    betas: list[np.ndarray] = []
    ses: list[np.ndarray] = []
    failures = 0
    for _ in range(count):
        pieces: list[np.ndarray] = []
        size = 0
        while size < len(selected):
            block = blocks[int(rng.integers(len(blocks)))]
            pieces.append(block)
            size += len(block)
        indices = np.concatenate(pieces)[: len(selected)]
        try:
            result = fit_matrix(
                matrix[indices],
                outcome[indices],
                np.arange(len(indices)),
                basis,
                candidate,
                features,
            )
            if not np.all(np.isfinite(result.beta)) or not np.all(np.isfinite(result.se)):
                raise np.linalg.LinAlgError("non-finite")
            betas.append(result.beta)
            ses.append(result.se)
        except np.linalg.LinAlgError:
            failures += 1
    return np.asarray(betas), np.asarray(ses), failures


def candidates(horizon: int) -> list[Candidate]:
    sizes = (7,) if horizon == 7 else (6, 9, 12)
    smooth = (0.03, 0.3, 3.0, 30.0) if horizon == 7 else (0.3, 3.0, 30.0, 300.0)
    ridge = (0.01, 0.1, 1.0)
    return [
        Candidate(size, smooth_value, ridge_value)
        for size in sizes
        for smooth_value in smooth
        for ridge_value in ridge
    ]


def scenarios(horizon: int, count: int, seed_offset: int = 0) -> list[Scenario]:
    repeats = math.ceil(count / len(PROFILES))
    levels: dict[str, tuple[float | int, ...]] = {
        "days": (180, 365, 730),
        "feature_correlation": (0.2, 0.75, 0.95),
        "overlap_ar": (0.15, 0.55, 0.85),
        "gap_days": (0, 14, 35),
        "context_lag": (0, 2),
        "noise_sd": (0.55, 1.0, 1.7),
        "residual_ar": (0.0, 0.35, 0.65),
        "training_support": (0.05, 0.15, 0.30),
    }
    schedules: dict[str, list[np.ndarray]] = {name: [] for name in levels}
    for profile_index in range(len(PROFILES)):
        rng = np.random.default_rng(horizon * 100_000 + seed_offset + profile_index)
        for name, choices in levels.items():
            schedule = np.resize(np.asarray(choices), repeats)
            rng.shuffle(schedule)
            schedules[name].append(schedule)
    result: list[Scenario] = []
    for index in range(count):
        profile_index = index % len(PROFILES)
        repeat = index // len(PROFILES)
        values = {
            name: float(schedule[profile_index][repeat]) for name, schedule in schedules.items()
        }

        result.append(
            Scenario(
                horizon=horizon,
                profile=PROFILES[profile_index],
                days=int(values["days"]),
                feature_correlation=values["feature_correlation"],
                overlap_ar=values["overlap_ar"],
                gap_days=int(values["gap_days"]),
                context_lag=int(values["context_lag"]),
                noise_sd=values["noise_sd"],
                residual_ar=values["residual_ar"],
                training_support=values["training_support"],
                seed=10_000 * horizon + seed_offset + index,
            )
        )
    return result


def tune(horizon: int, cases: list[Scenario]) -> tuple[Candidate, list[dict[str, object]]]:
    generated = [(case, simulate(case)) for case in cases]
    scores: list[dict[str, object]] = []
    for candidate in candidates(horizon):
        errors: dict[str, list[float]] = {profile: [] for profile in PROFILES}
        biases: list[float] = []
        standardized_errors: list[float] = []
        for case, data in generated:
            result, _ = fit(data, candidate)
            difference = result.beta - data.truth
            errors[case.profile].append(float(np.sqrt(np.mean(difference**2))))
            biases.append(float(np.mean(difference)))
            standardized_errors.append(
                float(np.max(np.abs(difference) / np.maximum(result.se, 1e-9)))
            )
        per_profile = {name: float(np.mean(values)) for name, values in errors.items()}
        score = (
            max(per_profile.values())
            + 0.5 * float(np.mean(list(per_profile.values())))
            + abs(float(np.mean(biases)))
            + 0.03 * float(np.quantile(standardized_errors, 0.90))
        )
        scores.append(
            {
                "candidate": asdict(candidate),
                "score": score,
                "rmse_by_profile": per_profile,
                "standardized_error_p90": float(np.quantile(standardized_errors, 0.90)),
                "simultaneous_critical_floor": float(np.quantile(standardized_errors, 0.95)),
            }
        )
    scores.sort(key=lambda item: float(item["score"]))
    return Candidate(**scores[0]["candidate"]), scores


def block_length(rule: int, horizon: int, rows: int) -> int:
    return min(
        max(math.ceil(horizon / 3), math.ceil(rule * rows ** (1.0 / 3.0))), max(2, rows // 4)
    )


def intervals(
    point: Fit, betas: np.ndarray, _ses: np.ndarray, count: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    betas = betas[:count]
    lower, upper = np.quantile(betas, (0.025, 0.975), axis=0)
    studentized = np.max(np.abs((betas - point.beta) / np.maximum(point.se, 1e-9)), axis=(1, 2))
    critical = float(np.quantile(studentized, 0.95))
    simultaneous_lower = point.beta - critical * point.se
    simultaneous_upper = point.beta + critical * point.se
    return lower, upper, simultaneous_lower, simultaneous_upper, critical


def evaluate_case(
    scenario: Scenario,
    candidate: Candidate,
    rule: int,
    bootstrap_count: int,
    critical_floor: float = 0.0,
) -> dict[str, object]:
    data = simulate(scenario)
    started = time.perf_counter()
    point, parts = fit(data, candidate)
    expanded_context, _ = fit(data, candidate, context_lags=(0, 1, 2))
    length = block_length(rule, scenario.horizon, point.complete_rows)
    betas, ses, failures = bootstrap(
        parts,
        candidate,
        data.exposures.shape[1],
        length,
        bootstrap_count,
        scenario.seed + 1_000_000,
    )
    if len(betas) < max(50, int(bootstrap_count * 0.8)):
        raise RuntimeError("too many bootstrap failures")
    lower, upper, simultaneous_lower, simultaneous_upper, critical = intervals(
        point, betas, ses, len(betas)
    )
    if critical < critical_floor:
        critical = critical_floor
        simultaneous_lower = point.beta - critical * point.se
        simultaneous_upper = point.beta + critical * point.se
    truth = data.truth
    null = scenario.profile == "null"
    result: dict[str, object] = {
        "scenario": asdict(scenario),
        "block_rule_multiplier": rule,
        "block_length": length,
        "bias": float(np.mean(point.beta - truth)),
        "rmse": float(np.sqrt(np.mean((point.beta - truth) ** 2))),
        "pointwise_coverage": float(np.mean((lower <= truth) & (truth <= upper))),
        "simultaneous_coverage": bool(
            np.all((simultaneous_lower <= truth) & (truth <= simultaneous_upper))
        ),
        "null_false_alarm": bool(
            null and np.any((simultaneous_lower > 0.0) | (simultaneous_upper < 0.0))
        ),
        "band_mean_width": float(np.mean(simultaneous_upper - simultaneous_lower)),
        "critical_value": critical,
        "bootstrap_failures": failures,
        "bootstrap_success_rate": len(betas) / bootstrap_count,
        "runtime_seconds": time.perf_counter() - started,
        "complete_rows": point.complete_rows,
        "input_completeness": point.complete_rows / scenario.days,
        "effective_blocks": point.complete_rows // length,
        "positive_training_days": data.positive_training_days,
        "unpenalized_condition": point.unpenalized_condition,
        "augmented_condition": point.augmented_condition,
        "effective_df": point.effective_df,
        "max_residual_acf": point.max_residual_acf,
        "ljung_box_per_lag": point.ljung_box_per_lag,
        "context_sensitivity": float(
            np.max(np.abs(point.cumulative - expanded_context.cumulative))
        ),
        "bootstrap_stability": {},
    }
    for count in (250, 500, 1_000, 2_000):
        if count <= len(betas):
            *_, prefix_critical = intervals(point, betas, ses, count)
            result["bootstrap_stability"][str(count)] = max(prefix_critical, critical_floor)
    return result


def aggregate(rows: list[dict[str, object]]) -> dict[str, object]:
    null_rows = [row for row in rows if row["scenario"]["profile"] == "null"]
    return {
        "cases": len(rows),
        "mean_bias": float(np.mean([row["bias"] for row in rows])),
        "mean_rmse": float(np.mean([row["rmse"] for row in rows])),
        "pointwise_coverage": float(np.mean([row["pointwise_coverage"] for row in rows])),
        "simultaneous_coverage": float(np.mean([row["simultaneous_coverage"] for row in rows])),
        "null_false_alarm": float(np.mean([row["null_false_alarm"] for row in null_rows]))
        if null_rows
        else None,
        "mean_band_width": float(np.mean([row["band_mean_width"] for row in rows])),
        "bootstrap_failure_rate": float(
            1.0 - np.mean([row["bootstrap_success_rate"] for row in rows])
        ),
        "runtime_seconds": float(np.sum([row["runtime_seconds"] for row in rows])),
    }


def grouped(rows: list[dict[str, object]], field: str) -> dict[str, dict[str, object]]:
    values = sorted({str(row["scenario"][field]) for row in rows})
    return {
        value: aggregate([row for row in rows if str(row["scenario"][field]) == value])
        for value in values
    }


def choose_block_rule(pilot: dict[int, list[dict[str, object]]]) -> int:
    def score(rule: int) -> float:
        summary = aggregate(pilot[rule])
        under = max(0.0, 0.95 - float(summary["simultaneous_coverage"]))
        return (
            8.0 * under
            + 3.0 * float(summary["null_false_alarm"] or 0.0)
            + float(summary["mean_band_width"]) / 20.0
        )

    return min(pilot, key=score)


def calibrate_maturity_gate(rows: list[dict[str, object]]) -> dict[str, object]:
    grids = (
        (0, 120, 250, 500),
        (0.0, 0.60, 0.75, 0.85, 0.90),
        (0, 12, 20, 30),
        (0, 20, 40, 80),
        (NO_MAXIMUM, 100.0, 500.0, 2_000.0, 10_000.0),
        (1.0, 0.20, 0.35, 0.50),
        (NO_MAXIMUM, 0.5, 1.0, 2.0),
        (35, 14, 0),
    )
    best: tuple[tuple[float, ...], dict[str, object], list[dict[str, object]]] | None = None
    for values in product(*grids):
        (
            minimum_rows,
            minimum_completeness,
            minimum_blocks,
            minimum_training_days,
            maximum_condition,
            maximum_acf,
            maximum_context_sensitivity,
            maximum_gap_days,
        ) = values
        eligible = [
            row
            for row in rows
            if row["complete_rows"] >= minimum_rows
            and row["input_completeness"] >= minimum_completeness
            and row["effective_blocks"] >= minimum_blocks
            and row["positive_training_days"] >= minimum_training_days
            and row["augmented_condition"] <= maximum_condition
            and row["max_residual_acf"] <= maximum_acf
            and row["context_sensitivity"] <= maximum_context_sensitivity
            and row["scenario"]["gap_days"] <= maximum_gap_days
        ]
        null_count = sum(row["scenario"]["profile"] == "null" for row in eligible)
        if len(eligible) < max(12, len(rows) // 5) or null_count < 2:
            continue
        summary = aggregate(eligible)
        passes = (
            summary["simultaneous_coverage"] >= 0.90
            and summary["pointwise_coverage"] >= 0.90
            and summary["null_false_alarm"] <= 0.05
        )
        score = (
            float(passes),
            float(len(eligible)),
            float(summary["simultaneous_coverage"]),
            float(summary["pointwise_coverage"]),
            -float(summary["null_false_alarm"]),
            -float(summary["mean_band_width"]),
        )
        thresholds = {
            "minimum_complete_rows": minimum_rows,
            "minimum_input_completeness": minimum_completeness,
            "minimum_effective_blocks": minimum_blocks,
            "minimum_positive_training_days": minimum_training_days,
            "maximum_augmented_condition": maximum_condition,
            "maximum_residual_acf": maximum_acf,
            "maximum_context_sensitivity_bpm": maximum_context_sensitivity,
            "maximum_gap_days": maximum_gap_days,
        }
        if best is None or score > best[0]:
            best = score, thresholds, eligible
    if best is None:
        raise RuntimeError("no maturity gate retained enough cases")
    _, thresholds, eligible = best
    horizon = int(rows[0]["scenario"]["horizon"])
    tested_floors = {
        7: (100, 0.60, 18, 10),
        30: (120, 0.65, 11, 10),
    }
    row_floor, completeness_floor, block_floor, support_floor = tested_floors[horizon]
    thresholds["minimum_complete_rows"] = max(thresholds["minimum_complete_rows"], row_floor)
    thresholds["minimum_input_completeness"] = max(
        thresholds["minimum_input_completeness"], completeness_floor
    )
    thresholds["minimum_effective_blocks"] = max(
        thresholds["minimum_effective_blocks"], block_floor
    )
    thresholds["minimum_positive_training_days"] = max(
        thresholds["minimum_positive_training_days"], support_floor
    )
    eligible = [
        row
        for row in rows
        if row["complete_rows"] >= thresholds["minimum_complete_rows"]
        and row["input_completeness"] >= thresholds["minimum_input_completeness"]
        and row["effective_blocks"] >= thresholds["minimum_effective_blocks"]
        and row["positive_training_days"] >= thresholds["minimum_positive_training_days"]
        and row["augmented_condition"] <= thresholds["maximum_augmented_condition"]
        and row["max_residual_acf"] <= thresholds["maximum_residual_acf"]
        and row["context_sensitivity"] <= thresholds["maximum_context_sensitivity_bpm"]
        and row["scenario"]["gap_days"] <= thresholds["maximum_gap_days"]
    ]
    retained_summary = aggregate(eligible)
    passes = (
        retained_summary["simultaneous_coverage"] >= 0.90
        and retained_summary["pointwise_coverage"] >= 0.90
        and retained_summary["null_false_alarm"] <= 0.05
    )
    defaults = {
        "minimum_complete_rows": 0,
        "minimum_input_completeness": 0.0,
        "minimum_effective_blocks": 0,
        "minimum_positive_training_days": 0,
        "maximum_augmented_condition": NO_MAXIMUM,
        "maximum_residual_acf": 1.0,
        "maximum_context_sensitivity_bpm": NO_MAXIMUM,
        "maximum_gap_days": 35,
    }
    return {
        "meets_calibration_targets": passes,
        "thresholds": thresholds,
        "active_thresholds": {
            name: value for name, value in thresholds.items() if value != defaults[name]
        },
        "retained_cases": len(eligible),
        "rejected_cases": len(rows) - len(eligible),
        "retained_summary": retained_summary,
    }


def recommendations(rows: list[dict[str, object]], selected: Candidate) -> dict[str, object]:
    gate = calibrate_maturity_gate(rows)
    thresholds = gate["thresholds"]
    eligible = [
        row
        for row in rows
        if row["complete_rows"] >= thresholds["minimum_complete_rows"]
        and row["input_completeness"] >= thresholds["minimum_input_completeness"]
        and row["effective_blocks"] >= thresholds["minimum_effective_blocks"]
        and row["positive_training_days"] >= thresholds["minimum_positive_training_days"]
        and row["augmented_condition"] <= thresholds["maximum_augmented_condition"]
        and row["max_residual_acf"] <= thresholds["maximum_residual_acf"]
        and row["context_sensitivity"] <= thresholds["maximum_context_sensitivity_bpm"]
        and row["scenario"]["gap_days"] <= thresholds["maximum_gap_days"]
    ]
    critical_2000 = np.asarray(
        [row["bootstrap_stability"].get("2000", row["critical_value"]) for row in eligible]
    )
    stability: dict[str, float] = {}
    for count in (250, 500, 1_000):
        differences = [
            abs(
                float(row["bootstrap_stability"][str(count)])
                - float(row["bootstrap_stability"]["2000"])
            )
            for row in eligible
            if str(count) in row["bootstrap_stability"] and "2000" in row["bootstrap_stability"]
        ]
        if differences:
            stability[str(count)] = float(np.quantile(differences, 0.95))
    return {
        "candidate": asdict(selected),
        "maturity_gate": gate,
        "maximum_unpenalized_condition": float(
            np.quantile([row["unpenalized_condition"] for row in eligible], 0.95)
        ),
        "maximum_augmented_condition": float(
            np.quantile([row["augmented_condition"] for row in eligible], 0.95)
        ),
        "maximum_residual_acf": float(
            np.quantile([row["max_residual_acf"] for row in eligible], 0.95)
        ),
        "maximum_ljung_box_per_lag": float(
            np.quantile([row["ljung_box_per_lag"] for row in eligible], 0.95)
        ),
        "maximum_context_sensitivity_bpm": float(
            np.quantile([row["context_sensitivity"] for row in eligible], 0.95)
        ),
        "minimum_bootstrap_success_rate": 0.99,
        "bootstrap_critical_value_median": float(np.median(critical_2000)),
        "bootstrap_prefix_p95_difference_from_2000": stability,
        "minimum_bootstrap_successes": 2_000,
    }


def markdown(report: dict[str, object]) -> str:
    lines = [
        "# V0.4 lag calibration prototype",
        "",
        f"Version: `{report['version']}` · Mode: `{report['mode']}`",
        "",
        "PROTOTYPE — throw away after its decisions are captured in the Wayfinder ticket.",
        "",
        "## Result",
        "",
    ]
    for horizon in sorted(report["horizons"], key=int):
        result = report["horizons"][horizon]
        summary = result["summary"]
        recommendation = result["recommendation"]
        gated = recommendation["maturity_gate"]["retained_summary"]
        lines.extend(
            [
                f"### Horizon 1-{horizon}",
                "",
                f"- Candidate: `{recommendation['candidate']}`",
                (
                    "- Synthetic-bias floor for the studentized simultaneous critical "
                    f"value: {result['simultaneous_critical_floor']:.3f}."
                ),
                (
                    "- Block rule: `max(ceil(L/3), "
                    f"ceil({result['block_rule']} x n^(1/3)))`; selected length range "
                    f"{result['block_length_range']} days."
                ),
                f"- Bias {summary['mean_bias']:.3f} bpm; RMSE {summary['mean_rmse']:.3f} bpm.",
                (
                    f"- Pointwise coverage {summary['pointwise_coverage']:.1%}; "
                    f"simultaneous coverage {summary['simultaneous_coverage']:.1%}; "
                    f"null false alarm {summary['null_false_alarm']:.1%}."
                ),
                (
                    f"- Bootstrap failure rate {summary['bootstrap_failure_rate']:.2%}; "
                    f"runtime {summary['runtime_seconds']:.1f}s."
                ),
                (
                    "- Candidate maturity gate: "
                    f"{recommendation['maturity_gate']['active_thresholds']}; retained "
                    f"{recommendation['maturity_gate']['retained_cases']} cases."
                ),
                (
                    f"- Gated coverage: pointwise {gated['pointwise_coverage']:.1%}; "
                    f"simultaneous {gated['simultaneous_coverage']:.1%}; "
                    f"null false alarm {gated['null_false_alarm']:.1%}."
                ),
                "",
                "Factor slices:",
                "",
                "```json",
                json.dumps(result["by_factor"], indent=2, sort_keys=True),
                "```",
                "",
                "Bootstrap-count stability (95th percentile absolute critical-value "
                "difference from 2,000):",
                "",
                "```json",
                json.dumps(recommendation["bootstrap_prefix_p95_difference_from_2000"], indent=2),
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## Interpretation limits",
            "",
            "This is a calibration experiment, not production analysis. The synthetic "
            "mechanisms are versioned in the script; thresholds are valid only for that "
            "envelope. `robust` still means stable conditional association, never causality "
            "or medical validity.",
            "",
            "Raw scenario-level results are in `lag_calibration_102_results.json`.",
            "",
        ]
    )
    return "\n".join(lines)


def run(mode: str) -> dict[str, object]:
    tuning_count = 40 if mode == "quick" else 90
    pilot_count = 10 if mode == "quick" else 20
    final_count = 20 if mode == "quick" else 60
    pilot_bootstraps = 250 if mode == "quick" else 500
    final_bootstraps = 2_000
    report: dict[str, object] = {"version": VERSION, "mode": mode, "horizons": {}}
    for horizon in (7, 30):
        selected, tuning = tune(horizon, scenarios(horizon, tuning_count))
        critical_floor = float(tuning[0]["simultaneous_critical_floor"])
        pilot_cases = scenarios(horizon, pilot_count, seed_offset=200_000)
        pilot = {
            rule: [
                evaluate_case(case, selected, rule, pilot_bootstraps, critical_floor)
                for case in pilot_cases
            ]
            for rule in (1, 2, 3)
        }
        rule = choose_block_rule(pilot)
        final_rows = [
            evaluate_case(case, selected, rule, final_bootstraps, critical_floor)
            for case in scenarios(horizon, final_count, seed_offset=400_000)
        ]
        report["horizons"][str(horizon)] = {
            "selected_candidate": asdict(selected),
            "simultaneous_critical_floor": critical_floor,
            "tuning_top_five": tuning[:5],
            "block_pilot": {str(key): aggregate(value) for key, value in pilot.items()},
            "block_rule": rule,
            "block_length_range": [
                min(int(row["block_length"]) for row in final_rows),
                max(int(row["block_length"]) for row in final_rows),
            ],
            "summary": aggregate(final_rows),
            "by_factor": {
                field: grouped(final_rows, field)
                for field in (
                    "profile",
                    "days",
                    "feature_correlation",
                    "overlap_ar",
                    "gap_days",
                    "context_lag",
                    "noise_sd",
                    "residual_ar",
                    "training_support",
                )
            },
            "recommendation": recommendations(final_rows, selected),
            "rows": final_rows,
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("quick", "full"), default="quick")
    parser.add_argument("--output-dir", type=Path, default=Path("prototypes"))
    args = parser.parse_args()
    started = time.perf_counter()
    report = run(args.mode)
    report["total_runtime_seconds"] = time.perf_counter() - started
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "lag_calibration_102_results.json"
    markdown_path = args.output_dir / "lag_calibration_102_results.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    markdown_path.write_text(markdown(report))
    print(markdown(report))


if __name__ == "__main__":
    assert np.allclose(lag_basis(7, 4).sum(axis=1), 1.0)
    main()
