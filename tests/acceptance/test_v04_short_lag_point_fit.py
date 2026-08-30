import random
import statistics
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from personal_health_lab.application import (
    AnalysisDefinitionId,
    AnalysisReceipt,
    AnalysisResultSelection,
    AnalysisStatus,
    DataMode,
    HealthLab,
    ImportHealthExport,
    ImportReceipt,
    ModelMaturityStatus,
    ProjectionUnavailable,
    ProjectionUnavailableCode,
    RhrActivityLag1To7Result,
    RunAnalysis,
    RuntimeConfig,
)

DEFINITION = AnalysisDefinitionId("rhr-activity-lag-1-7-v1")


def _record(kind: str, unit: str, value: float, day: date, hour: int) -> str:
    stamp = datetime.combine(day, time(hour), UTC).strftime("%Y-%m-%d %H:%M:%S +0000")
    return (
        f'<Record type="{kind}" unit="{unit}" value="{value}" '
        'sourceName="Apple Watch" sourceVersion="1" device="Apple Watch" '
        f'creationDate="{stamp}" startDate="{stamp}" endDate="{stamp}"/>'
    )


def _package(
    path: Path,
    *,
    calendar_days: int = 140,
    constant_steps: bool = False,
    duplicate_general: bool = False,
) -> tuple[Path, float]:
    rng = random.Random(118)
    start = date(2024, 1, 1)
    days = tuple(start + timedelta(days=offset) for offset in range(calendar_days))
    common_days = set(rng.sample(days, min(35, calendar_days)))
    rare_days = rng.sample(days, min(9, calendar_days))
    active_energy = [rng.uniform(200, 800) for _ in days]
    records: list[str] = []
    for index, day in enumerate(days):
        records.extend(
            (
                _record(
                    "HKQuantityTypeIdentifierActiveEnergyBurned",
                    "kcal",
                    active_energy[index],
                    day,
                    8,
                ),
                _record(
                    "HKQuantityTypeIdentifierAppleExerciseTime",
                    "min",
                    active_energy[index] if duplicate_general else rng.uniform(10, 90),
                    day,
                    9,
                ),
                _record(
                    "HKQuantityTypeIdentifierStepCount",
                    "count",
                    5_000 if constant_steps else rng.uniform(2_000, 18_000),
                    day,
                    10,
                ),
                _record(
                    "HKQuantityTypeIdentifierDistanceWalkingRunning",
                    "km",
                    rng.uniform(1, 16),
                    day,
                    11,
                ),
                _record(
                    "HKQuantityTypeIdentifierRestingHeartRate",
                    "count/min",
                    62
                    + (-0.006 * (active_energy[index - 1] - 500) if index else 0)
                    + rng.gauss(0, 0.3),
                    day,
                    12,
                ),
            )
        )
        workout_type = (
            "HKWorkoutActivityTypeRunning"
            if day in common_days
            else "HKWorkoutActivityTypeCycling"
            if day in rare_days[:5]
            else "HKWorkoutActivityTypeYoga"
            if day in rare_days[5:]
            else None
        )
        if workout_type is not None:
            duration = rng.uniform(20, 80)
            stamp = datetime.combine(day, time(13), UTC)
            end = stamp + timedelta(minutes=duration)
            records.append(
                f'<Workout workoutActivityType="{workout_type}" duration="{duration}" '
                f'durationUnit="min" totalEnergyBurned="{rng.uniform(120, 650)}" '
                'totalEnergyBurnedUnit="kcal" sourceName="Apple Watch" sourceVersion="1" '
                'device="Apple Watch" '
                f'creationDate="{end.strftime("%Y-%m-%d %H:%M:%S +0000")}" '
                f'startDate="{stamp.strftime("%Y-%m-%d %H:%M:%S +0000")}" '
                f'endDate="{end.strftime("%Y-%m-%d %H:%M:%S +0000")}"/>'
            )
    xml = (
        '<?xml version="1.0"?><HealthData>'
        f'<ExportDate value="{days[-1].isoformat()} 23:00:00 +0000"/>'
        + "".join(records)
        + "</HealthData>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    fit_values = [
        active_energy[exposure_day]
        for outcome_day in range(7, len(days))
        for exposure_day in range(outcome_day - 7, outcome_day)
    ]
    return path, statistics.pstdev(fit_values)


def test_short_lag_run_projects_full_activity_point_estimates(tmp_path: Path) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    request = RunAnalysis(DEFINITION)
    package, active_energy_sd = _package(tmp_path / "analysis.zip")
    with HealthLab.open(runtime) as health_lab:
        import_request = ImportHealthExport(package)
        imported = health_lab.execute_write(
            import_request,
            expected_plan=health_lab.preview_write(import_request).fingerprint,
        ).result
        assert isinstance(imported, ImportReceipt)
        receipt = health_lab.execute_write(
            request,
            expected_plan=health_lab.preview_write(request).fingerprint,
        ).result
        assert isinstance(receipt, AnalysisReceipt)
        result = health_lab.load_analysis_result(AnalysisResultSelection(DEFINITION))

    assert receipt.status is AnalysisStatus.COMPLETED
    assert receipt.model_maturity is ModelMaturityStatus.EXPLORATORY
    assert isinstance(result, RhrActivityLag1To7Result)
    features = {estimate.feature_id for estimate in result.lag_estimates}
    assert features == {
        "active_energy",
        "training_time",
        "steps",
        "walking_running_distance",
        "workout_duration_by_type:HKWorkoutActivityTypeRunning",
        "workout_duration_by_type:other",
        "workout_energy_by_type:HKWorkoutActivityTypeRunning",
        "workout_energy_by_type:other",
    }
    assert len(result.lag_estimates) == len(features) * 7
    assert len(result.contrasts) == len(features)
    assert {(item.start_day, item.end_day) for item in result.contrasts} == {(1, 7)}
    assert all(item.pointwise_interval is None for item in result.lag_estimates)
    assert all(item.simultaneous_band is None for item in result.lag_estimates)
    assert result.bootstrap_facts == ()
    assert result.maturity_criteria == ()
    fit_facts = dict(result.diagnostics[0].facts)
    assert fit_facts["basis_nodes"] == 7
    assert fit_facts["smoothing_penalty"] == 3.0
    assert fit_facts["ridge_penalty"] == 1.0
    active = next(
        item
        for item in result.lag_estimates
        if item.feature_id == "active_energy" and item.lag_day == 1
    )
    assert active.estimate_bpm_per_natural_scale < 0
    assert active.estimate_bpm_per_personal_sd == pytest.approx(
        active.estimate_bpm_per_natural_scale * active_energy_sd / active.natural_scale
    )


def test_short_lag_run_closes_scaling_and_rank_failures_without_a_result(
    tmp_path: Path,
) -> None:
    cases = (
        (
            140,
            {"constant_steps": True},
            AnalysisStatus.INSUFFICIENT_DATA,
            "insufficient_activity_scaling",
        ),
        (140, {"duplicate_general": True}, AnalysisStatus.UNSTABLE, "rank_deficient"),
        (8, {}, AnalysisStatus.UNSTABLE, "rank_deficient"),
    )
    for index, (calendar_days, package_options, expected_status, diagnostic) in enumerate(cases):
        package, _ = _package(
            tmp_path / f"analysis-{index}.zip",
            calendar_days=calendar_days,
            **package_options,
        )
        runtime = RuntimeConfig(
            DataMode.SYNTHETIC,
            tmp_path / f"store-{index}",
            tmp_path / f"real-{index}",
        )
        request = RunAnalysis(DEFINITION)
        with HealthLab.open(runtime) as health_lab:
            import_request = ImportHealthExport(package)
            health_lab.execute_write(
                import_request,
                expected_plan=health_lab.preview_write(import_request).fingerprint,
            )
            receipt = health_lab.execute_write(
                request,
                expected_plan=health_lab.preview_write(request).fingerprint,
            ).result
            result = health_lab.load_analysis_result(AnalysisResultSelection(DEFINITION))

        assert isinstance(receipt, AnalysisReceipt)
        assert receipt.status is expected_status
        assert receipt.diagnostics == (diagnostic,)
        assert receipt.result_ref is None
        assert receipt.model_maturity is None
        assert isinstance(result, ProjectionUnavailable)
        assert result.code is ProjectionUnavailableCode.RESULT_NOT_AVAILABLE
