import fcntl
import json
import platform
import plistlib
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from personal_health_lab.application import (
    ConfigurationError,
    DataMode,
    FileVaultStatus,
    HealthLab,
    HealthLabError,
    ImportHealthExport,
    ImportReceipt,
    ImportStatus,
    MigrateStore,
    PersonBindingStatus,
    RuntimeConfig,
    WorkspaceState,
    WriteApprovalStatus,
    WriteConfirmation,
    WriteNotStarted,
    WriteNotStartedStatus,
)
from personal_health_lab.storage import (
    ImportId,
    LocalStore,
    OperationId,
    SnapshotId,
    StoreError,
)


def _package(path: Path) -> Path:
    record = (
        '<Record type="HKQuantityTypeIdentifierRestingHeartRate" sourceName="Test Watch" '
        'sourceVersion="1" device="Test Device" unit="count/min" '
        'creationDate="2024-01-01 07:01:00 +0100" '
        'startDate="2024-01-01 07:00:00 +0100" '
        'endDate="2024-01-01 07:01:00 +0100" value="60"/>'
    )
    export = ZipInfo("apple_health_export/export.xml", date_time=(1980, 1, 1, 0, 0, 0))
    export.compress_type = ZIP_DEFLATED
    with ZipFile(path, "w") as archive:
        archive.writestr(export, f'<?xml version="1.0"?><HealthData>{record}</HealthData>')
    return path


def _real_config(root: Path) -> RuntimeConfig:
    return RuntimeConfig(
        mode=DataMode.REAL,
        synthetic_store=root.parent / f"{root.name}-synthetic",
        real_store=root,
    )


def _filevault_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: FileVaultStatus,
    device: str = "/dev/disk3s5",
) -> dict[str, object]:
    state: dict[str, object] = {
        "status": status,
        "device": device,
        "encryption": status is not FileVaultStatus.UNPROTECTED,
        "probes": 0,
        "target_volume_group": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "startup_volume_group": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "paths": [],
    }
    monkeypatch.setattr(platform, "system", lambda: "Darwin")

    def payload() -> dict[str, object]:
        current = state["status"]
        assert isinstance(current, FileVaultStatus)
        fixture_path = (
            Path(__file__).parents[1]
            / "fixtures"
            / "filevault"
            / "v1"
            / f"{current.value}.json"
        )
        loaded = json.loads(fixture_path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        if command[0] == "/bin/df":
            state["probes"] = int(state["probes"]) + 1
            paths = state["paths"]
            assert isinstance(paths, list)
            paths.append(command[-1])
            source = str(state["device"])
            output = (
                "Filesystem 512-blocks Used Available Capacity Mounted on\n"
                f"{source} 100 20 80 20% /System/Volumes/Data\n"
            ).encode()
            return subprocess.CompletedProcess(command, 0, output, b"")
        current_payload = payload()
        if command[1:3] == ["apfs", "list"]:
            apfs = {
                "Containers": [
                    {
                        "Volumes": [
                            {
                                "APFSVolumeUUID": current_payload["volume_uuid"],
                                "CryptoMigrationOn": current_payload["migration"],
                                "DeviceIdentifier": str(state["device"]).removeprefix("/dev/"),
                                "Roles": current_payload["roles"],
                            }
                        ]
                    }
                ]
            }
            return subprocess.CompletedProcess(
                command,
                1 if state.get("apfs_unavailable") else 0,
                plistlib.dumps(apfs),
                b"",
            )
        if command[0] == "/usr/bin/fdesetup":
            return subprocess.CompletedProcess(
                command,
                0,
                str(state.get("fdesetup", current_payload["fdesetup"])).encode(),
                b"",
            )
        info = {
            "DeviceIdentifier": str(state["device"]).removeprefix("/dev/"),
            "Encryption": bool(state["encryption"]),
            "FileVault": current_payload["filevault"],
            "Locked": False,
            "Roles": current_payload["roles"],
            "VolumeUUID": current_payload["volume_uuid"],
            "APFSVolumeGroupID": (
                state["startup_volume_group"]
                if command[-1] == "/"
                else state["target_volume_group"]
            ),
            "FilesystemType": state.get("filesystem_type", "apfs"),
        }
        if command[-1] == "/" and state.get("startup_info_unavailable"):
            return subprocess.CompletedProcess(command, 1, plistlib.dumps(info), b"")
        if current_payload["filevault"] is None:
            info.pop("FileVault")
        return subprocess.CompletedProcess(command, 0, plistlib.dumps(info), b"")

    monkeypatch.setattr(subprocess, "run", run)
    return state


@pytest.mark.parametrize("mode", tuple(DataMode))
def test_new_store_has_stable_identity_and_immutable_mode(
    mode: DataMode, tmp_path: Path
) -> None:
    config = RuntimeConfig(
        mode=mode,
        synthetic_store=tmp_path / "synthetic",
        real_store=tmp_path / "real",
    )

    with HealthLab.open(config) as health_lab:
        first = health_lab.load_workspace_status()
    with HealthLab.open(config) as health_lab:
        second = health_lab.load_workspace_status()

    assert first.store_id == second.store_id
    assert len(str(first.store_id)) == 32
    assert first.mode is mode
    assert first.person_binding is PersonBindingStatus.UNBOUND

    conflicting = RuntimeConfig(
        mode=DataMode.REAL if mode is DataMode.SYNTHETIC else DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "other-synthetic",
        real_store=config.active_store,
    )
    if mode is DataMode.REAL:
        conflicting = RuntimeConfig(
            mode=DataMode.SYNTHETIC,
            synthetic_store=config.active_store,
            real_store=tmp_path / "other-real",
        )
    with (
        pytest.raises(ConfigurationError, match="anderen Modus"),
        HealthLab.open(conflicting),
    ):
        pass


def test_real_store_binds_only_after_a_decided_personal_import(tmp_path: Path) -> None:
    aborted_config = _real_config(tmp_path / "aborted")
    rejected_config = _real_config(tmp_path / "rejected")
    busy_config = _real_config(tmp_path / "busy")
    committed_config = _real_config(tmp_path / "committed")

    with HealthLab.open(aborted_config) as health_lab:
        health_lab.preview_write(ImportHealthExport(_package(tmp_path / "aborted.zip")))
    with HealthLab.open(aborted_config) as health_lab:
        assert health_lab.load_workspace_status().person_binding is PersonBindingStatus.UNBOUND

    invalid = tmp_path / "invalid.zip"
    with ZipFile(invalid, "w") as archive:
        archive.writestr("unexpected.txt", "not a health export")
    with HealthLab.open(rejected_config) as health_lab:
        request = ImportHealthExport(invalid)
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        assert isinstance(receipt.result, ImportReceipt)
        assert receipt.result.status is ImportStatus.REJECTED
        assert health_lab.load_workspace_status().person_binding is PersonBindingStatus.UNBOUND

    busy_request = ImportHealthExport(_package(tmp_path / "busy.zip"))
    with HealthLab.open(busy_config) as health_lab:
        busy_plan = health_lab.preview_write(busy_request)
        with (busy_config.active_store / ".writer.lock").open("a+b") as writer_lock:
            fcntl.flock(writer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = health_lab.execute_write(busy_request, expected_plan=busy_plan.fingerprint)
        assert busy.result.status.value == "store_busy"
        assert health_lab.load_workspace_status().person_binding is PersonBindingStatus.UNBOUND

    committed_request = ImportHealthExport(_package(tmp_path / "committed.zip"))
    with HealthLab.open(committed_config) as health_lab:
        committed_plan = health_lab.preview_write(committed_request)
        committed = health_lab.execute_write(
            committed_request, expected_plan=committed_plan.fingerprint
        )
        assert isinstance(committed.result, ImportReceipt)
        assert committed.result.status is ImportStatus.COMMITTED
        assert health_lab.load_workspace_status().person_binding is PersonBindingStatus.BOUND

    later_request = ImportHealthExport(_package(tmp_path / "committed-later.zip"))
    with HealthLab.open(committed_config) as health_lab:
        later_plan = health_lab.preview_write(later_request)
        assert WriteConfirmation.REAL_IMPORT_SAME_PERSON in later_plan.confirmations
        later = health_lab.execute_write(
            later_request, expected_plan=later_plan.fingerprint
        )
    assert isinstance(later.result, ImportReceipt)
    assert later.result.status is ImportStatus.DUPLICATE


@pytest.mark.parametrize("status", tuple(FileVaultStatus))
def test_real_import_uses_one_typed_confirmation_plan(
    status: FileVaultStatus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _filevault_fixture(monkeypatch, status=status)
    config = _real_config(tmp_path / status.value)
    request = ImportHealthExport(_package(tmp_path / f"{status.value}.zip"))

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)

        assert plan.approval.status is WriteApprovalStatus.CONFIRMATION_REQUIRED
        assert plan.preflight.filevault is not None
        assert plan.preflight.filevault.status is status
        assert plan.preflight.filevault.target_volume == (
            "11111111-1111-1111-1111-111111111111"
        )
        assert WriteConfirmation.REAL_IMPORT_SAME_PERSON in plan.confirmations
        filevault_confirmations = {
            confirmation
            for confirmation in plan.confirmations
            if confirmation is not WriteConfirmation.REAL_IMPORT_SAME_PERSON
        }
        assert filevault_confirmations == (
            set()
            if status is FileVaultStatus.PROTECTED
            else {WriteConfirmation(f"filevault_{status.value}")}
        )

        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(receipt.result, ImportReceipt)
    assert receipt.result.status is ImportStatus.COMMITTED
    assert int(fixture["probes"]) == 3
    assert set(fixture["paths"]) == {str(config.active_store.resolve())}


def test_synthetic_import_skips_filevault_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_probe(*args: object, **kwargs: object) -> object:
        raise AssertionError("synthetic import must not inspect FileVault")

    monkeypatch.setattr(subprocess, "run", unexpected_probe)
    config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "synthetic",
        real_store=tmp_path / "real",
    )
    request = ImportHealthExport(_package(tmp_path / "synthetic.zip"))

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert plan.approval.status is WriteApprovalStatus.READY
    assert plan.preflight.filevault is None
    assert plan.confirmations == ()
    assert isinstance(receipt.result, ImportReceipt)


def test_changed_filevault_preflight_requires_a_new_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _filevault_fixture(monkeypatch, status=FileVaultStatus.PROTECTED)
    config = _real_config(tmp_path / "degraded")
    request = ImportHealthExport(_package(tmp_path / "degraded.zip"))

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        fixture["status"] = FileVaultStatus.UNPROTECTED
        degraded = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(degraded.result, WriteNotStarted)
    assert degraded.result.status is WriteNotStartedStatus.PLAN_CHANGED


def test_filevault_improvement_to_protected_may_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _filevault_fixture(monkeypatch, status=FileVaultStatus.UNPROTECTED)
    config = _real_config(tmp_path / "improved")
    request = ImportHealthExport(_package(tmp_path / "improved.zip"))

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        fixture["status"] = FileVaultStatus.PROTECTED
        improved = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(improved.result, ImportReceipt)
    assert improved.result.status is ImportStatus.COMMITTED
    assert improved.plan_fingerprint == plan.fingerprint
    assert WriteConfirmation.FILEVAULT_UNPROTECTED in improved.final_preflight.confirmations


def test_different_unknown_filevault_finding_requires_a_new_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _filevault_fixture(monkeypatch, status=FileVaultStatus.UNKNOWN)
    config = _real_config(tmp_path / "different-unknown")
    request = ImportHealthExport(_package(tmp_path / "different-unknown.zip"))

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        fixture["apfs_unavailable"] = True
        changed = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(changed.result, WriteNotStarted)
    assert changed.result.status is WriteNotStartedStatus.PLAN_CHANGED


def test_filevault_is_rechecked_after_the_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _filevault_fixture(monkeypatch, status=FileVaultStatus.PROTECTED)
    config = _real_config(tmp_path / "final-check")
    request = ImportHealthExport(_package(tmp_path / "final-check.zip"))

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)

        original_run = subprocess.run

        def degrade_on_third_probe(command: list[str], **kwargs: Any) -> Any:
            result = original_run(command, **kwargs)
            if command[0] == "/bin/df" and int(fixture["probes"]) == 3:
                fixture["status"] = FileVaultStatus.UNPROTECTED
            return result

        monkeypatch.setattr(subprocess, "run", degrade_on_third_probe)
        changed = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(changed.result, WriteNotStarted)
    assert changed.result.status is WriteNotStartedStatus.PLAN_CHANGED


def test_different_unknown_after_writer_lock_requires_a_new_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _filevault_fixture(monkeypatch, status=FileVaultStatus.UNKNOWN)
    config = _real_config(tmp_path / "final-unknown-check")
    request = ImportHealthExport(_package(tmp_path / "final-unknown-check.zip"))

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        original_run = subprocess.run

        def change_unknown_on_third_probe(command: list[str], **kwargs: Any) -> Any:
            result = original_run(command, **kwargs)
            if command[0] == "/bin/df" and int(fixture["probes"]) == 3:
                fixture["apfs_unavailable"] = True
            return result

        monkeypatch.setattr(subprocess, "run", change_unknown_on_third_probe)
        changed = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(changed.result, WriteNotStarted)
    assert changed.result.status is WriteNotStartedStatus.PLAN_CHANGED


def test_hardware_encryption_without_filevault_is_unprotected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _filevault_fixture(monkeypatch, status=FileVaultStatus.UNPROTECTED)
    fixture["encryption"] = True
    fixture["fdesetup"] = "true"
    fixture["target_volume_group"] = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    config = _real_config(tmp_path / "hardware-encryption")
    request = ImportHealthExport(_package(tmp_path / "hardware-encryption.zip"))

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)

    assert plan.preflight.filevault is not None
    assert plan.preflight.filevault.status is FileVaultStatus.UNPROTECTED
    assert WriteConfirmation.FILEVAULT_UNPROTECTED in plan.confirmations


def test_non_apfs_target_uses_its_own_filevault_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _filevault_fixture(monkeypatch, status=FileVaultStatus.UNPROTECTED)
    fixture["filesystem_type"] = "hfs"
    fixture["fdesetup"] = "true"
    config = _real_config(tmp_path / "non-apfs")

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(
            ImportHealthExport(_package(tmp_path / "non-apfs.zip"))
        )

    assert plan.preflight.filevault is not None
    assert plan.preflight.filevault.status is FileVaultStatus.UNPROTECTED


def test_filevault_true_without_encryption_proof_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _filevault_fixture(monkeypatch, status=FileVaultStatus.PROTECTED)
    fixture["filesystem_type"] = "hfs"
    fixture["encryption"] = False
    config = _real_config(tmp_path / "encryption-unconfirmed")

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(
            ImportHealthExport(_package(tmp_path / "encryption-unconfirmed.zip"))
        )

    assert plan.preflight.filevault is not None
    assert plan.preflight.filevault.status is FileVaultStatus.UNKNOWN


def test_unresolved_startup_group_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _filevault_fixture(monkeypatch, status=FileVaultStatus.PROTECTED)
    fixture["startup_info_unavailable"] = True
    config = _real_config(tmp_path / "startup-unresolved")

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(
            ImportHealthExport(_package(tmp_path / "startup-unresolved.zip"))
        )

    assert plan.preflight.filevault is not None
    assert plan.preflight.filevault.status is FileVaultStatus.UNKNOWN


def test_quarantined_real_import_binds_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _filevault_fixture(monkeypatch, status=FileVaultStatus.PROTECTED)
    config = _real_config(tmp_path / "quarantined")
    request = ImportHealthExport(_package(tmp_path / "quarantined.zip"))

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)

        def fail_publish(*args: object, **kwargs: object) -> None:
            raise StoreError("injected after import read")

        monkeypatch.setattr(LocalStore, "publish_import", fail_publish)
        with pytest.raises(HealthLabError):
            health_lab.execute_write(request, expected_plan=plan.fingerprint)

    monkeypatch.undo()
    with HealthLab.open(config) as health_lab:
        assert health_lab.load_workspace_status().person_binding is PersonBindingStatus.BOUND


def test_pending_person_binding_is_visible_in_workspace_status(tmp_path: Path) -> None:
    config = _real_config(tmp_path / "restore-pending")
    with HealthLab.open(config):
        pass
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        metadata.execute(
            "UPDATE store_identity SET person_binding = 'pending' WHERE singleton = 1"
        )

    with HealthLab.open(config) as health_lab:
        status = health_lab.load_workspace_status()

    assert status.person_binding is PersonBindingStatus.PENDING


def test_complete_store_copy_retains_the_same_identity(tmp_path: Path) -> None:
    original_config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "original",
        real_store=tmp_path / "real",
    )
    with HealthLab.open(original_config) as health_lab:
        original = health_lab.load_workspace_status()

    copied_root = tmp_path / "copy"
    shutil.copytree(original_config.active_store, copied_root)
    copied_config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=copied_root,
        real_store=tmp_path / "other-real",
    )
    with HealthLab.open(copied_config) as health_lab:
        copied = health_lab.load_workspace_status()

    assert copied.store_id == original.store_id


def test_populated_legacy_real_store_waits_for_copy_on_write_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _filevault_fixture(monkeypatch, status=FileVaultStatus.PROTECTED)
    config = _real_config(tmp_path / "legacy")
    initial_request = ImportHealthExport(_package(tmp_path / "initial.zip"))
    with HealthLab.open(config) as health_lab:
        initial_plan = health_lab.preview_write(initial_request)
        initial = health_lab.execute_write(
            initial_request, expected_plan=initial_plan.fingerprint
        )
        assert isinstance(initial.result, ImportReceipt)

    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        metadata.execute(
            "UPDATE store_identity SET schema_version = 1, store_id = NULL, "
            "person_binding = 'unbound' WHERE singleton = 1"
        )
        metadata.execute("ALTER TABLE imports DROP COLUMN diagnostics")

    request = ImportHealthExport(_package(tmp_path / "next.zip"))
    with HealthLab.open(config) as health_lab:
        before = health_lab.load_workspace_status()
        blocked_import = health_lab.preview_write(request)
        migration_request = MigrateStore()
        migration_plan = health_lab.preview_write(migration_request)
    assert before.store_id is None
    assert before.state is WorkspaceState.MIGRATION_REQUIRED
    assert blocked_import.approval.status is WriteApprovalStatus.BLOCKED
    assert blocked_import.diagnostics == ("migration_required",)
    assert WriteConfirmation.STORE_MIGRATION in migration_plan.confirmations
    assert migration_plan.approval.status is WriteApprovalStatus.BLOCKED
    assert migration_plan.diagnostics == ("populated_store_requires_cow",)

    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT store_id FROM store_identity WHERE singleton = 1"
        ).fetchone() == (None,)

    with HealthLab.open(config) as health_lab:
        migration_receipt = health_lab.execute_write(
            migration_request, expected_plan=migration_plan.fingerprint
        )
        migrated = health_lab.load_workspace_status()
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
    assert migration_receipt.result.status.value == "blocked"
    assert receipt.result.status.value == "blocked"
    assert migrated.store_id is None
    assert migrated.person_binding is PersonBindingStatus.UNBOUND
    assert migrated.state is WorkspaceState.MIGRATION_REQUIRED
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        assert "diagnostics" not in {
            str(row[1]) for row in metadata.execute("PRAGMA table_info(imports)")
        }
    assert not (config.active_store / "migration-backups").exists()


def test_legacy_identity_schema_is_not_changed_on_open(tmp_path: Path) -> None:
    config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "legacy-schema",
        real_store=tmp_path / "real",
    )
    with HealthLab.open(config):
        pass
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        metadata.execute("ALTER TABLE store_identity RENAME TO current_store_identity")
        metadata.execute(
            "CREATE TABLE store_identity (singleton INTEGER PRIMARY KEY, "
            "mode TEXT NOT NULL, schema_version TEXT NOT NULL)"
        )
        metadata.execute("INSERT INTO store_identity VALUES (1, 'synthetic', '1.1')")
        metadata.execute("DROP TABLE current_store_identity")
        before_schema = metadata.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()

    with HealthLab.open(config) as health_lab:
        assert health_lab.load_workspace_status().store_id is None

    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        after_schema = metadata.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        columns = {
            str(row[1]) for row in metadata.execute("PRAGMA table_info(store_identity)")
        }
    assert after_schema == before_schema
    assert columns == {"singleton", "mode", "schema_version"}


def test_previous_store_version_requires_explicit_migration_and_is_unchanged_on_open(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "previous-schema",
        real_store=tmp_path / "real",
    )
    with HealthLab.open(config):
        pass
    metadata_path = config.active_store / "metadata.sqlite3"
    with sqlite3.connect(metadata_path) as metadata:
        metadata.execute("UPDATE store_identity SET schema_version = 2 WHERE singleton = 1")

    with HealthLab.open(config) as health_lab:
        status = health_lab.load_workspace_status()
        plan = health_lab.preview_write(
            ImportHealthExport(_package(tmp_path / "blocked-migration.zip"))
        )

    assert status.state is WorkspaceState.MIGRATION_REQUIRED
    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.diagnostics == ("migration_required",)
    with sqlite3.connect(metadata_path) as metadata:
        assert metadata.execute(
            "SELECT schema_version FROM store_identity WHERE singleton = 1"
        ).fetchone() == (2,)


def test_store_identity_constraints_reject_invalid_persisted_values(tmp_path: Path) -> None:
    config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "constraints",
        real_store=tmp_path / "real",
    )
    with HealthLab.open(config):
        pass

    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        with pytest.raises(sqlite3.IntegrityError):
            metadata.execute("UPDATE store_identity SET store_id = 'not-an-id'")
        with pytest.raises(sqlite3.IntegrityError):
            metadata.execute("UPDATE store_identity SET person_binding = 'many-people'")


def test_recovery_before_package_read_leaves_real_store_unbound(tmp_path: Path) -> None:
    config = _real_config(tmp_path / "pre-read-crash")
    with HealthLab.open(config):
        pass
    writer = LocalStore.open_writer(config.active_store, DataMode.REAL)
    writer.start_import(
        operation_id=OperationId("1" * 32),
        import_id=ImportId("2" * 32),
        snapshot_id=SnapshotId("3" * 32),
    )
    writer.close()

    with HealthLab.open(config) as health_lab:
        assert health_lab.load_workspace_status().person_binding is PersonBindingStatus.UNBOUND
