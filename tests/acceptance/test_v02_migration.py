import sqlite3
from pathlib import Path

import pytest

from personal_health_lab.application import (
    CapacityCheck,
    CapacityStatus,
    CreateMetadataBackup,
    DataMode,
    HealthLab,
    HealthLabError,
    ImportHealthExport,
    MigrateStore,
    MigrationStatus,
    OverviewSelection,
    RuntimeConfig,
    StoreMigrationPlan,
    StoreMigrationReceipt,
    WorkspaceState,
    WriteApprovalStatus,
    WriteNotStarted,
    WriteNotStartedStatus,
)
from personal_health_lab.storage import LocalStore
from personal_health_lab.synthetic_export import generate_export


def _config(root: Path) -> RuntimeConfig:
    return RuntimeConfig(DataMode.SYNTHETIC, root, root.parent / "real")


def _set_version(config: RuntimeConfig, version: int) -> None:
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        metadata.execute(
            "UPDATE store_identity SET schema_version = ? WHERE singleton = 1",
            (version,),
        )


def test_current_store_migration_is_a_no_op_without_backup(tmp_path: Path) -> None:
    config = _config(tmp_path / "current")
    with HealthLab.open(config) as health_lab:
        request = MigrateStore()
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(plan.details, StoreMigrationPlan)
    assert plan.details.steps == ()
    assert plan.approval.status is WriteApprovalStatus.READY
    assert isinstance(receipt.result, StoreMigrationReceipt)
    assert receipt.result.status is MigrationStatus.NO_OP
    assert not (config.active_store / "migration-backups").exists()


@pytest.mark.parametrize(
    ("source_version", "expected_steps"),
    [
        (3, ((3, 4),)),
        (1, ((1, 2), (2, 3), (3, 4))),
    ],
)
def test_registered_store_migration_chain_executes_as_one_operation(
    tmp_path: Path,
    source_version: int,
    expected_steps: tuple[tuple[int, int], ...],
) -> None:
    config = _config(tmp_path / f"v{source_version}")
    with HealthLab.open(config):
        pass
    _set_version(config, source_version)

    with HealthLab.open(config) as health_lab:
        diagnostics = health_lab.load_migration_diagnostics()
        request = MigrateStore()
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert diagnostics.steps == expected_steps
    assert isinstance(plan.details, StoreMigrationPlan)
    assert plan.details.steps == expected_steps
    assert plan.details.backup_file == f"metadata-v{source_version}-to-v4.sqlite3"
    assert plan.details.affected_snapshot_refs == ()
    assert plan.details.existing_analyses_become_stale is False
    assert plan.preflight.capacity is not None
    assert isinstance(receipt.result, StoreMigrationReceipt)
    assert receipt.result.status is MigrationStatus.COMPLETED
    assert receipt.result.steps == expected_steps
    assert len(tuple((config.active_store / "migration-backups").glob("*.sqlite3"))) == 1
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT schema_version FROM store_identity WHERE singleton = 1"
        ).fetchone() == (4,)


def test_migration_required_session_only_allows_diagnosis_and_migration(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "restricted")
    with HealthLab.open(config):
        pass
    _set_version(config, 3)

    with HealthLab.open(config) as health_lab:
        status = health_lab.load_workspace_status()
        diagnostics = health_lab.load_migration_diagnostics()
        with pytest.raises(HealthLabError, match="Migration"):
            health_lab.load_overview(OverviewSelection())

    assert status.state is WorkspaceState.MIGRATION_REQUIRED
    assert status.allowed_reads == ("workspace_status", "migration_diagnostics")
    assert status.allowed_writes == ("migrate_store",)
    assert diagnostics.source_version == 3
    assert diagnostics.target_version == 4


def test_migration_required_blocks_metadata_backup_bypass(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.REAL, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config):
        pass
    _set_version(config, 3)

    with HealthLab.open(config) as health_lab:
        request = CreateMetadataBackup(tmp_path / "backup.zip")
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.diagnostics == ("migration_required",)
    assert isinstance(receipt.result, WriteNotStarted)
    assert receipt.result.status is WriteNotStartedStatus.BLOCKED
    assert not (tmp_path / "backup.zip").exists()


def test_populated_store_waits_for_copy_on_write_migration_slice(tmp_path: Path) -> None:
    fixture = generate_export("null-v1", 42, tmp_path / "fixture-populated")
    config = _config(tmp_path / "populated")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=plan.fingerprint)
    _set_version(config, 3)

    with HealthLab.open(config) as health_lab:
        request = MigrateStore()
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.diagnostics == ("populated_store_requires_cow",)
    assert isinstance(plan.details, StoreMigrationPlan)
    assert plan.details.affected_snapshot_refs == ()
    assert plan.details.existing_analyses_become_stale is False
    assert isinstance(receipt.result, WriteNotStarted)
    assert not (config.active_store / "migration-backups").exists()


def test_opening_legacy_store_does_not_recover_or_create_query_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path / "read-only-open")
    with HealthLab.open(config):
        pass
    _set_version(config, 3)
    (config.active_store / "query.duckdb").unlink()

    def unexpected_recovery(_store: LocalStore) -> None:
        raise AssertionError("legacy open attempted recovery")

    monkeypatch.setattr(LocalStore, "_recover_imports", unexpected_recovery)
    with HealthLab.open(config) as health_lab:
        assert health_lab.load_workspace_status().state is WorkspaceState.MIGRATION_REQUIRED

    assert not (config.active_store / "query.duckdb").exists()
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT schema_version FROM store_identity WHERE singleton = 1"
        ).fetchone() == (3,)


def test_migration_required_blocks_import_until_explicit_migration(tmp_path: Path) -> None:
    fixture = generate_export("null-v1", 42, tmp_path / "fixture")
    config = _config(tmp_path / "blocked-import")
    with HealthLab.open(config):
        pass
    _set_version(config, 3)

    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.diagnostics == ("migration_required",)
    assert isinstance(receipt.result, WriteNotStarted)
    assert receipt.result.status is WriteNotStartedStatus.BLOCKED
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT schema_version FROM store_identity WHERE singleton = 1"
        ).fetchone() == (3,)


def test_unknown_newer_schema_opens_for_blocked_diagnosis(tmp_path: Path) -> None:
    config = _config(tmp_path / "newer")
    with HealthLab.open(config):
        pass
    _set_version(config, 5)

    with HealthLab.open(config) as health_lab:
        diagnostics = health_lab.load_migration_diagnostics()
        plan = health_lab.preview_write(MigrateStore())

    assert diagnostics.source_version == 5
    assert diagnostics.steps == ()
    assert diagnostics.diagnostics == ("newer_schema",)
    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.diagnostics == ("newer_schema",)


@pytest.mark.parametrize("registered_steps", [((1, 3), (3, 4)), ((1, 0),)])
def test_unregistered_jump_or_downgrade_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registered_steps: tuple[tuple[int, int], ...],
) -> None:
    config = _config(tmp_path / "invalid-chain")
    with HealthLab.open(config):
        pass
    _set_version(config, 1)
    monkeypatch.setattr("personal_health_lab.migration._REGISTERED_STEPS", registered_steps)

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(MigrateStore())

    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.diagnostics == ("missing_migration_step",)


def test_migration_uses_one_backup_and_one_writer_lock(tmp_path: Path) -> None:
    config = _config(tmp_path / "busy")
    with HealthLab.open(config):
        pass
    _set_version(config, 3)

    with HealthLab.open(config) as health_lab:
        request = MigrateStore()
        plan = health_lab.preview_write(request)
        writer = LocalStore.open_writer(config.active_store, DataMode.SYNTHETIC)
        try:
            receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        finally:
            writer.close()

    assert isinstance(receipt.result, WriteNotStarted)
    assert receipt.result.status is WriteNotStartedStatus.STORE_BUSY
    assert not (config.active_store / "migration-backups").exists()

    with HealthLab.open(config) as health_lab:
        request = MigrateStore()
        plan = health_lab.preview_write(request)
        completed = health_lab.execute_write(request, expected_plan=plan.fingerprint)
    assert isinstance(completed.result, StoreMigrationReceipt)
    assert len(tuple((config.active_store / "migration-backups").glob("*.sqlite3"))) == 1


def test_failed_backup_or_capacity_preflight_prevents_migration_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path / "blocked-start")
    with HealthLab.open(config):
        pass
    _set_version(config, 3)
    blocked_capacity = CapacityCheck(
        CapacityStatus.INSUFFICIENT,
        "volume-test",
        "store-migration/v1",
        1,
        268_435_456,
        1_073_741_824,
        0,
        1_342_177_281,
        4096,
    )
    monkeypatch.setattr(LocalStore, "preflight_store_migration", lambda self: blocked_capacity)
    with HealthLab.open(config) as health_lab:
        blocked_plan = health_lab.preview_write(MigrateStore())
    assert blocked_plan.approval.status is WriteApprovalStatus.BLOCKED
    assert blocked_plan.diagnostics == ("capacity_insufficient",)
    assert not (config.active_store / "migration-backups").exists()

    monkeypatch.undo()

    def fail_backup(root: Path) -> None:
        raise sqlite3.OperationalError("fault")

    monkeypatch.setattr(
        "personal_health_lab.storage._store._migration_backup_fault_point", fail_backup
    )
    with HealthLab.open(config) as health_lab:
        request = MigrateStore()
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
    assert isinstance(receipt.result, WriteNotStarted)
    assert receipt.result.status is WriteNotStartedStatus.BLOCKED
    assert receipt.result.diagnostics == ("migration_backup_failed",)
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT schema_version FROM store_identity WHERE singleton = 1"
        ).fetchone() == (3,)
    assert not tuple((config.active_store / "migration-backups").glob("*.sqlite3"))


def test_abandoning_migration_plan_changes_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path / "abort")
    with HealthLab.open(config):
        pass
    _set_version(config, 2)

    before = (config.active_store / "metadata.sqlite3").read_bytes()
    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(MigrateStore())

    assert isinstance(plan.details, StoreMigrationPlan)
    assert plan.details.steps == ((2, 3), (3, 4))
    assert (config.active_store / "metadata.sqlite3").read_bytes() == before
    assert not (config.active_store / "migration-backups").exists()
