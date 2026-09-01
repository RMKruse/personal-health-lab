from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from personal_health_lab import DataMode
from personal_health_lab.application import (
    CapacityCheck,
    CapacityStatus,
    ConfirmDataReviewBatch,
    CreateMetadataBackup,
    DataReviewCaseKind,
    DataReviewSelection,
    FileVaultCheck,
    FileVaultStatus,
    HealthLab,
    ImportHealthExport,
    ImportReceipt,
    MetadataBackupReceipt,
    RuntimeConfig,
    WriteApprovalStatus,
    WriteNotStarted,
    WriteNotStartedStatus,
)


def _config(tmp_path: Path, mode: DataMode) -> RuntimeConfig:
    return RuntimeConfig(
        mode=mode,
        synthetic_store=tmp_path / "synthetic",
        real_store=tmp_path / "real",
    )


def _health_package(path: Path, record_count: int) -> Path:
    records = []
    start = datetime(2024, 1, 1, tzinfo=UTC)
    for index in range(record_count):
        measured_at = start + timedelta(minutes=index)
        records.append(
            f'<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
            f'sourceName="Test Watch" sourceVersion="1" device="Test Device" '
            f'unit="count/min" creationDate="{measured_at:%Y-%m-%d %H:%M:%S} +0000" '
            f'startDate="{measured_at:%Y-%m-%d %H:%M:%S} +0000" '
            f'endDate="{measured_at:%Y-%m-%d %H:%M:%S} +0000" value="300">'
            f'<MetadataEntry key="HKMetadataKeySyncIdentifier" value="backup-{index}"/>'
            "</Record>"
        )
    xml = (
        '<?xml version="1.0"?><HealthData>'
        '<ExportDate value="2024-03-01 00:00:00 +0000"/>'
        f"{''.join(records)}</HealthData>"
    )
    entry = ZipInfo("apple_health_export/export.xml", date_time=(1980, 1, 1, 0, 0, 0))
    entry.compress_type = ZIP_DEFLATED
    with ZipFile(path, "w") as archive:
        archive.writestr(entry, xml)
    return path


def _allocated_tree(root: Path) -> int:
    return sum(path.stat().st_blocks * 512 for path in (root, *root.rglob("*")) if path.exists())


def test_metadata_backup_is_one_redacted_portable_sqlite_file(tmp_path: Path) -> None:
    target = tmp_path / "outside" / "metadata.sqlite3"
    target.parent.mkdir()
    request = CreateMetadataBackup(target)

    with HealthLab.open(_config(tmp_path, DataMode.REAL)) as health_lab:
        plan = health_lab.preview_write(request)
        assert plan.approval.status is not WriteApprovalStatus.BLOCKED
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(receipt.result, MetadataBackupReceipt)
    assert receipt.result.status.value == "completed"
    assert receipt.result.target_file == "metadata.sqlite3"
    assert tuple(target.parent.iterdir()) == (target,)
    with sqlite3.connect(target) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        tables = {
            str(row[0])
            for row in backup.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "backup_manifest" in tables
        assert {
            "activity_derivation_versions",
            "audit_events",
            "as_needed_intake_publications",
            "as_needed_intake_revisions",
            "as_needed_intake_values",
            "context_coverage_start_values",
            "custom_context_label_values",
            "custom_context_period_values",
            "data_review_batch_actions",
            "data_review_batch_members",
            "data_review_decisions",
            "daily_stress_values",
            "historical_review_cycles",
            "illness_category_values",
            "illness_period_values",
            "import_refs",
            "intake_reason_category_publications",
            "intake_reason_category_revisions",
            "intake_reason_category_values",
            "manual_context_publications",
            "manual_context_revisions",
            "manual_revision_intents",
            "manual_revision_bindings",
            "medication_as_needed_entries",
            "medication_deviation_intakes",
            "medication_deviation_publications",
            "medication_deviation_revisions",
            "medication_deviation_values",
            "medication_publications",
            "medication_regime_revisions",
            "medication_regime_values",
            "medication_scheduled_doses",
            "metadata_tombstones",
            "nutrition_day_confirmation_publications",
            "nutrition_day_confirmation_snapshot_bindings",
            "nutrition_day_confirmations",
            "plausibility_rule_versions",
            "review_case_reasons",
            "review_cycle_cases",
            "review_case_facts",
            "review_cycles",
            "rule_version_refs",
            "snapshot_refs",
            "snapshot_origin",
            "write_operations",
        } <= tables
        assert not tables.intersection(
            {
                "active_snapshot",
                "analysis_receipts",
                "analysis_runs",
                "dataset_snapshots",
                "imports",
                "snapshot_activations",
            }
        )
        manifest = backup.execute(
            "SELECT canonical_content_sha256, audit_max_position, table_row_counts "
            "FROM backup_manifest"
        ).fetchone()
        assert manifest is not None
        assert json.loads(str(manifest[2])) == {
            table: backup.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in tables - {"backup_manifest", "backup_migration_provenance"}
        }
    assert manifest[:2] == (receipt.result.canonical_content_sha256, 0)
    assert receipt.final_preflight.capacity is not None
    assert receipt.final_preflight.capacity.method_id == "metadata-backup/v1"
    assert receipt.final_preflight.capacity.estimate_bytes is not None
    assert target.stat().st_blocks * 512 <= receipt.final_preflight.capacity.estimate_bytes
    assert str(tmp_path) not in repr(receipt)


def test_metadata_backup_is_real_only(tmp_path: Path) -> None:
    request = CreateMetadataBackup(tmp_path / "metadata.sqlite3")
    with HealthLab.open(_config(tmp_path, DataMode.SYNTHETIC)) as health_lab:
        plan = health_lab.preview_write(request)
    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.diagnostics == ("real_store_required",)


def test_metadata_backup_hash_survives_vacuum_and_same_target_is_no_op(
    tmp_path: Path,
) -> None:
    target = tmp_path / "backup.sqlite3"
    request = CreateMetadataBackup(target)
    config = _config(tmp_path, DataMode.REAL)
    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        first = health_lab.execute_write(request, expected_plan=plan.fingerprint)
    assert isinstance(first.result, MetadataBackupReceipt)
    with sqlite3.connect(target) as backup:
        before = backup.execute("SELECT canonical_content_sha256 FROM backup_manifest").fetchone()[
            0
        ]
        backup.execute("VACUUM")
        after = backup.execute("SELECT canonical_content_sha256 FROM backup_manifest").fetchone()[0]
    assert before == after == first.result.canonical_content_sha256

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        second = health_lab.execute_write(request, expected_plan=plan.fingerprint)
    assert isinstance(second.result, MetadataBackupReceipt)
    assert second.result.status.value == "no_op"
    assert second.result.backup_id == first.result.backup_id


def test_metadata_backup_conflict_blocks_without_overwriting(tmp_path: Path) -> None:
    target = tmp_path / "backup.sqlite3"
    config = _config(tmp_path, DataMode.REAL)
    request = CreateMetadataBackup(target)
    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=plan.fingerprint)
    original = target.read_bytes()
    with sqlite3.connect(target) as backup:
        backup.execute(
            "UPDATE rule_version_refs SET rule_kind = 'identity' "
            "WHERE rule_version_id = 'healthkit-canonical/v3'"
        )
    conflicted = target.read_bytes()

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
    assert isinstance(receipt.result, WriteNotStarted)
    assert receipt.result.status.value == "blocked"
    assert target.read_bytes() == conflicted != original
    assert tuple(tmp_path.glob(".*.tmp")) == ()


def test_metadata_backup_copy_gets_a_distinct_target_identity(tmp_path: Path) -> None:
    first = tmp_path / "first.sqlite3"
    copied = tmp_path / "copied.sqlite3"
    config = _config(tmp_path, DataMode.REAL)
    with HealthLab.open(config) as health_lab:
        request = CreateMetadataBackup(first)
        plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=plan.fingerprint)
    shutil.copyfile(first, copied)
    with HealthLab.open(config) as health_lab:
        request = CreateMetadataBackup(copied)
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
    assert isinstance(receipt.result, WriteNotStarted)
    assert receipt.result.status.value == "blocked"


def test_metadata_backup_closes_import_and_historical_review_references(
    tmp_path: Path,
) -> None:
    target = tmp_path / "metadata.sqlite3"
    config = _config(tmp_path, DataMode.REAL)
    with HealthLab.open(config) as health_lab:
        package = _health_package(tmp_path / "review.zip", 3)
        import_request = ImportHealthExport(package)
        import_plan = health_lab.preview_write(import_request)
        imported = health_lab.execute_write(import_request, expected_plan=import_plan.fingerprint)
        assert isinstance(imported.result, ImportReceipt)

        decision_request = ConfirmDataReviewBatch(
            DataReviewSelection(DataReviewCaseKind.PLAUSIBILITY), "historisch geprüft"
        )
        decision_plan = health_lab.preview_write(decision_request)
        health_lab.execute_write(decision_request, expected_plan=decision_plan.fingerprint)

        backup_request = CreateMetadataBackup(target)
        backup_plan = health_lab.preview_write(backup_request)
        backed_up = health_lab.execute_write(backup_request, expected_plan=backup_plan.fingerprint)

    assert isinstance(backed_up.result, MetadataBackupReceipt)
    with sqlite3.connect(target) as backup:
        assert (
            backup.execute("SELECT count(*) FROM import_publications").fetchone()
            == backup.execute("SELECT count(*) FROM import_refs").fetchone()
        )
        assert (
            backup.execute(
                "SELECT count(*) FROM review_cycle_cases cycle_case "
                "JOIN review_cycles cycle USING (cycle_id) "
                "JOIN review_case_facts fact ON fact.snapshot_id = cycle.snapshot_id "
                "AND fact.review_case_id = cycle_case.review_case_id"
            ).fetchone()
            == backup.execute("SELECT count(*) FROM review_cycle_cases").fetchone()
        )


def test_metadata_backup_capacity_is_rechecked_under_the_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = 0

    def probe(target_path: Path, estimate: int | None) -> CapacityCheck:
        nonlocal checks
        checks += 1
        ready = checks < 3
        return CapacityCheck(
            CapacityStatus.READY if ready else CapacityStatus.INSUFFICIENT,
            "volume-backup",
            "metadata-backup/v1",
            2 * 1024**2,
            256 * 1024**2,
            1024**3,
            2 * 1024**3 if ready else 1,
            1024**3 + 258 * 1024**2,
            4096,
        )

    monkeypatch.setattr("personal_health_lab.recovery._probe_metadata_backup_capacity", probe)
    target = tmp_path / "backup.sqlite3"
    with HealthLab.open(_config(tmp_path, DataMode.REAL)) as health_lab:
        request = CreateMetadataBackup(target)
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert plan.preflight.capacity is not None
    assert plan.preflight.capacity.method_id == "metadata-backup/v1"
    assert plan.preflight.capacity.target_volume == "volume-backup"
    assert isinstance(receipt.result, WriteNotStarted)
    assert receipt.result.status is WriteNotStartedStatus.BLOCKED
    assert receipt.final_preflight.capacity is not None
    assert receipt.final_preflight.capacity.status is CapacityStatus.INSUFFICIENT
    assert not target.exists()


@pytest.mark.parametrize("record_count", (8, 512), ids=("normal", "stress"))
def test_metadata_backup_v1_measures_populated_writer_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_count: int
) -> None:
    target_directory = tmp_path / "backup-target"
    target_directory.mkdir()
    target = target_directory / "metadata.sqlite3"
    config = _config(tmp_path, DataMode.REAL)
    phases: dict[str, int] = {}

    with HealthLab.open(config) as health_lab:
        package = _health_package(tmp_path / "health.zip", record_count)
        import_request = ImportHealthExport(package)
        import_plan = health_lab.preview_write(import_request)
        imported = health_lab.execute_write(import_request, expected_plan=import_plan.fingerprint)
        assert isinstance(imported.result, ImportReceipt)

        request = CreateMetadataBackup(target)
        plan = health_lab.preview_write(request)
        baseline = _allocated_tree(target_directory)

        def measure(root: Path, phase: str) -> None:
            assert root == target_directory
            phases[phase] = max(0, _allocated_tree(root) - baseline)

        monkeypatch.setattr("personal_health_lab.recovery._allocation_checkpoint", measure)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(receipt.result, MetadataBackupReceipt)
    assert set(phases) == {"temporary", "published"}
    assert plan.preflight.capacity is not None
    assert plan.preflight.capacity.fragment_size is not None
    assert plan.preflight.capacity.estimate_bytes is not None
    assert plan.preflight.capacity.estimate_bytes % plan.preflight.capacity.fragment_size == 0
    assert max(phases.values()) <= plan.preflight.capacity.estimate_bytes
    with sqlite3.connect(target) as backup:
        assert backup.execute("SELECT count(*) FROM review_case_facts").fetchone() == (
            record_count,
        )
        assert backup.execute("SELECT count(*) FROM review_case_reasons").fetchone() == (
            record_count,
        )


def test_metadata_backup_filevault_improvement_may_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = 0

    def probe(path: Path) -> FileVaultCheck:
        nonlocal probes
        probes += 1
        return FileVaultCheck(
            FileVaultStatus.UNPROTECTED if probes == 1 else FileVaultStatus.PROTECTED,
            "volume-backup",
        )

    monkeypatch.setattr("personal_health_lab.application._application.probe_filevault", probe)
    target = tmp_path / "backup.sqlite3"
    with HealthLab.open(_config(tmp_path, DataMode.REAL)) as health_lab:
        request = CreateMetadataBackup(target)
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(receipt.result, MetadataBackupReceipt)
    assert receipt.final_preflight.filevault is not None
    assert receipt.final_preflight.filevault.status is FileVaultStatus.PROTECTED
