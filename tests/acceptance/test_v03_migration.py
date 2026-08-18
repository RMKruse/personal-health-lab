import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import personal_health_lab.migration as migration
from personal_health_lab.application import (
    DataMode,
    HealthLab,
    ImportHealthExport,
    MigrateStore,
    RuntimeConfig,
    StoreMigrationPlan,
    StoreMigrationReceipt,
    WorkspaceState,
)
from personal_health_lab.synthetic_export import generate_export


def _config(root: Path) -> RuntimeConfig:
    return RuntimeConfig(DataMode.SYNTHETIC, root, root.parent / "real")


def _set_schema_versions(config: RuntimeConfig, *, store: int, snapshot: int) -> None:
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        snapshot_id = str(
            metadata.execute(
                "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
            ).fetchone()[0]
        )
        manifest_path = (
            config.active_store / "parquet" / "snapshots" / snapshot_id / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_bytes())
        manifest["snapshot_schema_version"] = snapshot
        manifest_bytes = json.dumps(
            manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        manifest_path.write_bytes(manifest_bytes)
        metadata.execute(
            "UPDATE dataset_snapshots SET snapshot_schema_version = ?, manifest_sha256 = ? "
            "WHERE snapshot_id = ?",
            (snapshot, hashlib.sha256(manifest_bytes).hexdigest(), snapshot_id),
        )
        metadata.execute(
            "UPDATE store_identity SET schema_version = ? WHERE singleton = 1", (store,)
        )


def _downgrade_to_v02(config: RuntimeConfig) -> str:
    legacy_files = {
        "source_occurrences.parquet",
        "measurement_versions.parquet",
        "resolved_measurements.parquet",
        "open_review_cases.parquet",
    }
    database = config.active_store / "metadata.sqlite3"
    with sqlite3.connect(database) as metadata:
        snapshot_id = str(
            metadata.execute(
                "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
            ).fetchone()[0]
        )
        snapshot = config.active_store / "parquet" / "snapshots" / snapshot_id
        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["snapshot_schema_version"] = 1
        manifest["resolution_basis"]["identity_rule_version_id"] = "healthkit-natural/v2"
        manifest["resolution_basis"]["mapping_rule_version_id"] = "healthkit-canonical/v1"
        manifest.pop("derivation_contract_ids")
        manifest.pop("snapshot_binding")
        manifest["files"] = [
            {key: value for key, value in entry.items() if key != "contract_ids"}
            for entry in manifest["files"]
            if entry["name"] in legacy_files
        ]
        manifest_bytes = json.dumps(
            manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        manifest_path.write_bytes(manifest_bytes)
        for path in snapshot.glob("*.parquet"):
            if path.name not in legacy_files:
                path.unlink()

        metadata.execute("PRAGMA foreign_keys = OFF")
        metadata.execute("DROP TABLE migration_publications")
        metadata.execute(
            "CREATE TABLE migration_publications ("
            "audit_event_id TEXT PRIMARY KEY REFERENCES audit_events(audit_event_id), "
            "snapshot_id TEXT REFERENCES dataset_snapshots(snapshot_id), "
            "source_schema_version INTEGER NOT NULL CHECK (source_schema_version > 0), "
            "target_schema_version INTEGER NOT NULL CHECK ("
            "target_schema_version > source_schema_version), "
            "backup_file TEXT NOT NULL) STRICT"
        )
        for table in (
            "activity_derivation_snapshot_bindings",
            "activity_derivation_active",
            "activity_derivation_versions",
            "as_needed_intake_snapshot_bindings",
            "as_needed_intake_publications",
            "as_needed_intake_values",
            "as_needed_intake_revisions",
            "intake_reason_category_snapshot_bindings",
            "intake_reason_category_publications",
            "intake_reason_category_values",
            "intake_reason_category_revisions",
            "medication_deviation_snapshot_bindings",
            "medication_deviation_publications",
            "medication_deviation_intakes",
            "medication_deviation_values",
            "medication_deviation_revisions",
            "medication_snapshot_bindings",
            "medication_publications",
            "medication_as_needed_entries",
            "medication_scheduled_doses",
            "medication_regime_values",
            "medication_regime_revisions",
            "manual_context_snapshot_bindings",
            "manual_context_publications",
            "custom_context_period_values",
            "custom_context_label_values",
            "daily_stress_values",
            "illness_period_values",
            "illness_category_values",
            "context_coverage_start_values",
            "manual_context_revisions",
            "snapshot_contract_bindings",
            "unsupported_import_content",
            "import_canonical_counts",
        ):
            metadata.execute(f"DROP TABLE IF EXISTS {table}")
        metadata.execute(
            "UPDATE dataset_snapshots SET snapshot_schema_version = 1, manifest_sha256 = ? "
            "WHERE snapshot_id = ?",
            (hashlib.sha256(manifest_bytes).hexdigest(), snapshot_id),
        )
        metadata.execute(
            "UPDATE store_identity SET schema_version = 6 WHERE singleton = 1"
        )
        metadata.commit()
    return snapshot_id


def test_v03_migration_plans_store_and_snapshot_versions_separately(tmp_path: Path) -> None:
    fixture = generate_export("null-v1", 84, tmp_path / "fixture")
    config = _config(tmp_path / "store")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    _set_schema_versions(config, store=12, snapshot=6)

    with HealthLab.open(config) as health_lab:
        workspace = health_lab.load_workspace_status()
        diagnostics = health_lab.load_migration_diagnostics()
        request = MigrateStore()
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert workspace.state is WorkspaceState.MIGRATION_REQUIRED
    assert diagnostics.source_version == 12
    assert diagnostics.target_version == 12
    assert diagnostics.steps == ()
    assert diagnostics.snapshot_source_version == 6
    assert diagnostics.snapshot_target_version == 7
    assert diagnostics.snapshot_steps == ((6, 7),)
    assert plan.details.snapshot_as_of is not None
    assert isinstance(receipt.result, StoreMigrationReceipt)
    assert receipt.result.steps == ()
    assert receipt.result.snapshot_steps == ((6, 7),)
    assert receipt.result.snapshot_as_of == plan.details.snapshot_as_of


def test_v02_store_migrates_to_one_complete_v03_snapshot(tmp_path: Path) -> None:
    fixture = generate_export("null-v1", 84, tmp_path / "v02-fixture")
    config = _config(tmp_path / "v02-store")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    old_snapshot = _downgrade_to_v02(config)

    with HealthLab.open(config) as health_lab:
        request = MigrateStore()
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(plan.details, StoreMigrationPlan)
    assert plan.details.snapshot_source_version == 1
    assert plan.details.snapshot_target_version == 7
    assert plan.details.snapshot_steps == (
        (1, 2),
        (2, 3),
        (3, 4),
        (4, 5),
        (5, 6),
        (6, 7),
    )
    assert isinstance(receipt.result, StoreMigrationReceipt)
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT schema_version FROM store_identity WHERE singleton = 1"
        ).fetchone() == (12,)
        new_snapshot = str(
            metadata.execute(
                "SELECT snapshot_id FROM active_snapshot WHERE singleton = 1"
            ).fetchone()[0]
        )
        assert new_snapshot != old_snapshot
        assert metadata.execute(
            "SELECT snapshot_schema_version FROM dataset_snapshots WHERE snapshot_id = ?",
            (new_snapshot,),
        ).fetchone() == (7,)
        for table in (
            "manual_context_revisions",
            "medication_regime_revisions",
            "medication_deviation_revisions",
            "as_needed_intake_revisions",
            "intake_reason_category_revisions",
        ):
            assert metadata.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        assert {
            str(row[1])
            for row in metadata.execute("PRAGMA table_info(migration_publications)")
        } >= {
            "snapshot_source_schema_version",
            "snapshot_target_schema_version",
        }

    manifest = json.loads(
        (
            config.active_store
            / "parquet"
            / "snapshots"
            / new_snapshot
            / "manifest.json"
        ).read_bytes()
    )
    assert manifest["snapshot_schema_version"] == 7
    assert len(manifest["files"]) == 17
    assert manifest["parent_snapshot_id"] == old_snapshot
    entries = {entry["name"]: entry for entry in manifest["files"]}
    assert entries["source_occurrences.parquet"]["contract_ids"] == [
        "healthkit-natural/v2",
        "healthkit-canonical/v1",
    ]
    assert entries["measurement_versions.parquet"]["contract_ids"] == [
        "healthkit-canonical/v1"
    ]
    for filename in (
        "source_occurrences.parquet",
        "measurement_versions.parquet",
        "resolved_measurements.parquet",
        "open_review_cases.parquet",
    ):
        assert (config.active_store / "parquet" / "snapshots" / new_snapshot / filename).samefile(
            config.active_store / "parquet" / "snapshots" / old_snapshot / filename
        )
    old_manifest = json.loads(
        (
            config.active_store
            / "parquet"
            / "snapshots"
            / old_snapshot
            / "manifest.json"
        ).read_bytes()
    )
    assert old_manifest["snapshot_schema_version"] == 1
    assert {entry["name"] for entry in old_manifest["files"]} == {
        "source_occurrences.parquet",
        "measurement_versions.parquet",
        "resolved_measurements.parquet",
        "open_review_cases.parquet",
    }


def test_v03_migration_rejects_newer_and_missing_registered_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert migration.plan_store_migration(13)[2] == ("newer_schema",)
    assert migration.plan_snapshot_migration(8)[2] == ("newer_snapshot_schema",)
    assert migration.plan_backup_migration(5, 4) is None

    monkeypatch.setattr(migration, "_REGISTERED_STEPS", ())
    monkeypatch.setattr(migration, "_REGISTERED_SNAPSHOT_STEPS", ())
    monkeypatch.setattr(migration, "_REGISTERED_BACKUP_STEPS", ())
    assert migration.plan_store_migration(10)[2] == ("missing_migration_step",)
    assert migration.plan_snapshot_migration(6)[2] == (
        "missing_snapshot_migration_step",
    )
    assert migration.plan_backup_migration(3, 4) is None


def test_v03_migration_fault_quarantines_attempt_and_retry_starts_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = generate_export("null-v1", 84, tmp_path / "retry-fixture")
    config = _config(tmp_path / "retry-store")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    old_snapshot = _downgrade_to_v02(config)
    old_snapshot_path = config.active_store / "parquet" / "snapshots" / old_snapshot
    old_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in old_snapshot_path.iterdir()
    }

    def fail_before_commit(_root: Path, fault_point: str) -> None:
        if fault_point == "migration.before_sqlite_commit/v1":
            raise RuntimeError("migration interrupted")

    with monkeypatch.context() as context:
        context.setattr(
            "personal_health_lab.storage._store._migration_fault_point",
            fail_before_commit,
        )
        with HealthLab.open(config) as health_lab:
            request = MigrateStore()
            plan = health_lab.preview_write(request)
            with pytest.raises(RuntimeError, match="migration interrupted"):
                health_lab.execute_write(request, expected_plan=plan.fingerprint)

    with HealthLab.open(config) as health_lab:
        assert health_lab.load_migration_diagnostics().source_version == 6
        retry = MigrateStore()
        retry_plan = health_lab.preview_write(retry)
        receipt = health_lab.execute_write(retry, expected_plan=retry_plan.fingerprint)

    assert isinstance(receipt.result, StoreMigrationReceipt)
    assert retry_plan.details.backup_file == "metadata-v6-to-v12-2.sqlite3"
    assert old_hashes == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in old_snapshot_path.iterdir()
    }
    diagnostics = tuple(
        (path / "diagnostic.json").read_text(encoding="utf-8")
        for path in (config.active_store / "quarantine" / "migrations").iterdir()
    )
    assert any("migration_not_activated" in diagnostic for diagnostic in diagnostics)
