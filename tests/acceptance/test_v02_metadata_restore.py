import hashlib
import shutil
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import duckdb
import pytest

import personal_health_lab.health_import as health_import
import personal_health_lab.recovery as recovery_module
from personal_health_lab.application import (
    AbortMetadataRestore,
    AsNeededMedication,
    BeginMetadataRestore,
    CanonicalUnit,
    ContextCoverageStartCreate,
    ContextCoverageStartWithdraw,
    CreateMetadataBackup,
    DataCorrection,
    DataMode,
    DataReviewSelection,
    FileVaultCheck,
    FileVaultStatus,
    HealthLab,
    HealthLabError,
    ImportHealthExport,
    ImportReceipt,
    ImportStatus,
    IntakeReasonCategoryCreate,
    MedicationRegimeCreate,
    MetadataRestorePlan,
    MetadataRestoreReceipt,
    MetadataRestoreStatus,
    MigrateStore,
    OverviewSelection,
    ResolveDataReviewCase,
    ReviseContextCoverageStart,
    ReviseIntakeReasonCategory,
    ReviseMedicationRegime,
    RevokeDataReviewDecision,
    RuntimeConfig,
    SingleDecisionTarget,
    SourceConflictResolution,
    SourceConflictStrategy,
    WorkspaceState,
    WriteApprovalStatus,
    WriteDecisionReceipt,
)
from personal_health_lab.synthetic_export import generate_export


def _config(root: Path, mode: DataMode = DataMode.REAL) -> RuntimeConfig:
    return RuntimeConfig(mode, root / "synthetic", root / "real")


def _allocated_tree(root: Path) -> int:
    return sum(path.stat().st_blocks * 512 for path in root.rglob("*") if path.is_file())


def _backup(root: Path) -> tuple[RuntimeConfig, Path, bytes]:
    config = _config(root / "source")
    target = root / "metadata.sqlite3"
    with HealthLab.open(config) as health_lab:
        request = CreateMetadataBackup(target)
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        assert receipt.result.status.value == "completed"
    return config, target, target.read_bytes()


def _source_package(path: Path, records: tuple[tuple[str | None, float], ...]) -> Path:
    measured_at = datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=1)))
    body = []
    for index, (sync_id, value) in enumerate(records):
        timestamp = measured_at.replace(minute=index)
        metadata = (
            ""
            if sync_id is None
            else (f'<MetadataEntry key="HKMetadataKeySyncIdentifier" value="{sync_id}"/>')
        )
        body.append(
            '<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
            'sourceName="Restore Watch" sourceVersion="1" device="Restore Device" '
            f'unit="count/min" creationDate="{timestamp:%Y-%m-%d %H:%M:%S %z}" '
            f'startDate="{timestamp:%Y-%m-%d %H:%M:%S %z}" '
            f'endDate="{timestamp:%Y-%m-%d %H:%M:%S %z}" value="{value}">'
            f"{metadata}</Record>"
        )
    xml = (
        '<?xml version="1.0"?><HealthData>'
        '<ExportDate value="2024-03-01 00:00:00 +0000"/>'
        f"{''.join(body)}</HealthData>"
    )
    entry = ZipInfo("apple_health_export/export.xml", date_time=(1980, 1, 1, 0, 0, 0))
    entry.compress_type = ZIP_DEFLATED
    with ZipFile(path, "w") as archive:
        archive.writestr(entry, xml)
    return path


def _interval_package(path: Path) -> Path:
    xml = """<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
      <Record type="HKCategoryTypeIdentifierSleepAnalysis"
        value="HKCategoryValueSleepAnalysisAsleepCore" sourceName="Apple Watch"
        sourceVersion="1" device="Apple Watch" creationDate="2024-01-02 07:00:00 +0100"
        startDate="2024-01-02 06:00:00 +0100" endDate="2024-01-02 07:00:00 +0100"/>
      <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30"
        durationUnit="min" sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 20:31:00 +0100" startDate="2024-01-02 20:00:00 +0100"
        endDate="2024-01-02 20:30:00 +0100"/>
    </HealthData>"""
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    return path


def _execute_begin(config: RuntimeConfig, backup: Path) -> MetadataRestoreReceipt:
    with HealthLab.open(config) as health_lab:
        request = BeginMetadataRestore(backup)
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
    assert isinstance(receipt.result, MetadataRestoreReceipt)
    return receipt.result


def test_valid_backup_begins_a_read_only_restore_pending_session(tmp_path: Path) -> None:
    source, backup, original = _backup(tmp_path)
    target = _config(tmp_path / "target")
    with HealthLab.open(source) as health_lab:
        source_store_id = health_lab.load_workspace_status().store_id

    with HealthLab.open(target) as health_lab:
        request = BeginMetadataRestore(backup)
        plan = health_lab.preview_write(request)
        assert isinstance(plan.details, MetadataRestorePlan)
        assert plan.approval.status is WriteApprovalStatus.CONFIRMATION_REQUIRED
        assert plan.preflight.capacity is not None
        assert plan.preflight.capacity.method_id == "restore-start/v1"
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        assert isinstance(receipt.result, MetadataRestoreReceipt)
        assert receipt.result.status is MetadataRestoreStatus.PENDING

        workspace = health_lab.load_workspace_status()
        recovery = health_lab.load_recovery_status()
        assert workspace.state is WorkspaceState.RESTORE_PENDING
        assert workspace.store_id == source_store_id
        assert workspace.person_binding.value == "pending"
        assert workspace.allowed_reads == ("workspace_status", "recovery_status")
        assert workspace.allowed_writes == ("import_health_export", "abort_metadata_restore")
        assert recovery.backup_id == plan.details.backup_id
        assert recovery.original_backup_sha256 == hashlib.sha256(original).hexdigest()
        assert recovery.working_copy_sha256 == receipt.result.working_copy_sha256
        assert recovery.status is MetadataRestoreStatus.PENDING
        with sqlite3.connect(target.active_store / "metadata.sqlite3") as metadata:
            assert metadata.execute("SELECT operation_id FROM metadata_restores").fetchone() == (
                str(receipt.operation_id),
            )
        with pytest.raises(HealthLabError, match="Wiederherstellung"):
            health_lab.load_overview(OverviewSelection())
        with pytest.raises(HealthLabError, match="Wiederherstellung"):
            health_lab.preview_write(MigrateStore())

    before = {
        path.relative_to(target.active_store): path.read_bytes()
        for path in target.active_store.rglob("*")
        if path.is_file()
    }
    with HealthLab.open(target) as health_lab:
        assert health_lab.load_workspace_status().state is WorkspaceState.RESTORE_PENDING
    after = {
        path.relative_to(target.active_store): path.read_bytes()
        for path in target.active_store.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert backup.read_bytes() == original


def test_restore_sources_match_exactly_and_activate_overlay_once(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    original = _source_package(tmp_path / "original.zip", (("restore-a", 300), ("restore-b", 301)))
    backup = tmp_path / "metadata.sqlite3"
    with HealthLab.open(source) as health_lab:
        request = ImportHealthExport(original)
        plan = health_lab.preview_write(request)
        imported = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        assert isinstance(imported.result, ImportReceipt)
        backup_request = CreateMetadataBackup(backup)
        backup_plan = health_lab.preview_write(backup_request)
        health_lab.execute_write(backup_request, expected_plan=backup_plan.fingerprint)

    target = _config(tmp_path / "target")
    _execute_begin(target, backup)
    first_exact = _source_package(tmp_path / "first-exact.zip", (("restore-a", 300),))
    duplicate_export = tmp_path / "duplicate-export.zip"
    with ZipFile(first_exact) as source_archive, ZipFile(duplicate_export, "w") as target_archive:
        target_archive.writestr(
            "apple_health_export/export.xml",
            source_archive.read("apple_health_export/export.xml"),
        )
    candidates = (
        _source_package(tmp_path / "changed.zip", (("restore-a", 299),)),
        _source_package(tmp_path / "missing-id.zip", ((None, 300),)),
        first_exact,
        duplicate_export,
    )
    for package in candidates:
        allocated_before = _allocated_tree(target.active_store)
        with HealthLab.open(target) as health_lab:
            request = ImportHealthExport(package)
            plan = health_lab.preview_write(request)
            assert plan.preflight.capacity is not None
            assert plan.preflight.capacity.method_id == "restore-source-import/v1"
            estimate = plan.preflight.capacity.estimate_bytes
            assert estimate is not None
            receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
            assert isinstance(receipt.result, ImportReceipt)
            assert receipt.result.status is ImportStatus.RESTORE_PENDING
            assert health_lab.load_workspace_status().state is WorkspaceState.RESTORE_PENDING
        assert _allocated_tree(target.active_store) - allocated_before <= estimate
        with sqlite3.connect(target.active_store / "metadata.sqlite3") as metadata:
            assert metadata.execute("SELECT count(*) FROM active_snapshot").fetchone() == (0,)

    allocated_before = _allocated_tree(target.active_store)
    with HealthLab.open(target) as health_lab:
        request = ImportHealthExport(
            _source_package(tmp_path / "last-exact.zip", ((None, 300), ("restore-b", 301)))
        )
        plan = health_lab.preview_write(request)
        assert plan.preflight.capacity is not None
        assert plan.preflight.capacity.method_id == "restore-activate/v1"
        estimate = plan.preflight.capacity.estimate_bytes
        assert estimate is not None
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        assert isinstance(receipt.result, ImportReceipt)
        assert receipt.result.status is ImportStatus.COMMITTED
        assert receipt.result.source_occurrence_count == 2
        assert health_lab.load_workspace_status().state is WorkspaceState.READY
        assert health_lab.load_overview(OverviewSelection()).snapshot_count == 1
    assert _allocated_tree(target.active_store) - allocated_before <= estimate

    with sqlite3.connect(target.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute("SELECT count(*) FROM active_snapshot").fetchone() == (1,)
        assert metadata.execute("SELECT count(*) FROM dataset_snapshots").fetchone() == (1,)
        assert metadata.execute("SELECT count(*) FROM exports").fetchone() == (2,)
        assert metadata.execute(
            "SELECT activated_at_utc IS NOT NULL FROM metadata_restores"
        ).fetchone() == (1,)
        assert metadata.execute(
            "SELECT audit_position FROM audit_events ORDER BY audit_position"
        ).fetchall() == [(1,), (2,)]


def test_v03_restore_requires_sleep_and_workout_source_families(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    original = _interval_package(tmp_path / "intervals.zip")
    backup = tmp_path / "metadata.sqlite3"
    with HealthLab.open(source) as health_lab:
        request = ImportHealthExport(original)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        backup_request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            backup_request,
            expected_plan=health_lab.preview_write(backup_request).fingerprint,
        )

    target = _config(tmp_path / "target")
    _execute_begin(target, backup)
    with HealthLab.open(target) as health_lab:
        unrelated = ImportHealthExport(
            _source_package(tmp_path / "numeric.zip", (("unrelated", 60),))
        )
        pending = health_lab.execute_write(
            unrelated, expected_plan=health_lab.preview_write(unrelated).fingerprint
        )
        assert pending.result.status is ImportStatus.RESTORE_PENDING
        assert health_lab.load_workspace_status().state is WorkspaceState.RESTORE_PENDING

        restore = ImportHealthExport(original)
        completed = health_lab.execute_write(
            restore, expected_plan=health_lab.preview_write(restore).fingerprint
        )
        assert completed.result.status is ImportStatus.COMMITTED
        assert health_lab.load_workspace_status().state is WorkspaceState.READY
        snapshot_id = str(completed.result.snapshot_ref)
    snapshot = target.active_store / "parquet/snapshots" / snapshot_id
    with duckdb.connect() as query:
        assert query.execute(
            "SELECT count(*) FROM read_parquet(?)",
            (str(snapshot / "measurement_versions.parquet"),),
        ).fetchone() == (0,)


def test_v03_restore_preserves_manual_revisions_and_rebinds_one_new_snapshot(
    tmp_path: Path,
) -> None:
    source = _config(tmp_path / "source")
    package = _source_package(tmp_path / "source.zip", (("restore-v03", 60),))
    backup = tmp_path / "metadata.sqlite3"
    with HealthLab.open(source) as health_lab:
        imported = ImportHealthExport(package)
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        context = ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 1)))
        context_receipt = health_lab.execute_write(
            context, expected_plan=health_lab.preview_write(context).fingerprint
        )
        category = ReviseIntakeReasonCategory(IntakeReasonCategoryCreate("Schmerz"))
        category_receipt = health_lab.execute_write(
            category, expected_plan=health_lab.preview_write(category).fingerprint
        )
        regime = ReviseMedicationRegime(
            MedicationRegimeCreate(
                datetime.fromisoformat("2024-03-01T00:00:00+01:00"),
                "Europe/Berlin",
                (),
                (
                    AsNeededMedication(
                        "Ibuprofen",
                        Decimal("400"),
                        "mg",
                        (category_receipt.result.logical_id,),
                    ),
                ),
            )
        )
        regime_receipt = health_lab.execute_write(
            regime, expected_plan=health_lab.preview_write(regime).fingerprint
        )
        source_snapshot_id = str(regime_receipt.result.snapshot_ref)
        request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    with sqlite3.connect(source.active_store / "metadata.sqlite3") as metadata:
        source_binding = metadata.execute(
            "SELECT snapshot_as_of, context_timezone, context_as_of_date, medication_as_of "
            "FROM snapshot_contract_bindings WHERE snapshot_id = ?",
            (source_snapshot_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            metadata.execute("UPDATE manual_revision_intents SET intent = intent")
        with pytest.raises(sqlite3.IntegrityError):
            metadata.execute("DELETE FROM manual_revision_intents")
        with pytest.raises(sqlite3.IntegrityError):
            metadata.execute(
                "INSERT INTO manual_revision_intents VALUES (?, 'create', NULL)",
                ("f" * 32,),
            )
    with sqlite3.connect(backup) as metadata:
        backup_id = str(metadata.execute("SELECT backup_id FROM backup_manifest").fetchone()[0])
        assert metadata.execute(
            "SELECT source_snapshot_id, snapshot_as_of, context_timezone, "
            "context_as_of_date, medication_as_of FROM snapshot_origin"
        ).fetchone() == (source_snapshot_id, *source_binding)
        assert {
            tuple(map(str, row))
            for row in metadata.execute(
                "SELECT revision_kind, revision_id FROM manual_revision_bindings"
            )
        } == {
            ("context", str(context_receipt.result.revision_id)),
            ("intake_reason_category", str(category_receipt.result.revision_id)),
            ("medication_regime", str(regime_receipt.result.revision_id)),
        }

    target = _config(tmp_path / "target")
    _execute_begin(target, backup)
    with HealthLab.open(target) as health_lab:
        restore = ImportHealthExport(package)
        restored = health_lab.execute_write(
            restore, expected_plan=health_lab.preview_write(restore).fingerprint
        )
        assert isinstance(restored.result, ImportReceipt)
        restored_snapshot_id = str(restored.result.snapshot_ref)
        assert restored_snapshot_id != source_snapshot_id
        assert str(health_lab.load_context_records().coverage_start.revision_id) == str(
            context_receipt.result.revision_id
        )
        assert str(
            health_lab.load_medication_plan().intake_reason_categories[0].revision_id
        ) == str(category_receipt.result.revision_id)
        assert health_lab.load_medication_plan().regimes[0].as_needed_medications[0].unit == "mg"
        assert len(health_lab.load_context_audit(context_receipt.result.logical_id).revisions) == 1
        category_audit = health_lab.load_medication_audit(category_receipt.result.logical_id)
        assert len(category_audit.revisions) == 1

    with sqlite3.connect(target.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT backup_id, source_snapshot_id FROM snapshot_restore_origins "
            "WHERE snapshot_id = ?",
            (restored_snapshot_id,),
        ).fetchone() == (backup_id, source_snapshot_id)
        assert (
            metadata.execute(
                "SELECT snapshot_as_of, context_timezone, context_as_of_date, medication_as_of "
                "FROM snapshot_contract_bindings WHERE snapshot_id = ?",
                (restored_snapshot_id,),
            ).fetchone()
            == source_binding
        )
        assert metadata.execute(
            "SELECT audit_position FROM audit_events ORDER BY audit_position"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,)]

    second_backup = tmp_path / "restored-metadata.sqlite3"
    with HealthLab.open(target) as health_lab:
        request = CreateMetadataBackup(second_backup)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    assert _execute_begin(
        _config(tmp_path / "second-target"), second_backup
    ).status is MetadataRestoreStatus.PENDING


def test_v03_restore_accepts_a_closed_withdrawn_context_chain(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    package = _source_package(tmp_path / "source.zip", (("withdrawn-context", 60),))
    backup = tmp_path / "metadata.sqlite3"
    with HealthLab.open(source) as health_lab:
        imported = ImportHealthExport(package)
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        create = ReviseContextCoverageStart(
            ContextCoverageStartCreate(date(2024, 1, 1))
        )
        created = health_lab.execute_write(
            create, expected_plan=health_lab.preview_write(create).fingerprint
        )
        withdraw = ReviseContextCoverageStart(
            ContextCoverageStartWithdraw(
                created.result.logical_id,
                created.result.revision_id,
                "coverage corrected",
            )
        )
        health_lab.execute_write(
            withdraw, expected_plan=health_lab.preview_write(withdraw).fingerprint
        )
        request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )

    assert _execute_begin(
        _config(tmp_path / "target"), backup
    ).status is MetadataRestoreStatus.PENDING


def _assert_v03_manual_corruption_is_blocked(tmp_path: Path, corruption: str) -> None:
    source = _config(tmp_path / "source")
    package = _source_package(tmp_path / "source.zip", (("restore-v03", 60),))
    backup = tmp_path / "metadata.sqlite3"
    with HealthLab.open(source) as health_lab:
        imported = ImportHealthExport(package)
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        context = ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 1)))
        health_lab.execute_write(
            context, expected_plan=health_lab.preview_write(context).fingerprint
        )
        request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    with sqlite3.connect(backup) as metadata:
        if corruption == "binding":
            metadata.execute(
                "UPDATE manual_revision_bindings SET revision_id = ?",
                ("f" * 32,),
            )
        elif corruption == "payload":
            metadata.execute(
                "UPDATE context_coverage_start_values SET start_date = '2024-02-01'"
            )
        elif corruption == "chain":
            metadata.execute(
                "UPDATE manual_context_revisions SET previous_revision_id = ?",
                ("f" * 32,),
            )
        elif corruption == "kind":
            metadata.execute(
                "UPDATE manual_context_revisions SET object_kind = 'unknown'"
            )
        elif corruption == "kind_null":
            metadata.execute("UPDATE manual_context_revisions SET object_kind = NULL")
        elif corruption == "state_null":
            metadata.execute("UPDATE manual_context_revisions SET state = NULL")
        else:
            metadata.execute("UPDATE manual_context_revisions SET state = 'unknown'")
        canonical_hash = recovery_module._canonical_hash(metadata)
        metadata.execute(
            "UPDATE backup_manifest SET canonical_content_sha256 = ?", (canonical_hash,)
        )
        metadata.execute(
            "UPDATE backup_migration_provenance SET original_content_sha256 = ?",
            (canonical_hash,),
        )

    target = _config(tmp_path / "target")
    with HealthLab.open(target) as health_lab:
        plan = health_lab.preview_write(BeginMetadataRestore(backup))
        assert plan.approval.status is WriteApprovalStatus.BLOCKED
        assert plan.diagnostics == ("backup_integrity_conflict",)
        assert health_lab.load_workspace_status().state is WorkspaceState.READY


def test_v03_restore_rejects_broken_manual_revision_binding(tmp_path: Path) -> None:
    _assert_v03_manual_corruption_is_blocked(tmp_path, "binding")


@pytest.mark.parametrize(
    "corruption", ("payload", "chain", "kind", "kind_null", "state", "state_null")
)
def test_v03_restore_rejects_corrupt_manual_payload_or_chain(
    tmp_path: Path, corruption: str
) -> None:
    _assert_v03_manual_corruption_is_blocked(tmp_path, corruption)


def test_v2_backup_restores_legacy_measurement_versions_with_current_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_version_ids = health_import._measurement_version_ids

    def legacy_version_ids(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
        _, legacy = original_version_ids(*args, **kwargs)
        return legacy, legacy

    source = _config(tmp_path / "v2-source")
    package = _source_package(tmp_path / "v2-source.zip", (("restore-v2", 300),))
    backup = tmp_path / "v2-backup.sqlite3"
    monkeypatch.setattr(health_import, "_measurement_version_ids", legacy_version_ids)
    with HealthLab.open(source) as health_lab:
        request = ImportHealthExport(package)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        backup_request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            backup_request,
            expected_plan=health_lab.preview_write(backup_request).fingerprint,
        )
    monkeypatch.setattr(health_import, "_measurement_version_ids", original_version_ids)
    with sqlite3.connect(backup) as metadata:
        metadata.execute(
            "UPDATE backup_manifest SET identity_rule_version_id = 'healthkit-natural/v2'"
        )

    target = _config(tmp_path / "v2-target")
    _execute_begin(target, backup)
    with HealthLab.open(target) as health_lab:
        request = ImportHealthExport(package)
        receipt = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )

    assert isinstance(receipt.result, ImportReceipt)
    assert receipt.result.status is ImportStatus.COMMITTED
    assert receipt.result.measurement_version_count == 1


def test_restore_keeps_revoked_correction_tombstoned(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    package = _source_package(tmp_path / "source.zip", (("restore-a", 60),))
    backup = tmp_path / "metadata.sqlite3"
    with HealthLab.open(source) as health_lab:
        import_request = ImportHealthExport(package)
        health_lab.execute_write(
            import_request,
            expected_plan=health_lab.preview_write(import_request).fingerprint,
        )
        version_id = (
            health_lab.load_overview(OverviewSelection())
            .daily_series[0]
            .values[0]
            .measurement_version_ids[0]
        )
        correction = ResolveDataReviewCase(
            None,
            DataCorrection(
                version_id,
                61,
                CanonicalUnit.BEATS_PER_MINUTE,
                "temporary correction",
            ),
        )
        corrected = health_lab.execute_write(
            correction, expected_plan=health_lab.preview_write(correction).fingerprint
        ).result
        assert isinstance(corrected, WriteDecisionReceipt)
        revoke = RevokeDataReviewDecision(
            SingleDecisionTarget(corrected.decision_id), "correction withdrawn"
        )
        health_lab.execute_write(revoke, expected_plan=health_lab.preview_write(revoke).fingerprint)
        backup_request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            backup_request,
            expected_plan=health_lab.preview_write(backup_request).fingerprint,
        )

    target = _config(tmp_path / "target")
    _execute_begin(target, backup)
    with HealthLab.open(target) as health_lab:
        request = ImportHealthExport(package)
        receipt = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        assert isinstance(receipt.result, ImportReceipt)
        assert receipt.result.status is ImportStatus.COMMITTED
        assert health_lab.load_overview(OverviewSelection()).daily_series[0].values[0].value == 60

    with sqlite3.connect(target.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute("SELECT count(*) FROM metadata_tombstones").fetchone() == (1,)
        assert metadata.execute("SELECT count(*) FROM data_review_decisions").fetchone() == (1,)


def test_restore_keeps_superseded_conflict_resolution_tombstoned(
    tmp_path: Path, source_conflict_package: Callable[[Path], Path]
) -> None:
    source = _config(tmp_path / "source")
    package = source_conflict_package(tmp_path / "source.zip")
    backup = tmp_path / "metadata.sqlite3"
    with HealthLab.open(source) as health_lab:
        import_request = ImportHealthExport(package)
        health_lab.execute_write(
            import_request,
            expected_plan=health_lab.preview_write(import_request).fingerprint,
        )
        conflict = health_lab.load_data_review(DataReviewSelection()).cases[0]
        prefer = ResolveDataReviewCase(
            conflict.case_id,
            SourceConflictResolution(
                SourceConflictStrategy.PREFER,
                preferred_version_id=conflict.candidate_version_ids[0],
            ),
        )
        preferred = health_lab.execute_write(
            prefer, expected_plan=health_lab.preview_write(prefer).fingerprint
        ).result
        assert isinstance(preferred, WriteDecisionReceipt)
        revoke = RevokeDataReviewDecision(
            SingleDecisionTarget(preferred.decision_id), "replace preference"
        )
        health_lab.execute_write(revoke, expected_plan=health_lab.preview_write(revoke).fingerprint)
        reopened = health_lab.load_data_review(DataReviewSelection()).cases[0]
        split = ResolveDataReviewCase(
            reopened.case_id,
            SourceConflictResolution(SourceConflictStrategy.SPLIT),
        )
        split_result = health_lab.execute_write(
            split, expected_plan=health_lab.preview_write(split).fingerprint
        ).result
        assert isinstance(split_result, WriteDecisionReceipt)
        backup_request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            backup_request,
            expected_plan=health_lab.preview_write(backup_request).fingerprint,
        )
        source_overview = health_lab.load_overview(OverviewSelection())

    target = _config(tmp_path / "target")
    _execute_begin(target, backup)
    with HealthLab.open(target) as health_lab:
        request = ImportHealthExport(package)
        receipt = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        assert isinstance(receipt.result, ImportReceipt)
        assert receipt.result.status is ImportStatus.COMMITTED
        assert health_lab.load_data_review(DataReviewSelection()).cases == ()
        assert receipt.result.snapshot_ref is not None
        restored_snapshot = receipt.result.snapshot_ref
        assert health_lab.load_overview(OverviewSelection()) == source_overview
    resolved = (
        target.active_store
        / "parquet"
        / "snapshots"
        / str(restored_snapshot)
        / "resolved_measurements.parquet"
    )
    with duckdb.connect() as connection:
        rows = connection.execute(
            "SELECT conflict_resolution_decision_id FROM read_parquet(?) "
            "ORDER BY logical_measurement_id",
            [str(resolved)],
        ).fetchall()
    assert rows == [(str(split_result.decision_id),), (str(split_result.decision_id),)]
    with sqlite3.connect(target.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute("SELECT count(*) FROM metadata_tombstones").fetchone() == (1,)
        assert metadata.execute("SELECT count(*) FROM data_review_decisions").fetchone() == (2,)


def test_restore_activation_fault_rolls_back_overlay_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _config(tmp_path / "source")
    package = _source_package(tmp_path / "source.zip", (("restore-a", 60),))
    backup = tmp_path / "metadata.sqlite3"
    with HealthLab.open(source) as health_lab:
        request = ImportHealthExport(package)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        backup_request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            backup_request,
            expected_plan=health_lab.preview_write(backup_request).fingerprint,
        )

    target = _config(tmp_path / "target")
    _execute_begin(target, backup)

    def fail_before_commit(_root: Path, fault_point: str) -> None:
        if fault_point == "import.before_sqlite_commit/v1":
            raise RuntimeError("activation fault")

    monkeypatch.setattr(
        "personal_health_lab.storage._store._publication_fault_point",
        fail_before_commit,
    )
    with HealthLab.open(target) as health_lab:
        request = ImportHealthExport(package)
        with pytest.raises(RuntimeError, match="activation fault"):
            health_lab.execute_write(
                request, expected_plan=health_lab.preview_write(request).fingerprint
            )
    monkeypatch.undo()

    with HealthLab.open(target) as health_lab:
        assert health_lab.load_workspace_status().state is WorkspaceState.RESTORE_PENDING
        assert (
            health_lab.preview_write(AbortMetadataRestore()).approval.status
            is not WriteApprovalStatus.BLOCKED
        )
        request = ImportHealthExport(package)
        retried = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        assert isinstance(retried.result, ImportReceipt)
        assert retried.result.status is ImportStatus.COMMITTED
    with sqlite3.connect(target.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute("SELECT count(*) FROM active_snapshot").fetchone() == (1,)
        assert metadata.execute("SELECT count(*) FROM metadata_restores").fetchone() == (1,)


def test_restore_working_copy_tamper_blocks_source_activation(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    package = _source_package(tmp_path / "source.zip", (("restore-a", 60),))
    backup = tmp_path / "metadata.sqlite3"
    with HealthLab.open(source) as health_lab:
        request = ImportHealthExport(package)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        backup_request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            backup_request,
            expected_plan=health_lab.preview_write(backup_request).fingerprint,
        )
    target = _config(tmp_path / "target")
    recovery = _execute_begin(target, backup)
    working = target.active_store / "recovery" / str(recovery.restore_id) / "working.sqlite3"
    with sqlite3.connect(working) as metadata:
        metadata.execute("DELETE FROM required_source_refs")

    with HealthLab.open(target) as health_lab:
        plan = health_lab.preview_write(ImportHealthExport(package))
        assert plan.approval.status is WriteApprovalStatus.BLOCKED
        assert plan.diagnostics == ("health_export_unavailable",)
        assert health_lab.load_workspace_status().state is WorkspaceState.RESTORE_PENDING
    with sqlite3.connect(target.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute("SELECT count(*) FROM active_snapshot").fetchone() == (0,)


def test_restore_source_publish_fault_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _config(tmp_path / "source")
    full = _source_package(tmp_path / "full.zip", (("restore-a", 60), ("restore-b", 61)))
    partial = _source_package(tmp_path / "partial.zip", (("restore-a", 60),))
    backup = tmp_path / "metadata.sqlite3"
    with HealthLab.open(source) as health_lab:
        request = ImportHealthExport(full)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        backup_request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            backup_request,
            expected_plan=health_lab.preview_write(backup_request).fingerprint,
        )
    target = _config(tmp_path / "target")
    _execute_begin(target, backup)

    def fail_before_publish(_root: Path, fault_point: str) -> None:
        if fault_point == "restore-source.before_publish/v1":
            raise RuntimeError("source publish fault")

    monkeypatch.setattr("personal_health_lab.recovery._restore_fault_point", fail_before_publish)
    with HealthLab.open(target) as health_lab:
        request = ImportHealthExport(partial)
        with pytest.raises(RuntimeError, match="source publish fault"):
            health_lab.execute_write(
                request, expected_plan=health_lab.preview_write(request).fingerprint
            )
    monkeypatch.undo()

    with HealthLab.open(target) as health_lab:
        assert (
            health_lab.preview_write(AbortMetadataRestore()).approval.status
            is not WriteApprovalStatus.BLOCKED
        )
        request = ImportHealthExport(partial)
        retried = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        assert isinstance(retried.result, ImportReceipt)
        assert retried.result.status is ImportStatus.RESTORE_PENDING
    source_directory = next((target.active_store / "recovery").glob("*/sources"))
    assert len(tuple(source_directory.glob("*.zip"))) == 1
    assert not tuple(source_directory.glob("*.tmp"))


def test_restore_rejects_synthetic_populated_and_conflicting_targets(tmp_path: Path) -> None:
    _, backup, _ = _backup(tmp_path)
    synthetic = _config(tmp_path / "synthetic-target", DataMode.SYNTHETIC)
    with HealthLab.open(synthetic) as health_lab:
        plan = health_lab.preview_write(BeginMetadataRestore(backup))
    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.diagnostics == ("real_store_required",)

    populated = _config(tmp_path / "populated")
    export = generate_export("null-v1", 53, tmp_path.parent / f"{tmp_path.name}-populated-export")
    with HealthLab.open(populated) as health_lab:
        import_request = ImportHealthExport(export.export_path)
        import_plan = health_lab.preview_write(import_request)
        health_lab.execute_write(import_request, expected_plan=import_plan.fingerprint)
        plan = health_lab.preview_write(BeginMetadataRestore(backup))
    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.diagnostics == ("restore_store_not_empty",)

    unexpected = _config(tmp_path / "unexpected")
    with HealthLab.open(unexpected) as health_lab:
        nested = unexpected.active_store / "parquet" / "keep.txt"
        nested.write_text("keep", encoding="utf-8")
        plan = health_lab.preview_write(BeginMetadataRestore(backup))
    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.diagnostics == ("restore_store_not_empty",)
    assert nested.read_text(encoding="utf-8") == "keep"

    pending = _config(tmp_path / "pending")
    _execute_begin(pending, backup)
    _, other_backup, _ = _backup(tmp_path / "other-source")
    with HealthLab.open(pending) as health_lab:
        plan = health_lab.preview_write(BeginMetadataRestore(other_backup))
    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.diagnostics == ("restore_backup_conflict",)


def test_supported_backup_schema_is_migrated_only_in_staging(tmp_path: Path) -> None:
    _, backup, original = _backup(tmp_path)
    with sqlite3.connect(backup) as metadata:
        metadata.execute("UPDATE backup_manifest SET backup_schema_version = 1 WHERE singleton = 1")
        metadata.execute("DROP TABLE backup_migration_provenance")
        metadata.execute("ALTER TABLE backup_manifest DROP COLUMN table_row_counts")
        metadata.execute("ALTER TABLE backup_manifest DROP COLUMN source_snapshot_id")
        for table in sorted(
            set(recovery_module._CONTENT_TABLES) - set(recovery_module._LEGACY_CONTENT_TABLES)
        ):
            metadata.execute(f"DROP TABLE {table}")
        canonical_hash = recovery_module._canonical_hash(
            metadata, recovery_module._LEGACY_CONTENT_TABLES
        )
        metadata.execute(
            "UPDATE backup_manifest SET canonical_content_sha256 = ? WHERE singleton = 1",
            (canonical_hash,),
        )
    legacy = backup.read_bytes()
    target = _config(tmp_path / "target")

    with HealthLab.open(target) as health_lab:
        request = BeginMetadataRestore(backup)
        plan = health_lab.preview_write(request)
        assert isinstance(plan.details, MetadataRestorePlan)
        assert plan.details.migration_steps == ((1, 2), (2, 3), (3, 4))
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        recovery = health_lab.load_recovery_status()

    assert isinstance(receipt.result, MetadataRestoreReceipt)
    assert receipt.result.status is MetadataRestoreStatus.PENDING
    assert backup.read_bytes() == legacy != original
    assert recovery.original_backup_sha256 == hashlib.sha256(legacy).hexdigest()
    assert recovery.working_copy_sha256 != recovery.original_backup_sha256
    assert recovery.source_schema_version == 1
    assert recovery.target_schema_version == 4


def test_unknown_or_unregistered_backup_schema_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, backup, _ = _backup(tmp_path)
    target = _config(tmp_path / "target")
    with sqlite3.connect(backup) as metadata:
        metadata.execute("UPDATE backup_manifest SET backup_schema_version = 5 WHERE singleton = 1")
    with HealthLab.open(target) as health_lab:
        newer = health_lab.preview_write(BeginMetadataRestore(backup))
    assert newer.approval.status is WriteApprovalStatus.BLOCKED
    assert newer.diagnostics == ("backup_schema_newer",)

    with sqlite3.connect(backup) as metadata:
        metadata.execute("UPDATE backup_manifest SET backup_schema_version = 1 WHERE singleton = 1")
        metadata.execute("DROP TABLE backup_migration_provenance")
    monkeypatch.setattr("personal_health_lab.migration._REGISTERED_BACKUP_STEPS", ())
    with HealthLab.open(target) as health_lab:
        missing = health_lab.preview_write(BeginMetadataRestore(backup))
    assert missing.approval.status is WriteApprovalStatus.BLOCKED
    assert missing.diagnostics == ("backup_migration_missing",)


def test_abort_discards_pending_store_and_activated_backup_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, backup, _ = _backup(tmp_path)
    activated = _config(tmp_path / "activated")
    _execute_begin(activated, backup)
    with sqlite3.connect(activated.active_store / "metadata.sqlite3") as metadata:
        metadata.execute(
            "UPDATE metadata_restores SET activated_at_utc = '2026-07-19T00:00:00+00:00'"
        )
        metadata.execute("UPDATE store_identity SET person_binding = 'bound' WHERE singleton = 1")
    with HealthLab.open(activated) as health_lab:
        request = BeginMetadataRestore(backup)
        same_plan = health_lab.preview_write(request)
        same = health_lab.execute_write(request, expected_plan=same_plan.fingerprint)
    assert isinstance(same.result, MetadataRestoreReceipt)
    assert same.result.status is MetadataRestoreStatus.NO_OP
    assert activated.active_store.exists()

    target = _config(tmp_path / "target")
    _execute_begin(target, backup)
    actual_rmtree = shutil.rmtree

    def remove_detached(path: Path) -> None:
        assert path != target.active_store
        assert not target.active_store.exists()
        actual_rmtree(path)

    monkeypatch.setattr(
        "personal_health_lab.application._application.shutil.rmtree", remove_detached
    )

    with HealthLab.open(target) as health_lab:
        abort = AbortMetadataRestore()
        abort_plan = health_lab.preview_write(abort)
        aborted = health_lab.execute_write(abort, expected_plan=abort_plan.fingerprint)
        assert isinstance(aborted.result, MetadataRestoreReceipt)
        assert aborted.result.status is MetadataRestoreStatus.ABORTED
        with pytest.raises(HealthLabError, match="Context Manager"):
            health_lab.load_workspace_status()

    assert not target.active_store.exists()

    fault_target = _config(tmp_path / "fault-target")
    _execute_begin(fault_target, backup)

    def fail_abort(_root: Path, fault_point_id: str) -> None:
        if fault_point_id == "restore.after_abort_detach/v1":
            raise RuntimeError("fault after abort detach")

    monkeypatch.setattr("personal_health_lab.recovery._restore_fault_point", fail_abort)
    with HealthLab.open(fault_target) as health_lab:
        abort = AbortMetadataRestore()
        abort_plan = health_lab.preview_write(abort)
        with pytest.raises(RuntimeError, match="fault after abort detach"):
            health_lab.execute_write(abort, expected_plan=abort_plan.fingerprint)
    assert not fault_target.active_store.exists()
    assert tuple(fault_target.active_store.parent.glob(".healthlab-quarantine-restore-*"))


@pytest.mark.parametrize(
    "fault_point_id",
    (
        "restore.after_working_copy/v1",
        "restore.after_working_copy_publish/v1",
        "restore.after_pending_catalog/v1",
    ),
)
def test_restore_faults_leave_empty_or_complete_pending_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_point_id: str
) -> None:
    _, backup, _ = _backup(tmp_path)
    target = _config(tmp_path / "target")

    def fail(_root: Path, current: str) -> None:
        if current == fault_point_id:
            raise RuntimeError(f"fault at {current}")

    monkeypatch.setattr("personal_health_lab.recovery._restore_fault_point", fail)
    with HealthLab.open(target) as health_lab:
        request = BeginMetadataRestore(backup)
        plan = health_lab.preview_write(request)
        with pytest.raises(RuntimeError, match="fault at"):
            health_lab.execute_write(request, expected_plan=plan.fingerprint)

    monkeypatch.setattr(
        "personal_health_lab.recovery._restore_fault_point",
        lambda _root, _fault_point_id: None,
    )
    with HealthLab.open(target) as health_lab:
        if fault_point_id == "restore.after_pending_catalog/v1":
            assert health_lab.load_workspace_status().state is WorkspaceState.RESTORE_PENDING
        else:
            request = BeginMetadataRestore(backup)
            plan = health_lab.preview_write(request)
            retry = health_lab.execute_write(request, expected_plan=plan.fingerprint)
            assert isinstance(retry.result, MetadataRestoreReceipt)
            assert retry.result.status is MetadataRestoreStatus.PENDING


def test_metadata_restore_filevault_improvement_may_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, backup, _ = _backup(tmp_path)
    probes = 0

    def probe(path: Path) -> FileVaultCheck:
        nonlocal probes
        probes += 1
        return FileVaultCheck(
            FileVaultStatus.UNPROTECTED if probes == 1 else FileVaultStatus.PROTECTED,
            "volume-restore",
        )

    monkeypatch.setattr("personal_health_lab.application._application.probe_filevault", probe)
    target = _config(tmp_path / "target")
    with HealthLab.open(target) as health_lab:
        request = BeginMetadataRestore(backup)
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(receipt.result, MetadataRestoreReceipt)
    assert receipt.result.status is MetadataRestoreStatus.PENDING
    assert receipt.final_preflight.filevault is not None
    assert receipt.final_preflight.filevault.status is FileVaultStatus.PROTECTED


@pytest.mark.parametrize("copies", (1, 8), ids=("normal", "stress"))
def test_restore_start_v1_bounds_normal_and_stress_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, copies: int
) -> None:
    _, backup, _ = _backup(tmp_path)
    if copies > 1:
        with sqlite3.connect(backup) as metadata:
            metadata.execute("CREATE TABLE padding (value BLOB) STRICT")
            metadata.execute("INSERT INTO padding VALUES (zeroblob(?))", (4 * 1024**2,))
            metadata.execute("DROP TABLE padding")
    target = _config(tmp_path / f"target-{copies}")
    phases: dict[str, int] = {}
    with HealthLab.open(target) as health_lab:
        request = BeginMetadataRestore(backup)
        plan = health_lab.preview_write(request)
        baseline = _allocated_tree(target.active_store)

        def measure(root: Path, phase: str) -> None:
            assert root == target.active_store
            phases[phase] = max(0, _allocated_tree(root) - baseline)

        monkeypatch.setattr("personal_health_lab.recovery._restore_allocation_checkpoint", measure)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(receipt.result, MetadataRestoreReceipt)
    assert receipt.result.status is MetadataRestoreStatus.PENDING
    assert set(phases) == {
        "working_copy",
        "migrated_copy",
        "published_copy",
        "pending_catalog",
    }
    assert plan.preflight.capacity is not None
    assert plan.preflight.capacity.method_id == "restore-start/v1"
    assert plan.preflight.capacity.estimate_bytes is not None
    assert plan.preflight.capacity.fragment_size is not None
    assert plan.preflight.capacity.estimate_bytes % plan.preflight.capacity.fragment_size == 0
    assert max(phases.values()) <= plan.preflight.capacity.estimate_bytes


@pytest.mark.parametrize("padding_bytes", (0, 4 * 1024**2), ids=("normal", "stress"))
def test_restore_activate_v1_bounds_snapshot_and_overlay_allocation(
    tmp_path: Path, padding_bytes: int
) -> None:
    source = _config(tmp_path / "source")
    package = _source_package(tmp_path / "source.zip", (("restore-a", 60),))
    backup = tmp_path / "metadata.sqlite3"
    with HealthLab.open(source) as health_lab:
        request = ImportHealthExport(package)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        backup_request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            backup_request,
            expected_plan=health_lab.preview_write(backup_request).fingerprint,
        )
    if padding_bytes:
        with sqlite3.connect(backup) as metadata:
            metadata.execute("CREATE TABLE padding (value BLOB) STRICT")
            metadata.execute("INSERT INTO padding VALUES (zeroblob(?))", (padding_bytes,))
            metadata.execute("DROP TABLE padding")

    target = _config(tmp_path / f"target-{padding_bytes}")
    recovery = _execute_begin(target, backup)
    working = target.active_store / "recovery" / str(recovery.restore_id) / "working.sqlite3"
    working_allocation = working.stat().st_blocks * 512
    allocated_before = _allocated_tree(target.active_store)
    with HealthLab.open(target) as health_lab:
        request = ImportHealthExport(package)
        plan = health_lab.preview_write(request)
        assert plan.preflight.capacity is not None
        assert plan.preflight.capacity.method_id == "restore-activate/v1"
        estimate = plan.preflight.capacity.estimate_bytes
        assert estimate is not None
        assert estimate >= 2 * working_allocation
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        assert isinstance(receipt.result, ImportReceipt)
        assert receipt.result.status is ImportStatus.COMMITTED
    assert _allocated_tree(target.active_store) - allocated_before <= estimate
