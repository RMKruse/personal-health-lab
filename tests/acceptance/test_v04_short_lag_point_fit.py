import json
import random
import statistics
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from personal_health_lab.application import (
    AnalysisDefinitionId,
    AnalysisReceipt,
    AnalysisResultSelection,
    AnalysisStatus,
    AsNeededIntakeCreate,
    AsNeededMedication,
    ContextCoverageStartCreate,
    DailyStressCreate,
    DataMode,
    DataQualityStatus,
    DataStatusReasonCode,
    HealthLab,
    IllnessCategoryCreate,
    IllnessPeriodCreate,
    IllnessSeverity,
    ImportHealthExport,
    ImportReceipt,
    MedicationActualIntake,
    MedicationDeviationCreate,
    MedicationRegimeCreate,
    ModelMaturityStatus,
    ProjectionUnavailable,
    ProjectionUnavailableCode,
    ReviseAsNeededIntake,
    ReviseContextCoverageStart,
    ReviseDailyStress,
    ReviseIllnessCategory,
    ReviseIllnessPeriod,
    ReviseMedicationDeviation,
    ReviseMedicationRegime,
    RhrActivityLag1To7Result,
    RunAnalysis,
    RuntimeConfig,
    ScheduledDose,
    StressLevel,
    Weekday,
)

DEFINITION = AnalysisDefinitionId("rhr-activity-lag-1-7-v1")


def _record(
    kind: str,
    unit: str,
    value: float,
    day: date,
    hour: int,
    *,
    duration_minutes: int = 0,
) -> str:
    start = datetime.combine(day, time(hour), UTC)
    stamp = start.strftime("%Y-%m-%d %H:%M:%S +0000")
    end = (start + timedelta(minutes=duration_minutes)).strftime("%Y-%m-%d %H:%M:%S +0000")
    return (
        f'<Record type="{kind}" unit="{unit}" value="{value}" '
        'sourceName="Apple Watch" sourceVersion="1" device="Apple Watch" '
        f'creationDate="{stamp}" startDate="{stamp}" endDate="{end}"/>'
    )


def _package(
    path: Path,
    *,
    calendar_days: int = 140,
    constant_steps: bool = False,
    duplicate_general: bool = False,
    missing_sleep_day: int | None = None,
    partial_sleep_day: int | None = None,
) -> tuple[Path, float]:
    rng = random.Random(118)
    start = date(2024, 1, 1)
    days = tuple(start + timedelta(days=offset) for offset in range(calendar_days))
    common_days = set(rng.sample(days, min(35, calendar_days)))
    rare_days = rng.sample(days, min(9, calendar_days))
    active_energy = [rng.uniform(200, 800) for _ in days]
    records: list[str] = []
    for index, day in enumerate(days):
        if index != missing_sleep_day:
            sleep_intervals = (
                (
                    (datetime.combine(day, time(0), UTC), datetime.combine(day, time(2), UTC)),
                    (datetime.combine(day, time(3), UTC), datetime.combine(day, time(6), UTC)),
                )
                if index == partial_sleep_day
                else (
                    (
                        (sleep_end := datetime.combine(day, time(6), UTC)
                        + timedelta(minutes=rng.randrange(0, 120)))
                        - timedelta(minutes=rng.randrange(360, 541)),
                        sleep_end,
                    ),
                )
            )
            records.extend(
                '<Record type="HKCategoryTypeIdentifierSleepAnalysis" '
                'value="HKCategoryValueSleepAnalysisAsleepREM" '
                'sourceName="Apple Watch" sourceVersion="1" device="Apple Watch" '
                f'creationDate="{sleep_end.strftime("%Y-%m-%d %H:%M:%S +0000")}" '
                f'startDate="{sleep_start.strftime("%Y-%m-%d %H:%M:%S +0000")}" '
                f'endDate="{sleep_end.strftime("%Y-%m-%d %H:%M:%S +0000")}"/>'
                for sleep_start, sleep_end in sleep_intervals
            )
        records.extend(
            (
                _record(
                    "HKQuantityTypeIdentifierActiveEnergyBurned",
                    "kcal",
                    active_energy[index],
                    day,
                    8,
                    duration_minutes=60 if index in {0, len(days) - 1} else 0,
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


def _write(health_lab: HealthLab, request: object):
    return health_lab.execute_write(
        request, expected_plan=health_lab.preview_write(request).fingerprint
    ).result


def _add_baseline_context(health_lab: HealthLab, start: date) -> None:
    _write(
        health_lab,
        ReviseContextCoverageStart(ContextCoverageStartCreate(start)),
    )
    _write(
        health_lab,
        ReviseMedicationRegime(
            MedicationRegimeCreate(
                datetime.combine(start, time(), UTC),
                "UTC",
                (),
            )
        ),
    )


def _add_varying_context(health_lab: HealthLab, start: date) -> None:
    _write(
        health_lab,
        ReviseContextCoverageStart(ContextCoverageStartCreate(start)),
    )
    category = _write(
        health_lab,
        ReviseIllnessCategory(IllnessCategoryCreate("Infekt")),
    )
    _write(
        health_lab,
        ReviseIllnessPeriod(
            IllnessPeriodCreate(
                category.logical_id,
                start + timedelta(days=35),
                start + timedelta(days=39),
                IllnessSeverity.MODERATE,
            )
        ),
    )
    for offset, level in ((50, StressLevel.AVERAGE), (60, StressLevel.HIGH)):
        _write(
            health_lab,
            ReviseDailyStress(DailyStressCreate(start + timedelta(days=offset), level)),
        )
    weekdays = frozenset(Weekday)
    first = _write(
        health_lab,
        ReviseMedicationRegime(
            MedicationRegimeCreate(
                datetime.combine(start, time(), UTC),
                "UTC",
                (ScheduledDose("A", Decimal("1"), "mg", time(8), weekdays),),
                (AsNeededMedication("B", Decimal("1"), "mg"),),
            )
        ),
    )
    deviation_day = start + timedelta(days=75)
    scheduled_at = datetime.combine(deviation_day, time(8), UTC)
    _write(
        health_lab,
        ReviseMedicationDeviation(
            MedicationDeviationCreate(
                first.logical_id,
                scheduled_at,
                (MedicationActualIntake(scheduled_at + timedelta(hours=1), Decimal("1")),),
            )
        ),
    )
    entry_id = health_lab.load_medication_plan().regimes[-1].as_needed_medications[0].entry_id
    _write(
        health_lab,
        ReviseAsNeededIntake(
            AsNeededIntakeCreate(
                first.logical_id,
                entry_id,
                datetime.combine(start + timedelta(days=85), time(12), UTC),
                Decimal("1"),
            )
        ),
    )
    _write(
        health_lab,
        ReviseMedicationRegime(
            MedicationRegimeCreate(
                datetime.combine(start + timedelta(days=100), time(), UTC),
                "UTC",
                (),
            )
        ),
    )


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
        _add_baseline_context(health_lab, date(2024, 1, 1))
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
        (
            75,
            {"missing_sleep_day": 30},
            AnalysisStatus.UNSTABLE,
            "context_days_0_2:rank_deficient",
        ),
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
            _add_baseline_context(health_lab, date(2024, 1, 1))
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
        if diagnostic.startswith("context_days_0_2"):
            payload = json.loads(
                (
                    runtime.active_store
                    / "parquet"
                    / "analysis-runs"
                    / str(receipt.analysis_run_id)
                    / "input.jsonl"
                ).read_bytes()
            )
            assert any(
                    (item["component"] or "").startswith("variant:context_days_0_2:")
                and item["status"] == "observed"
                for item in payload["scalings"]
            )


def test_short_lag_run_freezes_context_and_runs_all_fixed_variants(tmp_path: Path) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    package, _ = _package(
        tmp_path / "context-analysis.zip",
        missing_sleep_day=30,
        partial_sleep_day=40,
    )
    with HealthLab.open(runtime) as health_lab:
        import_request = ImportHealthExport(package)
        _write(health_lab, import_request)
        _add_varying_context(health_lab, date(2024, 1, 1))
        request = RunAnalysis(DEFINITION)
        receipt = _write(health_lab, request)
        assert isinstance(receipt, AnalysisReceipt)
        result = health_lab.load_analysis_result(AnalysisResultSelection(DEFINITION))

    assert receipt.status is AnalysisStatus.COMPLETED
    assert isinstance(result, RhrActivityLag1To7Result)
    assert result.data_status is DataQualityStatus.PROVISIONAL
    reasons = {item.code: item.evidence_ids for item in result.data_status_reasons}
    assert reasons[DataStatusReasonCode.PASSIVE_COVERAGE_GAP] == ("activity-coverage/v1",)
    assert reasons[DataStatusReasonCode.PROVISIONAL_INPUT_QUALITY]
    input_facts = dict(
        next(item for item in result.diagnostics if item.code.value == "input").facts
    )
    missingness_facts = dict(
        next(item for item in result.diagnostics if item.code.value == "missingness").facts
    )
    sensitivity_facts = dict(
        next(item for item in result.diagnostics if item.code.value == "sensitivity").facts
    )
    assert input_facts["context_variants"] == (
        "primary",
        "context_days_0_2",
        "without_context",
    )
    assert input_facts["activity_coverage_incomplete"] is True
    components = set(input_facts["context_components"])
    assert {
        "sleep_duration_minutes",
        "sleep_observation_status",
        "illness_severity",
        "stress_deviation",
        "stress_origin_observed",
        "medication_regime_segment",
        "medication_deviation",
        "medication_as_needed_intake",
    } <= components
    fit_days = dict(missingness_facts["fit_days"])
    assert len(fit_days["without_context"]) > len(fit_days["primary"])
    assert len(fit_days["primary"]) > len(fit_days["context_days_0_2"])
    assert set(dict(sensitivity_facts["cumulative_bpm_per_personal_sd"])) == {
        "context_days_0_2",
        "without_context",
    }
    input_missingness = dict(missingness_facts["input_missingness"])
    sleep_missingness = dict(
        input_missingness["outcome_day_context:sleep_duration_minutes"]
    )
    assert sleep_missingness["partial"] == 1

    payload = json.loads(
        (
            runtime.active_store
            / "parquet"
            / "analysis-runs"
            / str(receipt.analysis_run_id)
            / "input.jsonl"
        ).read_bytes()
    )
    context_values = [
        item for item in payload["values"] if item["input_id"] == "outcome_day_context"
    ]
    frozen = {(item["day"], item["component"]): item["value"] for item in context_values}
    assert any(
        item["component"] == "sleep_duration_minutes"
        and item["value"] is None
        and item["missingness"] == "missing"
        for item in context_values
    )
    assert frozen[("2024-02-05", "illness_severity")] == 2.0
    assert frozen[("2024-02-10", "sleep_duration_minutes")] == 300.0
    assert frozen[("2024-02-10", "sleep_observation_status")] == 1.0
    assert any(
        item["day"] == "2024-02-10"
        and item["component"] == "sleep_duration_minutes"
        and item["missingness"] == "partial"
        for item in context_values
    )
    assert frozen[("2024-02-20", "stress_deviation")] == 0.0
    assert frozen[("2024-02-20", "stress_origin_observed")] == 1.0
    assert frozen[("2024-03-01", "stress_deviation")] == 1.0
    assert frozen[("2024-03-16", "medication_deviation")] == 1.0
    assert frozen[("2024-03-26", "medication_as_needed_intake")] == 1.0
    assert frozen[("2024-04-10", "medication_regime_segment")] == 1.0
    assert any(
        item["day"] == "2024-02-05"
        and item["component"] == "illness_severity"
        and {source["source_record_kind"] for source in item["source_records"]}
        == {"context_revision"}
        for item in context_values
    )
    assert any(
        item["day"] == "2024-03-16"
        and item["component"] == "medication_deviation"
        and {source["source_record_kind"] for source in item["source_records"]}
        == {"medication_revision"}
        for item in context_values
    )
    assert any(
        item["component"] == "sleep_duration_minutes"
        and item["value"] is not None
        and item["source_evidence"]
        and {source["source_record_kind"] for source in item["source_records"]}
        == {"measurement_version"}
        for item in context_values
    )
    context_scalings = {
        item["component"]: item["status"]
        for item in payload["scalings"]
        if item["input_id"] == "outcome_day_context"
    }
    assert context_scalings["sleep_duration_minutes@day-0"] == "observed"
    assert context_scalings["sleep_observation_status@day-0"] == "observed"
    assert context_scalings["medication_regime_segment=1@day-0"] == "observed"
    assert context_scalings[
        "variant:context_days_0_2:sleep_duration_minutes@day-2"
    ] == "observed"
    assert any(
        item["input_id"] == "active_energy"
        and item["component"] == "variant:without_context:value"
        and item["status"] == "observed"
        for item in payload["scalings"]
    )
    assert {
        "sleep-episode/v1",
        "sleep-night/v1",
        "daily-context/v1",
        "medication-context/v1",
    } <= set(payload["rule_versions"])
