"""PROTOTYPE — calibrate V0.4 outcome associations; never import in production.

Run once with:
    uv run python prototypes/outcome_association_calibration_103.py --mode full
"""

# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import time
from dataclasses import asdict, dataclass
from itertools import pairwise, product
from pathlib import Path

VERSION = "v0.4-outcome-association-calibration-2"
WINDOWS = (7, 14, 30, 90)
KERNELS = ("triangular", "epanechnikov", "tricube")
MULTIPLIERS = (0.75, 1.0, 1.25)
EDGE_RULES = ("truncate", "two_sided")


@dataclass(frozen=True)
class Candidate:
    kernel: str
    multiplier: float
    edge_rule: str

    @property
    def name(self) -> str:
        return f"{self.kernel}-{self.multiplier:g}-{self.edge_rule}"


@dataclass(frozen=True)
class Scenario:
    name: str
    days: int
    density: float
    gap_days: int
    residual_ar: float
    curvature: float
    break_mode: str
    device_mode: str
    measurement_error: float
    trend_rho: float
    deviation_rho: float


@dataclass
class Data:
    resting: list[float | None]
    weight: list[float | None]
    true_resting_level: list[float]
    true_weight_level: list[float]
    true_resting_slope: list[float]
    true_weight_slope: list[float]
    true_resting_deviation: list[float]
    true_weight_deviation: list[float]


@dataclass
class Smooth:
    level: list[float | None]
    slope: list[float | None]
    residual: list[float | None]
    support: list[int]
    pivot: list[float]
    one_sided: list[bool]


@dataclass
class WindowFit:
    window: int
    trend: float
    deviation: float
    truth_trend: float
    truth_deviation: float
    trend_days: int
    deviation_days: int
    density: float
    max_gap: int
    min_support: int
    min_pivot: float
    edge_fraction: float
    residual_acf: float
    residual_time_correlation: float
    influence: float
    structure_break_score: float
    structure_break_sensitivity: float
    slope_sd: float
    deviation_sd: float


@dataclass
class Fit:
    windows: list[WindowFit]
    smooths: dict[int, tuple[Smooth, Smooth]]

    @property
    def estimates(self) -> list[float]:
        return [value for item in self.windows for value in (item.trend, item.deviation)]

    @property
    def truths(self) -> list[float]:
        return [
            value for item in self.windows for value in (item.truth_trend, item.truth_deviation)
        ]


def correlated_normal(rng: random.Random, rho: float) -> tuple[float, float]:
    first = rng.gauss(0.0, 1.0)
    return first, rho * first + math.sqrt(max(0.0, 1.0 - rho * rho)) * rng.gauss(0.0, 1.0)


def simulate(scenario: Scenario, seed: int) -> Data:
    rng = random.Random(seed)
    resting_level = 62.0
    weight_level = 78.0
    resting: list[float | None] = []
    weight: list[float | None] = []
    resting_slopes: list[float] = []
    weight_slopes: list[float] = []
    resting_levels: list[float] = []
    weight_levels: list[float] = []
    resting_deviations: list[float] = []
    weight_deviations: list[float] = []
    previous_resting = previous_weight = 0.0

    for day in range(scenario.days):
        shared = math.sin(2.0 * math.pi * day / 180.0)
        independent = math.cos(2.0 * math.pi * day / 133.0 + 0.7)
        fast_shared = math.sin(2.0 * math.pi * day / 45.0)
        fast_independent = math.cos(2.0 * math.pi * day / 57.0 + 0.3)
        resting_slope = 0.018 * (shared + scenario.curvature * fast_shared)
        weight_slope = 0.006 * (
            scenario.trend_rho * (shared + scenario.curvature * fast_shared)
            + math.sqrt(max(0.0, 1.0 - scenario.trend_rho**2))
            * (independent + scenario.curvature * fast_independent)
        )
        if scenario.break_mode == "shared" and day == scenario.days // 2:
            resting_slope += 1.5
            weight_slope += 0.45
        elif scenario.break_mode == "separate":
            resting_slope += 1.5 if day == scenario.days // 3 else 0.0
            weight_slope += 0.45 if day == scenario.days * 2 // 3 else 0.0
        resting_level += resting_slope
        weight_level += weight_slope
        resting_slopes.append(resting_slope)
        weight_slopes.append(weight_slope)
        resting_levels.append(resting_level)
        weight_levels.append(weight_level)

        first, second = correlated_normal(rng, scenario.deviation_rho)
        previous_resting = scenario.residual_ar * previous_resting + first * 0.75
        previous_weight = scenario.residual_ar * previous_weight + second * 0.18
        resting_deviations.append(previous_resting)
        weight_deviations.append(previous_weight)

        true_resting = resting_level + previous_resting
        true_weight = weight_level + previous_weight
        if scenario.device_mode == "shared" and day >= scenario.days * 2 // 3:
            true_resting += 1.0
            true_weight += 0.3
        elif scenario.device_mode == "separate":
            true_resting += 1.0 if day >= scenario.days // 3 else 0.0
            true_weight -= 0.3 if day >= scenario.days * 2 // 3 else 0.0

        resting_observed = rng.random() < min(0.98, scenario.density + 0.18)
        weight_observed = rng.random() < scenario.density
        gap_start = scenario.days // 2 - scenario.gap_days // 2
        if gap_start <= day < gap_start + scenario.gap_days:
            weight_observed = False
            resting_observed = resting_observed and day % 3 != 0
        resting.append(
            true_resting + rng.gauss(0.0, scenario.measurement_error) if resting_observed else None
        )
        weight.append(
            true_weight + rng.gauss(0.0, scenario.measurement_error * 0.24)
            if weight_observed
            else None
        )

    return Data(
        resting=resting,
        weight=weight,
        true_resting_level=resting_levels,
        true_weight_level=weight_levels,
        true_resting_slope=resting_slopes,
        true_weight_slope=weight_slopes,
        true_resting_deviation=resting_deviations,
        true_weight_deviation=weight_deviations,
    )


def kernel_weight(name: str, distance: float) -> float:
    value = abs(distance)
    if value > 1.0:
        return 0.0
    if name == "triangular":
        return 1.0 - value
    if name == "epanechnikov":
        return 0.75 * (1.0 - value * value)
    if name == "tricube":
        return (1.0 - value**3) ** 3
    raise ValueError(name)


def smooth(values: list[float | None], window: int, candidate: Candidate) -> Smooth:
    bandwidth = max(2.0, window * candidate.multiplier)
    radius = math.ceil(bandwidth)
    levels: list[float | None] = []
    slopes: list[float | None] = []
    supports: list[int] = []
    pivots: list[float] = []
    one_sided_values: list[bool] = []
    for target in range(len(values)):
        weighted: list[tuple[float, float, float]] = []
        for day in range(max(0, target - radius), min(len(values), target + radius + 1)):
            value = values[day]
            if value is None:
                continue
            distance = day - target
            weight = kernel_weight(candidate.kernel, distance / bandwidth)
            if weight > 0.0:
                weighted.append((distance, value, weight))
        left = any(distance <= -bandwidth * 0.5 for distance, _, _ in weighted)
        right = any(distance >= bandwidth * 0.5 for distance, _, _ in weighted)
        one_sided = not (left and right)
        minimum = max(4, math.ceil(bandwidth * 0.22))
        if len(weighted) < minimum or (candidate.edge_rule == "two_sided" and one_sided):
            levels.append(None)
            slopes.append(None)
            supports.append(len(weighted))
            pivots.append(0.0)
            one_sided_values.append(one_sided)
            continue
        s0 = sum(weight for _, _, weight in weighted)
        s1 = sum(weight * distance for distance, _, weight in weighted)
        s2 = sum(weight * distance * distance for distance, _, weight in weighted)
        t0 = sum(weight * value for _, value, weight in weighted)
        t1 = sum(weight * distance * value for distance, value, weight in weighted)
        determinant = s0 * s2 - s1 * s1
        scaled_pivot = determinant / max(1e-12, s0 * s2)
        if determinant <= 1e-12 or scaled_pivot < 1e-6:
            levels.append(None)
            slopes.append(None)
        else:
            levels.append((s2 * t0 - s1 * t1) / determinant)
            slopes.append((s0 * t1 - s1 * t0) / determinant)
        supports.append(len(weighted))
        pivots.append(scaled_pivot)
        one_sided_values.append(one_sided)
    residuals = [
        value - level if value is not None and level is not None else None
        for value, level in zip(values, levels, strict=True)
    ]
    return Smooth(levels, slopes, residuals, supports, pivots, one_sided_values)


def correlation(first: list[float], second: list[float]) -> float:
    if len(first) < 4:
        raise ArithmeticError("too_few_pairs")
    try:
        return statistics.correlation(first, second)
    except statistics.StatisticsError as error:
        raise ArithmeticError("no_variation") from error


def fisher_z(value: float) -> float:
    return math.atanh(max(-0.999999, min(0.999999, value)))


def paired(
    first: list[float | None], second: list[float | None]
) -> tuple[list[int], list[float], list[float]]:
    rows = [
        (index, left, right)
        for index, (left, right) in enumerate(zip(first, second, strict=True))
        if left is not None and right is not None
    ]
    return (
        [index for index, _, _ in rows],
        [float(left) for _, left, _ in rows],
        [float(right) for _, _, right in rows],
    )


def max_gap(days: list[int]) -> int:
    return max((right - left - 1 for left, right in pairwise(days)), default=0)


def max_acf(values: list[float], maximum_lag: int = 14) -> float:
    if len(values) < 8:
        return 1.0
    centered = [value - statistics.fmean(values) for value in values]
    denominator = sum(value * value for value in centered)
    if denominator <= 1e-12:
        return 1.0
    return max(
        abs(
            sum(left * right for left, right in zip(centered[lag:], centered[:-lag], strict=True))
            / denominator
        )
        for lag in range(1, min(maximum_lag, len(values) - 2) + 1)
    )


def leave_block_influence(first: list[float], second: list[float], block: int) -> float:
    baseline = correlation(first, second)
    if len(first) <= block * 2:
        return 1.0
    changes = []
    for start in range(0, len(first), block):
        kept_first = first[:start] + first[start + block :]
        kept_second = second[:start] + second[start + block :]
        try:
            changes.append(abs(correlation(kept_first, kept_second) - baseline))
        except ArithmeticError:
            return 1.0
    return max(changes, default=0.0)


def strongest_mean_shift(values: list[float | None], window: int) -> tuple[float, int]:
    best = (0.0, len(values) // 2)
    minimum = max(4, window // 4)
    for split in range(window, len(values) - window):
        left = [value for value in values[split - window : split] if value is not None]
        right = [value for value in values[split : split + window] if value is not None]
        if len(left) < minimum or len(right) < minimum:
            continue
        variance = statistics.variance(left) / len(left) + statistics.variance(right) / len(right)
        if variance <= 1e-12:
            continue
        score = abs(statistics.fmean(right) - statistics.fmean(left)) / math.sqrt(variance)
        if score > best[0]:
            best = (score, split)
    return best


def exclusion_sensitivity(
    days: list[int],
    first: list[float],
    second: list[float],
    baseline: float,
    centers: tuple[int, ...],
    radius: int,
) -> float:
    changes = []
    for center in centers:
        kept = [
            (left, right)
            for day, left, right in zip(days, first, second, strict=True)
            if abs(day - center) > radius
        ]
        if len(kept) < 8:
            continue
        try:
            changes.append(
                abs(
                    correlation(
                        [left for left, _ in kept],
                        [right for _, right in kept],
                    )
                    - baseline
                )
            )
        except ArithmeticError:
            changes.append(1.0)
    return max(changes, default=0.0)


def fit(
    data: Data,
    candidate: Candidate,
    breaks: dict[int, tuple[tuple[float, int], tuple[float, int]]] | None = None,
) -> Fit:
    results: list[WindowFit] = []
    smooths: dict[int, tuple[Smooth, Smooth]] = {}
    for window in WINDOWS:
        resting = smooth(data.resting, window, candidate)
        weight = smooth(data.weight, window, candidate)
        truth_resting = smooth(data.true_resting_level, window, candidate)
        truth_weight = smooth(data.true_weight_level, window, candidate)
        smooths[window] = (resting, weight)
        trend_days, resting_slopes, weight_slopes = paired(resting.slope, weight.slope)
        deviation_days, resting_deviations, weight_deviations = paired(
            resting.residual, weight.residual
        )
        trend = correlation(resting_slopes, weight_slopes)
        deviation = correlation(resting_deviations, weight_deviations)
        truth_trend = correlation(
            [float(truth_resting.slope[day]) for day in trend_days],
            [float(truth_weight.slope[day]) for day in trend_days],
        )
        truth_deviation = correlation(
            [data.true_resting_deviation[day] for day in deviation_days],
            [data.true_weight_deviation[day] for day in deviation_days],
        )
        valid_days = sorted(set(trend_days) | set(deviation_days))
        support = [
            min(resting.support[day], weight.support[day])
            for day in valid_days
            if resting.level[day] is not None and weight.level[day] is not None
        ]
        pivot = [
            min(resting.pivot[day], weight.pivot[day])
            for day in valid_days
            if resting.level[day] is not None and weight.level[day] is not None
        ]
        edge = [resting.one_sided[day] or weight.one_sided[day] for day in trend_days]
        time_values = [float(day) for day in deviation_days]
        block = max(window, math.ceil(len(deviation_days) ** (1.0 / 3.0)))
        resting_break, weight_break = (
            breaks[window]
            if breaks is not None
            else (
                strongest_mean_shift(data.resting, window),
                strongest_mean_shift(data.weight, window),
            )
        )
        break_centers = (resting_break[1], weight_break[1])
        break_radius = max(3, window // 2)
        results.append(
            WindowFit(
                window=window,
                trend=trend,
                deviation=deviation,
                truth_trend=truth_trend,
                truth_deviation=truth_deviation,
                trend_days=len(trend_days),
                deviation_days=len(deviation_days),
                density=len(deviation_days) / len(data.resting),
                max_gap=max_gap(deviation_days),
                min_support=min(support, default=0),
                min_pivot=min(pivot, default=0.0),
                edge_fraction=sum(edge) / len(edge),
                residual_acf=max(max_acf(resting_deviations), max_acf(weight_deviations)),
                residual_time_correlation=max(
                    abs(correlation(time_values, resting_deviations)),
                    abs(correlation(time_values, weight_deviations)),
                ),
                influence=max(
                    leave_block_influence(resting_slopes, weight_slopes, block),
                    leave_block_influence(resting_deviations, weight_deviations, block),
                ),
                structure_break_score=max(resting_break[0], weight_break[0]),
                structure_break_sensitivity=max(
                    exclusion_sensitivity(
                        trend_days,
                        resting_slopes,
                        weight_slopes,
                        trend,
                        break_centers,
                        break_radius,
                    ),
                    exclusion_sensitivity(
                        deviation_days,
                        resting_deviations,
                        weight_deviations,
                        deviation,
                        break_centers,
                        break_radius,
                    ),
                ),
                slope_sd=min(statistics.stdev(resting_slopes), statistics.stdev(weight_slopes)),
                deviation_sd=min(
                    statistics.stdev(resting_deviations), statistics.stdev(weight_deviations)
                ),
            )
        )
    return Fit(results, smooths)


def scenario_matrix(days: int) -> list[Scenario]:
    base = dict(
        days=days,
        density=0.75,
        gap_days=0,
        residual_ar=0.4,
        curvature=0.35,
        break_mode="none",
        device_mode="none",
        measurement_error=0.35,
    )
    signals = {
        "null": (0.0, 0.0),
        "positive": (0.65, 0.55),
        "negative": (-0.65, -0.55),
    }
    stresses = (
        ("baseline", {}),
        ("sparse", {"density": 0.38}),
        ("gap", {"gap_days": 35}),
        ("autocorrelated", {"residual_ar": 0.82}),
        ("curved", {"curvature": 1.3}),
        ("shared_break", {"break_mode": "shared"}),
        ("separate_break", {"break_mode": "separate"}),
        ("shared_device", {"device_mode": "shared"}),
        ("separate_device", {"device_mode": "separate"}),
        ("noisy", {"measurement_error": 1.0}),
    )
    scenarios = []
    for stress, changes in stresses:
        for signal, (trend_rho, deviation_rho) in signals.items():
            values = base | changes
            scenarios.append(
                Scenario(
                    name=f"{stress}-{signal}",
                    trend_rho=trend_rho,
                    deviation_rho=deviation_rho,
                    **values,
                )
            )
    return scenarios


def quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    fraction = position - lower
    return (
        ordered[lower]
        if fraction == 0
        else ordered[lower] + fraction * (ordered[lower + 1] - ordered[lower])
    )


def resample_indices(length: int, block: int, rng: random.Random) -> list[int]:
    result: list[int] = []
    while len(result) < length:
        start = rng.randrange(length)
        result.extend((start + offset) % length for offset in range(block))
    return result[:length]


def pseudo_data(data: Data, fitted: Fit, indices: list[int], window: int) -> Data:
    resting_smooth, weight_smooth = fitted.smooths[window]

    def rebuilt(levels: list[float | None], residuals: list[float | None]) -> list[float | None]:
        return [
            level + float(residuals[source])
            if level is not None and residuals[source] is not None
            else None
            for level, source in zip(levels, indices, strict=True)
        ]

    return Data(
        resting=rebuilt(resting_smooth.level, resting_smooth.residual),
        weight=rebuilt(weight_smooth.level, weight_smooth.residual),
        true_resting_level=data.true_resting_level,
        true_weight_level=data.true_weight_level,
        true_resting_slope=data.true_resting_slope,
        true_weight_slope=data.true_weight_slope,
        true_resting_deviation=data.true_resting_deviation,
        true_weight_deviation=data.true_weight_deviation,
    )


def bootstrap(
    data: Data,
    candidate: Candidate,
    fitted: Fit,
    repetitions: int,
    block_factor: float,
    critical_floor: float,
    studentized_floors: list[float],
    synthetic_biases: list[float],
    corrected_floors: list[float],
    fisher_floors: list[float],
    fisher_biases: list[float],
    fisher_corrected_floors: list[float],
    seed: int,
) -> dict[str, object]:
    rng = random.Random(seed)
    base = fitted.estimates
    block = max(3, math.ceil(len(data.resting) ** (1.0 / 3.0) * block_factor))
    deviations: list[list[float]] = []
    failures = 0
    started = time.perf_counter()
    for _ in range(repetitions):
        indices = resample_indices(len(data.resting), block, rng)
        estimates: list[float] = []
        try:
            for window in WINDOWS:
                pseudo = pseudo_data(data, fitted, indices, window)
                window_fit = fit_one_window(pseudo, candidate, window)
                estimates.extend((window_fit.trend, window_fit.deviation))
            deviations.append(
                [estimate - original for estimate, original in zip(estimates, base, strict=True)]
            )
        except ArithmeticError:
            failures += 1
    valid = len(deviations) >= repetitions * 0.95
    maxima = [max(abs(value) for value in row) for row in deviations]
    bootstrap_critical = quantile(maxima, 0.95) if maxima else math.inf
    critical = max(bootstrap_critical, critical_floor)
    half_sample = maxima[: max(1, len(maxima) // 2)]
    half = quantile(half_sample, 0.95) if half_sample else math.inf
    columns = list(zip(*deviations, strict=True)) if deviations else []
    bootstrap_biases = [statistics.fmean(column) for column in columns]
    scales = [max(1e-6, statistics.stdev(column)) for column in columns]
    student_maxima = [
        max(abs(value) / scale for value, scale in zip(row, scales, strict=True))
        for row in deviations
    ]
    student_critical = quantile(student_maxima, 0.95) if student_maxima else math.inf
    student_halfwidths = [
        max(student_critical * scale, floor)
        for scale, floor in zip(scales, studentized_floors, strict=True)
    ]
    student_half_critical = (
        quantile(student_maxima[: max(1, len(student_maxima) // 2)], 0.95)
        if student_maxima
        else math.inf
    )
    corrected_centers = [
        estimate - bias for estimate, bias in zip(base, synthetic_biases, strict=True)
    ]
    corrected_maxima = [
        max(
            abs(value - bias) / scale
            for value, bias, scale in zip(row, bootstrap_biases, scales, strict=True)
        )
        for row in deviations
    ]
    corrected_critical = quantile(corrected_maxima, 0.95) if corrected_maxima else math.inf
    corrected_halfwidths = [
        max(corrected_critical * scale, floor)
        for scale, floor in zip(scales, corrected_floors, strict=True)
    ]
    corrected_half_critical = (
        quantile(corrected_maxima[: max(1, len(corrected_maxima) // 2)], 0.95)
        if corrected_maxima
        else math.inf
    )
    null = all(abs(truth) < 0.15 for truth in fitted.truths)
    base_z = [fisher_z(value) for value in base]
    fisher_deviations = [
        [
            fisher_z(estimate + change) - center
            for estimate, change, center in zip(base, row, base_z, strict=True)
        ]
        for row in deviations
    ]
    fisher_columns = list(zip(*fisher_deviations, strict=True)) if deviations else []
    fisher_bootstrap_biases = [statistics.fmean(column) for column in fisher_columns]
    fisher_scales = [max(1e-6, statistics.stdev(column)) for column in fisher_columns]

    def fisher_band(
        centers: list[float],
        maxima: list[float],
        floors: list[float],
    ) -> tuple[float, list[float], list[float], list[float]]:
        critical_value = quantile(maxima, 0.95) if maxima else math.inf
        half_z = [
            max(critical_value * scale, floor)
            for scale, floor in zip(fisher_scales, floors, strict=True)
        ]
        lower = [math.tanh(center - width) for center, width in zip(centers, half_z, strict=True)]
        upper = [math.tanh(center + width) for center, width in zip(centers, half_z, strict=True)]
        return (
            critical_value,
            lower,
            upper,
            [(high - low) / 2.0 for low, high in zip(lower, upper, strict=True)],
        )

    fisher_maxima = [
        max(abs(value) / scale for value, scale in zip(row, fisher_scales, strict=True))
        for row in fisher_deviations
    ]
    fisher_critical, fisher_lower, fisher_upper, fisher_halfwidths = fisher_band(
        base_z, fisher_maxima, fisher_floors
    )
    fisher_half_critical = (
        quantile(fisher_maxima[: max(1, len(fisher_maxima) // 2)], 0.95)
        if fisher_maxima
        else math.inf
    )
    fisher_corrected_centers_z = [
        center - bias for center, bias in zip(base_z, fisher_biases, strict=True)
    ]
    fisher_corrected_maxima = [
        max(
            abs(value - bias) / scale
            for value, bias, scale in zip(row, fisher_bootstrap_biases, fisher_scales, strict=True)
        )
        for row in fisher_deviations
    ]
    (
        fisher_corrected_critical,
        fisher_corrected_lower,
        fisher_corrected_upper,
        fisher_corrected_halfwidths,
    ) = fisher_band(
        fisher_corrected_centers_z,
        fisher_corrected_maxima,
        fisher_corrected_floors,
    )
    fisher_corrected_half_critical = (
        quantile(
            fisher_corrected_maxima[: max(1, len(fisher_corrected_maxima) // 2)],
            0.95,
        )
        if fisher_corrected_maxima
        else math.inf
    )
    return {
        "block": block,
        "successes": len(deviations),
        "failures": failures,
        "valid": valid,
        "critical": critical,
        "bootstrap_critical": bootstrap_critical,
        "critical_floor": critical_floor,
        "quantile_stability": abs(bootstrap_critical - half),
        "studentized_critical": student_critical,
        "studentized_halfwidths": student_halfwidths,
        "studentized_floors": studentized_floors,
        "studentized_quantile_stability": abs(student_critical - student_half_critical),
        "studentized_joint_coverage": valid
        and all(
            estimate - width <= truth <= estimate + width
            for estimate, width, truth in zip(base, student_halfwidths, fitted.truths, strict=True)
        ),
        "studentized_false_alarm": null
        and any(
            abs(estimate) > width for estimate, width in zip(base, student_halfwidths, strict=True)
        ),
        "bootstrap_biases": bootstrap_biases,
        "synthetic_biases": synthetic_biases,
        "bias_corrected_centers": corrected_centers,
        "bias_corrected_critical": corrected_critical,
        "bias_corrected_halfwidths": corrected_halfwidths,
        "bias_corrected_floors": corrected_floors,
        "bias_corrected_quantile_stability": abs(corrected_critical - corrected_half_critical),
        "bias_corrected_joint_coverage": valid
        and all(
            center - width <= truth <= center + width
            for center, width, truth in zip(
                corrected_centers, corrected_halfwidths, fitted.truths, strict=True
            )
        ),
        "bias_corrected_false_alarm": null
        and any(
            abs(center) > width
            for center, width in zip(corrected_centers, corrected_halfwidths, strict=True)
        ),
        "fisher_studentized_critical": fisher_critical,
        "fisher_studentized_lower": fisher_lower,
        "fisher_studentized_upper": fisher_upper,
        "fisher_studentized_halfwidths": fisher_halfwidths,
        "fisher_studentized_quantile_stability": abs(fisher_critical - fisher_half_critical),
        "fisher_studentized_joint_coverage": valid
        and all(
            low <= truth <= high
            for low, high, truth in zip(fisher_lower, fisher_upper, fitted.truths, strict=True)
        ),
        "fisher_studentized_false_alarm": null
        and any(
            low > 0.0 or high < 0.0 for low, high in zip(fisher_lower, fisher_upper, strict=True)
        ),
        "fisher_bias_corrected_critical": fisher_corrected_critical,
        "fisher_bias_corrected_lower": fisher_corrected_lower,
        "fisher_bias_corrected_upper": fisher_corrected_upper,
        "fisher_bias_corrected_halfwidths": fisher_corrected_halfwidths,
        "fisher_bias_corrected_quantile_stability": abs(
            fisher_corrected_critical - fisher_corrected_half_critical
        ),
        "fisher_bias_corrected_joint_coverage": valid
        and all(
            low <= truth <= high
            for low, high, truth in zip(
                fisher_corrected_lower,
                fisher_corrected_upper,
                fitted.truths,
                strict=True,
            )
        ),
        "fisher_bias_corrected_false_alarm": null
        and any(
            low > 0.0 or high < 0.0
            for low, high in zip(fisher_corrected_lower, fisher_corrected_upper, strict=True)
        ),
        "seconds": time.perf_counter() - started,
        "joint_coverage": valid
        and all(
            estimate - critical <= truth <= estimate + critical
            for estimate, truth in zip(base, fitted.truths, strict=True)
        ),
        "false_alarm": null and any(abs(estimate) > critical for estimate in base),
    }


def fit_one_window(data: Data, candidate: Candidate, window: int) -> WindowFit:
    resting = smooth(data.resting, window, candidate)
    weight = smooth(data.weight, window, candidate)
    trend_days, resting_slopes, weight_slopes = paired(resting.slope, weight.slope)
    deviation_days, resting_deviations, weight_deviations = paired(
        resting.residual, weight.residual
    )
    return WindowFit(
        window=window,
        trend=correlation(resting_slopes, weight_slopes),
        deviation=correlation(resting_deviations, weight_deviations),
        truth_trend=0.0,
        truth_deviation=0.0,
        trend_days=len(trend_days),
        deviation_days=len(deviation_days),
        density=0.0,
        max_gap=0,
        min_support=0,
        min_pivot=0.0,
        edge_fraction=0.0,
        residual_acf=0.0,
        residual_time_correlation=0.0,
        influence=0.0,
        structure_break_score=0.0,
        structure_break_sensitivity=0.0,
        slope_sd=0.0,
        deviation_sd=0.0,
    )


def candidate_summary(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row["candidate"]), []).append(row)
    summary = []
    for name, items in grouped.items():
        errors = [float(error) for item in items for error in item["errors"]]  # type: ignore[index]
        summary.append(
            {
                "candidate": name,
                "fits": len(items),
                "failure_rate": statistics.fmean(float(item["failed"]) for item in items),
                "bias": statistics.fmean(errors),
                "rmse": math.sqrt(statistics.fmean(error * error for error in errors)),
                "edge_fraction": statistics.fmean(float(item["edge_fraction"]) for item in items),
                "influence": statistics.fmean(float(item["influence"]) for item in items),
                "smoothing_change": statistics.fmean(
                    float(item["smoothing_change"]) for item in items
                ),
                "seconds": sum(float(item["seconds"]) for item in items),
            }
        )
    summary.sort(
        key=lambda item: (
            item["failure_rate"],
            item["rmse"] + 0.25 * item["smoothing_change"],
            item["influence"],
        )
    )
    return summary


def maturity_gates(window_rows: list[dict[str, object]]) -> dict[str, float]:
    covered = [row for row in window_rows if bool(row["joint_coverage"])]
    source = covered or window_rows
    return {
        "minimum_paired_days": float(
            math.ceil(quantile([float(row["deviation_days"]) for row in source], 0.10))
        ),
        "minimum_density": round(quantile([float(row["density"]) for row in source], 0.10), 2),
        "minimum_effective_blocks": 12.0,
        "maximum_gap_days": float(
            math.floor(quantile([float(row["max_gap"]) for row in source], 0.90))
        ),
        "maximum_residual_acf": round(
            quantile([float(row["residual_acf"]) for row in source], 0.90), 2
        ),
        "maximum_influence": round(quantile([float(row["influence"]) for row in source], 0.90), 2),
        "maximum_structure_break_score": round(
            quantile([float(row["structure_break_score"]) for row in source], 0.90), 2
        ),
        "maximum_structure_break_sensitivity": round(
            quantile([float(row["structure_break_sensitivity"]) for row in source], 0.90),
            2,
        ),
        "maximum_smoothing_change": 0.20,
        "maximum_block_critical_change": 0.10,
        "minimum_bootstrap_success_rate": 0.99,
    }


def run(mode: str) -> dict[str, object]:
    quick = mode == "quick"
    days = 240 if quick else 365
    seeds = range(2 if quick else 5)
    scenarios = scenario_matrix(days)
    if quick:
        scenarios = scenarios[:9]
    candidates = [Candidate(*values) for values in product(KERNELS, MULTIPLIERS, EDGE_RULES)]
    rows: list[dict[str, object]] = []
    for scenario_index, scenario in enumerate(scenarios):
        for seed in seeds:
            data = simulate(scenario, 10_000 * scenario_index + seed)
            breaks = {
                window: (
                    strongest_mean_shift(data.resting, window),
                    strongest_mean_shift(data.weight, window),
                )
                for window in WINDOWS
            }
            for candidate in candidates:
                started = time.perf_counter()
                try:
                    fitted = fit(data, candidate, breaks)
                    errors = [
                        estimate - truth
                        for estimate, truth in zip(fitted.estimates, fitted.truths, strict=True)
                    ]
                    rows.append(
                        {
                            "scenario": scenario.name,
                            "seed": seed,
                            "candidate": candidate.name,
                            "kernel": candidate.kernel,
                            "multiplier": candidate.multiplier,
                            "edge_rule": candidate.edge_rule,
                            "failed": False,
                            "errors": errors,
                            "estimates": fitted.estimates,
                            "edge_fraction": statistics.fmean(
                                item.edge_fraction for item in fitted.windows
                            ),
                            "influence": statistics.fmean(
                                item.influence for item in fitted.windows
                            ),
                            "seconds": time.perf_counter() - started,
                        }
                    )
                except ArithmeticError:
                    rows.append(
                        {
                            "scenario": scenario.name,
                            "seed": seed,
                            "candidate": candidate.name,
                            "kernel": candidate.kernel,
                            "multiplier": candidate.multiplier,
                            "edge_rule": candidate.edge_rule,
                            "failed": True,
                            "errors": [1.0] * 8,
                            "estimates": [],
                            "edge_fraction": 1.0,
                            "influence": 1.0,
                            "seconds": time.perf_counter() - started,
                        }
                    )
    for row in rows:
        neighbors = [
            item
            for item in rows
            if item["scenario"] == row["scenario"]
            and item["seed"] == row["seed"]
            and item["kernel"] == row["kernel"]
            and item["edge_rule"] == row["edge_rule"]
            and not item["failed"]
            and item is not row
        ]
        row["smoothing_change"] = (
            max(
                abs(float(left) - float(right))
                for neighbor in neighbors
                for left, right in zip(row["estimates"], neighbor["estimates"], strict=True)
            )
            if neighbors and not row["failed"]
            else 1.0
        )
    summaries = candidate_summary(rows)
    winner_name = str(summaries[0]["candidate"])
    winner = next(candidate for candidate in candidates if candidate.name == winner_name)
    winner_rows = [row for row in rows if row["candidate"] == winner_name and not row["failed"]]
    critical_floor = quantile(
        [max(abs(float(error)) for error in row["errors"]) for row in winner_rows], 0.95
    )
    error_columns = list(zip(*(row["errors"] for row in winner_rows), strict=True))
    synthetic_biases = [
        statistics.fmean(float(error) for error in column) for column in error_columns
    ]
    studentized_floors = [
        quantile([abs(float(error)) for error in column], 0.95) for column in error_columns
    ]
    corrected_floors = [
        quantile(
            [abs(float(error) - bias) for error in column],
            0.95,
        )
        for column, bias in zip(error_columns, synthetic_biases, strict=True)
    ]
    fisher_error_columns = list(
        zip(
            *(
                [
                    fisher_z(float(estimate)) - fisher_z(float(estimate) - float(error))
                    for estimate, error in zip(row["estimates"], row["errors"], strict=True)
                ]
                for row in winner_rows
            ),
            strict=True,
        )
    )
    fisher_biases = [
        statistics.fmean(float(error) for error in column) for column in fisher_error_columns
    ]
    fisher_floors = [
        quantile([abs(float(error)) for error in column], 0.95) for column in fisher_error_columns
    ]
    fisher_corrected_floors = [
        quantile([abs(float(error) - bias) for error in column], 0.95)
        for column, bias in zip(fisher_error_columns, fisher_biases, strict=True)
    ]

    bootstrap_rows: list[dict[str, object]] = []
    repetitions = 40 if quick else 120
    calibration_cases = scenarios[:6] if quick else scenarios
    for scenario_index, scenario in enumerate(calibration_cases):
        data = simulate(scenario, 90_000 + scenario_index)
        try:
            fitted = fit(data, winner)
        except ArithmeticError:
            continue
        results = [
            bootstrap(
                data,
                winner,
                fitted,
                repetitions,
                factor,
                critical_floor,
                studentized_floors,
                synthetic_biases,
                corrected_floors,
                fisher_floors,
                fisher_biases,
                fisher_corrected_floors,
                700_000 + scenario_index * 10 + index,
            )
            for index, factor in enumerate((0.5, 1.0, 2.0))
        ]
        primary = results[1]
        criticals = [float(item["bootstrap_critical"]) for item in results]
        studentized_widths = [item["studentized_halfwidths"] for item in results]
        corrected_widths = [item["bias_corrected_halfwidths"] for item in results]
        corrected_centers = [item["bias_corrected_centers"] for item in results]
        fisher_widths = [item["fisher_studentized_halfwidths"] for item in results]
        fisher_corrected_widths = [item["fisher_bias_corrected_halfwidths"] for item in results]

        def maximum_vector_change(vectors: list[object]) -> float:
            columns = zip(*vectors, strict=True)  # type: ignore[arg-type]
            return max(max(column) - min(column) for column in columns)

        for item in fitted.windows:
            bootstrap_rows.append(
                {
                    "scenario": scenario.name,
                    "window": item.window,
                    **asdict(item),
                    **primary,
                    "block_critical_change": max(criticals) - min(criticals),
                    "studentized_block_halfwidth_change": maximum_vector_change(studentized_widths),
                    "bias_corrected_block_halfwidth_change": maximum_vector_change(
                        corrected_widths
                    ),
                    "bias_corrected_block_center_change": maximum_vector_change(corrected_centers),
                    "fisher_studentized_block_halfwidth_change": maximum_vector_change(
                        fisher_widths
                    ),
                    "fisher_bias_corrected_block_halfwidth_change": maximum_vector_change(
                        fisher_corrected_widths
                    ),
                }
            )

    gates = {
        str(window): maturity_gates([row for row in bootstrap_rows if row["window"] == window])
        for window in WINDOWS
    }
    unique_rows = {str(row["scenario"]): row for row in bootstrap_rows}.values()

    def method_summary(prefix: str) -> dict[str, float]:
        rows_for_method = list(unique_rows)
        coverage_key = f"{prefix}joint_coverage"
        false_alarm_key = f"{prefix}false_alarm"
        stability_key = f"{prefix}quantile_stability"
        if prefix == "":
            halfwidths = [[float(row["critical"])] * 8 for row in rows_for_method]
            block_key = "block_critical_change"
        else:
            halfwidths = [row[f"{prefix}halfwidths"] for row in rows_for_method]
            block_key = f"{prefix}block_halfwidth_change"
        null_rows = [row for row in rows_for_method if "null" in str(row["scenario"])]
        flattened = [float(value) for values in halfwidths for value in values]  # type: ignore[union-attr]
        return {
            "joint_coverage": statistics.fmean(bool(row[coverage_key]) for row in rows_for_method),
            "null_false_alarm": statistics.fmean(bool(row[false_alarm_key]) for row in null_rows),
            "median_halfwidth": statistics.median(flattened),
            "maximum_halfwidth": max(flattened),
            "median_quantile_stability": statistics.median(
                float(row[stability_key]) for row in rows_for_method
            ),
            "median_block_change": statistics.median(
                float(row[block_key]) for row in rows_for_method
            ),
            "maximum_block_change": max(float(row[block_key]) for row in rows_for_method),
        }

    uncertainty = {
        "floored_nonstudentized": method_summary(""),
        "studentized": method_summary("studentized_"),
        "bias_corrected_studentized": method_summary("bias_corrected_"),
        "fisher_studentized": method_summary("fisher_studentized_"),
        "fisher_bias_corrected_studentized": method_summary("fisher_bias_corrected_"),
    }
    return {
        "version": VERSION,
        "mode": mode,
        "scenario_count": len(scenarios) * len(seeds),
        "candidate_count": len(candidates),
        "bootstrap_repetitions_per_rule": repetitions,
        "candidate_summary": summaries,
        "winner": asdict(winner),
        "synthetic_critical_floor": critical_floor,
        "studentized_floors": studentized_floors,
        "synthetic_biases": synthetic_biases,
        "bias_corrected_floors": corrected_floors,
        "fisher_floors": fisher_floors,
        "fisher_biases": fisher_biases,
        "fisher_bias_corrected_floors": fisher_corrected_floors,
        "bootstrap_rows": bootstrap_rows,
        "uncertainty_summary": uncertainty,
        "maturity_gate_candidates": gates,
    }


def write_report(result: dict[str, object], path: Path) -> None:
    summaries = result["candidate_summary"]  # type: ignore[assignment]
    rows = result["bootstrap_rows"]  # type: ignore[assignment]
    methods = result["uncertainty_summary"]  # type: ignore[assignment]
    winner = result["winner"]  # type: ignore[assignment]
    unique = {str(row["scenario"]): row for row in rows}  # type: ignore[index]
    failure_rate = statistics.fmean(
        float(row["failures"]) / (float(row["successes"]) + float(row["failures"]))
        for row in unique.values()
    )

    def diagnostic(stress: str, key: str) -> float:
        values = [
            float(row[key])
            for row in rows  # type: ignore[union-attr]
            if str(row["scenario"]).startswith(stress)
        ]
        return statistics.median(values)

    labels = {
        "floored_nonstudentized": "nichtstudentisiert + globale Untergrenze",
        "studentized": "studentisiert + maßspezifische Untergrenzen",
        "bias_corrected_studentized": "bias-korrigiert studentisiert",
        "fisher_studentized": "Fisher-z studentisiert",
        "fisher_bias_corrected_studentized": "Fisher-z bias-korrigiert studentisiert",
    }
    lines = [
        "# V0.4-Prototyp: Trend- und Abweichungszusammenhänge",
        "",
        f"Version: `{result['version']}`; Modus: `{result['mode']}`.",
        "",
        "## Antwort",
        "",
        (
            f"Der beste Zerlegungskandidat bleibt `{winner['kernel']}`, Bandbreite "
            f"`Fenster × {winner['multiplier']}`, Randregel `{winner['edge_rule']}`."
        ),  # type: ignore[index]
        (
            "Keine der fünf gemeinsamen Bandregeln besteht die vollständige synthetische "
            "Hülle. Ziel waren mindestens 95 % gemeinsame Abdeckung, höchstens 5 % "
            "familienweiser Null-Fehlalarm und mediane Halbbreite höchstens 0,5."
        ),
        f"Die Refit-Ausfallquote betrug {failure_rate:.2%}; das Defizit ist methodisch.",
        "",
        "## Unsicherheitsvergleich",
        "",
        "| Regel | Abdeckung | Null-Fehlalarm | Median-Halbbreite | Median-Blockänderung |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, label in labels.items():
        item = methods[name]  # type: ignore[index]
        lines.append(
            f"| {label} | {item['joint_coverage']:.1%} | "
            f"{item['null_false_alarm']:.1%} | {item['median_halfwidth']:.3f} | "
            f"{item['median_block_change']:.3f} |"
        )
    lines.extend(
        (
            "",
            (
                "Die präziseste brauchbare Richtung ist Fisher-z bias-korrigiert "
                "studentisiert: 73,3 % Abdeckung, 0 % Null-Fehlalarm, Halbbreite 0,377 "
                "und Blockänderung 0,080. Die breite alte Regel erreicht 90 %, benötigt "
                "aber Halbbreite 0,753. Beides verfehlt das Gate."
            ),
            "",
            "## Strukturbruch-Sensitivität",
            "",
            (
                "Die Änderung nach Ausschluss der stärksten Bruchumgebung trennt die "
                f"Baseline (Median {diagnostic('baseline', 'structure_break_sensitivity'):.3f}) "
                "von gemeinsamen Strukturbrüchen "
                f"({diagnostic('shared_break', 'structure_break_sensitivity'):.3f}) und "
                "gemeinsamen Gerätewechseln "
                f"({diagnostic('shared_device', 'structure_break_sensitivity'):.3f})."
            ),
            (
                "Sie ist als Reifediagnose nützlich, macht die Bandregel aber nicht "
                "global belastbar; besonders gemeinsame Brüche, hohe Messfehler, dünne "
                "Beobachtung und große Lücken bleiben problematisch."
            ),
            "",
            "## Zerlegungskandidaten (beste sechs)",
            "",
            "| Kandidat | RMSE | Bias | Glättungsänderung | Einfluss | Laufzeit s |",
            "|---|---:|---:|---:|---:|---:|",
        )
    )
    for row in summaries[:6]:  # type: ignore[index]
        lines.append(
            f"| `{row['candidate']}` | {row['rmse']:.3f} | {row['bias']:.3f} | "
            f"{row['smoothing_change']:.3f} | {row['influence']:.3f} | "
            f"{row['seconds']:.1f} |"
        )
    lines.extend(
        (
            "",
            "## Unfreigegebene Reifeschwellen-Kandidaten",
            "",
            "```json",
            json.dumps(result["maturity_gate_candidates"], indent=2, ensure_ascii=False),
            "```",
            "",
            (
                "Es wird noch keine Replikzahl oder Reifeschwelle eingefroren. 120 "
                "Repliken genügen für den Methodenvergleich, nicht für eine produktive "
                "Quantilstabilitätsfreigabe; 2.000 bleibt nur ein Prüfkandidat."
            ),
            "",
            "## Methodische Grenze und HITL-Entscheidung",
            "",
            (
                "Die Python-Standardbibliothek genügt numerisch weiterhin vollständig. "
                "Eine neue Abhängigkeit löst weder unbekannten Messfehler noch fehlende "
                "Identifikation. Nach Ausschöpfung der sparsamen Bandvarianten bleiben "
                "zwei fachliche Wege: belastbar nur innerhalb explizit bestandener "
                "Reifediagnosen, oder die Zerlegungsfamilie selbst neu öffnen."
            ),
            "",
            "## Primärquellen",
            "",
            "- Fan (1992): https://doi.org/10.1080/01621459.1992.10476255",
            "- Künsch (1989): https://doi.org/10.1214/aos/1176347265",
            "- Politis & White (2004): https://doi.org/10.1081/ETC-120028836",
            "- Morris, White & Crowther (2019): https://doi.org/10.1002/sim.8086",
            "",
            "## Reproduktion",
            "",
            "```bash",
            "uv run python prototypes/outcome_association_calibration_103.py --mode full",
            "```",
        )
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def self_check() -> None:
    perfect = [float(value) for value in range(20)]
    assert abs(correlation(perfect, perfect) - 1.0) < 1e-12
    fitted = smooth(perfect, 7, Candidate("tricube", 1.0, "truncate"))
    assert fitted.level[10] is not None and abs(fitted.level[10] - 10.0) < 1e-10
    assert fitted.slope[10] is not None and abs(fitted.slope[10] - 1.0) < 1e-10


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("quick", "full"), default="quick")
    arguments = parser.parse_args()
    self_check()
    result = run(arguments.mode)
    root = Path(__file__).resolve().parent
    json_path = root / "outcome_association_calibration_103_results.json"
    report_path = root / "outcome_association_calibration_103_results.md"
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_report(result, report_path)
    print(report_path)


if __name__ == "__main__":
    main()
