from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

from personal_health_lab.application import (
    DataMode,
    HealthLab,
    ImportHealthExport,
    MedicationRegimeCreate,
    MedicationRegimeRevise,
    ReviseMedicationRegime,
    RuntimeConfig,
    ScheduledDose,
    SnapshotDateSelection,
    Weekday,
)


def _package(path: Path) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            '<HealthData><ExportDate value="2024-04-01 12:00:00 +0200"/>'
            '<Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="1" '
            'sourceName="Apple Watch" sourceVersion="1" device="Apple Watch" '
            'creationDate="2024-04-01 12:00:00 +0200" startDate="2024-04-01 12:00:00 +0200" '
            'endDate="2024-04-01 12:01:00 +0200"/></HealthData>',
        )
    return path


def test_medication_regime_projects_dst_and_keeps_old_snapshot_stable(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    dose = ScheduledDose(
        "  Levothyroxin  ", Decimal("75"), " µg ", time(2, 30), frozenset({Weekday.SUNDAY})
    )
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(_package(tmp_path / "export.zip"))
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        request = ReviseMedicationRegime(
            MedicationRegimeCreate(
                datetime.fromisoformat("2024-03-01T00:00:00+01:00"), "Europe/Berlin", (dose,)
            )
        )
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        old_snapshot = receipt.result.snapshot_ref
        projected = health_lab.load_medication_days(
            SnapshotDateSelection(
                old_snapshot, datetime(2024, 3, 31).date(), datetime(2024, 3, 31).date()
            )
        )
        corrected = ReviseMedicationRegime(
            MedicationRegimeRevise(
                receipt.result.logical_id,
                receipt.result.revision_id,
                datetime.fromisoformat("2024-03-01T00:00:00+01:00"),
                "Europe/Berlin",
                (),
            )
        )
        health_lab.execute_write(
            corrected, expected_plan=health_lab.preview_write(corrected).fingerprint
        )
        old_plan = health_lab.load_medication_plan(old_snapshot)
        new_plan = health_lab.load_medication_plan()

    assert projected.days[0].occurrences[0].scheduled_at.isoformat() == "2024-03-31T03:00:00+02:00"
    assert projected.days[0].occurrences[0].status == "assumed_as_planned"
    assert old_plan.regimes[0].scheduled_doses[0].medication_name == "Levothyroxin"
    assert not new_plan.regimes[0].scheduled_doses
