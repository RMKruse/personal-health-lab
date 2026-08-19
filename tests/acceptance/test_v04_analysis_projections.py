import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import personal_health_lab.analysis as analysis_module
from personal_health_lab.application import (
    AnalysisDefinitionId,
    AnalysisResultRef,
    AnalysisResultSelection,
    AnalysisRunId,
    AnalysisRunSelection,
    AnalysisStatus,
    DataMode,
    HealthLab,
    ImportHealthExport,
    ImportReceipt,
    ProjectionUnavailable,
    ProjectionUnavailableCode,
    ReproducibilityStatus,
    RhrActivityLag1To7Result,
    RhrActivityLag1To30Result,
    RhrWeightAssociationResult,
    RunAnalysis,
    RuntimeConfig,
    SnapshotRef,
    WeightCoreResult,
    WeightTrendSupportStatus,
)
from personal_health_lab.storage import (
    AnalysisJsonlArtifact,
    AnalysisProvenance,
    AnalysisResultId,
    AnalysisRunConfiguration,
    AnalysisRunPublication,
    DataQualityStatus,
    LocalStore,
    ModelMaturityStatus,
    OperationId,
)

DEFINITION = AnalysisDefinitionId("rhr-activity-lag-1-7-v1")


def _package(path: Path, day: int) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            f"""<HealthData><ExportDate value="2024-01-{day:02d} 12:00:00 +0100"/>
            <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="1"
            sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
            creationDate="2024-01-{day:02d} 12:00:00 +0100"
            startDate="2024-01-{day:02d} 12:00:00 +0100"
            endDate="2024-01-{day:02d} 12:01:00 +0100"/></HealthData>""",
        )
    return path


def _import(health_lab: HealthLab, package: Path) -> None:
    request = ImportHealthExport(package)
    receipt = health_lab.execute_write(
        request, expected_plan=health_lab.preview_write(request).fingerprint
    )
    assert isinstance(receipt.result, ImportReceipt)


def _run(health_lab: HealthLab) -> AnalysisRunId:
    request = RunAnalysis(DEFINITION)
    receipt = health_lab.execute_write(
        request, expected_plan=health_lab.preview_write(request).fingerprint
    )
    assert receipt.result.status is AnalysisStatus.INSUFFICIENT_DATA
    return receipt.result.analysis_run_id


def test_analysis_history_is_snapshot_coherent_and_latest_unavailable_run_wins(
    tmp_path: Path,
) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")

    with HealthLab.open(runtime) as health_lab:
        _import(health_lab, _package(tmp_path / "first.zip", 2))
        first_snapshot = health_lab.load_snapshot_catalog().selected_snapshot_ref
        _run(health_lab)
        _import(health_lab, _package(tmp_path / "second.zip", 3))
        active_snapshot = health_lab.load_snapshot_catalog().selected_snapshot_ref
        _run(health_lab)
        _run(health_lab)

        active = health_lab.load_analysis_runs(AnalysisRunSelection(DEFINITION))
        historical = health_lab.load_analysis_runs(AnalysisRunSelection(DEFINITION, first_snapshot))
        unavailable = health_lab.load_analysis_result(AnalysisResultSelection(DEFINITION))

    assert active.snapshot_ref == active_snapshot
    assert active.analysis_definition_id == DEFINITION
    assert active.projection_id == "analysis-runs"
    assert active.projection_version == 1
    assert len(active.runs) == 2
    assert all(run.snapshot_ref == active_snapshot for run in active.runs)
    assert all(run.analysis_definition_id == DEFINITION for run in active.runs)
    assert all(run.start_date == date(2024, 1, 2) for run in active.runs)
    assert all(run.end_date == date(2024, 1, 3) for run in active.runs)
    assert all(run.status is AnalysisStatus.INSUFFICIENT_DATA for run in active.runs)
    assert len(historical.runs) == 1
    assert historical.snapshot_ref == first_snapshot
    assert isinstance(unavailable, ProjectionUnavailable)
    assert unavailable.code is ProjectionUnavailableCode.RESULT_NOT_AVAILABLE
    assert unavailable.snapshot_ref == active_snapshot
    assert unavailable.analysis_definition_id == DEFINITION
    assert unavailable.analysis_run_id == active.runs[0].analysis_run_id
    assert unavailable.start_date == date(2024, 1, 2)
    assert unavailable.end_date == date(2024, 1, 3)


def test_analysis_result_returns_closed_selection_failures(tmp_path: Path) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")

    with HealthLab.open(runtime) as health_lab:
        no_snapshot = health_lab.load_analysis_result(AnalysisResultSelection(DEFINITION))
        _import(health_lab, _package(tmp_path / "snapshot.zip", 2))
        no_run = health_lab.load_analysis_result(AnalysisResultSelection(DEFINITION))
        run_id = _run(health_lab)
        mismatch = health_lab.load_analysis_result(
            AnalysisResultSelection(
                AnalysisDefinitionId("rhr-activity-lag-1-30-v1"), analysis_run_id=run_id
            )
        )
        missing_snapshot = health_lab.load_analysis_runs(
            AnalysisRunSelection(DEFINITION, SnapshotRef("a" * 32))
        )

    assert no_snapshot.code is ProjectionUnavailableCode.NO_SNAPSHOT
    assert no_snapshot.projection_id == "analysis-result"
    assert no_run.code is ProjectionUnavailableCode.NO_RUN_FOR_SNAPSHOT
    assert no_run.snapshot_ref is not None
    assert mismatch.code is ProjectionUnavailableCode.RUN_SELECTION_MISMATCH
    assert mismatch.analysis_run_id == run_id
    assert missing_snapshot.code is ProjectionUnavailableCode.SNAPSHOT_NOT_FOUND


def _artifact(
    record: dict[str, object], schema_id: str, identity_fields: tuple[str, ...]
) -> AnalysisJsonlArtifact:
    payload = json.dumps(record, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    content = {key: value for key, value in record.items() if key not in identity_fields}
    return AnalysisJsonlArtifact(
        schema_id,
        1,
        hashlib.sha256(
            json.dumps(content, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest(),
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        1,
        payload,
        (
            analysis_module._validate_input_publication
            if schema_id == "analysis-input-bundle"
            else analysis_module._validate_result_artifact
        ),
    )


def _result_fields(family: str) -> dict[str, object]:
    if family.startswith("rhr_activity_lag"):
        return {
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
        }
    if family == "weight_core":
        return {
            "bootstrap_facts": [],
            "diagnostics": [],
            "maturity_criteria": [],
            "models": [],
            "predictions": [],
            "trends": [
                {
                    "day": "2024-01-02",
                    "failure_reason": None,
                    "level_kg": 75.0,
                    "local_support": 4,
                    "numerical_pivot": 0.5,
                    "rate_kg_per_week": -0.1,
                    "support_status": "estimated",
                    "window_days": 7,
                }
            ],
        }
    return {
        "associations": [
            {
                "estimate": 0.2,
                "measure": "pearson_local_slopes",
                "paired_days": 10,
                "pointwise_interval": {"lower": 0.1, "upper": 0.3},
                "simultaneous_band": {"lower": 0.0, "upper": 0.4},
                "window_days": 7,
            }
        ],
        "bootstrap_facts": [],
        "diagnostics": [],
        "maturity_criteria": [],
    }


def _publish_completed_result(
    runtime: RuntimeConfig,
    source_run_id: AnalysisRunId,
    snapshot_ref: SnapshotRef,
    definition_id: AnalysisDefinitionId,
    family: str,
) -> tuple[AnalysisRunId, AnalysisResultRef]:
    source = json.loads(
        (
            runtime.active_store / "parquet" / "analysis-runs" / str(source_run_id) / "input.jsonl"
        ).read_bytes()
    )
    run_id = AnalysisRunId(uuid4().hex)
    result_id = AnalysisResultId(uuid4().hex)
    source["analysis_run_id"] = str(run_id)
    source["analysis_definition_id"] = str(definition_id)
    result_record = {
        "analysis_definition_id": str(definition_id),
        "analysis_result_id": str(result_id),
        "analysis_run_id": str(run_id),
        "result_family": family,
        "result_schema_version": 1,
        **_result_fields(family),
    }
    configuration = AnalysisRunConfiguration(definition_id, None, None, "1.0")
    publication = AnalysisRunPublication(
        OperationId(uuid4().hex),
        AnalysisProvenance(
            run_id,
            result_id,
            snapshot_ref,
            definition_id,
            configuration.content_hash,
            "1.0",
            "a" * 40,
            False,
            None,
            "b" * 64,
        ),
        date.fromisoformat(source["analysis_period"]["start_date"]),
        date.fromisoformat(source["analysis_period"]["end_date"]),
        configuration,
        _artifact(source, "analysis-input-bundle", ("analysis_run_id",)),
        ("completed",),
        DataQualityStatus.REVIEWED,
        (),
        "completed",
        family,
        _artifact(
            result_record,
            f"analysis-result-{family}",
            ("analysis_run_id", "analysis_result_id"),
        ),
        ModelMaturityStatus.EXPLORATORY,
    )
    writer = LocalStore.open_writer(root=runtime.active_store, mode=runtime.mode)
    try:
        analysis_module._publish_analysis_run(writer, publication)
    finally:
        writer.close()
    return run_id, result_id


def test_analysis_result_projects_each_typed_result_family(tmp_path: Path) -> None:
    cases = (
        (DEFINITION, "rhr_activity_lag_1_7", RhrActivityLag1To7Result),
        (
            AnalysisDefinitionId("rhr-activity-lag-1-30-v1"),
            "rhr_activity_lag_1_30",
            RhrActivityLag1To30Result,
        ),
        (AnalysisDefinitionId("weight-core-7-14-30-90-v1"), "weight_core", WeightCoreResult),
        (
            AnalysisDefinitionId("rhr-weight-association-7-14-30-90-v1"),
            "rhr_weight_association",
            RhrWeightAssociationResult,
        ),
    )
    for index, (definition, family, result_type) in enumerate(cases):
        case_path = tmp_path / str(index)
        case_path.mkdir()
        runtime = RuntimeConfig(DataMode.SYNTHETIC, case_path / "store", case_path / "real")
        with HealthLab.open(runtime) as health_lab:
            _import(health_lab, _package(case_path / "snapshot.zip", 2))
            snapshot_ref = health_lab.load_snapshot_catalog().selected_snapshot_ref
            request = RunAnalysis(definition)
            source = health_lab.execute_write(
                request, expected_plan=health_lab.preview_write(request).fingerprint
            ).result
        run_id, result_ref = _publish_completed_result(
            runtime, source.analysis_run_id, snapshot_ref, definition, family
        )

        with HealthLab.open(runtime) as health_lab:
            result = health_lab.load_analysis_result(AnalysisResultSelection(definition))

        assert isinstance(result, result_type)
        assert result.projection_id == "analysis-result"
        assert result.projection_version == 1
        assert result.snapshot_ref == snapshot_ref
        assert result.analysis_definition_id == definition
        assert result.analysis_run_id == run_id
        assert result.result_ref == result_ref
        assert result.start_date == date(2024, 1, 2)
        assert result.end_date == date(2024, 1, 2)
        if isinstance(result, WeightCoreResult):
            assert result.trends[0].support_status is WeightTrendSupportStatus.ESTIMATED


def test_latest_started_run_does_not_fall_back_to_an_older_result(tmp_path: Path) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(runtime) as health_lab:
        _import(health_lab, _package(tmp_path / "snapshot.zip", 2))
        snapshot_ref = health_lab.load_snapshot_catalog().selected_snapshot_ref
        source_run_id = _run(health_lab)
    completed_run_id, _ = _publish_completed_result(
        runtime, source_run_id, snapshot_ref, DEFINITION, "rhr_activity_lag_1_7"
    )
    with HealthLab.open(runtime) as health_lab:
        latest_run_id = _run(health_lab)

    with sqlite3.connect(runtime.active_store / "metadata.sqlite3") as metadata:
        metadata.execute(
            "UPDATE analysis_runs SET completed_at = '2099-01-01T00:00:00+00:00' "
            "WHERE analysis_run_id = ?",
            (str(completed_run_id),),
        )
        metadata.execute(
            "UPDATE analysis_runs SET completed_at = '2000-01-01T00:00:00+00:00' "
            "WHERE analysis_run_id = ?",
            (str(latest_run_id),),
        )

    with HealthLab.open(runtime) as health_lab:
        implicit = health_lab.load_analysis_result(AnalysisResultSelection(DEFINITION))
        explicit = health_lab.load_analysis_result(
            AnalysisResultSelection(DEFINITION, analysis_run_id=completed_run_id)
        )

    assert implicit.code is ProjectionUnavailableCode.RESULT_NOT_AVAILABLE
    assert implicit.analysis_run_id == latest_run_id
    assert isinstance(explicit, RhrActivityLag1To7Result)


def test_analysis_reproducibility_is_derived_from_current_material(tmp_path: Path) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(runtime) as health_lab:
        _import(health_lab, _package(tmp_path / "snapshot.zip", 2))
        run_id = _run(health_lab)
        before = health_lab.load_analysis_runs(AnalysisRunSelection(DEFINITION))

    assert before.runs[0].reproducibility is not ReproducibilityStatus.NOT_RECORDED
    (runtime.active_store / "parquet" / "analysis-runs" / str(run_id) / "input.jsonl").unlink()

    with HealthLab.open(runtime) as health_lab:
        after = health_lab.load_analysis_runs(AnalysisRunSelection(DEFINITION))

    assert after.runs[0].reproducibility is ReproducibilityStatus.NOT_RECORDED


def test_analysis_result_distinguishes_missing_and_corrupt_artifacts(tmp_path: Path) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(runtime) as health_lab:
        _import(health_lab, _package(tmp_path / "snapshot.zip", 2))
        snapshot_ref = health_lab.load_snapshot_catalog().selected_snapshot_ref
        source_run_id = _run(health_lab)
    run_id, _ = _publish_completed_result(
        runtime, source_run_id, snapshot_ref, DEFINITION, "rhr_activity_lag_1_7"
    )
    path = (
        runtime.active_store / "parquet" / "analysis-runs" / str(run_id) / "result" / "result.jsonl"
    )
    payload = path.read_bytes()
    path.unlink()
    with HealthLab.open(runtime) as health_lab:
        unavailable = health_lab.load_analysis_result(AnalysisResultSelection(DEFINITION))
    path.write_bytes(payload + b"corrupt")
    with HealthLab.open(runtime) as health_lab:
        corrupt = health_lab.load_analysis_result(AnalysisResultSelection(DEFINITION))

    assert unavailable.code is ProjectionUnavailableCode.ARTIFACT_UNAVAILABLE
    assert corrupt.code is ProjectionUnavailableCode.INTEGRITY_FAILED
