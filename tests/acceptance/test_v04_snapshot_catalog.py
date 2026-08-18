from datetime import date
from pathlib import Path
from zipfile import ZipFile

import pytest

from personal_health_lab.application import (
    ConfigurationError,
    CreateMetadataBackup,
    DataMode,
    HealthLab,
    ImportHealthExport,
    ImportReceipt,
    MigrateStore,
    ProjectionUnavailable,
    ProjectionUnavailableCode,
    RuntimeConfig,
    SnapshotAction,
    SnapshotCatalog,
    SnapshotRef,
    SnapshotSelection,
    SnapshotSelectionMode,
    WriteApprovalStatus,
    WriteNotStarted,
    WriteNotStartedStatus,
)


def _package(path: Path, value: int) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            f'''<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
            <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="{value}"
            sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
            creationDate="2024-01-02 12:00:00 +0100"
            startDate="2024-01-02 12:00:00 +0100"
            endDate="2024-01-02 12:01:00 +0100"/></HealthData>''',
        )
    return path


def _import(health_lab: HealthLab, package: Path) -> SnapshotRef:
    request = ImportHealthExport(package)
    receipt = health_lab.execute_write(
        request, expected_plan=health_lab.preview_write(request).fingerprint
    )
    assert isinstance(receipt.result, ImportReceipt)
    assert receipt.result.snapshot_ref is not None
    return receipt.result.snapshot_ref


def test_snapshot_catalog_tracks_active_selection_and_keeps_explicit_selection_pinned(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        first = _import(health_lab, _package(tmp_path / "first.zip", 1))
        first_active = health_lab.load_snapshot_catalog()
        first_pinned = health_lab.load_snapshot_catalog(SnapshotSelection(first))
        second = _import(health_lab, _package(tmp_path / "second.zip", 2))
        current = health_lab.load_snapshot_catalog()
        pinned = health_lab.load_snapshot_catalog(SnapshotSelection(first))

    assert isinstance(first_active, SnapshotCatalog)
    assert isinstance(first_pinned, SnapshotCatalog)
    assert isinstance(current, SnapshotCatalog)
    assert isinstance(pinned, SnapshotCatalog)
    assert first_active.selected_snapshot_ref == first_pinned.selected_snapshot_ref == first
    assert first_active.selection_mode is SnapshotSelectionMode.ACTIVE
    assert first_pinned.selection_mode is SnapshotSelectionMode.HISTORICAL
    assert current.selected_snapshot_ref == second
    assert pinned.selected_snapshot_ref == first
    assert current.projection_id == pinned.projection_id == "snapshot-catalog"
    assert current.projection_version == pinned.projection_version == 1

    entries = {entry.snapshot_ref: entry for entry in current.snapshots}
    assert set(entries) == {first, second}
    assert entries[first].is_active is False
    assert entries[first].available_start_date == date(2024, 1, 2)
    assert entries[first].available_end_date == date(2024, 1, 2)
    assert entries[first].allowed_actions == (SnapshotAction.READ,)
    assert entries[second].is_active is True
    assert entries[second].allowed_actions == (SnapshotAction.READ, SnapshotAction.WRITE)


def test_snapshot_catalog_returns_closed_reasons_for_missing_selections(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        empty = health_lab.load_snapshot_catalog()
        _import(health_lab, _package(tmp_path / "first.zip", 1))
        unknown_ref = SnapshotRef("0" * 32)
        unknown = health_lab.load_snapshot_catalog(SnapshotSelection(unknown_ref))

    assert empty == ProjectionUnavailable(
        projection_id="snapshot-catalog",
        projection_version=1,
        code=ProjectionUnavailableCode.NO_SNAPSHOT,
        snapshot_ref=None,
    )
    assert unknown == ProjectionUnavailable(
        projection_id="snapshot-catalog",
        projection_version=1,
        code=ProjectionUnavailableCode.SNAPSHOT_NOT_FOUND,
        snapshot_ref=unknown_ref,
    )
    with pytest.raises(ConfigurationError):
        SnapshotSelection("not-a-snapshot-ref")  # type: ignore[arg-type]


def test_historical_selection_blocks_write_preview_and_active_selection_unlocks(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        historical_ref = _import(health_lab, _package(tmp_path / "first.zip", 1))
        active_ref = _import(health_lab, _package(tmp_path / "second.zip", 2))
        request = ImportHealthExport(_package(tmp_path / "third.zip", 3))
        selection = SnapshotSelection(historical_ref)

        blocked_plans = tuple(
            health_lab.preview_write(write_request, selection)
            for write_request in (
                request,
                CreateMetadataBackup(tmp_path / "backup.sqlite3"),
                MigrateStore(),
            )
        )
        blocked = blocked_plans[0]
        receipt = health_lab.execute_write(
            request, selection=selection, expected_plan=blocked.fingerprint
        )
        unlocked = health_lab.preview_write(request)
        catalog = health_lab.load_snapshot_catalog()

    assert all(
        plan.approval.status is WriteApprovalStatus.BLOCKED
        and plan.diagnostics == ("historical_snapshot_read_only",)
        for plan in blocked_plans
    )
    assert isinstance(receipt.result, WriteNotStarted)
    assert receipt.result.status is WriteNotStartedStatus.BLOCKED
    assert receipt.result.diagnostics == ("historical_snapshot_read_only",)
    assert unlocked.approval.status is not WriteApprovalStatus.BLOCKED
    assert isinstance(catalog, SnapshotCatalog)
    assert catalog.selected_snapshot_ref == active_ref
