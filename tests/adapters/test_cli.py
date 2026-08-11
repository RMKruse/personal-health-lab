import fcntl
import json
import platform
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from zipfile import ZipFile

import pytest
from jsonschema import Draft202012Validator, ValidationError

from personal_health_lab.adapters.cli import main
from personal_health_lab.application import (
    AsNeededMedication,
    CanonicalUnit,
    ContextCoverageStartCreate,
    CreateMetadataBackup,
    DataCorrection,
    DataMode,
    DataReviewCaseKind,
    DataReviewSelection,
    FeatureNotAvailableError,
    HealthLab,
    ImportHealthExport,
    ManualContextRevisionReceipt,
    MedicationRegimeCreate,
    ResolveDataReviewCase,
    ReviseContextCoverageStart,
    ReviseMedicationRegime,
    RuntimeConfig,
    SnapshotDateSelection,
)
from personal_health_lab.synthetic_export import GenerationOptions, generate_export

_EXPECTED_RUNTIME_CONFIG = {
    "max_import_compression_ratio": 200.0,
    "max_import_entries": 8,
    "max_import_entry_bytes": 512 * 1024 * 1024,
    "max_import_package_bytes": 512 * 1024 * 1024,
    "max_import_uncompressed_bytes": 512 * 1024 * 1024,
    "mode": "synthetic",
    "real_store": "<redacted>",
    "schema_version": "1.0",
    "synthetic_store": "<redacted>",
}


def _assert_json_contract(output: object) -> None:
    assert isinstance(output, dict)
    schema = json.loads(
        files("personal_health_lab.adapters.cli")
        .joinpath(f"schemas/output-{output['schema_version']}.schema.json")
        .read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(output)


def _execute_json_import(
    common_args: list[str],
    package: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, object]]:
    assert main([*common_args, "import", str(package), "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    _assert_json_contract(plan)
    assert plan["kind"] == "write_plan"
    exit_code = main(
        [
            *common_args,
            "import",
            str(package),
            "--json",
            "--execute",
            "--expect-plan",
            plan["fingerprint"],
        ]
    )
    receipt = json.loads(capsys.readouterr().out)
    _assert_json_contract(receipt)
    invalid_receipt = {**receipt, "result": {"type": "totally_unknown", "secret": "accepted"}}
    with pytest.raises(ValidationError):
        _assert_json_contract(invalid_receipt)
    assert receipt["kind"] == "write_receipt"
    assert receipt["plan_fingerprint"] == plan["fingerprint"]
    return exit_code, receipt


def _execute_json_analysis(
    common_args: list[str],
    capsys: pytest.CaptureFixture[str],
    *analysis_args: str,
) -> tuple[int, dict[str, object]]:
    assert main([*common_args, "analyze", *analysis_args, "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    _assert_json_contract(plan)
    assert plan["details"]["type"] == "run_resting_heart_rate_analysis"
    exit_code = main(
        [
            *common_args,
            "analyze",
            *analysis_args,
            "--json",
            "--execute",
            "--expect-plan",
            plan["fingerprint"],
        ]
    )
    receipt = json.loads(capsys.readouterr().out)
    _assert_json_contract(receipt)
    assert receipt["kind"] == "write_receipt"
    return exit_code, receipt


def test_cli_runs_intake_reason_category_lifecycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    fixture = generate_export("null-v1", 42, tmp_path / "fixture")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    common = [
        "--mode", "synthetic", "--synthetic-store", str(config.synthetic_store),
        "--real-store", str(config.real_store), "medication", "reason",
    ]
    monkeypatch.setattr("builtins.input", lambda _prompt: "j")

    assert main([*common, "create", "Kopfschmerz"]) == 0
    capsys.readouterr()
    with HealthLab.open(config) as health_lab:
        category = health_lab.load_medication_plan().intake_reason_categories[0]

    assert main(
        [*common, "revise", str(category.logical_id), str(category.revision_id), "Schmerz"]
    ) == 0
    capsys.readouterr()
    with HealthLab.open(config) as health_lab:
        revision = health_lab.load_medication_audit(category.logical_id).revisions[-1]

    assert main(
        [
            *common,
            "withdraw",
            str(category.logical_id),
            str(revision.revision_id),
            "--reason",
            "Nicht mehr gebraucht",
        ]
    ) == 0
    capsys.readouterr()
    with HealthLab.open(config) as health_lab:
        withdrawn = health_lab.load_medication_audit(category.logical_id).revisions[-1]

    assert main(
        [*common, "restore", str(category.logical_id), str(withdrawn.revision_id), "Schmerz"]
    ) == 0
    capsys.readouterr()
    with HealthLab.open(config) as health_lab:
        restored = health_lab.load_medication_plan().intake_reason_categories[0]

    assert restored.name == "Schmerz"

    with HealthLab.open(config) as health_lab:
        regime_request = ReviseMedicationRegime(
            MedicationRegimeCreate(
                datetime.fromisoformat("2024-03-01T00:00:00+01:00"),
                "Europe/Berlin",
                (),
                (AsNeededMedication("Ibuprofen", Decimal("400"), "mg"),),
            )
        )
        regime_receipt = health_lab.execute_write(
            regime_request,
            expected_plan=health_lab.preview_write(regime_request).fingerprint,
        )
        entry_id = health_lab.load_medication_plan().regimes[0].as_needed_medications[0].entry_id
    intake = [*common[:-1], "intake"]
    taken_at = "2024-04-01T12:00:00+02:00"

    assert main(
        [
            *intake,
            "create",
            str(regime_receipt.result.logical_id),
            str(entry_id),
            taken_at,
            "200",
            "--reason-category",
            str(restored.logical_id),
        ]
    ) == 0
    capsys.readouterr()
    with HealthLab.open(config) as health_lab:
        day = health_lab.load_medication_days(
            SnapshotDateSelection(None, date(2024, 4, 1), date(2024, 4, 1))
        ).days[0]
        intake_record = day.as_needed_intakes[0]
        intake_revision = health_lab.load_medication_audit(intake_record.logical_id).revisions[-1]

    assert main(
        [
            *intake,
            "revise",
            str(intake_record.logical_id),
            str(intake_revision.revision_id),
            taken_at,
            "300",
            "--reason-category",
            str(restored.logical_id),
        ]
    ) == 0
    capsys.readouterr()
    with HealthLab.open(config) as health_lab:
        intake_revision = health_lab.load_medication_audit(intake_record.logical_id).revisions[-1]

    assert main(
        [
            *intake,
            "withdraw",
            str(intake_record.logical_id),
            str(intake_revision.revision_id),
            "--reason",
            "Doppelt erfasst",
        ]
    ) == 0
    capsys.readouterr()
    with HealthLab.open(config) as health_lab:
        intake_revision = health_lab.load_medication_audit(intake_record.logical_id).revisions[-1]

    assert main(
        [
            *intake,
            "restore",
            str(intake_record.logical_id),
            str(intake_revision.revision_id),
            taken_at,
            "300",
            "--reason-category",
            str(restored.logical_id),
        ]
    ) == 0
    capsys.readouterr()
    with HealthLab.open(config) as health_lab:
        restored_day = health_lab.load_medication_days(
            SnapshotDateSelection(None, date(2024, 4, 1), date(2024, 4, 1))
        ).days[0]

    assert restored_day.as_needed_intakes[0].amount == Decimal("300")


def test_cli_renders_import_details_as_human_text_and_json_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    xml = (Path(__file__).parents[1] / "fixtures/v03/unsupported-import-content.xml").read_text(
        encoding="utf-8"
    )
    package = tmp_path / "unsupported.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        receipt = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    import_id = str(receipt.result.import_id)
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(config.synthetic_store),
        "--real-store",
        str(config.real_store),
    ]

    assert main([*common, "import-details", import_id]) == 0
    human = capsys.readouterr().out
    assert f"Import-ID: {import_id}" in human
    assert "Kanonische Records im Paket: 1" in human
    assert "workout_child · WorkoutEvent · 2" in human

    assert main([*common, "import-details", import_id, "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    _assert_json_contract(output)
    assert output["schema_version"] == "3.0"
    assert output["kind"] == "import_details"
    assert output["canonical_counts"] == {
        "anomaly_count": 0,
        "logical_measurement_count": 1,
        "measurement_version_count": 1,
        "package_record_count": 1,
        "record_count": 1,
        "source_occurrence_count": 1,
    }
    assert output["unsupported_content"][-1] == {
        "category": "workout_child",
        "count": 2,
        "external_identifier": "WorkoutEvent",
    }

    assert main([*common, "overview", "--json"]) == 0
    overview = json.loads(capsys.readouterr().out)
    _assert_json_contract(overview)
    assert overview["schema_version"] == "3.0"


@pytest.mark.v02_adapter(
    "cli",
    "ContextAudit",
    "ContextRecords",
    "DailyContext",
    "ManualContextRevisionPlan",
    "ManualContextRevisionReceipt",
    "MedicationRegimePlan",
    "MedicationRegimeReceipt",
    "MedicationDeviationPlan",
    "MedicationDeviationReceipt",
    "AsNeededIntakePlan",
    "AsNeededIntakeReceipt",
    "IntakeReasonCategoryPlan",
    "IntakeReasonCategoryReceipt",
    "IllnessRevisionPlan",
    "NoChangeStatus",
    "ReviseContextCoverageStart",
    "ReviseCustomContextLabel",
    "ReviseCustomContextPeriod",
    "ReviseDailyStress",
    "ReviseIllnessCategory",
    "ReviseIllnessPeriod",
    "ReviseMedicationRegime",
    "ReviseMedicationDeviation",
    "ReviseAsNeededIntake",
    "ReviseIntakeReasonCategory",
    "WriteNoChange",
)
def test_cli_loads_context_projections(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    package = tmp_path / "context.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            """<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
            <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="1"
            sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
            creationDate="2024-01-02 12:00:00 +0100"
            startDate="2024-01-02 12:00:00 +0100" endDate="2024-01-02 12:01:00 +0100"/>
            </HealthData>""",
        )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(package)
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        request = ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 2)))
        receipt = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    assert isinstance(receipt.result, ManualContextRevisionReceipt)
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(config.synthetic_store),
        "--real-store",
        str(config.real_store),
        "context",
    ]
    assert (
        main(
            [
                *common,
                "days",
                "--start-date",
                "2024-01-01",
                "--end-date",
                "2024-01-03",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["kind"] == "daily_context"
    assert main([*common, "records", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "context_records"
    assert main([*common, "audit", str(receipt.result.logical_id), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "context_audit"


@pytest.mark.v02_adapter("cli", "WeightDayStatus")
def test_cli_renders_the_complete_weight_projection_as_human_text_and_json_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    xml = (Path(__file__).parents[1] / "fixtures/v03/weight-edges.xml").read_text(encoding="utf-8")
    package = tmp_path / "weights.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(config.synthetic_store),
        "--real-store",
        str(config.real_store),
        "weight-nutrition",
        "--start-date",
        "2024-01-01",
        "--end-date",
        "2024-01-06",
    ]

    assert main(common) == 0
    human = capsys.readouterr().out
    assert "Gewicht und Ernährung" in human
    assert "2024-01-02 · missing · - kg" in human
    assert "2024-01-05 · ambiguous · - kg" in human
    assert "Original: 100.0 lb" in human
    assert "2024-01-01 · Energie: 100.0 kcal · Protein: 20.0 g" in human
    assert "2024-01-02 · Energie: - kcal · Protein: - g · Kohlenhydrate: 30.0 g" in human
    assert "Ernährungsmerkmal: dietary_energy_consumed · Qualität: reviewed" in human
    assert "Logische Messungen:" in human
    assert "Messungsversionen:" in human
    assert "dietary_biotin · 0.0001 g · Original: 100.0 mcg" in human

    assert main([*common, "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    _assert_json_contract(output)
    assert output["kind"] == "weight_nutrition"
    assert output["selection"] == {
        "end_date": "2024-01-06",
        "snapshot_ref": None,
        "start_date": "2024-01-01",
    }
    assert output["days"][1]["status"] == "missing"
    assert output["days"][4]["status"] == "ambiguous"
    assert len(output["weight_measurements"]) == 7
    assert output["nutrition_days"][0]["energy"]["value"] == 100
    assert output["nutrition_days"][0]["energy"]["quality_status"] == "reviewed"
    assert output["nutrition_days"][0]["energy"]["logical_measurement_ids"]
    assert output["nutrition_days"][0]["energy"]["measurement_version_ids"]
    assert output["nutrition_days"][0]["protein"]["value"] == 20
    assert output["nutrition_days"][1]["energy"]["value"] is None
    assert output["nutrition_days"][1]["carbohydrates"]["value"] == 30
    assert len(output["healthkit_nutrition_samples"]) == 5
    biotin = next(
        item
        for item in output["healthkit_nutrition_samples"]
        if item["data_type"] == "dietary_biotin"
    )
    assert biotin["value"] == pytest.approx(0.0001)
    assert biotin["original_value"] == 100
    assert biotin["original_unit"] == "mcg"
    pounds = next(item for item in output["weight_measurements"] if item["original_unit"] == "lb")
    assert pounds["value_kg"] == pytest.approx(45.359237)
    assert pounds["original_value"] == 100

    with HealthLab.open(config) as health_lab:
        conflict = health_lab.load_data_review(
            DataReviewSelection(DataReviewCaseKind.PREFERRED_DAILY_WEIGHT_CONFLICT)
        ).cases[0]
        assert conflict.measurement_version_id is not None
        correction = ResolveDataReviewCase(
            conflict.case_id,
            DataCorrection(
                conflict.measurement_version_id,
                82,
                CanonicalUnit.KILOGRAM,
                "Waage falsch abgelesen",
            ),
        )
        health_lab.execute_write(
            correction,
            expected_plan=health_lab.preview_write(correction).fingerprint,
        )

    assert main(common) == 0
    corrected_human = capsys.readouterr().out
    assert "Effektiv: 82.0 kg · Disposition: included_correction · Ausgewählt: true" in (
        corrected_human
    )
    assert "Logische Messungen:" in corrected_human
    assert "Messungsversionen:" in corrected_human
    assert "Prüffälle:" in corrected_human


@pytest.mark.v02_adapter(
    "cli",
    "CreateMetadataBackup",
    "MetadataBackupPlan",
    "MetadataBackupReceipt",
    "MetadataBackupStatus",
)
def test_cli_maps_metadata_backup_without_exposing_its_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = [
        "--mode",
        "real",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    target = tmp_path / "private" / "metadata.sqlite3"
    target.parent.mkdir()
    with HealthLab.open(RuntimeConfig(DataMode.REAL, tmp_path / "synthetic", tmp_path / "real")):
        pass
    args = [*common, "backup", str(target), "--json"]
    assert main(args) == 0
    plan_text = capsys.readouterr().out
    plan = json.loads(plan_text)
    assert plan["details"]["type"] == "create_metadata_backup"
    assert plan["request"]["target"] == "<redacted>"
    assert str(tmp_path) not in plan_text

    assert main([*args, "--execute", "--expect-plan", plan["fingerprint"]]) == 0
    receipt_text = capsys.readouterr().out
    receipt = json.loads(receipt_text)
    assert receipt["result"]["status"] == "completed"
    assert receipt["result"]["target_file"] == "metadata.sqlite3"
    assert str(tmp_path) not in receipt_text


@pytest.mark.v02_adapter(
    "cli",
    "BeginMetadataRestore",
    "AbortMetadataRestore",
    "MetadataRestorePlan",
    "AbortMetadataRestorePlan",
    "MetadataRestoreReceipt",
    "MetadataRestoreStatus",
    "RecoveryStatus",
)
def test_cli_maps_metadata_restore_status_and_abort_without_exposing_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backup = tmp_path / "private" / "metadata.sqlite3"
    backup.parent.mkdir()
    source_config = RuntimeConfig(
        DataMode.REAL, tmp_path / "source-synthetic", tmp_path / "source-real"
    )
    with HealthLab.open(source_config) as health_lab:
        request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    common = [
        "--mode",
        "real",
        "--synthetic-store",
        str(tmp_path / "target-synthetic"),
        "--real-store",
        str(tmp_path / "target-real"),
    ]

    assert main([*common, "restore", str(backup), "--json"]) == 0
    plan_text = capsys.readouterr().out
    plan = json.loads(plan_text)
    _assert_json_contract(plan)
    assert plan["details"]["type"] == "begin_metadata_restore"
    assert plan["request"] == {"backup": "<redacted>", "type": "begin_metadata_restore"}
    assert str(tmp_path) not in plan_text

    assert (
        main(
            [
                *common,
                "restore",
                str(backup),
                "--json",
                "--execute",
                "--expect-plan",
                plan["fingerprint"],
            ]
        )
        == 0
    )
    receipt_text = capsys.readouterr().out
    receipt = json.loads(receipt_text)
    _assert_json_contract(receipt)
    assert receipt["result"]["type"] == "metadata_restore"
    assert receipt["result"]["status"] == "pending"
    assert str(tmp_path) not in receipt_text

    assert main([*common, "recovery-status", "--json"]) == 0
    status_text = capsys.readouterr().out
    status = json.loads(status_text)
    _assert_json_contract(status)
    assert status["kind"] == "recovery_status"
    assert status["recovery"]["status"] == "pending"
    assert status["workspace"]["state"] == "restore_pending"
    assert str(tmp_path) not in status_text

    assert main([*common, "abort-restore", "--json"]) == 0
    abort_plan = json.loads(capsys.readouterr().out)
    _assert_json_contract(abort_plan)
    assert abort_plan["details"]["type"] == "abort_metadata_restore"
    assert (
        main(
            [
                *common,
                "abort-restore",
                "--json",
                "--execute",
                "--expect-plan",
                abort_plan["fingerprint"],
            ]
        )
        == 0
    )
    aborted = json.loads(capsys.readouterr().out)
    _assert_json_contract(aborted)
    assert aborted["result"]["status"] == "aborted"
    assert not (tmp_path / "target-real").exists()


def test_cli_completes_restore_through_the_shared_import_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "fixture.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            '<?xml version="1.0"?><HealthData>'
            '<ExportDate value="2024-03-01 00:00:00 +0000"/>'
            '<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
            'sourceName="Restore Watch" sourceVersion="1" device="Restore Device" '
            'unit="count/min" creationDate="2024-01-01 00:00:00 +0000" '
            'startDate="2024-01-01 00:00:00 +0000" '
            'endDate="2024-01-01 00:00:00 +0000" value="60">'
            '<MetadataEntry key="HKMetadataKeySyncIdentifier" value="cli-restore"/>'
            "</Record></HealthData>",
        )
    backup = tmp_path / "private" / "metadata.sqlite3"
    backup.parent.mkdir()
    source_config = RuntimeConfig(
        DataMode.REAL, tmp_path / "source-synthetic", tmp_path / "source-real"
    )
    with HealthLab.open(source_config) as health_lab:
        import_request = ImportHealthExport(package)
        health_lab.execute_write(
            import_request,
            expected_plan=health_lab.preview_write(import_request).fingerprint,
        )
        backup_request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            backup_request,
            expected_plan=health_lab.preview_write(backup_request).fingerprint,
        )
    common = [
        "--mode",
        "real",
        "--synthetic-store",
        str(tmp_path / "target-synthetic"),
        "--real-store",
        str(tmp_path / "target-real"),
    ]

    assert main([*common, "restore", str(backup), "--json"]) == 0
    restore_plan = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                *common,
                "restore",
                str(backup),
                "--json",
                "--execute",
                "--expect-plan",
                restore_plan["fingerprint"],
            ]
        )
        == 0
    )
    capsys.readouterr()

    exit_code, receipt = _execute_json_import(common, package, capsys)
    assert exit_code == 0
    assert receipt["result"]["status"] == "committed"
    with HealthLab.open(
        RuntimeConfig(
            DataMode.REAL,
            tmp_path / "target-synthetic",
            tmp_path / "target-real",
        )
    ) as health_lab:
        assert health_lab.load_workspace_status().state.value == "ready"


@pytest.mark.v02_adapter("cli", "DataReviewAction", "DataReviewCycleStatus")
def test_cli_projects_source_conflicts_from_the_shared_data_review(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    source_conflict_package: Callable[[Path], Path],
) -> None:
    config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "synthetic",
        real_store=tmp_path / "real",
    )
    package = source_conflict_package(tmp_path / "conflict.zip")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert (
        main(
            [
                "--mode",
                "synthetic",
                "--synthetic-store",
                str(config.synthetic_store),
                "--real-store",
                str(config.real_store),
                "review",
                "--json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)

    _assert_json_contract(output)
    assert output["cases"][0]["kind"] == "source_conflict"
    assert output["cases"][0]["logical_measurement_id"] is not None
    assert output["cases"][0]["allowed_actions"] == ["prefer", "split"]
    assert len(output["cases"][0]["candidate_version_ids"]) == 2
    assert output["status"] == "provisional"
    assert output["cycles"][0]["status"] == "open"
    assert output["cases"][0]["detail"]["reasons"] == []


@pytest.mark.v02_adapter(
    "cli",
    "ResolveDataReviewCase",
    "RevokeDataReviewDecision",
    "DataReviewDecisionPlan",
    "DataReviewBatchRevokePlan",
    "WriteDecisionReceipt",
    "WriteBatchDecisionReceipt",
    "DataReview",
    "DataReviewCaseDetail",
    "DataReviewAction",
    "DataReviewCycleStatus",
)
def test_cli_maps_data_review_plan_receipt_and_revocation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    source_conflict_package: Callable[[Path], Path],
) -> None:
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    package = source_conflict_package(tmp_path / "conflict.zip")
    assert _execute_json_import(common, package, capsys)[0] == 0
    assert main([*common, "review", "--json"]) == 0
    case = json.loads(capsys.readouterr().out)["cases"][0]
    args = [
        *common,
        "review-resolve",
        case["case_id"],
        "--conflict-strategy",
        "prefer",
        "--preferred-version",
        case["candidate_version_ids"][0],
        "--json",
    ]
    assert main(args) == 0
    plan = json.loads(capsys.readouterr().out)
    _assert_json_contract(plan)
    assert plan["details"]["type"] == "data_review_decision"
    assert main([*args, "--execute", "--expect-plan", plan["fingerprint"]]) == 0
    receipt = json.loads(capsys.readouterr().out)
    _assert_json_contract(receipt)
    decision_id = receipt["result"]["decision_id"]

    revoke = [
        *common,
        "review-revoke",
        decision_id,
        "--reason",
        "test",
        "--json",
    ]
    assert main(revoke) == 0
    plan = json.loads(capsys.readouterr().out)
    _assert_json_contract(plan)
    assert main([*revoke, "--execute", "--expect-plan", plan["fingerprint"]]) == 0
    _assert_json_contract(json.loads(capsys.readouterr().out))


@pytest.mark.v02_adapter("cli", "ConfirmDataReviewBatch", "DataReviewBatchPlan")
def test_json_cli_keeps_the_complete_batch_and_reports_plan_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    records = "".join(
        '<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
        'sourceName="Test Watch" device="Test Device" unit="count/min" '
        'sourceVersion="1" creationDate="2024-01-01 07:00:00 +0000" '
        'startDate="2024-01-01 07:00:00 +0000" '
        f'endDate="2024-01-01 07:00:00 +0000" value="{251 + index}">'
        f'<MetadataEntry key="HKMetadataKeySyncIdentifier" value="batch-{index}"/>'
        "</Record>"
        for index in range(100)
    )
    package = tmp_path / "large-batch.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            '<?xml version="1.0"?><HealthData>'
            '<ExportDate value="2024-02-01 12:00:00 +0000"/>'
            f"{records}</HealthData>",
        )
    assert _execute_json_import(common, package, capsys)[0] == 0

    batch = [
        *common,
        "review-confirm-batch",
        "--kind",
        "plausibility",
        "--json",
    ]
    assert main(batch) == 0
    batch_plan = json.loads(capsys.readouterr().out)
    _assert_json_contract(batch_plan)
    assert batch_plan["details"]["count"] == 100
    assert len(batch_plan["details"]["matches"]) == 100

    single = [
        *common,
        "review-resolve",
        batch_plan["details"]["matches"][0]["case_id"],
        "--confirm",
        "--json",
    ]
    assert main(single) == 0
    single_plan = json.loads(capsys.readouterr().out)
    assert main([*single, "--execute", "--expect-plan", single_plan["fingerprint"]]) == 0
    capsys.readouterr()

    assert main([*batch, "--execute", "--expect-plan", batch_plan["fingerprint"]]) == 3
    changed = json.loads(capsys.readouterr().out)
    _assert_json_contract(changed)
    assert changed["result"]["status"] == "plan_changed"


def test_human_batch_cli_displays_every_match_before_confirmation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    package = tmp_path / "plausibility.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            '<?xml version="1.0"?><HealthData>'
            '<ExportDate value="2024-02-01 12:00:00 +0000"/>'
            '<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
            'sourceName="Test Watch" device="Test Device" unit="count/min" '
            'sourceVersion="1" creationDate="2024-01-01 07:00:00 +0000" '
            'startDate="2024-01-01 07:00:00 +0000" '
            'endDate="2024-01-01 07:00:00 +0000" value="251">'
            '<MetadataEntry key="HKMetadataKeySyncIdentifier" value="batch-human"/>'
            "</Record></HealthData>",
        )
    assert _execute_json_import(common, package, capsys)[0] == 0
    displayed: list[str] = []
    monkeypatch.setattr("personal_health_lab.adapters.cli._cli.pydoc.pager", displayed.append)

    def decline(_prompt: str) -> str:
        assert displayed
        return "n"

    monkeypatch.setattr("builtins.input", decline)
    assert main([*common, "review-confirm-batch", "--kind", "plausibility"]) == 0
    assert displayed[0]


def test_cli_maps_correction_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    personal_range_package: Callable[[Path], Path],
) -> None:
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    assert (
        _execute_json_import(common, personal_range_package(tmp_path / "personal.zip"), capsys)[0]
        == 0
    )
    assert main([*common, "review", "--json"]) == 0
    case = json.loads(capsys.readouterr().out)["cases"][0]
    args = [
        *common,
        "review-resolve",
        case["case_id"],
        "--correct",
        "61",
        "--unit",
        "count/min",
        "--measurement-version",
        case["measurement_version_id"],
        "--reason",
        "falscher Wert",
        "--json",
    ]
    assert main(args) == 0
    plan = json.loads(capsys.readouterr().out)
    assert main([*args, "--execute", "--expect-plan", plan["fingerprint"]]) == 0
    _assert_json_contract(json.loads(capsys.readouterr().out))


@pytest.mark.v02_adapter("cli", "ReviewReasonCode")
def test_cli_projects_the_personal_range_finding(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    personal_range_package: Callable[[Path], Path],
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    package = personal_range_package(tmp_path / "personal.zip")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert (
        main(
            [
                "--mode",
                "synthetic",
                "--synthetic-store",
                str(config.synthetic_store),
                "--real-store",
                str(config.real_store),
                "review",
                "--json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)

    _assert_json_contract(output)
    reason = output["cases"][0]["detail"]["reasons"][0]
    assert reason["code"] == "above_personal_upper_bound"
    assert reason["lower_bound"] < 64 < reason["upper_bound"] + 1


@pytest.mark.v02_adapter(
    "cli", "RunHistoricalReview", "HistoricalReviewPlan", "HistoricalReviewReceipt"
)
def test_cli_maps_the_pinned_historical_review_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        plan = health_lab.preview_write(request)
        imported = health_lab.execute_write(request, expected_plan=plan.fingerprint).result

    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(config.synthetic_store),
        "--real-store",
        str(config.real_store),
        "historical-review",
        "apple_resting_heart_rate",
        "--start-date",
        "2024-01-01",
        "--end-date",
        "2024-01-31",
        "--json",
    ]
    assert main(common) == 0
    output = json.loads(capsys.readouterr().out)

    _assert_json_contract(output)
    assert output["details"]["type"] == "run_historical_review"
    assert output["details"]["base_snapshot_ref"] == str(imported.snapshot_ref)
    assert output["details"]["rule_version_id"] == "fixed-plausibility/v1"

    assert main([*common, "--execute", "--expect-plan", output["fingerprint"]]) == 0
    receipt = json.loads(capsys.readouterr().out)
    _assert_json_contract(receipt)
    assert receipt["result"]["type"] == "run_historical_review"


@pytest.mark.v02_adapter(
    "cli",
    "CapacityReason",
    "CapacityStatus",
    "FileVaultReason",
    "FileVaultStatus",
    "PersonBindingStatus",
    "WriteApprovalStatus",
)
def test_real_json_import_renders_shared_confirmation_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    assert (
        main(
            [
                "--mode",
                "real",
                "--synthetic-store",
                str(tmp_path / "synthetic"),
                "--real-store",
                str(tmp_path / "real"),
                "import",
                str(fixture.export_path),
                "--json",
            ]
        )
        == 0
    )
    plan = json.loads(capsys.readouterr().out)

    _assert_json_contract(plan)
    assert plan["approval"]["status"] == "confirmation_required"
    assert plan["confirmations"] == ["real_import_same_person", "filevault_unknown"]
    assert plan["preflight"]["filevault"] == {
        "status": "unknown",
        "target_volume": "unresolved",
        "reason": "unsupported_platform",
    }
    capacity = plan["preflight"]["capacity"]
    assert capacity["status"] == "ready"
    assert capacity["method_id"] == "full-snapshot-import/v1"
    assert capacity["target_volume"].startswith("volume-")
    assert str(tmp_path) not in json.dumps(capacity)
    assert plan["workspace"]["mode"] == "real"
    assert plan["workspace"]["person_binding"] == "unbound"
    assert len(plan["workspace"]["store_id"]) == 32
    assert plan["workspace"]["allowed_writes"] == [
        "import_health_export",
        "resolve_data_review_case",
        "confirm_data_review_batch",
        "revoke_data_review_decision",
        "create_plausibility_rule_version",
        "run_historical_review",
        "run_resting_heart_rate_analysis",
        "create_metadata_backup",
        "begin_metadata_restore",
        "migrate_store",
        "rollback_migration",
    ]


@pytest.mark.v02_adapter(
    "cli",
    "CreatePlausibilityRuleVersion",
    "PlausibilityRuleVersionPlan",
    "PlausibilityRuleVersionReceipt",
    "PlausibilityRules",
)
def test_cli_projects_and_creates_plausibility_rule_versions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    assert main([*common, "rules", "--json"]) == 0
    rules = json.loads(capsys.readouterr().out)
    _assert_json_contract(rules)
    assert len(rules["rules"]) == 45
    body_mass = next(rule for rule in rules["rules"] if rule["data_type"] == "body_mass")
    assert body_mass["versions"] == []
    assert body_mass["recommendation"]["specification"] == {
        "active": True,
        "fixed_lower_bound": 1.0,
        "fixed_upper_bound": None,
        "personal_range_enabled": True,
        "unit": "kg",
    }

    args = [
        *common,
        "rule",
        "apple_resting_heart_rate",
        "--unit",
        "count/min",
        "--lower",
        "30",
        "--upper",
        "200",
        "--effective-from",
        "2024-02-12T00:00:00+01:00",
        "--json",
    ]
    assert main(args) == 0
    plan = json.loads(capsys.readouterr().out)
    _assert_json_contract(plan)
    assert plan["details"]["type"] == "create_plausibility_rule_version"
    assert main([*args, "--execute", "--expect-plan", plan["fingerprint"]]) == 0
    receipt = json.loads(capsys.readouterr().out)
    _assert_json_contract(receipt)
    assert receipt["result"]["status"] == "committed"


def test_cli_returns_expected_incomplete_for_rejected_import(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "unsafe.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr("apple_health_export/export.xml", "<HealthData/>")
        archive.writestr("../private-health-value", "181")

    exit_code, output = _execute_json_import(
        [
            "--mode",
            "synthetic",
            "--synthetic-store",
            str(tmp_path / "synthetic"),
            "--real-store",
            str(tmp_path / "real"),
        ],
        package,
        capsys,
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert output["result"]["status"] == "rejected"
    assert captured.err == ""


@pytest.mark.v02_adapter("cli", "WriteNotStarted", "WriteNotStartedStatus")
def test_cli_returns_expected_incomplete_while_store_is_busy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common_args = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    assert main([*common_args, "overview"]) == 0
    capsys.readouterr()
    package = tmp_path / "health.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr("apple_health_export/export.xml", "<HealthData/>")

    with (tmp_path / "synthetic" / ".writer.lock").open("a+b") as writer_lock:
        fcntl.flock(writer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        exit_code, import_receipt = _execute_json_import(common_args, package, capsys)
        assert exit_code == 3
        analysis_exit, analysis_receipt = _execute_json_analysis(common_args, capsys)
        assert analysis_exit == 3

    _assert_json_contract(import_receipt)
    _assert_json_contract(analysis_receipt)
    assert import_receipt["result"]["status"] == "store_busy"
    assert analysis_receipt["result"]["type"] == "write_not_started"
    assert analysis_receipt["result"]["status"] == "store_busy"


def test_cli_returns_expected_incomplete_for_unstable_analysis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = generate_export(
        "null-v1",
        42,
        tmp_path / "fixture",
        options=GenerationOptions(resting_heart_rate_noise_standard_deviation=0.0),
    )
    common_args = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    assert _execute_json_import(common_args, fixture.export_path, capsys)[0] == 0

    exit_code, receipt = _execute_json_analysis(common_args, capsys)
    assert exit_code == 3
    _assert_json_contract(receipt)
    assert receipt["result"]["status"] == "unstable"


def test_json_cli_analysis_reports_plan_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = generate_export("null-v1", 42, tmp_path / "first")
    second = generate_export("null-v1", 43, tmp_path / "second")
    common_args = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    assert _execute_json_import(common_args, first.export_path, capsys)[0] == 0
    assert main([*common_args, "analyze", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert _execute_json_import(common_args, second.export_path, capsys)[0] == 0

    assert (
        main(
            [
                *common_args,
                "analyze",
                "--json",
                "--execute",
                "--expect-plan",
                plan["fingerprint"],
            ]
        )
        == 3
    )
    receipt = json.loads(capsys.readouterr().out)

    _assert_json_contract(receipt)
    assert receipt["result"]["type"] == "write_not_started"
    assert receipt["result"]["status"] == "plan_changed"


@pytest.mark.v02_adapter(
    "cli",
    "ConfigurationError",
    "HealthLabError",
)
def test_installed_cli_contracts_streams_and_redacts_technical_errors(tmp_path: Path) -> None:
    executable = Path(sys.executable).with_name("healthlab")
    common_args = [
        str(executable),
        "--mode",
        "synthetic",
        "--real-store",
        str(tmp_path / "real"),
    ]

    success = subprocess.run(
        [
            *common_args,
            "--synthetic-store",
            str(tmp_path / "synthetic"),
            "overview",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert success.returncode == 0
    _assert_json_contract(json.loads(success.stdout))
    assert success.stderr == ""

    sensitive_store = tmp_path / "resting-heart-rate-181"
    sensitive_store.write_text("not a directory", encoding="utf-8")

    failure = subprocess.run(
        [
            *common_args,
            "--synthetic-store",
            str(sensitive_store),
            "overview",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert failure.returncode == 1
    assert failure.stdout == ""
    assert failure.stderr == (
        "ERROR healthlab_failed error_class=HealthLabError\nTechnischer HealthLab-Fehler.\n"
    )


@pytest.mark.v02_adapter("cli", "FeatureNotAvailableError")
def test_cli_translates_feature_not_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        HealthLab,
        "open",
        lambda _config: (_ for _ in ()).throw(FeatureNotAvailableError("unavailable")),
    )

    assert (
        main(
            [
                "--mode",
                "synthetic",
                "--synthetic-store",
                str(tmp_path / "synthetic"),
                "overview",
            ]
        )
        == 1
    )
    assert "Technischer HealthLab-Fehler." in capsys.readouterr().err


def test_cli_returns_usage_error_for_unknown_arguments_and_invalid_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid_config = tmp_path / "config.json"
    invalid_config.write_text('{"mode": "unsupported"}', encoding="utf-8")
    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")

    for args in (
        ["--unknown"],
        [
            "--mode",
            "synthetic",
            "--synthetic-store",
            str(tmp_path / "synthetic"),
            "--real-store",
            str(tmp_path / "real"),
            "import",
            str(tmp_path / "health.zip"),
            "--json",
            "--execute",
            "--expect-plan",
            "not-a-fingerprint",
        ],
        ["--config", str(invalid_config), "overview"],
        ["--config", str(invalid_utf8), "overview"],
        ["--config", str(tmp_path / "missing.json"), "overview"],
    ):
        with pytest.raises(SystemExit) as exit_info:
            main(args)

        captured = capsys.readouterr()
        assert exit_info.value.code == 2
        assert captured.out == ""
        assert captured.err.startswith("usage: healthlab")


def test_cli_prints_empty_overview_as_versioned_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "--mode",
            "synthetic",
            "--synthetic-store",
            str(tmp_path / "synthetic"),
            "--real-store",
            str(tmp_path / "real"),
            "overview",
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    _assert_json_contract(output)
    assert output == {
        "analysis_history": [],
        "daily_series": [],
        "last_reviewed_analysis": None,
        "message": "Keine Gesundheitsdaten vorhanden.",
        "resting_hr_analysis": None,
        "provenance": {
            "import_count": 0,
            "logical_measurement_count": 0,
            "measurement_version_count": 0,
            "package_count": 0,
            "quarantined_import_count": 0,
            "snapshot_count": 0,
        },
        "runtime_config": _EXPECTED_RUNTIME_CONFIG,
        "schema_version": "3.0",
        "selection": {"end_date": None, "start_date": None},
        "status": "empty",
    }


@pytest.mark.v02_adapter(
    "cli",
    "ImportHealthExport",
    "ImportHealthExportPlan",
    "ImportReceipt",
    "ImportStatus",
    "Overview",
    "OverviewStatus",
    "WorkspaceStatus",
)
def test_cli_imports_export_and_prints_daily_series(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    common_args = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]

    result = _execute_json_import(common_args, fixture.export_path, capsys)
    assert result[0] == 0
    receipt = result[1]
    assert receipt["result"]["status"] == "committed"
    assert receipt["result"]["anomaly_count"] == 1
    assert receipt["result"]["record_count"] == 1_825
    assert receipt["runtime_config"] == _EXPECTED_RUNTIME_CONFIG

    result = _execute_json_import(common_args, fixture.export_path, capsys)
    assert result[0] == 0
    duplicate = result[1]
    assert duplicate["result"]["status"] == "duplicate"

    assert main([*common_args, "overview", "--json"]) == 0
    overview = json.loads(capsys.readouterr().out)
    _assert_json_contract(overview)
    assert [(series["data_type"], series["unit"]) for series in overview["daily_series"]] == [
        ("active_energy", "kcal"),
        ("apple_resting_heart_rate", "count/min"),
    ]
    assert len(overview["daily_series"][0]["values"]) == 365
    assert overview["provenance"]["quarantined_import_count"] == 0


@pytest.mark.v02_adapter(
    "cli",
    "RunRestingHeartRateAnalysis",
    "RestingHeartRateAnalysisPlan",
    "AnalysisReceipt",
    "AnalysisStatus",
)
def test_cli_runs_and_exposes_the_built_in_lag_analysis(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    common_args = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    monkeypatch.setattr("builtins.input", lambda _: "j")
    assert main([*common_args, "import", str(fixture.export_path)]) == 0
    human_import = capsys.readouterr().out
    assert "Konfiguration:" in human_import
    assert str(tmp_path) not in human_import

    analysis_exit, receipt = _execute_json_analysis(common_args, capsys)
    assert analysis_exit == 0
    _assert_json_contract(receipt)
    analysis_receipt = receipt["result"]
    assert analysis_receipt["status"] == "completed"
    assert analysis_receipt["analysis_definition_id"] == "lag-signal-v2"
    analysis_result = analysis_receipt["analysis"]
    assert len(analysis_result["lag_associations"]) == 7
    assert analysis_result["model_maturity"] == "robust"
    assert analysis_result["methodology"]["bootstrap_method"] == "moving_block"
    assert analysis_result["diagnostics"]
    provenance = analysis_receipt["provenance"]
    assert provenance == analysis_result["provenance"]
    assert set(provenance) == {
        "analysis_definition_id",
        "analysis_run_id",
        "code_commit",
        "code_diff_hash",
        "code_dirty",
        "config_hash",
        "config_schema_version",
        "environment_lock_hash",
        "result_ref",
        "snapshot_ref",
    }
    assert len(provenance["config_hash"]) == 64
    assert len(provenance["environment_lock_hash"]) == 64
    assert all(
        item["pointwise_interval"] and item["simultaneous_band"]
        for item in analysis_result["lag_associations"]
    )
    structured_result = json.dumps(analysis_result, ensure_ascii=False).lower()
    assert all(
        forbidden not in structured_result
        for forbidden in (
            "*",
            "significant",
            "signifikant",
            "causal",
            "kausal",
            "medical",
            "medizin",
            "recommend",
            "empfehl",
            "therap",
        )
    )

    assert main([*common_args, "overview", "--json"]) == 0
    overview = json.loads(capsys.readouterr().out)
    _assert_json_contract(overview)
    result = overview["resting_hr_analysis"]
    assert len(result["lag_associations"]) == 7
    assert result["lag_associations"][0]["direction"] == "negative"
    assert result["provenance"] == provenance

    assert main([*common_args, "overview"]) == 0
    human_overview = capsys.readouterr().out
    assert "Konfiguration:" in human_overview
    assert str(tmp_path) not in human_overview

    reused_exit, reused = _execute_json_analysis(common_args, capsys)
    assert reused_exit == 0
    _assert_json_contract(reused)
    assert reused["result"]["status"] == "reused"

    assert main([*common_args, "analyze"]) == 0
    human_output = capsys.readouterr().out
    assert "Konfiguration:" in human_output
    assert str(tmp_path) not in human_output
    assert "simultan" in human_output
    assert "Diagnosen:" in human_output

    insufficient_exit, insufficient = _execute_json_analysis(
        common_args, capsys, "--start-date", "2024-12-20"
    )
    assert insufficient_exit == 3
    _assert_json_contract(insufficient)
    assert insufficient["result"]["status"] == "insufficient_data"
    assert insufficient["result"]["analysis"] is None
    assert insufficient["result"]["provenance"]["snapshot_ref"] == provenance["snapshot_ref"]
    assert insufficient["result"]["provenance"]["result_ref"] is None


@pytest.mark.v02_adapter(
    "cli",
    "MigrateStore",
    "RollbackMigration",
    "StoreMigrationPlan",
    "RollbackMigrationPlan",
    "StoreMigrationReceipt",
    "RollbackMigrationReceipt",
    "MigrationDiagnostics",
    "MigrationStatus",
)
def test_cli_maps_store_migration_plan_and_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "migration-store"
    config = RuntimeConfig(DataMode.SYNTHETIC, store, tmp_path / "real")
    fixture = generate_export("null-v1", 42, tmp_path / "migration-fixture")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        imported = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    snapshot_ref = str(imported.result.snapshot_ref)
    with sqlite3.connect(store / "metadata.sqlite3") as metadata:
        metadata.execute("UPDATE store_identity SET schema_version = 2 WHERE singleton = 1")
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(store),
        "--real-store",
        str(tmp_path / "real"),
    ]

    assert main([*common, "migrate", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    _assert_json_contract(plan)
    assert plan["workspace"]["allowed_writes"] == ["migrate_store"]
    assert plan["details"]["steps"] == [
        [2, 3],
        [3, 4],
        [4, 5],
        [5, 6],
        [6, 7],
        [7, 8],
        [8, 9],
        [9, 10],
        [10, 11],
    ]
    assert plan["details"]["backup_file"] == "metadata-v2-to-v11.sqlite3"
    assert plan["details"]["snapshot_source_version"] == 7
    assert plan["details"]["snapshot_as_of"] is not None
    assert plan["details"]["snapshot_target_version"] == 7
    assert plan["details"]["snapshot_steps"] == []
    assert plan["details"]["affected_snapshot_refs"] == [snapshot_ref]
    assert plan["details"]["existing_analyses_become_stale"] is True

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")
        assert main([*common, "migrate"]) == 0
    human_plan = capsys.readouterr().out
    assert "Migrationskette: 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8 -> 9 -> 10 -> 11" in human_plan
    assert "Snapshot-Migrationskette: -" in human_plan
    assert "Snapshot-Stichtag: " in human_plan
    assert "Migrationssicherung: metadata-v2-to-v11.sqlite3" in human_plan
    assert f"Betroffene Snapshots: {snapshot_ref}" in human_plan
    assert "Bestehende Analysen werden veraltet: ja" in human_plan

    assert (
        main(
            [
                *common,
                "migrate",
                "--json",
                "--execute",
                "--expect-plan",
                plan["fingerprint"],
            ]
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    _assert_json_contract(receipt)
    assert receipt["result"]["status"] == "completed"
    assert receipt["result"]["steps"] == [
        [2, 3],
        [3, 4],
        [4, 5],
        [5, 6],
        [6, 7],
        [7, 8],
        [8, 9],
        [9, 10],
        [10, 11],
    ]
    assert receipt["result"]["snapshot_source_version"] == 7
    assert receipt["result"]["snapshot_as_of"] == plan["details"]["snapshot_as_of"]
    assert receipt["result"]["snapshot_target_version"] == 7
    assert receipt["result"]["snapshot_steps"] == []

    assert main([*common, "rollback-migration", "--json"]) == 0
    rollback_plan = json.loads(capsys.readouterr().out)
    _assert_json_contract(rollback_plan)
    assert rollback_plan["details"]["type"] == "rollback_migration"
    assert rollback_plan["details"]["source_version"] == 11
    assert rollback_plan["details"]["target_version"] == 2
    assert rollback_plan["details"]["restored_snapshot_ref"] == snapshot_ref

    assert (
        main(
            [
                *common,
                "rollback-migration",
                "--json",
                "--execute",
                "--expect-plan",
                rollback_plan["fingerprint"],
            ]
        )
        == 0
    )
    rollback_receipt = json.loads(capsys.readouterr().out)
    _assert_json_contract(rollback_receipt)
    assert rollback_receipt["result"]["status"] == "completed"
    assert rollback_receipt["result"]["restored_snapshot_ref"] == snapshot_ref
