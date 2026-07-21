import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from personal_health_lab.application import (
    AbortMetadataRestore,
    BeginMetadataRestore,
    CreateMetadataBackup,
    DataMode,
    FileVaultCheck,
    FileVaultStatus,
    HealthLab,
    HealthLabError,
    ImportHealthExport,
    MetadataRestorePlan,
    MetadataRestoreReceipt,
    MetadataRestoreStatus,
    MigrateStore,
    OverviewSelection,
    RuntimeConfig,
    WorkspaceState,
    WriteApprovalStatus,
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
            assert metadata.execute(
                "SELECT operation_id FROM metadata_restores"
            ).fetchone() == (str(receipt.operation_id),)
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
    legacy = backup.read_bytes()
    target = _config(tmp_path / "target")

    with HealthLab.open(target) as health_lab:
        request = BeginMetadataRestore(backup)
        plan = health_lab.preview_write(request)
        assert isinstance(plan.details, MetadataRestorePlan)
        assert plan.details.migration_steps == ((1, 2),)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        recovery = health_lab.load_recovery_status()

    assert isinstance(receipt.result, MetadataRestoreReceipt)
    assert receipt.result.status is MetadataRestoreStatus.PENDING
    assert backup.read_bytes() == legacy != original
    assert recovery.original_backup_sha256 == hashlib.sha256(legacy).hexdigest()
    assert recovery.working_copy_sha256 != recovery.original_backup_sha256
    assert recovery.source_schema_version == 1
    assert recovery.target_schema_version == 2


def test_unknown_or_unregistered_backup_schema_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, backup, _ = _backup(tmp_path)
    target = _config(tmp_path / "target")
    with sqlite3.connect(backup) as metadata:
        metadata.execute("UPDATE backup_manifest SET backup_schema_version = 3 WHERE singleton = 1")
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
        metadata.execute(
            "UPDATE store_identity SET person_binding = 'bound' WHERE singleton = 1"
        )
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

    monkeypatch.setattr(
        "personal_health_lab.recovery._restore_fault_point", fail_abort
    )
    with HealthLab.open(fault_target) as health_lab:
        abort = AbortMetadataRestore()
        abort_plan = health_lab.preview_write(abort)
        with pytest.raises(RuntimeError, match="fault after abort detach"):
            health_lab.execute_write(abort, expected_plan=abort_plan.fingerprint)
    assert not fault_target.active_store.exists()
    assert tuple(
        fault_target.active_store.parent.glob(".healthlab-quarantine-restore-*")
    )


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

    monkeypatch.setattr(
        "personal_health_lab.application._application.probe_filevault", probe
    )
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
