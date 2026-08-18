import fcntl
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import get_args
from zipfile import ZipFile

import pytest

from personal_health_lab.application import (
    AnalysisDefinitionId,
    AnalysisReceipt,
    AnalysisStatus,
    ConfigurationError,
    DataMode,
    HealthLab,
    HealthLabError,
    ImportHealthExport,
    ImportReceipt,
    OverviewSelection,
    ResolveDataReviewCase,
    RunAnalysis,
    RunAnalysisPlan,
    RunRestingHeartRateAnalysis,
    RuntimeConfig,
    SnapshotDateSelection,
    WorkoutCorrection,
    WriteApprovalStatus,
    WriteNotStarted,
    WriteNotStartedStatus,
    WriteRequest,
)
from personal_health_lab.synthetic_export import generate_export


def _package(path: Path, *, day: int, value: int = 1) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            f'''<HealthData><ExportDate value="2024-01-{day:02d} 12:00:00 +0100"/>
            <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="{value}"
            sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
            creationDate="2024-01-{day:02d} 12:00:00 +0100"
            startDate="2024-01-{day:02d} 12:00:00 +0100"
            endDate="2024-01-{day:02d} 12:01:00 +0100"/>
            <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="100"
            sourceName="iPhone" sourceVersion="1" device="iPhone"
            creationDate="2024-01-{day:02d} 12:00:00 +0100"
            startDate="2024-01-{day:02d} 12:00:00 +0100"
            endDate="2024-01-{day:02d} 12:01:00 +0100"/>
            <Record type="HKQuantityTypeIdentifierActiveEnergyBurned" unit="kcal" value="-1"
            sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
            creationDate="2024-01-{day:02d} 12:10:00 +0100"
            startDate="2024-01-{day:02d} 12:10:00 +0100"
            endDate="2024-01-{day:02d} 12:11:00 +0100"/>
            <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30"
            durationUnit="min" totalEnergyBurned="200" totalEnergyBurnedUnit="kcal"
            sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
            creationDate="2024-01-{day:02d} 13:31:00 +0100"
            startDate="2024-01-{day:02d} 13:00:00 +0100"
            endDate="2024-01-{day:02d} 13:30:00 +0100"/></HealthData>''',
        )
    return path


def _import(health_lab: HealthLab, package: Path) -> None:
    request = ImportHealthExport(package)
    receipt = health_lab.execute_write(
        request, expected_plan=health_lab.preview_write(request).fingerprint
    )
    assert isinstance(receipt.result, ImportReceipt)


def _request(*, start_date: date | None = None, end_date: date | None = None) -> RunAnalysis:
    return RunAnalysis(
        AnalysisDefinitionId("rhr-activity-lag-1-7-v1"),
        start_date=start_date,
        end_date=end_date,
    )


def test_run_analysis_is_the_only_canonical_analysis_request_and_validates_inputs() -> None:
    requests = set(get_args(WriteRequest))

    assert RunAnalysis in requests
    assert RunRestingHeartRateAnalysis not in requests
    with pytest.raises(ConfigurationError):
        RunAnalysis(AnalysisDefinitionId("unknown-definition"))
    with pytest.raises(ConfigurationError):
        RunAnalysis(AnalysisDefinitionId("rhr-activity-lag-1-7-v1"), schema_version="2.0")
    with pytest.raises(ConfigurationError):
        _request(start_date=date(2024, 1, 2), end_date=date(2024, 1, 1))
    with pytest.raises(ConfigurationError):
        RunAnalysis(
            AnalysisDefinitionId("rhr-activity-lag-1-7-v1"),
            start_date="2024-01-01",  # type: ignore[arg-type]
        )


def test_run_analysis_preview_binds_definition_snapshot_and_inclusive_eligible_range(
    tmp_path: Path,
) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    request = _request(start_date=date(2024, 1, 1), end_date=date(2024, 1, 3))

    with HealthLab.open(runtime) as health_lab:
        _import(health_lab, _package(tmp_path / "snapshot.zip", day=2))
        plan = health_lab.preview_write(request)

    assert isinstance(plan.details, RunAnalysisPlan)
    assert plan.details.analysis_definition.analysis_definition_id == request.analysis_definition_id
    assert plan.details.analysis_definition.definition_version == 1
    assert plan.details.base_snapshot_ref is not None
    assert plan.details.requested_start_date == date(2024, 1, 1)
    assert plan.details.requested_end_date == date(2024, 1, 3)
    assert plan.details.eligible_start_date == date(2024, 1, 2)
    assert plan.details.eligible_end_date == date(2024, 1, 2)
    assert plan.details.schema_version == "1.0"
    assert plan.details.reuse_candidate is None
    assert plan.approval.status is WriteApprovalStatus.READY


def test_run_analysis_nonstart_paths_never_create_a_model_run(tmp_path: Path) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    request = _request()

    with HealthLab.open(runtime) as health_lab:
        blocked_plan = health_lab.preview_write(request)
        blocked = health_lab.execute_write(request, expected_plan=blocked_plan.fingerprint).result
        _import(health_lab, _package(tmp_path / "first.zip", day=2))
        ready_plan = health_lab.preview_write(request)
        with (runtime.active_store / ".writer.lock").open("a+b") as writer_lock:
            fcntl.flock(writer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = health_lab.execute_write(request, expected_plan=ready_plan.fingerprint).result
        _import(health_lab, _package(tmp_path / "second.zip", day=3, value=2))
        changed = health_lab.execute_write(request, expected_plan=ready_plan.fingerprint).result
        overview = health_lab.load_overview(OverviewSelection())

    assert isinstance(blocked, WriteNotStarted)
    assert blocked.status is WriteNotStartedStatus.BLOCKED
    assert blocked.diagnostics == ("no_snapshot",)
    assert isinstance(busy, WriteNotStarted)
    assert busy.status is WriteNotStartedStatus.STORE_BUSY
    assert isinstance(changed, WriteNotStarted)
    assert changed.status is WriteNotStartedStatus.PLAN_CHANGED
    assert overview.analysis_history == ()


def test_started_run_freezes_input_and_persists_insufficient_data_without_result(
    tmp_path: Path,
) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    request = _request()

    with HealthLab.open(runtime) as health_lab:
        _import(health_lab, _package(tmp_path / "snapshot.zip", day=2))
        plan = health_lab.preview_write(request)
        first = health_lab.execute_write(request, expected_plan=plan.fingerprint).result
        second_plan = health_lab.preview_write(request)
        second = health_lab.execute_write(request, expected_plan=second_plan.fingerprint).result

    assert isinstance(first, AnalysisReceipt)
    assert first.status is AnalysisStatus.INSUFFICIENT_DATA
    assert first.result_ref is None
    assert first.model_maturity is None
    assert isinstance(second, AnalysisReceipt)
    assert second.analysis_run_id != first.analysis_run_id

    with sqlite3.connect(runtime.active_store / "metadata.sqlite3") as metadata:
        row = metadata.execute(
            "SELECT result_id, status, data_status FROM analysis_runs "
            "WHERE analysis_run_id = ?",
            (str(first.analysis_run_id),),
        ).fetchone()
    assert row is not None
    assert row == (None, "insufficient_data", "provisional")
    artifact = (
        runtime.active_store
        / "parquet"
        / "analysis-inputs"
        / f"{first.analysis_run_id}.json"
    )
    frozen_input = artifact.read_bytes()
    payload = json.loads(frozen_input)
    assert payload["analysis_run_id"] == str(first.analysis_run_id)
    assert payload["snapshot_id"] == str(first.snapshot_ref)
    assert payload["calendar"] == ["2024-01-02"]
    assert payload["activity_coverage_incomplete"] is True
    assert {value["input_id"] for value in payload["values"]} >= {
        "active_energy",
        "steps",
        "outcome_day_context",
    }
    assert any(
        value["input_id"] == "steps"
        and value["value"] == 1.0
        and len(value["measurement_version_ids"]) == 1
        and value["source_evidence"][0]["source_name"] == "Apple Watch"
        for value in payload["values"]
    )
    assert any(
        value["input_id"] == "workout_duration_by_type"
        and value["component"] == "HKWorkoutActivityTypeRunning"
        and value["value"] == 30.0
        and value["measurement_version_ids"]
        for value in payload["values"]
    )
    assert "activity-derivation/v1" in payload["rule_versions"]
    assert any(item.startswith("plausibility/step_count/") for item in payload["rule_versions"])
    assert any(
        value["input_id"] == "outcome_day_context"
        and value["missingness_reason"] == "input_not_available"
        for value in payload["values"]
    )
    assert payload["data_quality_fact_ids"]
    assert any(
        value["input_id"] == "active_energy"
        and value["source_evidence"]
        and value["data_quality_fact_ids"]
        for value in payload["values"]
    )
    assert payload["scalings"]
    assert all(item["population_standard_deviation"] is None for item in payload["scalings"])
    assert {item["status"] for item in payload["scalings"]} == {"unavailable"}
    with HealthLab.open(runtime) as health_lab:
        _import(health_lab, _package(tmp_path / "later.zip", day=3, value=2))
    assert artifact.read_bytes() == frozen_input


def test_analysis_fault_never_exposes_a_partial_run_after_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "legacy-fixture")
    legacy_request = RunRestingHeartRateAnalysis(AnalysisDefinitionId("lag-signal-v2"))
    with HealthLab.open(runtime) as health_lab:
        _import(health_lab, fixture.export_path)
        legacy = health_lab.execute_write(
            legacy_request,
            expected_plan=health_lab.preview_write(legacy_request).fingerprint,
        ).result
    assert isinstance(legacy, AnalysisReceipt)
    assert legacy.result_ref is not None
    legacy_artifact = (
        runtime.active_store
        / "parquet"
        / "analyses"
        / str(legacy.result_ref)
        / "result.parquet"
    )
    legacy_bytes = legacy_artifact.read_bytes()

    for fault_point in (
        "analysis.after_input_write/v1",
        "analysis.after_input_publish/v1",
        "analysis.before_catalog_commit/v1",
    ):
        request = _request()
        with monkeypatch.context() as fault, HealthLab.open(runtime) as health_lab:
            plan = health_lab.preview_write(request)

            def fail_at_analysis_point(
                _root: Path, current: str, expected: str = fault_point
            ) -> None:
                if current == expected:
                    raise OSError("injected analysis publication fault")

            fault.setattr(
                "personal_health_lab.storage._store._publication_fault_point",
                fail_at_analysis_point,
            )
            with pytest.raises(HealthLabError):
                health_lab.execute_write(request, expected_plan=plan.fingerprint)

        with HealthLab.open(runtime) as health_lab:
            overview = health_lab.load_overview(OverviewSelection())
        assert overview.resting_hr_analysis is not None
        assert overview.resting_hr_analysis.provenance is not None
        assert overview.resting_hr_analysis.provenance.result_id == legacy.result_ref
        assert legacy_artifact.read_bytes() == legacy_bytes
        with sqlite3.connect(runtime.active_store / "metadata.sqlite3") as metadata:
            assert metadata.execute("SELECT count(*) FROM analysis_runs").fetchone() == (1,)
        inputs = runtime.active_store / "parquet" / "analysis-inputs"
        assert not inputs.exists() or list(inputs.iterdir()) == []


def test_analysis_bundle_uses_corrected_workout_energy(tmp_path: Path) -> None:
    package = tmp_path / "workout.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            """<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
            <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="90"
              durationUnit="min" totalEnergyBurned="200" totalEnergyBurnedUnit="kcal"
              sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
              creationDate="2024-01-02 20:31:00 +0100"
              startDate="2024-01-02 20:00:00 +0100"
              endDate="2024-01-02 20:30:00 +0100"/>
            <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30"
              durationUnit="min" sourceName="Apple Watch" sourceVersion="1"
              device="Apple Watch" creationDate="2024-01-02 21:31:00 +0100"
              startDate="2024-01-02 21:00:00 +0100"
              endDate="2024-01-02 21:30:00 +0100"/></HealthData>""",
        )
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(runtime) as health_lab:
        _import(health_lab, package)
        workout = health_lab.load_workouts(SnapshotDateSelection()).workouts[0]
        correction = ResolveDataReviewCase(
            workout.review_case_ids[0],
            WorkoutCorrection(workout.workout_version_id, 30, None, 50, "source typo"),
        )
        health_lab.execute_write(
            correction,
            expected_plan=health_lab.preview_write(correction).fingerprint,
        )
        request = _request()
        receipt = health_lab.execute_write(
            request,
            expected_plan=health_lab.preview_write(request).fingerprint,
        ).result
    assert isinstance(receipt, AnalysisReceipt)
    payload = json.loads(
        (
            runtime.active_store
            / "parquet"
            / "analysis-inputs"
            / f"{receipt.analysis_run_id}.json"
        ).read_bytes()
    )
    assert any(
        value["input_id"] == "workout_energy_by_type"
        and value["value"] == 50.0
        and value["missingness"] == "partial"
        and len(value["source_evidence"]) == 2
        for value in payload["values"]
    )


def test_analysis_bundle_omits_unrelated_input_quality_facts(tmp_path: Path) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    request = RunAnalysis(AnalysisDefinitionId("rhr-weight-association-7-14-30-90-v1"))
    with HealthLab.open(runtime) as health_lab:
        _import(health_lab, _package(tmp_path / "snapshot.zip", day=2))
        receipt = health_lab.execute_write(
            request,
            expected_plan=health_lab.preview_write(request).fingerprint,
        ).result
    assert isinstance(receipt, AnalysisReceipt)
    with sqlite3.connect(runtime.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT data_status FROM analysis_runs WHERE analysis_run_id = ?",
            (str(receipt.analysis_run_id),),
        ).fetchone() == ("reviewed",)
    payload = json.loads(
        (
            runtime.active_store
            / "parquet"
            / "analysis-inputs"
            / f"{receipt.analysis_run_id}.json"
        ).read_bytes()
    )
    assert {value["input_id"] for value in payload["values"]} == {
        "apple_resting_heart_rate",
        "preferred_daily_weight",
    }
    assert payload["data_quality_fact_ids"] == []
    assert payload["activity_coverage_incomplete"] is False
    assert "activity-derivation/v1" not in payload["rule_versions"]
