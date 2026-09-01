import sqlite3
from datetime import date
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from personal_health_lab.application import (
    BeginMetadataRestore,
    ConfigurationError,
    ConfirmNutritionDays,
    CreateMetadataBackup,
    DataMode,
    DataQualityStatus,
    HealthLab,
    ImportHealthExport,
    NutritionDayConfirmationReceipt,
    NutritionDayConfirmationTarget,
    NutritionObservationReason,
    NutritionObservationStatus,
    RuntimeConfig,
    SnapshotDateSelection,
    WriteNotStarted,
    WriteNotStartedStatus,
)


def _package(path: Path, *, day_one_energy: int) -> Path:
    samples = (
        ("EnergyConsumed", "kcal", day_one_energy, "day-one-energy", 1),
        ("Protein", "g", 80, "day-one-protein", 1),
        ("Carbohydrates", "g", 200, "day-one-carbohydrates", 1),
        ("FatTotal", "g", 70, "day-one-fat", 1),
        ("EnergyConsumed", "kcal", 1500, "day-two-energy", 2),
    )
    records = "".join(
        f'<Record type="HKQuantityTypeIdentifierDietary{kind}" sourceName="Food Log" '
        f'sourceVersion="1" device="Phone" unit="{unit}" value="{value}" '
        f'creationDate="2024-01-0{day} 12:01:00 +0100" '
        f'startDate="2024-01-0{day} 12:00:00 +0100" '
        f'endDate="2024-01-0{day} 12:00:00 +0100">'
        f'<MetadataEntry key="HKMetadataKeySyncIdentifier" value="{sync_id}"/>'
        "</Record>"
        for kind, unit, value, sync_id, day in samples
    )
    xml = (
        '<?xml version="1.0"?><HealthData>'
        '<ExportDate value="2024-02-01 12:00:00 +0100"/>'
        f"{records}</HealthData>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    return path


def _execute_import(health_lab: HealthLab, package: Path) -> None:
    request = ImportHealthExport(package)
    plan = health_lab.preview_write(request)
    health_lab.execute_write(request, expected_plan=plan.fingerprint)


def test_nutrition_confirmations_are_content_bound_and_snapshot_reproducible(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(DataMode.REAL, tmp_path / "synthetic", tmp_path / "store")
    selection = SnapshotDateSelection(
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 3)
    )
    backup = tmp_path / "metadata-backup.sqlite3"

    with HealthLab.open(config) as health_lab:
        empty = health_lab.load_weight_nutrition(selection)
        assert empty.nutrition_days[0].content_fingerprint == ""

        _execute_import(health_lab, _package(tmp_path / "first.zip", day_one_energy=2000))
        before = health_lab.load_weight_nutrition(selection)
        first, second, third = before.nutrition_days

        assert first.observation_status is NutritionObservationStatus.PARTIAL
        assert first.observation_reasons == (NutritionObservationReason.CONFIRMATION_MISSING,)
        assert second.observation_status is NutritionObservationStatus.PARTIAL
        assert NutritionObservationReason.PROTEIN_MISSING in second.observation_reasons
        assert third.observation_status is NutritionObservationStatus.MISSING
        assert third.energy.value is None
        assert first.energy.source_evidence[0].source_name == "Food Log"

        assert before.snapshot_ref is not None
        with pytest.raises(ConfigurationError):
            ConfirmNutritionDays(
                before.snapshot_ref,
                [NutritionDayConfirmationTarget(first.day, first.content_fingerprint)],  # type: ignore[arg-type]
            )
        request = ConfirmNutritionDays(
            before.snapshot_ref,
            (
                NutritionDayConfirmationTarget(first.day, first.content_fingerprint),
                NutritionDayConfirmationTarget(second.day, second.content_fingerprint),
            ),
        )
        plan = health_lab.preview_write(request)
        assert plan.details.base_snapshot_ref == before.snapshot_ref
        assert plan.details.targets == request.targets

        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint).result
        assert isinstance(receipt, NutritionDayConfirmationReceipt)
        assert len(receipt.confirmation_ids) == 2

        confirmed_snapshot = receipt.snapshot_ref
        confirmed = health_lab.load_weight_nutrition(selection)
        first_confirmed, second_confirmed, _ = confirmed.nutrition_days
        assert first_confirmed.observation_status is NutritionObservationStatus.COMPLETE
        assert first_confirmed.observation_reasons == ()
        assert first_confirmed.quality_status is DataQualityStatus.REVIEWED
        assert first_confirmed.confirmation is not None
        assert first_confirmed.confirmation.confirmation_id == receipt.confirmation_ids[0]
        assert second_confirmed.observation_status is NutritionObservationStatus.PARTIAL
        assert NutritionObservationReason.CONFIRMATION_MISSING not in (
            second_confirmed.observation_reasons
        )
        backup_request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            backup_request,
            expected_plan=health_lab.preview_write(backup_request).fingerprint,
        )

        _execute_import(health_lab, _package(tmp_path / "changed.zip", day_one_energy=2100))
        changed = health_lab.load_weight_nutrition(selection)
        first_changed, second_unchanged, _ = changed.nutrition_days
        assert first_changed.observation_status is NutritionObservationStatus.PARTIAL
        assert first_changed.observation_reasons == (
            NutritionObservationReason.CONTENT_CHANGED_SINCE_CONFIRMATION,
        )
        assert second_unchanged.confirmation is not None
        assert second_unchanged.confirmation.is_valid

        historical = health_lab.load_weight_nutrition(
            SnapshotDateSelection(
                snapshot_ref=confirmed_snapshot,
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 3),
            )
        )
        assert (
            historical.nutrition_days[0].observation_status
            is NutritionObservationStatus.COMPLETE
        )

        stale = health_lab.execute_write(request, expected_plan=plan.fingerprint).result
        assert isinstance(stale, WriteNotStarted)
        assert stale.status is WriteNotStartedStatus.BLOCKED
        assert stale.diagnostics == ("nutrition_snapshot_changed",)

    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT event_kind FROM audit_events WHERE operation_id = ?",
            (str(receipt.operation_id),),
        ).fetchone() == ("nutrition_day_confirmation",)
        assert metadata.execute(
            "SELECT count(*) FROM nutrition_day_confirmation_publications"
        ).fetchone() == (1,)

    assert backup.exists()

    restored_config = RuntimeConfig(
        DataMode.REAL, tmp_path / "restored-synthetic", tmp_path / "restored-real"
    )
    with HealthLab.open(restored_config) as health_lab:
        begin = BeginMetadataRestore(backup)
        health_lab.execute_write(begin, expected_plan=health_lab.preview_write(begin).fingerprint)
        _execute_import(health_lab, tmp_path / "first.zip")
        restored = health_lab.load_weight_nutrition(selection)

    assert restored.nutrition_days[0].confirmation is not None
    assert restored.nutrition_days[0].confirmation.is_valid
    assert restored.nutrition_days[0].observation_status is NutritionObservationStatus.COMPLETE
    assert restored.nutrition_days[1].confirmation is not None
    assert restored.nutrition_days[1].confirmation.is_valid
