"""PROTOTYPE — calibrate V0.4 weight trends/models; never import in production.

Run once with:
    uv run python prototypes/weight_model_calibration_106.py --mode full

Question: which fixed Ridge penalties, residual-block rule, bootstrap minimum,
common-coverage gate, and maturity diagnostics are adequate for the four V0.4
weight windows when the only available truth is versioned synthetic truth?
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

VERSION = "v0.4-weight-model-calibration-1"
WINDOWS = (7, 14, 30, 90)
FEATURES = ("intake", "protein", "carbohydrate", "fat", "active", "resting")
MODELS = {"energy": (0, 4, 5), "energy_macro": (0, 1, 2, 3, 4, 5)}
PROFILES = ("null", "energy", "energy_macro")
PENALTIES = (0.03, 0.1, 0.3, 1.0, 3.0, 10.0, 30.0, 100.0)
BLOCK_FACTORS = (1.0, 2.0, 3.0)


@dataclass(frozen=True)
class Scenario:
    name: str
    profile: str
    days: int
    collinearity: float
    residual_ar: float
    measurement_error: float
    coverage: float
    gap_days: int
    seed: int


@dataclass
class Data:
    weight: np.ndarray
    true_weight: np.ndarray
    features: np.ndarray
    true_features: np.ndarray
    common_complete: np.ndarray
    nutrition_status: np.ndarray
    activity_complete: np.ndarray
    resting_complete: np.ndarray


@dataclass
class Design:
    window: int
    model: str
    matrix: np.ndarray
    truth_matrix: np.ndarray
    outcome: np.ndarray
    truth_outcome: np.ndarray
    truth_beta: np.ndarray
    slope_operator: np.ndarray
    anchor_days: np.ndarray
    mean_coverage: float
    edge_fraction: float


@dataclass
class Prepared:
    scenario: Scenario
    data: Data
    observed_days: np.ndarray
    observed_weight: np.ndarray
    levels: dict[int, np.ndarray]
    residuals: dict[int, np.ndarray]
    designs: dict[tuple[int, str], Design]


def ar1(rng: np.random.Generator, size: int, coefficient: float) -> np.ndarray:
    values = np.empty(size)
    innovations = rng.normal(size=size)
    values[0] = innovations[0]
    for index in range(1, size):
        values[index] = coefficient * values[index - 1] + innovations[index]
    return (values - values.mean()) / values.std()


def simulate(scenario: Scenario) -> Data:
    rng = np.random.default_rng(scenario.seed)
    days = np.arange(scenario.days)
    common = ar1(rng, scenario.days, 0.55)
    independent = np.column_stack([ar1(rng, scenario.days, 0.35) for _ in range(5)])
    shared = math.sqrt(scenario.collinearity) * common[:, None]
    separate = math.sqrt(1.0 - scenario.collinearity) * independent
    protein_z, carbohydrate_z, fat_z, active_z, resting_z = (shared + separate).T

    protein = 120.0 + 24.0 * protein_z
    carbohydrate = 235.0 + 52.0 * carbohydrate_z
    fat = 76.0 + 17.0 * fat_z
    untracked = (1.0 - scenario.collinearity) * 350.0 * ar1(rng, scenario.days, 0.25)
    intake = 300.0 + 4.0 * protein + 4.0 * carbohydrate + 9.0 * fat + untracked
    active = 520.0 + 155.0 * active_z
    resting = 1_720.0 + 85.0 * resting_z
    true_features = np.column_stack((intake, protein, carbohydrate, fat, active, resting))
    standardized = (true_features - true_features.mean(axis=0)) / true_features.std(axis=0)

    beta = np.zeros(len(FEATURES))
    if scenario.profile in {"energy", "energy_macro"}:
        beta[[0, 4, 5]] = (0.11, -0.065, -0.04)
    if scenario.profile == "energy_macro":
        beta[[1, 2, 3]] = (0.045, -0.025, 0.035)
    baseline = 0.035 * (days / max(1, scenario.days - 1) - 0.5)
    rate_noise = 0.035 * ar1(rng, scenario.days, scenario.residual_ar)
    daily_rate = baseline + standardized @ beta + rate_noise
    true_weight = 78.0 + np.cumsum(daily_rate / 7.0)

    error_scale = 0.03 + 0.35 * scenario.measurement_error
    food_error = ar1(rng, scenario.days, 0.45)
    feature_noise = rng.normal(size=true_features.shape)
    feature_noise[:, :4] += food_error[:, None]
    features = true_features + error_scale * true_features.std(axis=0) * feature_noise
    weight = true_weight + scenario.measurement_error * ar1(rng, scenario.days, 0.45)

    nutrition_complete = rng.random(scenario.days) < scenario.coverage
    activity_complete = rng.random(scenario.days) < min(0.98, scenario.coverage + 0.08)
    resting_complete = rng.random(scenario.days) < min(0.98, scenario.coverage + 0.04)
    weight_observed = rng.random(scenario.days) < min(0.98, scenario.coverage + 0.12)
    if scenario.gap_days:
        width = min(scenario.gap_days, scenario.days // 5)
        starts = (
            scenario.days // 4,
            scenario.days // 2,
            scenario.days * 2 // 3,
            scenario.days // 3,
        )
        nutrition_complete[starts[0] : starts[0] + width] = False
        activity_complete[starts[1] : starts[1] + width] = False
        resting_complete[starts[2] : starts[2] + width] = False
        weight_observed[starts[3] : starts[3] + width] = False

    nutrition_status = np.full(scenario.days, "complete", dtype="U8")
    incomplete = ~nutrition_complete
    partial = incomplete & (rng.random(scenario.days) < 0.65)
    nutrition_status[partial] = "partial"
    nutrition_status[incomplete & ~partial] = "missing"
    features[nutrition_status == "missing", :4] = np.nan
    for day in np.flatnonzero(nutrition_status == "partial"):
        hidden = rng.choice(4, size=int(rng.integers(1, 4)), replace=False)
        features[day, hidden] = np.nan
    features[~activity_complete & (rng.random(scenario.days) < 0.65), 4] = np.nan
    features[~resting_complete & (rng.random(scenario.days) < 0.65), 5] = np.nan
    weight[~weight_observed] = np.nan
    common_complete = nutrition_complete & activity_complete & resting_complete
    return Data(
        weight=weight,
        true_weight=true_weight,
        features=features,
        true_features=true_features,
        common_complete=common_complete,
        nutrition_status=nutrition_status,
        activity_complete=activity_complete,
        resting_complete=resting_complete,
    )


def local_operator(
    observed_days: np.ndarray, targets: np.ndarray, bandwidth: float, derivative: bool
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rows = np.zeros((len(targets), len(observed_days)))
    valid = np.zeros(len(targets), dtype=bool)
    supports = np.zeros(len(targets), dtype=int)
    pivots = np.zeros(len(targets))
    minimum = max(4, math.ceil(0.22 * bandwidth))
    for row, target in enumerate(targets):
        distance = observed_days - target
        weights = np.maximum(0.0, 1.0 - np.abs(distance) / bandwidth)
        used = weights > 0.0
        supports[row] = int(np.count_nonzero(used))
        if supports[row] < minimum:
            continue
        selected_distance = distance[used]
        selected_weights = weights[used]
        s0 = float(selected_weights.sum())
        s1 = float(selected_weights @ selected_distance)
        s2 = float(selected_weights @ (selected_distance**2))
        determinant = s0 * s2 - s1 * s1
        pivots[row] = determinant / max(1e-12, s0 * s2)
        if determinant <= 1e-12 or pivots[row] < 1e-6:
            continue
        if derivative:
            rows[row, used] = selected_weights * (s0 * selected_distance - s1) / determinant
        else:
            rows[row, used] = selected_weights * (s2 - s1 * selected_distance) / determinant
        valid[row] = True
    return rows, valid, supports, pivots


def feature_averages(
    data: Data, targets: np.ndarray, window: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    observed = np.full((len(targets), len(FEATURES)), np.nan)
    truth = np.full_like(observed, np.nan)
    coverage = np.zeros(len(targets))
    valid = np.zeros(len(targets), dtype=bool)
    all_days = np.arange(len(data.weight))
    for row, target in enumerate(targets):
        in_window = np.abs(all_days - target) < window
        eligible = in_window & data.common_complete & np.all(np.isfinite(data.features), axis=1)
        expected = int(np.count_nonzero(in_window))
        count = int(np.count_nonzero(eligible))
        coverage[row] = count / max(1, expected)
        if count < max(3, math.ceil(0.12 * expected)):
            continue
        weights = 1.0 - np.abs(all_days[eligible] - target) / window
        observed[row] = np.average(data.features[eligible], axis=0, weights=weights)
        truth[row] = np.average(data.true_features[eligible], axis=0, weights=weights)
        valid[row] = True
    return observed, truth, coverage, valid


def make_designs(
    data: Data,
) -> tuple[
    np.ndarray,
    np.ndarray,
    dict[int, np.ndarray],
    dict[int, np.ndarray],
    dict[tuple[int, str], Design],
]:
    observed_days = np.flatnonzero(np.isfinite(data.weight))
    observed_weight = data.weight[observed_days]
    targets = np.arange(len(data.weight))
    levels: dict[int, np.ndarray] = {}
    residuals: dict[int, np.ndarray] = {}
    designs: dict[tuple[int, str], Design] = {}
    for window in WINDOWS:
        level_operator, level_valid, _, _ = local_operator(
            observed_days, observed_days, float(window), derivative=False
        )
        levels[window] = level_operator @ observed_weight
        levels[window][~level_valid] = observed_weight[~level_valid]
        residuals[window] = observed_weight - levels[window]
        slope_operator, slope_valid, _, _ = local_operator(
            observed_days, targets, float(window), derivative=True
        )
        averages, true_averages, coverage, feature_valid = feature_averages(data, targets, window)
        valid = slope_valid & feature_valid
        for model, columns in MODELS.items():
            selected = np.flatnonzero(valid)
            if len(selected) < len(columns) + 8:
                raise ArithmeticError("too_few_anchors")
            feature_values = averages[selected][:, columns]
            means = feature_values.mean(axis=0)
            scales = feature_values.std(axis=0)
            if np.any(scales <= 1e-9):
                raise ArithmeticError("no_feature_variation")
            scaled_time = (targets[selected] - targets[selected].mean()) / max(
                1.0, targets[selected].std()
            )
            matrix = np.column_stack(
                (np.ones(len(selected)), scaled_time, (feature_values - means) / scales)
            )
            truth_matrix = np.column_stack(
                (
                    np.ones(len(selected)),
                    scaled_time,
                    (true_averages[selected][:, columns] - means) / scales,
                )
            )
            outcome = slope_operator[selected] @ observed_weight * 7.0
            truth_outcome = slope_operator[selected] @ data.true_weight[observed_days] * 7.0
            truth_beta = np.linalg.lstsq(truth_matrix, truth_outcome, rcond=None)[0][2:]
            designs[(window, model)] = Design(
                window=window,
                model=model,
                matrix=matrix,
                truth_matrix=truth_matrix,
                outcome=outcome,
                truth_outcome=truth_outcome,
                truth_beta=truth_beta,
                slope_operator=slope_operator[selected] * 7.0,
                anchor_days=targets[selected],
                mean_coverage=float(np.mean(coverage[selected])),
                edge_fraction=float(
                    np.mean(
                        (targets[selected] < window)
                        | (targets[selected] >= len(data.weight) - window)
                    )
                ),
            )
    return observed_days, observed_weight, levels, residuals, designs


def prepare(scenario: Scenario) -> Prepared:
    data = simulate(scenario)
    observed_days, observed_weight, levels, residuals, designs = make_designs(data)
    return Prepared(scenario, data, observed_days, observed_weight, levels, residuals, designs)


def ridge_operator(
    design: Design, penalty: float
) -> tuple[np.ndarray, np.ndarray, float, float, int]:
    diagonal = np.concatenate((np.zeros(2), np.ones(design.matrix.shape[1] - 2)))
    system = design.matrix.T @ design.matrix + penalty * np.diag(diagonal)
    operator = np.linalg.pinv(system) @ design.matrix.T
    coefficients = operator @ design.outcome
    augmented = np.vstack((design.matrix, math.sqrt(penalty) * np.diag(np.sqrt(diagonal))))
    return (
        coefficients[2:],
        operator[2:] @ design.slope_operator,
        float(np.linalg.cond(design.matrix)),
        float(np.linalg.cond(augmented)),
        int(np.linalg.matrix_rank(design.matrix)),
    )


def blocked_prediction(design: Design, penalty: float) -> tuple[float, float]:
    predictions = np.full(len(design.outcome), np.nan)
    baseline = np.full(len(design.outcome), np.nan)
    for held_out in np.array_split(np.arange(len(design.outcome)), min(5, len(design.outcome))):
        train = np.ones(len(design.outcome), dtype=bool)
        train[held_out] = False
        if np.count_nonzero(train) <= design.matrix.shape[1] + 2:
            continue
        diagonal = np.concatenate((np.zeros(2), np.ones(design.matrix.shape[1] - 2)))
        system = design.matrix[train].T @ design.matrix[train] + penalty * np.diag(diagonal)
        coefficients = np.linalg.pinv(system) @ design.matrix[train].T @ design.outcome[train]
        base = np.linalg.lstsq(design.matrix[train, :2], design.outcome[train], rcond=None)[0]
        predictions[held_out] = design.matrix[held_out] @ coefficients
        baseline[held_out] = design.matrix[held_out, :2] @ base
    valid = np.isfinite(predictions)
    return (
        float(np.sqrt(np.mean((predictions[valid] - design.outcome[valid]) ** 2))),
        float(np.sqrt(np.mean((baseline[valid] - design.outcome[valid]) ** 2))),
    )


def fit_diagnostics(design: Design, penalty: float) -> dict[str, float | int | np.ndarray]:
    beta, transform, unpenalized, augmented, rank = ridge_operator(design, penalty)
    residuals = design.outcome - design.matrix @ np.concatenate(
        (
            np.linalg.lstsq(
                design.matrix[:, :2], design.outcome - design.matrix[:, 2:] @ beta, rcond=None
            )[0],
            beta,
        )
    )
    independent_residuals = residuals[:: max(1, design.window)]
    centered = independent_residuals - independent_residuals.mean()
    denominator = float(centered @ centered)
    max_acf = 1.0
    if denominator > 1e-12 and len(centered) > 4:
        max_acf = max(
            abs(float(centered[lag:] @ centered[:-lag] / denominator))
            for lag in range(1, min(4, len(centered) - 2) + 1)
        )
    lower = ridge_operator(design, max(PENALTIES[0], penalty / 3.0))[0]
    upper = ridge_operator(design, min(PENALTIES[-1], penalty * 3.0))[0]
    prediction_rmse, baseline_rmse = blocked_prediction(design, penalty)
    return {
        "beta": beta,
        "transform": transform,
        "coefficient_rmse": float(np.sqrt(np.mean((beta - design.truth_beta) ** 2))),
        "bias": float(np.mean(beta - design.truth_beta)),
        "unpenalized_condition": unpenalized,
        "augmented_condition": augmented,
        "rank": rank,
        "max_residual_acf": max_acf,
        "ridge_sensitivity": float(max(np.max(np.abs(lower - beta)), np.max(np.abs(upper - beta)))),
        "prediction_rmse": prediction_rmse,
        "baseline_rmse": baseline_rmse,
        "prediction_skill": 1.0 - prediction_rmse / max(1e-12, baseline_rmse),
    }


def scenarios(count: int, seed_offset: int) -> list[Scenario]:
    repeats = math.ceil(count / len(PROFILES))
    levels: dict[str, tuple[float | int, ...]] = {
        "days": (365, 540, 730),
        "collinearity": (0.25, 0.70, 0.95),
        "residual_ar": (0.0, 0.35, 0.65),
        "measurement_error": (0.05, 0.15, 0.30),
        "coverage": (0.55, 0.72, 0.90),
        "gap_days": (0, 14, 35),
    }
    schedules: dict[str, list[np.ndarray]] = {name: [] for name in levels}
    for profile_index in range(len(PROFILES)):
        rng = np.random.default_rng(seed_offset + profile_index)
        for name, choices in levels.items():
            schedule = np.resize(np.asarray(choices), repeats)
            rng.shuffle(schedule)
            schedules[name].append(schedule)
    result = []
    for index in range(count):
        profile_index = index % len(PROFILES)
        repeat = index // len(PROFILES)
        values = {name: schedule[profile_index][repeat] for name, schedule in schedules.items()}
        result.append(
            Scenario(
                name=f"{PROFILES[profile_index]}-{seed_offset + index:04d}",
                profile=PROFILES[profile_index],
                days=int(values["days"]),
                collinearity=float(values["collinearity"]),
                residual_ar=float(values["residual_ar"]),
                measurement_error=float(values["measurement_error"]),
                coverage=float(values["coverage"]),
                gap_days=int(values["gap_days"]),
                seed=100_000 + seed_offset + index,
            )
        )
    return result


def tune(prepared: list[Prepared]) -> tuple[dict[tuple[int, str], float], dict[str, object]]:
    selected: dict[tuple[int, str], float] = {}
    report: dict[str, object] = {}
    for key in product(WINDOWS, MODELS):
        rows = []
        for penalty in PENALTIES:
            coefficient_errors = []
            prediction_ratios = []
            conditions = []
            for case in prepared:
                design = case.designs[key]
                fit = fit_diagnostics(design, penalty)
                scale = 0.04 + float(np.sqrt(np.mean(design.truth_beta**2)))
                coefficient_errors.append(float(fit["coefficient_rmse"]) / scale)
                prediction_ratios.append(
                    float(fit["prediction_rmse"]) / max(1e-12, float(fit["baseline_rmse"]))
                )
                conditions.append(float(fit["augmented_condition"]))
            score = (
                float(np.median(prediction_ratios))
                + 0.55 * float(np.mean(coefficient_errors))
                + 0.01 * float(np.mean(np.log10(np.maximum(1.0, conditions))))
            )
            rows.append(
                {
                    "penalty": penalty,
                    "score": score,
                    "mean_scaled_coefficient_rmse": float(np.mean(coefficient_errors)),
                    "median_prediction_rmse_ratio": float(np.median(prediction_ratios)),
                    "median_augmented_condition": float(np.median(conditions)),
                }
            )
        rows.sort(key=lambda row: float(row["score"]))
        selected[key] = float(rows[0]["penalty"])
        report[f"{key[0]}:{key[1]}"] = rows
    return selected, report


def max_gap(mask: np.ndarray) -> int:
    observed = np.flatnonzero(mask)
    if len(observed) < 2:
        return len(mask)
    return int(max(np.diff(observed) - 1, default=0))


def block_length(observed_weights: int, factor: float, window: int) -> int:
    return max(math.ceil(window / 3.0), math.ceil(factor * observed_weights ** (1.0 / 3.0)))


def bootstrap_case(
    case: Prepared,
    penalties: dict[tuple[int, str], float],
    factor: float,
    repetitions: int,
    seed_offset: int = 0,
) -> dict[str, object]:
    fits = {key: fit_diagnostics(design, penalties[key]) for key, design in case.designs.items()}
    points = {
        model: np.concatenate([np.asarray(fits[(window, model)]["beta"]) for window in WINDOWS])
        for model in MODELS
    }
    truths = {
        model: np.concatenate([case.designs[(window, model)].truth_beta for window in WINDOWS])
        for model in MODELS
    }
    samples = {model: np.empty((repetitions, len(points[model]))) for model in MODELS}
    rng = np.random.default_rng(case.scenario.seed + seed_offset + 9_000_000)
    lengths = {window: block_length(len(case.observed_days), factor, window) for window in WINDOWS}
    failures = 0
    for repetition in range(repetitions):
        try:
            by_model: dict[str, list[np.ndarray]] = {model: [] for model in MODELS}
            for window in WINDOWS:
                length = lengths[window]
                starts = rng.integers(
                    0,
                    len(case.observed_days),
                    size=math.ceil(len(case.observed_days) / length),
                )
                indices = np.concatenate(
                    [(start + np.arange(length)) % len(case.observed_days) for start in starts]
                )[: len(case.observed_days)]
                pseudo_weight = case.levels[window] + case.residuals[window][indices]
                for model in MODELS:
                    transform = np.asarray(fits[(window, model)]["transform"])
                    by_model[model].append(transform @ pseudo_weight)
            for model in MODELS:
                samples[model][repetition] = np.concatenate(by_model[model])
        except (FloatingPointError, np.linalg.LinAlgError):
            failures += 1
            for model in MODELS:
                samples[model][repetition] = np.nan
    valid = np.all(
        np.column_stack([np.all(np.isfinite(value), axis=1) for value in samples.values()]), axis=1
    )
    return {
        "points": points,
        "truths": truths,
        "samples": {model: value[valid] for model, value in samples.items()},
        "fits": fits,
        "block_lengths": lengths,
        "successes": int(np.count_nonzero(valid)),
        "failures": failures,
    }


def floor_vector(calibration: dict[str, object], model: str) -> np.ndarray:
    return np.concatenate(
        [
            np.full(len(MODELS[model]), calibration["absolute_floors"][f"{window}:{model}"])
            for window in WINDOWS
        ]
    )


def evaluate_run(
    case: Prepared, run: dict[str, object], calibration: dict[str, object]
) -> dict[str, object]:
    family_metrics: dict[str, object] = {}
    for model in MODELS:
        point = np.asarray(run["points"][model])
        truth = np.asarray(run["truths"][model])
        samples = np.asarray(run["samples"][model])
        deviations = samples - point
        scale = np.maximum(1e-6, deviations.std(axis=0, ddof=1))
        maxima = np.max(np.abs(deviations) / scale, axis=1)
        bootstrap_critical = float(np.quantile(maxima, 0.95))
        absolute_floor = floor_vector(calibration, model)
        multiplier = calibration["family_multipliers"][model]
        half_width = np.maximum(bootstrap_critical * scale, multiplier * absolute_floor)
        point_half_width = np.maximum(1.96 * scale, absolute_floor)
        prediction_rmse = float(
            np.mean([run["fits"][(window, model)]["prediction_rmse"] for window in WINDOWS])
        )
        baseline_rmse = float(
            np.mean([run["fits"][(window, model)]["baseline_rmse"] for window in WINDOWS])
        )
        family_metrics[model] = {
            "bias": float(np.mean(point - truth)),
            "rmse": float(np.sqrt(np.mean((point - truth) ** 2))),
            "pointwise_coverage": float(np.mean(np.abs(point - truth) <= point_half_width)),
            "simultaneous_coverage": bool(np.all(np.abs(point - truth) <= half_width)),
            "null_false_alarm": bool(
                case.scenario.profile == "null" and np.any(np.abs(point) > half_width)
            ),
            "bootstrap_critical": bootstrap_critical,
            "synthetic_family_floor_multiplier": multiplier,
            "absolute_floor_by_window": {
                str(window): calibration["absolute_floors"][f"{window}:{model}"]
                for window in WINDOWS
            },
            "mean_band_width": float(np.mean(2.0 * half_width)),
            "prediction_rmse": prediction_rmse,
            "baseline_rmse": baseline_rmse,
            "prediction_skill": 1.0 - prediction_rmse / max(1e-12, baseline_rmse),
        }
    fit_values = list(run["fits"].values())
    diagnostics = {
        "overall_common_coverage": float(np.mean(case.data.common_complete)),
        "minimum_window_common_coverage": min(
            design.mean_coverage for design in case.designs.values()
        ),
        "maximum_common_gap": max_gap(case.data.common_complete),
        "weight_density": float(np.mean(np.isfinite(case.data.weight))),
        "minimum_anchors": min(len(design.outcome) for design in case.designs.values()),
        "maximum_unpenalized_condition": max(
            float(value["unpenalized_condition"]) for value in fit_values
        ),
        "maximum_augmented_condition": max(
            float(value["augmented_condition"]) for value in fit_values
        ),
        "maximum_residual_acf": max(float(value["max_residual_acf"]) for value in fit_values),
        "maximum_ridge_sensitivity": max(float(value["ridge_sensitivity"]) for value in fit_values),
        "minimum_prediction_skill": min(float(value["prediction_skill"]) for value in fit_values),
        "maximum_band_half_width": max(
            float(family_metrics[model]["mean_band_width"]) / 2.0 for model in MODELS
        ),
        "nutrition_complete_days": int(np.count_nonzero(case.data.nutrition_status == "complete")),
        "nutrition_partial_days": int(np.count_nonzero(case.data.nutrition_status == "partial")),
        "nutrition_missing_days": int(np.count_nonzero(case.data.nutrition_status == "missing")),
        "activity_complete_days": int(np.count_nonzero(case.data.activity_complete)),
        "resting_complete_days": int(np.count_nonzero(case.data.resting_complete)),
    }
    return {
        "scenario": asdict(case.scenario),
        "families": family_metrics,
        "diagnostics": diagnostics,
        "block_lengths": run["block_lengths"],
        "bootstrap_successes": run["successes"],
        "bootstrap_failures": run["failures"],
    }


def aggregate(rows: list[dict[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {"cases": len(rows)}
    for model in MODELS:
        metrics = [row["families"][model] for row in rows]
        nulls = [
            metric
            for row, metric in zip(rows, metrics, strict=True)
            if row["scenario"]["profile"] == "null"
        ]
        result[model] = {
            "mean_bias": float(np.mean([metric["bias"] for metric in metrics])),
            "mean_rmse": float(np.mean([metric["rmse"] for metric in metrics])),
            "pointwise_coverage": float(
                np.mean([metric["pointwise_coverage"] for metric in metrics])
            ),
            "simultaneous_coverage": float(
                np.mean([metric["simultaneous_coverage"] for metric in metrics])
            ),
            "null_false_alarm": float(np.mean([metric["null_false_alarm"] for metric in nulls]))
            if nulls
            else None,
            "mean_band_width": float(np.mean([metric["mean_band_width"] for metric in metrics])),
            "mean_prediction_skill": float(
                np.mean([metric["prediction_skill"] for metric in metrics])
            ),
        }
    result["bootstrap_failures"] = int(sum(row["bootstrap_failures"] for row in rows))
    result["bootstrap_attempts"] = int(
        sum(row["bootstrap_successes"] + row["bootstrap_failures"] for row in rows)
    )
    return result


def derive_band_calibration(runs: list[dict[str, object]]) -> dict[str, object]:
    absolute_floors = {
        f"{window}:{model}": float(
            np.quantile(
                [
                    np.max(
                        np.abs(
                            np.asarray(run["points"][model])[start:stop]
                            - np.asarray(run["truths"][model])[start:stop]
                        )
                    )
                    for run in runs
                ],
                0.975,
            )
        )
        for model, columns in MODELS.items()
        for window_index, window in enumerate(WINDOWS)
        for start, stop in [(window_index * len(columns), (window_index + 1) * len(columns))]
    }
    provisional = {"absolute_floors": absolute_floors, "family_multipliers": {}}
    family_multipliers = {}
    for model in MODELS:
        floor = floor_vector(provisional, model)
        family_multipliers[model] = float(
            np.quantile(
                [
                    np.max(
                        np.abs(np.asarray(run["points"][model]) - run["truths"][model])
                        / np.maximum(1e-9, floor)
                    )
                    for run in runs
                ],
                0.975,
            )
        )
    return {"absolute_floors": absolute_floors, "family_multipliers": family_multipliers}


def calibrate_block_rule(
    prepared: list[Prepared], penalties: dict[tuple[int, str], float], repetitions: int
) -> tuple[float, dict[str, object], dict[str, object]]:
    candidates: dict[str, object] = {}
    for factor in BLOCK_FACTORS:
        runs = [bootstrap_case(case, penalties, factor, repetitions) for case in prepared]
        calibration = derive_band_calibration(runs)
        rows = [
            evaluate_run(case, run, calibration) for case, run in zip(prepared, runs, strict=True)
        ]
        summary = aggregate(rows)
        minimum_coverage = min(float(summary[model]["simultaneous_coverage"]) for model in MODELS)
        maximum_false_alarm = max(
            float(summary[model]["null_false_alarm"] or 0.0) for model in MODELS
        )
        width = float(np.mean([summary[model]["mean_band_width"] for model in MODELS]))
        score = 20.0 * max(0.0, 0.95 - minimum_coverage) + 8.0 * maximum_false_alarm + width
        candidates[str(factor)] = {
            "band_calibration": calibration,
            "summary": summary,
            "score": score,
            "rows": rows,
            "runs": runs,
        }
    selected_key = min(candidates, key=lambda key: float(candidates[key]["score"]))
    selected = float(selected_key)
    return selected, candidates[selected_key]["band_calibration"], candidates


def gate_passes(row: dict[str, object], values: tuple[float, ...]) -> bool:
    coverage, gap, anchors, condition, acf, ridge, prediction, width = values
    diagnostic = row["diagnostics"]
    return bool(
        diagnostic["minimum_window_common_coverage"] >= coverage
        and diagnostic["maximum_common_gap"] <= gap
        and diagnostic["minimum_anchors"] >= anchors
        and diagnostic["maximum_unpenalized_condition"] <= condition
        and diagnostic["maximum_residual_acf"] <= acf
        and diagnostic["maximum_ridge_sensitivity"] <= ridge
        and diagnostic["minimum_prediction_skill"] >= prediction
        and diagnostic["maximum_band_half_width"] <= width
    )


def calibrate_gate(
    prepared: list[Prepared],
    runs: list[dict[str, object]],
    diagnostic_rows: list[dict[str, object]],
    minimum_cases: int,
) -> dict[str, object]:
    widths = (0.50, 0.35, 0.25) if minimum_cases <= 3 else (0.25, 0.20, 0.15)
    grids = (
        (0.35, 0.50, 0.65),
        (35.0, 18.0, 14.0),
        (100.0, 180.0, 250.0),
        (1_000.0, 300.0, 100.0),
        (0.80, 0.65, 0.50),
        (0.05, 0.025, 0.015),
        (-0.50,),
        widths,
    )
    best = None
    minimum_nulls = 1 if minimum_cases <= 3 else 2
    for values in product(*grids):
        diagnostic_values = (*values[:-1], 1e12)
        indices = [
            index
            for index, row in enumerate(diagnostic_rows)
            if gate_passes(row, diagnostic_values)
        ]
        if len(indices) < minimum_cases:
            continue
        calibration = derive_band_calibration([runs[index] for index in indices])
        calibrated = [evaluate_run(prepared[index], runs[index], calibration) for index in indices]
        kept = [row for row in calibrated if gate_passes(row, values)]
        nulls = sum(row["scenario"]["profile"] == "null" for row in kept)
        if len(kept) < minimum_cases or nulls < minimum_nulls:
            continue
        summary = aggregate(kept)
        coverage = min(float(summary[model]["simultaneous_coverage"]) for model in MODELS)
        pointwise = min(float(summary[model]["pointwise_coverage"]) for model in MODELS)
        false_alarm = max(float(summary[model]["null_false_alarm"] or 0.0) for model in MODELS)
        passes = coverage >= 0.90 and pointwise >= 0.85 and false_alarm <= 0.05
        score = (passes, len(kept), coverage, pointwise, -false_alarm)
        if best is None or score > best[0]:
            best = score, values, kept, calibration
    if best is None:
        raise ArithmeticError("no_maturity_gate")
    score, values, kept, calibration = best
    names = (
        "minimum_common_coverage_per_window",
        "maximum_common_gap_days",
        "minimum_anchors",
        "maximum_unpenalized_condition",
        "maximum_residual_acf",
        "maximum_ridge_sensitivity",
        "minimum_time_blocked_prediction_skill",
        "maximum_mean_band_half_width",
    )
    return {
        "meets_calibration_targets": bool(score[0]),
        "thresholds": dict(zip(names, values, strict=True)),
        "retained_cases": len(kept),
        "rejected_cases": len(diagnostic_rows) - len(kept),
        "retained_summary": aggregate(kept),
        "band_calibration": calibration,
    }


def apply_gate(rows: list[dict[str, object]], gate: dict[str, object]) -> list[dict[str, object]]:
    thresholds = gate["thresholds"]
    values = (
        thresholds["minimum_common_coverage_per_window"],
        thresholds["maximum_common_gap_days"],
        thresholds["minimum_anchors"],
        thresholds["maximum_unpenalized_condition"],
        thresholds["maximum_residual_acf"],
        thresholds["maximum_ridge_sensitivity"],
        thresholds["minimum_time_blocked_prediction_skill"],
        thresholds["maximum_mean_band_half_width"],
    )
    return [row for row in rows if gate_passes(row, values)]


def stability_probe(
    prepared: list[Prepared],
    penalties: dict[tuple[int, str], float],
    factor: float,
    calibration: dict[str, object],
    repetitions: int,
) -> dict[str, object]:
    ranked = sorted(
        prepared,
        key=lambda case: (
            -float(np.mean(case.data.common_complete)),
            max(np.linalg.cond(design.matrix) for design in case.designs.values()),
        ),
        reverse=True,
    )[: min(4, len(prepared))]
    prefix_changes: dict[str, list[float]] = {"250": [], "500": [], "1000": []}
    block_changes: list[float] = []
    successes = failures = 0
    neighbor = 2.0 if factor != 2.0 else 3.0
    for case in ranked:
        run = bootstrap_case(case, penalties, factor, repetitions, seed_offset=40_000)
        other = bootstrap_case(
            case, penalties, neighbor, min(1_000, repetitions), seed_offset=50_000
        )
        successes += int(run["successes"])
        failures += int(run["failures"])
        for model in MODELS:
            point = np.asarray(run["points"][model])
            samples = np.asarray(run["samples"][model])
            scale = np.maximum(1e-6, (samples - point).std(axis=0, ddof=1))
            maxima = np.max(np.abs(samples - point) / scale, axis=1)
            floor = floor_vector(calibration, model)
            multiplier = calibration["family_multipliers"][model]
            final_critical = float(np.quantile(maxima, 0.95))
            final = np.maximum(final_critical * scale, multiplier * floor)
            for prefix in (250, 500, 1_000):
                if prefix <= len(maxima):
                    critical = float(np.quantile(maxima[:prefix], 0.95))
                    value = np.maximum(critical * scale, multiplier * floor)
                    prefix_changes[str(prefix)].append(float(np.mean(np.abs(value - final))))
            other_point = np.asarray(other["points"][model])
            other_samples = np.asarray(other["samples"][model])
            other_scale = np.maximum(1e-6, (other_samples - other_point).std(axis=0, ddof=1))
            other_max = np.max(np.abs(other_samples - other_point) / other_scale, axis=1)
            other_critical = float(np.quantile(other_max, 0.95))
            other_width = np.maximum(other_critical * other_scale, multiplier * floor)
            block_changes.append(abs(float(np.mean(final)) - float(np.mean(other_width))))
    return {
        "cases": len(ranked),
        "successful_refits": successes,
        "failed_refits": failures,
        "mean_halfwidth_p95_change_from_full": {
            key: float(np.quantile(values, 0.95)) if values else None
            for key, values in prefix_changes.items()
        },
        "neighbor_block_mean_halfwidth_p95_change": float(np.quantile(block_changes, 0.95)),
        "neighbor_block_factor": neighbor,
    }


def serializable_block_candidates(candidates: dict[str, object]) -> dict[str, object]:
    return {
        key: {name: value for name, value in candidate.items() if name not in {"rows", "runs"}}
        for key, candidate in candidates.items()
    }


def markdown(report: dict[str, object]) -> str:
    selected = report["recommendation"]
    holdout = report["holdout"]["retained_summary"]
    lines = [
        "# V0.4 weight-model calibration prototype",
        "",
        f"Version: `{report['version']}` · Mode: `{report['mode']}`",
        "",
        "PROTOTYPE — synthetic evidence only; real personal data require V0.5 revalidation.",
        "",
        "## Candidate definition for HITL review",
        "",
        "| Window | Energy Ridge | Energy + macro Ridge |",
        "|---:|---:|---:|",
    ]
    penalties = selected["ridge_penalties"]
    for window in WINDOWS:
        energy = penalties[f"{window}:energy"]
        macro = penalties[f"{window}:energy_macro"]
        lines.append(f"| {window} days | {energy:g} | {macro:g} |")
    successes = selected["minimum_bootstrap_successes"]
    attempts = selected["maximum_bootstrap_attempts"]
    lines.extend(
        [
            "",
            "- Primary block rule per window: "
            f"`max(ceil(window/3), ceil({selected['block_factor']:g} x n_weight^(1/3)))` "
            "observed weights.",
            f"- Required bootstrap: {successes:,} successful refits in at most "
            f"{attempts:,} attempts.",
            f"- Synthetic absolute bias floors and family multipliers: "
            f"`{selected['band_calibration']}`.",
            "- Trend: the already-decided triangular local-linear fit, bandwidth = window, "
            "truncated edge support.",
            "- Missingness: only common `complete` nutrition/activity/resting-energy days "
            "contribute; partial values remain excluded and unfilled.",
            "",
            "## Independent synthetic holdout after maturity gate",
            "",
            "| Family | Cases | Bias | RMSE | Pointwise coverage | Simultaneous coverage | "
            "Null false alarm | Prediction skill |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for model in MODELS:
        item = holdout[model]
        lines.append(
            f"| {model} | {holdout['cases']} | {item['mean_bias']:.4f} | {item['mean_rmse']:.4f} | "
            f"{item['pointwise_coverage']:.1%} | {item['simultaneous_coverage']:.1%} | "
            f"{item['null_false_alarm']:.1%} | {item['mean_prediction_skill']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## Candidate maturity gate",
            "",
        ]
    )
    for name, value in selected["maturity_gate"]["thresholds"].items():
        lines.append(f"- `{name}`: `{value}`")
    stability = report["stability"]
    stability_successes = stability["successful_refits"]
    stability_failures = stability["failed_refits"]
    prefix_changes = stability["mean_halfwidth_p95_change_from_full"]
    block_change = stability["neighbor_block_mean_halfwidth_p95_change"]
    lines.extend(
        [
            "",
            "## Bootstrap stability and dependency",
            "",
            f"- Full stability probe: {stability_successes:,} successes, "
            f"{stability_failures:,} failures.",
            f"- Mean-halfwidth p95 prefix changes: `{prefix_changes}`.",
            f"- Neighbor-block p95 mean-halfwidth change: {block_change:.4f} kg/week "
            "per personal SD.",
            "- NumPy completed all final linear algebra and bootstrap work; no additional "
            "statistics dependency was needed.",
            "",
            "## Synthetic-data boundary",
            "",
            "These gates measure performance inside the versioned generator, not validity "
            "on personal data. Until V0.5 compares real coverage, collinearity, "
            "autocorrelation, gaps, source changes, and error proxies with this envelope, "
            "real outputs must remain `exploratory`; mismatches require extending the "
            "synthetic scenarios and recalibrating the whole definition, never tuning "
            "thresholds to the observed result.",
        ]
    )
    return "\n".join(lines) + "\n"


def run(mode: str) -> dict[str, object]:
    config = {
        "smoke": {"tune": 12, "calibrate": 12, "holdout": 12, "bootstrap": 100, "stability": 500},
        "full": {"tune": 48, "calibrate": 90, "holdout": 90, "bootstrap": 500, "stability": 2_000},
    }[mode]
    started = time.perf_counter()
    tuning = [prepare(value) for value in scenarios(config["tune"], 1_000)]
    calibration = [prepare(value) for value in scenarios(config["calibrate"], 20_000)]
    holdout_cases = [prepare(value) for value in scenarios(config["holdout"], 40_000)]
    penalties, tuning_report = tune(tuning)
    factor, _, block_candidates = calibrate_block_rule(calibration, penalties, config["bootstrap"])
    calibration_rows = block_candidates[str(factor)]["rows"]
    calibration_runs = block_candidates[str(factor)]["runs"]
    gate = calibrate_gate(
        calibration,
        calibration_runs,
        calibration_rows,
        3 if mode == "smoke" else 18,
    )
    band_calibration = gate["band_calibration"]
    holdout_runs = [
        bootstrap_case(case, penalties, factor, config["bootstrap"], seed_offset=20_000)
        for case in holdout_cases
    ]
    holdout_rows = [
        evaluate_run(case, result, band_calibration)
        for case, result in zip(holdout_cases, holdout_runs, strict=True)
    ]
    retained = apply_gate(holdout_rows, gate)
    stability = stability_probe(
        holdout_cases, penalties, factor, band_calibration, config["stability"]
    )
    report = {
        "version": VERSION,
        "mode": mode,
        "config": config,
        "synthetic_only": True,
        "recommendation": {
            "ridge_penalties": {
                f"{window}:{model}": value for (window, model), value in penalties.items()
            },
            "block_factor": factor,
            "band_calibration": band_calibration,
            "minimum_bootstrap_successes": 2_000,
            "maximum_bootstrap_attempts": 2_020,
            "maturity_gate": gate,
            "numpy_sufficient": True,
        },
        "tuning": tuning_report,
        "block_candidates": serializable_block_candidates(block_candidates),
        "calibration": {"all_summary": aggregate(calibration_rows), "gate": gate},
        "holdout": {
            "all_summary": aggregate(holdout_rows),
            "retained_cases": len(retained),
            "rejected_cases": len(holdout_rows) - len(retained),
            "retained_summary": aggregate(retained),
            "rows": holdout_rows,
        },
        "stability": stability,
        "runtime_seconds": time.perf_counter() - started,
    }
    assert all(value >= 0.0 for value in penalties.values())
    assert all(np.count_nonzero(case.data.nutrition_status == "partial") > 0 for case in tuning)
    assert report["holdout"]["retained_cases"] > 0
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("smoke", "full"), default="smoke")
    arguments = parser.parse_args()
    report = run(arguments.mode)
    stem = Path(__file__).with_name("weight_model_calibration_106_results")
    stem.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    stem.with_suffix(".md").write_text(markdown(report))
    print(markdown(report))
    print(f"Runtime: {report['runtime_seconds']:.1f}s")


if __name__ == "__main__":
    main()
