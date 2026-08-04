import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from personal_health_lab.application import (
    AnalysisDefinitionId,
    AnalysisFreshness,
    AnalysisReceipt,
    AnalysisStatus,
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
    RollbackMigration,
    RollbackMigrationPlan,
    RollbackMigrationReceipt,
    RunRestingHeartRateAnalysis,
    RuntimeConfig,
    StoreMigrationPlan,
    StoreMigrationReceipt,
    WorkspaceState,
    WriteApprovalStatus,
    WriteNotStarted,
    WriteNotStartedStatus,
)
from personal_health_lab.storage import LocalStore, StoreError, cow_migration_estimate
from personal_health_lab.synthetic_export import generate_export


def _config(root: Path) -> RuntimeConfig:
    return RuntimeConfig(DataMode.SYNTHETIC, root, root.parent / "real")


def _set_version(config: RuntimeConfig, version: int) -> None:
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        metadata.execute(
            "UPDATE store_identity SET schema_version = ? WHERE singleton = 1",
            (version,),
        )


def _set_legacy_migration_constraints(config: RuntimeConfig) -> None:
    database = config.active_store / "metadata.sqlite3"
    with sqlite3.connect(database) as metadata:
        schema_version = int(metadata.execute("PRAGMA schema_version").fetchone()[0])
        metadata.execute("PRAGMA writable_schema = ON")
        metadata.execute(
            "UPDATE sqlite_schema SET sql = replace(sql, ?, ?) WHERE name = ?",
            (
                "'run_historical_review', 'migrate_store',\n"
                "                                 'rollback_migration')",
                "'run_historical_review')",
                "write_operations",
            ),
        )
        metadata.execute(
            "UPDATE sqlite_schema SET sql = replace(sql, ?, ?) WHERE name = ?",
            (
                "'historical',\n                    'migration'",
                "'historical'",
                "snapshot_activations",
            ),
        )
        metadata.execute(
            "UPDATE sqlite_schema SET sql = replace(sql, ?, ?) WHERE name = ?",
            (
                "'metadata_tombstone',\n                    'store_migrated'",
                "'metadata_tombstone'",
                "audit_events",
            ),
        )
        metadata.execute("PRAGMA writable_schema = OFF")
        metadata.execute(f"PRAGMA schema_version = {schema_version + 1}")


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
        (3, ((3, 4), (4, 5), (5, 6), (6, 7), (7, 8))),
        (1, ((1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8))),
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
    assert plan.details.backup_file == f"metadata-v{source_version}-to-v8.sqlite3"
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
        ).fetchone() == (8,)


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
    assert diagnostics.target_version == 8


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


def test_populated_store_is_migrated_copy_on_write(tmp_path: Path) -> None:
    fixture = generate_export("null-v1", 42, tmp_path / "fixture-populated")
    config = _config(tmp_path / "populated")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        plan = health_lab.preview_write(request)
        imported = health_lab.execute_write(request, expected_plan=plan.fingerprint)
    old_snapshot = imported.result.snapshot_ref
    assert old_snapshot is not None
    old_directory = config.active_store / "parquet" / "snapshots" / str(old_snapshot)
    old_files = {
        path.name: path.read_bytes() for path in old_directory.iterdir() if path.is_file()
    }
    _set_version(config, 3)
    _set_legacy_migration_constraints(config)

    with HealthLab.open(config) as health_lab:
        request = MigrateStore()
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert plan.approval.status is WriteApprovalStatus.CONFIRMATION_REQUIRED
    assert plan.diagnostics == ()
    assert isinstance(plan.details, StoreMigrationPlan)
    assert plan.details.affected_snapshot_refs == (old_snapshot,)
    assert plan.details.existing_analyses_become_stale is True
    assert plan.preflight.capacity is not None
    assert plan.preflight.capacity.method_id == "cow-migration/v1"
    assert isinstance(receipt.result, StoreMigrationReceipt)
    assert receipt.result.status is MigrationStatus.COMPLETED
    snapshot_ids = {
        path.name for path in (config.active_store / "parquet" / "snapshots").iterdir()
    }
    snapshot_ids.remove(str(old_snapshot))
    new_snapshot = snapshot_ids.pop()
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone() == (new_snapshot,)
        assert metadata.execute(
            "SELECT request_kind FROM write_operations WHERE operation_id = ?",
            (str(receipt.result.operation_id),),
        ).fetchone() == ("migrate_store",)
        assert metadata.execute(
            "SELECT activation_kind FROM snapshot_activations WHERE operation_id = ?",
            (str(receipt.result.operation_id),),
        ).fetchone() == ("migration",)
        assert metadata.execute(
            "SELECT event_kind FROM audit_events WHERE operation_id = ?",
            (str(receipt.result.operation_id),),
        ).fetchone() == ("store_migrated",)
        assert metadata.execute(
            "SELECT snapshot_id FROM migration_publications "
            "JOIN audit_events USING (audit_event_id) WHERE operation_id = ?",
            (str(receipt.result.operation_id),),
        ).fetchone() == (new_snapshot,)
    assert {path.name: path.read_bytes() for path in old_directory.iterdir() if path.is_file()} == (
        old_files
    )
    new_manifest = (
        config.active_store
        / "parquet"
        / "snapshots"
        / new_snapshot
        / "manifest.json"
    ).read_bytes()
    assert new_manifest != old_files["manifest.json"]
    assert json.loads(new_manifest)["created_by_operation_id"] == str(
        receipt.result.operation_id
    )


@pytest.mark.parametrize(
    "fault_point",
    (
        "migration.after_backup/v1",
        "migration.before_snapshot_move/v1",
        "migration.after_snapshot_move/v1",
        "migration.before_sqlite_commit/v1",
    ),
)
def test_migration_fault_keeps_old_snapshot_and_retry_starts_fresh(
    fault_point: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = generate_export("null-v1", 42, tmp_path / "fixture-fault")
    config = _config(tmp_path / "fault")
    with HealthLab.open(config) as health_lab:
        imported = health_lab.execute_write(
            ImportHealthExport(fixture.export_path),
            expected_plan=health_lab.preview_write(
                ImportHealthExport(fixture.export_path)
            ).fingerprint,
        )
    old_snapshot = imported.result.snapshot_ref
    assert old_snapshot is not None
    _set_version(config, 3)
    _set_legacy_migration_constraints(config)

    def fail_at(_root: Path, current: str) -> None:
        if current == fault_point:
            raise RuntimeError(f"fault at {current}")

    monkeypatch.setattr("personal_health_lab.storage._store._migration_fault_point", fail_at)
    with HealthLab.open(config) as health_lab:
        request = MigrateStore()
        plan = health_lab.preview_write(request)
        with pytest.raises(RuntimeError, match="fault at"):
            health_lab.execute_write(request, expected_plan=plan.fingerprint)

    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        assert "'migrate_store'" not in metadata.execute(
            "SELECT sql FROM sqlite_schema WHERE name = 'write_operations'"
        ).fetchone()[0]

    monkeypatch.undo()
    with HealthLab.open(config) as health_lab:
        retry_request = MigrateStore()
        retry_plan = health_lab.preview_write(retry_request)
        retry = health_lab.execute_write(
            retry_request, expected_plan=retry_plan.fingerprint
        )

    assert isinstance(retry.result, StoreMigrationReceipt)
    assert retry.result.status is MigrationStatus.COMPLETED
    assert retry_plan.details.backup_file == "metadata-v3-to-v8-2.sqlite3"
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        active = metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone()
        assert active is not None and active != (str(old_snapshot),)
        assert metadata.execute(
            "SELECT schema_version FROM store_identity WHERE singleton = 1"
        ).fetchone() == (8,)


@pytest.mark.parametrize(
    "validation_layer",
    ("snapshot", "store"),
)
def test_migration_validation_failure_prevents_activation(
    validation_layer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = generate_export("null-v1", 42, tmp_path / f"fixture-{validation_layer}")
    config = _config(tmp_path / validation_layer)
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        imported = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    old_snapshot = imported.result.snapshot_ref
    assert old_snapshot is not None
    _set_version(config, 3)

    if validation_layer == "store":
        original_validate_store = LocalStore._validate_store

        def fail_store_validation(store: LocalStore) -> None:
            if store.load_identity().is_current:
                raise StoreError("injected store validation failure")
            original_validate_store(store)

        monkeypatch.setattr(LocalStore, "_validate_store", fail_store_validation)
    else:
        original_validate_snapshot = LocalStore._validate_snapshot

        def fail_snapshot_validation(
            store: LocalStore,
            directory: Path,
            snapshot_id: str,
            manifest_sha256: str,
            **kwargs: str | None,
        ) -> None:
            if directory.parent.name == "migration-staging":
                raise StoreError("injected snapshot validation failure")
            original_validate_snapshot(
                store, directory, snapshot_id, manifest_sha256, **kwargs
            )

        monkeypatch.setattr(LocalStore, "_validate_snapshot", fail_snapshot_validation)
    with HealthLab.open(config) as health_lab:
        request = MigrateStore()
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(receipt.result, WriteNotStarted)
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone() == (str(old_snapshot),)
        assert metadata.execute(
            "SELECT schema_version FROM store_identity WHERE singleton = 1"
        ).fetchone() == (3,)


def test_cow_migration_v1_bounds_normal_and_stress_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    normal = cow_migration_estimate(425_984, 294_403, 4096)
    stress = cow_migration_estimate(8 * 425_984, 8 * 294_403, 4096)

    assert normal is not None and normal >= 8.62 * 1024**2
    assert stress is not None and stress >= 68.56 * 1024**2
    assert cow_migration_estimate(1, 1, 4096) == 73_728
    assert cow_migration_estimate(425_984, 294_403, 4096, writer_bound=False) is None
    assert cow_migration_estimate(425_984, 294_403, 4096, scratch_bound=False) is None

    for export_count in (1, 8):
        config = _config(tmp_path / f"allocation-{export_count}")
        with HealthLab.open(config) as health_lab:
            for seed in range(export_count):
                fixture = generate_export(
                    "null-v1", seed, tmp_path / f"allocation-fixture-{export_count}-{seed}"
                )
                request = ImportHealthExport(fixture.export_path)
                health_lab.execute_write(
                    request, expected_plan=health_lab.preview_write(request).fingerprint
                )
        _set_version(config, 3)
        baseline = sum(
            path.stat().st_blocks * 512
            for path in config.active_store.rglob("*")
            if path.is_file()
        )
        measured: list[int] = []
        measured_phases: list[str] = []

        def checkpoint(
            root: Path,
            _phase: str,
            baseline_bytes: int = baseline,
            samples: list[int] = measured,
            phases: list[str] = measured_phases,
        ) -> None:
            allocated = sum(
                path.stat().st_blocks * 512 for path in root.rglob("*") if path.is_file()
            )
            samples.append(max(0, allocated - baseline_bytes))
            phases.append(_phase)

        monkeypatch.setattr(
            "personal_health_lab.storage._store._allocation_checkpoint", checkpoint
        )
        with HealthLab.open(config) as health_lab:
            request = MigrateStore()
            plan = health_lab.preview_write(request)
            health_lab.execute_write(request, expected_plan=plan.fingerprint)
        assert plan.preflight.capacity is not None
        assert plan.preflight.capacity.estimate_bytes is not None
        assert max(measured) <= plan.preflight.capacity.estimate_bytes
        assert measured_phases == [
            "migration_backup",
            "migration_staged",
            "migration_moved",
            "migration_activated",
        ]
        assert plan.preflight.capacity.target_volume.startswith("volume-")
        assert (
            plan.preflight.capacity.estimate_bytes % plan.preflight.capacity.fragment_size == 0
        )


def test_missing_cow_migration_bound_blocks_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path / "unknown-bound")
    with HealthLab.open(config):
        pass
    _set_version(config, 3)
    monkeypatch.setattr(
        "personal_health_lab.storage._store.cow_migration_estimate",
        lambda *_args, **_kwargs: None,
    )

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(MigrateStore())

    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.diagnostics == ("capacity_estimate_unknown",)
    assert not (config.active_store / "migration-backups").exists()


def test_migration_makes_existing_analysis_stale(tmp_path: Path) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture-analysis")
    config = _config(tmp_path / "analysis")
    analysis = RunRestingHeartRateAnalysis(AnalysisDefinitionId("lag-signal-v2"))
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(fixture.export_path)
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        completed = health_lab.execute_write(
            analysis, expected_plan=health_lab.preview_write(analysis).fingerprint
        )
    assert isinstance(completed.result, AnalysisReceipt)
    old_result = completed.result.result_ref
    assert old_result is not None
    _set_version(config, 3)

    with HealthLab.open(config) as health_lab:
        migration = MigrateStore()
        health_lab.execute_write(
            migration, expected_plan=health_lab.preview_write(migration).fingerprint
        )
    with HealthLab.open(config) as health_lab:
        overview = health_lab.load_overview(OverviewSelection())

    stale = next(
        item
        for item in overview.analysis_history
        if item.provenance is not None and item.provenance.result_id == old_result
    )
    assert stale.freshness is AnalysisFreshness.STALE


def test_restart_keeps_cataloged_snapshot_and_quarantines_incomplete_backup(
    tmp_path: Path,
) -> None:
    fixture = generate_export("null-v1", 42, tmp_path / "fixture-recovery")
    config = _config(tmp_path / "recovery")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        imported = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    snapshot = imported.result.snapshot_ref
    assert snapshot is not None
    operation_id = "a" * 32
    marker_root = config.active_store / "migration-staging"
    marker_root.mkdir()
    marker = marker_root / f"{operation_id}.json"
    marker.write_text(
        json.dumps({"operation_id": operation_id, "snapshot_id": str(snapshot)}),
        encoding="utf-8",
    )
    backup_root = config.active_store / "migration-backups"
    backup_root.mkdir()
    temporary_backup = backup_root / "metadata-v3-to-v8.sqlite3.tmp"
    temporary_backup.write_bytes(b"partial")

    with HealthLab.open(config):
        pass

    assert not marker.exists()
    assert not temporary_backup.exists()
    assert (
        config.active_store / "parquet" / "snapshots" / str(snapshot) / "manifest.json"
    ).exists()
    quarantined_backups = tuple(
        (config.active_store / "quarantine" / "migrations").glob("*/backup")
    )
    assert len(quarantined_backups) == 1


def test_restart_quarantines_markerless_migration_snapshot(tmp_path: Path) -> None:
    fixture = generate_export("null-v1", 42, tmp_path / "fixture-orphan")
    config = _config(tmp_path / "orphan")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        imported = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    active = imported.result.snapshot_ref
    assert active is not None
    orphan_id = "b" * 32
    shutil.copytree(
        config.active_store / "parquet" / "snapshots" / str(active),
        config.active_store / "parquet" / "snapshots" / orphan_id,
    )
    _set_version(config, 3)

    with HealthLab.open(config):
        pass

    assert not (config.active_store / "parquet" / "snapshots" / orphan_id).exists()
    quarantined = tuple(
        (config.active_store / "quarantine" / "migrations").glob("*/snapshot")
    )
    assert len(quarantined) == 1


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
    _set_version(config, 9)

    with HealthLab.open(config) as health_lab:
        diagnostics = health_lab.load_migration_diagnostics()
        plan = health_lab.preview_write(MigrateStore())

    assert diagnostics.source_version == 9
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
    assert plan.details.steps == ((2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8))
    assert (config.active_store / "metadata.sqlite3").read_bytes() == before
    assert not (config.active_store / "migration-backups").exists()


def test_direct_migration_rollback_restores_backup_and_old_snapshot(tmp_path: Path) -> None:
    fixture = generate_export("null-v1", 42, tmp_path / "fixture-rollback")
    config = _config(tmp_path / "rollback")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(fixture.export_path)
        import_receipt = health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
    old_snapshot = import_receipt.result.snapshot_ref
    assert old_snapshot is not None
    _set_version(config, 2)
    _set_legacy_migration_constraints(config)

    with HealthLab.open(config) as health_lab:
        migration = MigrateStore()
        migration_receipt = health_lab.execute_write(
            migration, expected_plan=health_lab.preview_write(migration).fingerprint
        )
    assert isinstance(migration_receipt.result, StoreMigrationReceipt)
    migration_operation_id = migration_receipt.result.operation_id
    snapshot_root = config.active_store / "parquet" / "snapshots"
    generated_snapshot = next(
        path.name for path in snapshot_root.iterdir() if path.name != str(old_snapshot)
    )

    with HealthLab.open(config) as health_lab:
        rollback = RollbackMigration()
        plan = health_lab.preview_write(rollback)
        receipt = health_lab.execute_write(rollback, expected_plan=plan.fingerprint)

    assert plan.approval.status is WriteApprovalStatus.CONFIRMATION_REQUIRED
    assert isinstance(plan.details, RollbackMigrationPlan)
    assert plan.details.migration_operation_id == migration_operation_id
    assert plan.details.current_snapshot_ref is not None
    assert str(plan.details.current_snapshot_ref) == generated_snapshot
    assert plan.details.restored_snapshot_ref == old_snapshot
    assert isinstance(receipt.result, RollbackMigrationReceipt)
    assert receipt.result.status is MigrationStatus.COMPLETED
    assert receipt.result.restored_snapshot_ref == old_snapshot
    assert receipt.result.unreferenced_snapshot_ref == plan.details.current_snapshot_ref

    assert (snapshot_root / generated_snapshot).is_dir()
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT schema_version FROM store_identity WHERE singleton = 1"
        ).fetchone() == (2,)
        assert metadata.execute(
            "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
        ).fetchone() == (str(old_snapshot),)
        assert metadata.execute(
            "SELECT 1 FROM dataset_snapshots WHERE snapshot_id = ?", (generated_snapshot,)
        ).fetchone() is None
        assert metadata.execute(
            "SELECT request_kind FROM write_operations ORDER BY rowid DESC LIMIT 1"
        ).fetchone() == ("rollback_migration",)


def test_later_successful_state_change_blocks_migration_rollback(tmp_path: Path) -> None:
    first = generate_export("null-v1", 42, tmp_path / "fixture-before-migration")
    later = generate_export("null-v1", 43, tmp_path / "fixture-after-migration")
    config = _config(tmp_path / "rollback-blocked")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(first.export_path)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    _set_version(config, 3)
    _set_legacy_migration_constraints(config)
    with HealthLab.open(config) as health_lab:
        migration = MigrateStore()
        health_lab.execute_write(
            migration, expected_plan=health_lab.preview_write(migration).fingerprint
        )
        request = ImportHealthExport(later.export_path)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        rollback = RollbackMigration()
        plan = health_lab.preview_write(rollback)
        receipt = health_lab.execute_write(rollback, expected_plan=plan.fingerprint)

    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.diagnostics == ("migration_rollback_superseded",)
    assert isinstance(receipt.result, WriteNotStarted)
    assert receipt.result.status is WriteNotStartedStatus.BLOCKED


def test_later_successful_analysis_blocks_migration_rollback(tmp_path: Path) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture-analysis-after")
    config = _config(tmp_path / "rollback-blocked-by-analysis")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    _set_version(config, 3)
    _set_legacy_migration_constraints(config)

    with HealthLab.open(config) as health_lab:
        migration = MigrateStore()
        migration_receipt = health_lab.execute_write(
            migration, expected_plan=health_lab.preview_write(migration).fingerprint
        )
        analysis = RunRestingHeartRateAnalysis(AnalysisDefinitionId("lag-signal-v2"))
        analysis_receipt = health_lab.execute_write(
            analysis, expected_plan=health_lab.preview_write(analysis).fingerprint
        )

    assert isinstance(migration_receipt.result, StoreMigrationReceipt)
    assert isinstance(analysis_receipt.result, AnalysisReceipt)
    assert analysis_receipt.result.status is AnalysisStatus.COMPLETED
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        metadata.execute(
            "UPDATE write_operations SET started_at_utc = ?, completed_at_utc = ? "
            "WHERE operation_id = ?",
            (
                "2099-01-01T00:00:00.123456+00:00",
                "2099-01-01T00:00:00.123456+00:00",
                str(migration_receipt.result.operation_id),
            ),
        )
        metadata.execute(
            "UPDATE analysis_receipts SET created_at = ? WHERE operation_id = ?",
            (
                "2099-01-01T00:00:00.123457+00:00",
                str(analysis_receipt.result.operation_id),
            ),
        )

    with HealthLab.open(config) as health_lab:
        rollback = RollbackMigration()
        plan = health_lab.preview_write(rollback)

    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.diagnostics == ("migration_rollback_superseded",)
