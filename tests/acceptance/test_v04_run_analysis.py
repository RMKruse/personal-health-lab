import fcntl
import hashlib
import json
import shutil
import sqlite3
from copy import deepcopy
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import get_args
from uuid import uuid4
from zipfile import ZipFile

import pytest

import personal_health_lab.analysis as analysis_module
from personal_health_lab.application import (
    AnalysisDefinitionId,
    AnalysisReceipt,
    AnalysisStatus,
    ConfigurationError,
    DataMode,
    DataQualityStatus,
    HealthLab,
    HealthLabError,
    ImportHealthExport,
    ImportReceipt,
    MigrateStore,
    ModelMaturityStatus,
    OverviewSelection,
    ResolveDataReviewCase,
    RunAnalysis,
    RunAnalysisPlan,
    RunRestingHeartRateAnalysis,
    RuntimeConfig,
    SnapshotDateSelection,
    StoreMigrationReceipt,
    WorkoutCorrection,
    WriteApprovalStatus,
    WriteNotStarted,
    WriteNotStartedStatus,
    WriteRequest,
)
from personal_health_lab.storage import (
    AnalysisJsonlArtifact,
    AnalysisProvenance,
    AnalysisResultId,
    AnalysisRunConfiguration,
    AnalysisRunId,
    AnalysisRunPublication,
    LocalStore,
    OperationId,
    StoreError,
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
            "SELECT result_id, status, data_status, data_status_reasons FROM analysis_runs "
            "WHERE analysis_run_id = ?",
            (str(first.analysis_run_id),),
        ).fetchone()
    assert row is not None
    assert row[:3] == (None, "insufficient_data", "provisional")
    assert {reason["code"] for reason in json.loads(row[3])} == {
        "open_review_case",
        "passive_coverage_gap",
        "provisional_input_quality",
    }
    artifact_directory = (
        runtime.active_store / "parquet" / "analysis-runs" / str(first.analysis_run_id)
    )
    artifact = artifact_directory / "input.jsonl"
    manifest_path = artifact_directory / "input-manifest.json"
    frozen_input = artifact.read_bytes()
    assert frozen_input.endswith(b"\n")
    payload = json.loads(frozen_input)
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest == {
        "analysis_definition_id": str(request.analysis_definition_id),
        "analysis_result_id": None,
        "analysis_run_id": str(first.analysis_run_id),
        "artifact_kind": "input",
        "content_hash": manifest["content_hash"],
        "files": [
            {
                "name": "input.jsonl",
                "row_count": 1,
                "sha256": manifest["files"][0]["sha256"],
                "size_bytes": len(frozen_input),
            }
        ],
        "manifest_schema_version": 1,
        "result_family": None,
        "schema_version": 2,
    }
    assert len(manifest["content_hash"]) == 64
    assert len(manifest["files"][0]["sha256"]) == 64
    with sqlite3.connect(runtime.active_store / "metadata.sqlite3") as metadata:
        catalog = metadata.execute(
            "SELECT artifact_kind, schema_id, schema_version, content_hash, "
            "manifest_sha256, artifact_path, artifact_size_bytes "
            "FROM analysis_artifacts WHERE analysis_run_id = ?",
            (str(first.analysis_run_id),),
        ).fetchone()
    assert catalog is not None
    assert catalog[:4] == (
        "input",
        "analysis-input-bundle",
        2,
        manifest["content_hash"],
    )
    assert len(catalog[4]) == 64
    assert catalog[5:] == (
        f"parquet/analysis-runs/{first.analysis_run_id}",
        len(frozen_input) + len(manifest_path.read_bytes()),
    )
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
        and value["component"] == "other"
        and value["value"] == 30.0
        and value["measurement_version_ids"]
        for value in payload["values"]
    )
    assert "activity-derivation/v1" in payload["rule_versions"]
    assert any(item.startswith("plausibility/step_count/") for item in payload["rule_versions"])
    assert any(
        value["input_id"] == "outcome_day_context" and value["missingness"] == "missing"
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
    assert {item["status"] for item in payload["scalings"]} == {
        "insufficient_observations",
        "unavailable",
    }
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
        runtime.active_store / "parquet" / "analyses" / str(legacy.result_ref) / "result.parquet"
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
        inputs = runtime.active_store / "parquet" / "analysis-runs"
        assert not inputs.exists() or list(inputs.iterdir()) == []


def test_analysis_publication_rejects_schema_size_hash_and_snapshot_violations(
    tmp_path: Path,
) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    request = _request()
    with HealthLab.open(runtime) as health_lab:
        _import(health_lab, _package(tmp_path / "snapshot.zip", day=2))
        first = health_lab.execute_write(
            request,
            expected_plan=health_lab.preview_write(request).fingerprint,
        ).result
    assert isinstance(first, AnalysisReceipt)

    source = json.loads(
        (
            runtime.active_store
            / "parquet"
            / "analysis-runs"
            / str(first.analysis_run_id)
            / "input.jsonl"
        ).read_bytes()
    )

    def publication(*, violation: str) -> AnalysisRunPublication:
        run_id = AnalysisRunId(uuid4().hex)
        payload = {**deepcopy(source), "analysis_run_id": str(run_id)}
        if violation == "schema":
            payload["values"][0]["unexpected"] = True
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode() + b"\n"
        content = dict(payload)
        del content["analysis_run_id"]
        artifact = AnalysisJsonlArtifact(
            "analysis-input-bundle",
            1,
            hashlib.sha256(
                json.dumps(content, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest(),
            hashlib.sha256(encoded).hexdigest(),
            len(encoded),
            1,
            encoded,
            analysis_module._validate_input_publication,
        )
        if violation == "size":
            artifact = replace(artifact, size_bytes=artifact.size_bytes + 1)
        if violation == "hash":
            artifact = replace(artifact, sha256="0" * 64)
        configuration = AnalysisRunConfiguration(request.analysis_definition_id, None, None, "1.0")
        return AnalysisRunPublication(
            OperationId(uuid4().hex),
            AnalysisProvenance(
                run_id,
                None,
                (
                    first.snapshot_ref
                    if violation != "snapshot"
                    else type(first.snapshot_ref)(uuid4().hex)
                ),
                request.analysis_definition_id,
                configuration.content_hash,
                "1.0",
                "a" * 40,
                False,
                None,
                "b" * 64,
            ),
            date(2024, 1, 2),
            date(2024, 1, 2),
            configuration,
            artifact,
            ("insufficient_data",),
            DataQualityStatus.REVIEWED,
            (),
        )

    for violation in ("schema", "size", "hash", "snapshot"):
        invalid = publication(violation=violation)
        writer = LocalStore.open_writer(root=runtime.active_store, mode=runtime.mode)
        try:
            with pytest.raises(StoreError):
                analysis_module._publish_analysis_run(writer, invalid)
        finally:
            writer.close()
        assert not (
            runtime.active_store
            / "parquet"
            / "analysis-runs"
            / str(invalid.provenance.analysis_run_id)
        ).exists()

    with sqlite3.connect(runtime.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute("SELECT count(*) FROM analysis_runs").fetchone() == (1,)
        assert metadata.execute("SELECT count(*) FROM analysis_artifacts").fetchone() == (1,)


def test_store_upgrade_registers_legacy_analysis_artifacts_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    request = RunRestingHeartRateAnalysis(AnalysisDefinitionId("lag-signal-v2"))
    with HealthLab.open(runtime) as health_lab:
        _import(health_lab, fixture.export_path)
        legacy = health_lab.execute_write(
            request,
            expected_plan=health_lab.preview_write(request).fingerprint,
        ).result
        current_request = _request()
        current = health_lab.execute_write(
            current_request,
            expected_plan=health_lab.preview_write(current_request).fingerprint,
        ).result
    assert isinstance(legacy, AnalysisReceipt)
    assert legacy.result_ref is not None
    assert isinstance(current, AnalysisReceipt)
    result_path = (
        runtime.active_store / "parquet" / "analyses" / str(legacy.result_ref) / "result.parquet"
    )
    result_bytes = result_path.read_bytes()
    result_path.with_name("manifest.json").unlink()
    shutil.rmtree(result_path.parent / "input")
    interrupted_run_id = uuid4().hex

    with sqlite3.connect(runtime.active_store / "metadata.sqlite3") as metadata:
        columns = [str(row[1]) for row in metadata.execute("PRAGMA table_info(analysis_runs)")]
        interrupted = list(
            metadata.execute(
                "SELECT * FROM analysis_runs WHERE analysis_run_id = ?",
                (str(legacy.analysis_run_id),),
            ).fetchone()
        )
        interrupted[columns.index("analysis_run_id")] = interrupted_run_id
        interrupted[columns.index("result_id")] = uuid4().hex
        interrupted[columns.index("status")] = "interrupted"
        metadata.execute(
            f"INSERT INTO analysis_runs VALUES ({','.join('?' for _ in columns)})",
            interrupted,
        )
        metadata.execute("DROP TABLE analysis_artifact_files")
        metadata.execute("DROP TABLE analysis_artifacts")
        metadata.execute("DROP TABLE analysis_run_facts")
        metadata.execute("UPDATE store_identity SET schema_version = 11 WHERE singleton = 1")

    with monkeypatch.context() as fault:

        def fail_before_commit(_root: Path, point: str) -> None:
            if point == "migration.before_sqlite_commit/v1":
                raise RuntimeError("injected migration fault")

        fault.setattr(
            "personal_health_lab.storage._store._migration_fault_point",
            fail_before_commit,
        )
        with HealthLab.open(runtime) as health_lab:
            migration = MigrateStore()
            with pytest.raises(RuntimeError, match="injected migration fault"):
                health_lab.execute_write(
                    migration,
                    expected_plan=health_lab.preview_write(migration).fingerprint,
                )
    with HealthLab.open(runtime):
        pass
    assert not result_path.with_name("manifest.json").exists()

    for _ in range(2):
        with HealthLab.open(runtime) as health_lab:
            migration = MigrateStore()
            receipt = health_lab.execute_write(
                migration,
                expected_plan=health_lab.preview_write(migration).fingerprint,
            ).result
        assert isinstance(receipt, StoreMigrationReceipt)

    assert result_path.read_bytes() == result_bytes
    manifest_path = result_path.with_name("manifest.json")
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest["artifact_kind"] == "result"
    assert manifest["result_family"] == "legacy_resting_hr_analysis"
    assert manifest["files"] == [
        {
            "name": "result.parquet",
            "row_count": 8,
            "sha256": hashlib.sha256(result_bytes).hexdigest(),
            "size_bytes": len(result_bytes),
        }
    ]
    with sqlite3.connect(runtime.active_store / "metadata.sqlite3") as metadata:
        artifact = metadata.execute(
            "SELECT artifact_kind, result_id, result_family, schema_id, schema_version, "
            "content_hash, artifact_path, artifact_size_bytes, is_legacy "
            "FROM analysis_artifacts WHERE analysis_run_id = ?",
            (str(legacy.analysis_run_id),),
        ).fetchone()
        facts = metadata.execute(
            "SELECT fact_code FROM analysis_run_facts WHERE analysis_run_id = ?",
            (str(legacy.analysis_run_id),),
        ).fetchall()
        definition = metadata.execute(
            "SELECT analysis_definition_id FROM analysis_runs WHERE analysis_run_id = ?",
            (str(legacy.analysis_run_id),),
        ).fetchone()
        current_artifact = metadata.execute(
            "SELECT artifact_kind, schema_id, schema_version, is_legacy "
            "FROM analysis_artifacts WHERE analysis_run_id = ?",
            (str(current.analysis_run_id),),
        ).fetchone()
        current_facts = metadata.execute(
            "SELECT fact_code FROM analysis_run_facts WHERE analysis_run_id = ?",
            (str(current.analysis_run_id),),
        ).fetchall()
        interrupted_artifacts = metadata.execute(
            "SELECT * FROM analysis_artifacts WHERE analysis_run_id = ?",
            (interrupted_run_id,),
        ).fetchall()
    assert artifact is not None
    assert artifact[:5] == (
        "result",
        str(legacy.result_ref),
        "legacy_resting_hr_analysis",
        "legacy-resting-hr-result",
        1,
    )
    assert len(artifact[5]) == 64
    assert artifact[6:] == (
        f"parquet/analyses/{legacy.result_ref}",
        len(result_bytes) + len(manifest_path.read_bytes()),
        1,
    )
    assert facts == [("legacy_input_bundle_not_persisted",)]
    assert definition == ("lag-signal-v2",)
    assert current_artifact == ("input", "analysis-input-bundle", 2, 0)
    assert current_facts == []
    assert interrupted_artifacts == []

    with HealthLab.open(runtime) as health_lab:
        overview = health_lab.load_overview(OverviewSelection())
    assert overview.analysis_history
    assert overview.analysis_history[0].provenance is not None
    assert overview.analysis_history[0].provenance.analysis_run_id == legacy.analysis_run_id


def test_analysis_run_publication_commits_input_and_result_family_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    request = _request()
    with HealthLab.open(runtime) as health_lab:
        _import(health_lab, _package(tmp_path / "snapshot.zip", day=2))
        source = health_lab.execute_write(
            request,
            expected_plan=health_lab.preview_write(request).fingerprint,
        ).result
    assert isinstance(source, AnalysisReceipt)
    input_record = json.loads(
        (
            runtime.active_store
            / "parquet"
            / "analysis-runs"
            / str(source.analysis_run_id)
            / "input.jsonl"
        ).read_bytes()
    )

    def artifact(
        record: dict[str, object], schema_id: str, *, identity_fields: tuple[str, ...]
    ) -> AnalysisJsonlArtifact:
        encoded = json.dumps(record, separators=(",", ":"), sort_keys=True).encode() + b"\n"
        content = dict(record)
        for field in identity_fields:
            del content[field]
        return AnalysisJsonlArtifact(
            schema_id,
            int(record.get("input_schema_version", 1))
            if schema_id == "analysis-input-bundle"
            else 1,
            hashlib.sha256(
                json.dumps(content, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest(),
            hashlib.sha256(encoded).hexdigest(),
            len(encoded),
            1,
            encoded,
            (
                analysis_module._validate_input_publication
                if schema_id == "analysis-input-bundle"
                else analysis_module._validate_result_artifact
            ),
        )

    def publication() -> AnalysisRunPublication:
        run_id = AnalysisRunId(uuid4().hex)
        result_id = AnalysisResultId(uuid4().hex)
        configuration = AnalysisRunConfiguration(request.analysis_definition_id, None, None, "1.0")
        result_record: dict[str, object] = {
            "analysis_definition_id": str(request.analysis_definition_id),
            "analysis_result_id": str(result_id),
            "analysis_run_id": str(run_id),
            "bootstrap_facts": [],
            "contrasts": [],
            "diagnostics": [],
            "lag_estimates": [
                {
                    "estimate_bpm_per_natural_scale": -0.1,
                    "estimate_bpm_per_personal_sd": -0.2,
                    "feature_id": "active_energy",
                    "lag_day": 1,
                    "natural_scale": 100.0,
                    "natural_unit": "kcal",
                    "pointwise_interval": {"lower": -0.2, "upper": 0.0},
                    "simultaneous_band": {"lower": -0.3, "upper": 0.1},
                }
            ],
            "maturity_criteria": [],
            "result_family": "rhr_activity_lag_1_7",
            "result_schema_version": 1,
        }
        return AnalysisRunPublication(
            OperationId(uuid4().hex),
            AnalysisProvenance(
                run_id,
                result_id,
                source.snapshot_ref,
                request.analysis_definition_id,
                configuration.content_hash,
                "1.0",
                "a" * 40,
                False,
                None,
                "b" * 64,
            ),
            date(2024, 1, 2),
            date(2024, 1, 2),
            configuration,
            artifact(
                {**deepcopy(input_record), "analysis_run_id": str(run_id)},
                "analysis-input-bundle",
                identity_fields=("analysis_run_id",),
            ),
            ("completed",),
            DataQualityStatus.REVIEWED,
            (),
            "completed",
            "rhr_activity_lag_1_7",
            artifact(
                result_record,
                "analysis-result-rhr_activity_lag_1_7",
                identity_fields=("analysis_run_id", "analysis_result_id"),
            ),
            ModelMaturityStatus.EXPLORATORY,
        )

    malformed = publication()
    assert malformed.result_artifact is not None
    malformed_record = json.loads(malformed.result_artifact.payload)
    malformed_record["lag_estimates"] = [True]
    malformed = replace(
        malformed,
        result_artifact=artifact(
            malformed_record,
            "analysis-result-rhr_activity_lag_1_7",
            identity_fields=("analysis_run_id", "analysis_result_id"),
        ),
    )
    writer = LocalStore.open_writer(root=runtime.active_store, mode=runtime.mode)
    try:
        with pytest.raises(StoreError, match="Familienschema"):
            analysis_module._publish_analysis_run(writer, malformed)
    finally:
        writer.close()

    unvalidated = publication()
    unvalidated = replace(
        unvalidated,
        input_artifact=replace(unvalidated.input_artifact, schema_validator=None),
    )
    writer = LocalStore.open_writer(root=runtime.active_store, mode=runtime.mode)
    try:
        staging = writer.prepare_analysis_staging(unvalidated.provenance.analysis_run_id)
        (staging / "input.jsonl").write_bytes(unvalidated.input_artifact.payload)
        with pytest.raises(StoreError, match="fachliches Schema"):
            writer.persist_analysis_run(unvalidated)
    finally:
        writer.close()
    assert staging.is_dir()
    assert not (
        runtime.active_store
        / "parquet"
        / "analysis-runs"
        / str(unvalidated.provenance.analysis_run_id)
    ).exists()

    complete = publication()
    writer = LocalStore.open_writer(root=runtime.active_store, mode=runtime.mode)
    try:
        analysis_module._publish_analysis_run(writer, complete)
    finally:
        writer.close()
    directory = (
        runtime.active_store
        / "parquet"
        / "analysis-runs"
        / str(complete.provenance.analysis_run_id)
    )
    assert (directory / "input.jsonl").is_file()
    assert (directory / "result" / "result.jsonl").is_file()
    assert (directory / "result" / "result-manifest.json").is_file()
    with sqlite3.connect(runtime.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT artifact_kind, result_family FROM analysis_artifacts "
            "WHERE analysis_run_id = ? ORDER BY artifact_kind",
            (str(complete.provenance.analysis_run_id),),
        ).fetchall() == [("input", None), ("result", "rhr_activity_lag_1_7")]

    failed = publication()

    def fail_after_result(_root: Path, point: str) -> None:
        if point == "analysis.after_result_write/v1":
            raise OSError("injected result publication fault")

    monkeypatch.setattr(
        "personal_health_lab.storage._store._publication_fault_point", fail_after_result
    )
    writer = LocalStore.open_writer(root=runtime.active_store, mode=runtime.mode)
    try:
        with pytest.raises(StoreError):
            analysis_module._publish_analysis_run(writer, failed)
    finally:
        writer.close()
    assert not (
        runtime.active_store / "parquet" / "analysis-runs" / str(failed.provenance.analysis_run_id)
    ).exists()

    orphan_id = uuid4().hex
    orphan = runtime.active_store / "parquet" / "analysis-runs" / orphan_id
    orphan.mkdir(parents=True)
    (orphan / "input.jsonl").write_text("{}\n", encoding="utf-8")
    stale_id = uuid4().hex
    stale = runtime.active_store / "analysis-staging" / stale_id
    stale.mkdir(parents=True)
    with HealthLab.open(runtime):
        pass
    assert not orphan.exists()
    assert not stale.exists()
    assert (runtime.active_store / "quarantine" / "analyses" / orphan_id).is_dir()
    with sqlite3.connect(runtime.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT count(*) FROM analysis_runs WHERE analysis_run_id = ?",
            (str(failed.provenance.analysis_run_id),),
        ).fetchone() == (0,)


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
            / "analysis-runs"
            / str(receipt.analysis_run_id)
            / "input.jsonl"
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
            "SELECT data_status, data_status_reasons FROM analysis_runs WHERE analysis_run_id = ?",
            (str(receipt.analysis_run_id),),
        ).fetchone() == ("reviewed", "[]")
    payload = json.loads(
        (
            runtime.active_store
            / "parquet"
            / "analysis-runs"
            / str(receipt.analysis_run_id)
            / "input.jsonl"
        ).read_bytes()
    )
    assert {value["input_id"] for value in payload["values"]} == {
        "apple_resting_heart_rate",
        "preferred_daily_weight",
    }
    assert payload["data_quality_fact_ids"] == []
    assert payload["activity_coverage_incomplete"] is False
    assert "activity-derivation/v1" not in payload["rule_versions"]
