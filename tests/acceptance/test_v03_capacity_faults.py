import json
import sqlite3
from datetime import date
from pathlib import Path
from zipfile import ZipFile

import pytest

from personal_health_lab.application import (
    ContextCoverageStartCreate,
    CreateActivityDerivationVersion,
    CreateMetadataBackup,
    CustomContextLabelCreate,
    DataMode,
    HealthLab,
    ImportHealthExport,
    ReviseContextCoverageStart,
    ReviseCustomContextLabel,
    RuntimeConfig,
    WriteApprovalStatus,
    WriteNotStarted,
    WriteNotStartedStatus,
)
from personal_health_lab.storage import (
    CapacityCheck,
    CapacityMethodId,
    CapacityReason,
    CapacityStatus,
    LocalStore,
)


def _allocation_fixture(*, seed: int, options: dict[str, object]) -> str:
    return json.dumps(
        {"generator": "v03-allocation-v1", "seed": seed, **options},
        sort_keys=True,
        separators=(",", ":"),
    )


def _package(path: Path, *, records: int = 1) -> Path:
    rows = "".join(
        f'''<Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="{index}"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 12:{index % 60:02d}:00 +0100"
        startDate="2024-01-02 12:{index % 60:02d}:00 +0100"
        endDate="2024-01-02 12:{index % 60:02d}:30 +0100"/>'''
        for index in range(records)
    )
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            f'<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>{rows}</HealthData>',
        )
    return path


def _config_with_snapshot(tmp_path: Path) -> RuntimeConfig:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(_package(tmp_path / "export.zip"))
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
    return config


def _active_snapshot(config: RuntimeConfig) -> str:
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        row = metadata.execute("SELECT snapshot_id FROM active_snapshot").fetchone()
    assert row is not None
    return str(row[0])


@pytest.mark.parametrize(
    ("write_request", "method_id"),
    (
        (
            ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 2))),
            "manual-snapshot/v1",
        ),
        (CreateActivityDerivationVersion(180), "activity-derivation/v1"),
    ),
)
def test_v03_snapshot_writes_expose_versioned_capacity_methods(
    tmp_path: Path, write_request: object, method_id: str
) -> None:
    config = _config_with_snapshot(tmp_path)
    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(write_request)  # type: ignore[arg-type]

    assert plan.preflight.capacity is not None
    assert isinstance(plan.preflight.capacity.method_id, CapacityMethodId)
    assert plan.preflight.capacity.method_id == method_id
    assert plan.preflight.capacity.estimate_bytes is not None
    assert plan.preflight.capacity.required_bytes > plan.preflight.capacity.estimate_bytes


def test_manual_snapshot_capacity_bounds_the_requested_materialization_range(
    tmp_path: Path,
) -> None:
    config = _config_with_snapshot(tmp_path)
    with HealthLab.open(config) as health_lab:
        recent = health_lab.preview_write(
            ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 2)))
        )
        historical = health_lab.preview_write(
            ReviseContextCoverageStart(ContextCoverageStartCreate(date(1924, 1, 2)))
        )

    assert recent.preflight.capacity is not None
    assert historical.preflight.capacity is not None
    assert recent.preflight.capacity.estimate_bytes is not None
    assert historical.preflight.capacity.estimate_bytes is not None
    assert historical.preflight.capacity.estimate_bytes > recent.preflight.capacity.estimate_bytes


def test_unknown_manual_snapshot_bound_blocks_the_public_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_with_snapshot(tmp_path)
    unknown = CapacityCheck(
        CapacityStatus.UNKNOWN,
        "volume-test",
        "manual-snapshot/v1",
        None,
        None,
        1024**3,
        2 * 1024**3,
        None,
        4096,
        CapacityReason.ESTIMATE_UNKNOWN,
    )
    monkeypatch.setattr(LocalStore, "preflight_snapshot_write", lambda self, **_: unknown)

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(
            ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 2)))
        )

    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.diagnostics == ("capacity_estimate_unknown",)


def test_manual_snapshot_capacity_is_rechecked_under_the_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_with_snapshot(tmp_path)
    checks = 0

    def preflight(self: LocalStore, **_: object) -> CapacityCheck:
        nonlocal checks
        checks += 1
        ready = checks < 3
        return CapacityCheck(
            CapacityStatus.READY if ready else CapacityStatus.INSUFFICIENT,
            "volume-test",
            "manual-snapshot/v1",
            1,
            256 * 1024**2,
            1024**3,
            2 * 1024**3 if ready else 1,
            1024**3 + 256 * 1024**2 + 1,
            4096,
        )

    monkeypatch.setattr(LocalStore, "preflight_snapshot_write", preflight)
    request = ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 2)))
    with HealthLab.open(config) as health_lab:
        old_snapshot = _active_snapshot(config)
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        assert _active_snapshot(config) == old_snapshot

    assert isinstance(receipt.result, WriteNotStarted)
    assert receipt.result.status is WriteNotStartedStatus.BLOCKED
    assert receipt.final_preflight.capacity is not None
    assert receipt.final_preflight.capacity.status is CapacityStatus.INSUFFICIENT


def _new_allocation(root: Path, baseline_inodes: set[tuple[int, int]]) -> int:
    files = (path for path in root.rglob("*") if path.is_file())
    seen = set(baseline_inodes)
    allocated = 0
    for path in files:
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity not in seen:
            seen.add(identity)
            allocated += stat.st_blocks * 512
    return allocated


@pytest.mark.parametrize("records", (1, 128), ids=("normal", "stress"))
@pytest.mark.parametrize("method_id", ("manual-snapshot/v1", "activity-derivation/v1"))
def test_v03_snapshot_methods_bound_normal_and_stress_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, records: int, method_id: str
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(_package(tmp_path / "export.zip", records=records))
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        baseline_inodes = {
            (stat.st_dev, stat.st_ino)
            for path in config.active_store.rglob("*")
            if path.is_file()
            for stat in (path.stat(),)
        }
        phases: dict[str, int] = {}
        monkeypatch.setattr(
            "personal_health_lab.storage._store._allocation_checkpoint",
            lambda root, phase: phases.__setitem__(
                phase, _new_allocation(root, baseline_inodes)
            ),
        )
        request = (
            ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 2)))
            if method_id == "manual-snapshot/v1"
            else CreateActivityDerivationVersion(180)
        )
        plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert set(phases) == {"snapshot_staged", "snapshot_activated"}
    assert plan.preflight.capacity is not None
    assert plan.preflight.capacity.method_id == method_id
    assert plan.preflight.capacity.estimate_bytes is not None
    assert max(phases.values()) <= plan.preflight.capacity.estimate_bytes


def test_late_manual_snapshot_enospc_never_reaches_the_commit_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_with_snapshot(tmp_path)
    request = ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 2)))

    def fail(_root: Path, phase: str) -> None:
        if phase == "snapshot_activated":
            raise OSError(28, "injected full volume")

    monkeypatch.setattr("personal_health_lab.storage._store._allocation_checkpoint", fail)
    with HealthLab.open(config) as health_lab:
        old_snapshot = _active_snapshot(config)
        plan = health_lab.preview_write(request)
        with pytest.raises(OSError, match="injected full volume"):
            health_lab.execute_write(request, expected_plan=plan.fingerprint)
        assert _active_snapshot(config) == old_snapshot


@pytest.mark.parametrize(
    "fault_point",
    (
        "snapshot.before_move/v1",
        "snapshot.after_move/v1",
        "snapshot.before_sqlite_commit/v1",
        "snapshot.after_sqlite_commit/v1",
    ),
)
@pytest.mark.parametrize("method_id", ("manual-snapshot/v1", "activity-derivation/v1"))
def test_v03_fault_cutpoints_leave_only_old_or_complete_new_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fault_point: str,
    method_id: str,
) -> None:
    config = _config_with_snapshot(tmp_path)
    sentinel = "PRIVATE-CONTEXT-86"
    with HealthLab.open(config) as health_lab:
        seeded = ReviseCustomContextLabel(CustomContextLabelCreate(sentinel))
        health_lab.execute_write(
            seeded, expected_plan=health_lab.preview_write(seeded).fingerprint
        )
    old_snapshot = _active_snapshot(config)
    request = (
        ReviseCustomContextLabel(CustomContextLabelCreate(sentinel + "-CANDIDATE"))
        if method_id == "manual-snapshot/v1"
        else CreateActivityDerivationVersion(180)
    )
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        old_counts = tuple(
            metadata.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("dataset_snapshots", "audit_events", "manual_context_revisions")
        )

    def fail(_root: Path, point: str) -> None:
        if point == fault_point:
            raise RuntimeError("fault")

    monkeypatch.setattr("personal_health_lab.storage._store._publication_fault_point", fail)
    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        with pytest.raises(RuntimeError, match="fault") as error:
            health_lab.execute_write(request, expected_plan=plan.fingerprint)

    active_snapshot = _active_snapshot(config)
    if fault_point == "snapshot.after_sqlite_commit/v1":
        assert active_snapshot != old_snapshot
        assert (config.active_store / "parquet/snapshots" / active_snapshot).is_dir()
    else:
        assert active_snapshot == old_snapshot
    with HealthLab.open(config):
        pass
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        new_counts = tuple(
            metadata.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("dataset_snapshots", "audit_events", "manual_context_revisions")
        )
    committed = fault_point == "snapshot.after_sqlite_commit/v1"
    assert new_counts == (
        old_counts[0] + committed,
        old_counts[1] + (committed and method_id == "manual-snapshot/v1"),
        old_counts[2] + (committed and method_id == "manual-snapshot/v1"),
    )
    diagnostics = str(error.value) + caplog.text + "".join(
        path.read_text(encoding="utf-8")
        for path in config.active_store.rglob("*")
        if path.is_file() and (path.suffix == ".log" or path.name == "diagnostic.json")
    )
    assert sentinel not in diagnostics


@pytest.mark.parametrize("fault_point", ("backup.before_publish/v1", "backup.after_publish/v1"))
def test_v03_backup_fault_cutpoints_leave_no_file_or_one_complete_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_point: str
) -> None:
    config = RuntimeConfig(DataMode.REAL, tmp_path / "synthetic", tmp_path / "real")
    target = tmp_path / "backup" / "metadata.sqlite3"
    target.parent.mkdir()

    def fail(_root: Path, point: str) -> None:
        if point == fault_point:
            raise RuntimeError("fault")

    monkeypatch.setattr("personal_health_lab.recovery._backup_fault_point", fail)
    with HealthLab.open(config) as health_lab:
        request = CreateMetadataBackup(target)
        plan = health_lab.preview_write(request)
        with pytest.raises(RuntimeError, match="fault"):
            health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert target.exists() is (fault_point == "backup.after_publish/v1")
    if target.exists():
        with sqlite3.connect(target) as backup:
            assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
