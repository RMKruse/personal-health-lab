from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import pytest

from personal_health_lab.application import (
    AsNeededIntakeCreate,
    AsNeededIntakeRestore,
    AsNeededIntakeRevise,
    AsNeededIntakeWithdraw,
    AsNeededMedication,
    ConfigurationError,
    DataMode,
    HealthLab,
    ImportHealthExport,
    IntakeReasonCategoryCreate,
    IntakeReasonCategoryRestore,
    IntakeReasonCategoryRevise,
    IntakeReasonCategoryWithdraw,
    MedicationActualIntake,
    MedicationDeviationCreate,
    MedicationDeviationRestore,
    MedicationDeviationWithdraw,
    MedicationPlanEntryId,
    MedicationRegimeCreate,
    MedicationRegimeRevise,
    ReviseAsNeededIntake,
    ReviseIntakeReasonCategory,
    ReviseMedicationDeviation,
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


def test_medication_deviation_binds_one_occurrence_and_keeps_old_snapshot_stable(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    scheduled_at = datetime.fromisoformat("2024-04-01T08:00:00+02:00")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(_package(tmp_path / "export.zip"))
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        regime = ReviseMedicationRegime(
            MedicationRegimeCreate(
                datetime.fromisoformat("2024-03-01T00:00:00+01:00"),
                "Europe/Berlin",
                (
                    ScheduledDose(
                        "Levothyroxin", Decimal("75"), "µg", time(8), frozenset({Weekday.MONDAY})
                    ),
                ),
            )
        )
        regime_receipt = health_lab.execute_write(
            regime, expected_plan=health_lab.preview_write(regime).fingerprint
        )
        deviation = ReviseMedicationDeviation(
            MedicationDeviationCreate(
                regime_receipt.result.logical_id,
                scheduled_at,
                (
                    MedicationActualIntake(
                        datetime.fromisoformat("2024-04-02T09:00:00+09:00"), Decimal("150")
                    ),
                ),
            )
        )
        receipt = health_lab.execute_write(
            deviation, expected_plan=health_lab.preview_write(deviation).fingerprint
        )
        old_snapshot = receipt.result.snapshot_ref
        unchanged = ReviseMedicationDeviation(
            MedicationDeviationCreate(
                regime_receipt.result.logical_id,
                scheduled_at,
                (MedicationActualIntake(scheduled_at, Decimal("75")),),
            )
        )
        assert health_lab.preview_write(unchanged).approval.status.value == "no_change"
        blocked_regime = ReviseMedicationRegime(
            MedicationRegimeRevise(
                regime_receipt.result.logical_id,
                regime_receipt.result.revision_id,
                datetime.fromisoformat("2024-03-01T00:00:00+01:00"),
                "Europe/Berlin",
                (),
            )
        )
        assert health_lab.preview_write(blocked_regime).approval.status.value == "blocked"
        withdraw = ReviseMedicationDeviation(
            MedicationDeviationWithdraw(
                receipt.result.logical_id, receipt.result.revision_id, "Falscher Eintrag"
            )
        )
        withdrawn = health_lab.execute_write(
            withdraw, expected_plan=health_lab.preview_write(withdraw).fingerprint
        )
        day = health_lab.load_medication_days(
            SnapshotDateSelection(old_snapshot, scheduled_at.date(), scheduled_at.date())
        ).days[0]
        current_day = health_lab.load_medication_days(
            SnapshotDateSelection(None, scheduled_at.date(), scheduled_at.date())
        ).days[0]
        restore = ReviseMedicationDeviation(
            MedicationDeviationRestore(
                receipt.result.logical_id,
                withdrawn.result.revision_id,
                (MedicationActualIntake(scheduled_at, Decimal("150")),),
            )
        )
        health_lab.execute_write(
            restore, expected_plan=health_lab.preview_write(restore).fingerprint
        )
        restored_day = health_lab.load_medication_days(
            SnapshotDateSelection(None, scheduled_at.date(), scheduled_at.date())
        ).days[0]
        audit = health_lab.load_medication_audit(receipt.result.logical_id)

    assert day.occurrences[0].status == "deviated"
    assert day.occurrences[0].actual_intakes[0].taken_at.isoformat() == "2024-04-02T09:00:00+09:00"
    assert current_day.occurrences[0].status == "assumed_as_planned"
    assert restored_day.occurrences[0].status == "deviated"
    assert audit.revisions[-1].state == "active"


def test_as_needed_intakes_and_reason_categories_are_snapshot_bound(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    taken_at = datetime.fromisoformat("2024-04-01T00:30:00+09:00")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(_package(tmp_path / "export.zip"))
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        category = ReviseIntakeReasonCategory(IntakeReasonCategoryCreate("  Kopfschmerz  "))
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
                        "Ibuprofen", Decimal("400"), "mg", (category_receipt.result.logical_id,)
                    ),
                ),
            )
        )
        regime_receipt = health_lab.execute_write(
            regime, expected_plan=health_lab.preview_write(regime).fingerprint
        )
        entry_id = health_lab.load_medication_plan().regimes[0].as_needed_medications[0].entry_id
        intake = ReviseAsNeededIntake(
            AsNeededIntakeCreate(
                regime_receipt.result.logical_id,
                entry_id,
                taken_at,
                Decimal("200"),
                category_receipt.result.logical_id,
            )
        )
        intake_receipt = health_lab.execute_write(
            intake, expected_plan=health_lab.preview_write(intake).fingerprint
        )
        intake_snapshot = intake_receipt.result.snapshot_ref
        renamed = ReviseIntakeReasonCategory(
            IntakeReasonCategoryRevise(
                category_receipt.result.logical_id,
                category_receipt.result.revision_id,
                "Schmerz",
            )
        )
        renamed_receipt = health_lab.execute_write(
            renamed, expected_plan=health_lab.preview_write(renamed).fingerprint
        )
        blocked = ReviseIntakeReasonCategory(
            IntakeReasonCategoryWithdraw(
                category_receipt.result.logical_id,
                health_lab.load_medication_plan().intake_reason_categories[0].revision_id,
                "Nicht mehr gebraucht",
            )
        )
        blocked_plan = health_lab.preview_write(blocked)
        old_day = health_lab.load_medication_days(
            SnapshotDateSelection(intake_snapshot, taken_at.date(), taken_at.date())
        ).days[0]
        new_day = health_lab.load_medication_days(
            SnapshotDateSelection(None, taken_at.date(), taken_at.date())
        ).days[0]
        revised_intake = ReviseAsNeededIntake(
            AsNeededIntakeRevise(
                intake_receipt.result.logical_id,
                intake_receipt.result.revision_id,
                taken_at,
                Decimal("300"),
                category_receipt.result.logical_id,
            )
        )
        intake_receipt = health_lab.execute_write(
            revised_intake, expected_plan=health_lab.preview_write(revised_intake).fingerprint
        )
        regime_revise = ReviseMedicationRegime(
            MedicationRegimeRevise(
                regime_receipt.result.logical_id,
                regime_receipt.result.revision_id,
                datetime.fromisoformat("2024-03-01T00:00:00+01:00"),
                "Europe/Berlin",
                (),
                (AsNeededMedication("Ibuprofen", Decimal("400"), "mg", entry_id=entry_id),),
            )
        )
        health_lab.execute_write(
            regime_revise, expected_plan=health_lab.preview_write(regime_revise).fingerprint
        )
        withdrawal = ReviseAsNeededIntake(
            AsNeededIntakeWithdraw(
                intake_receipt.result.logical_id,
                intake_receipt.result.revision_id,
                "Doppelt erfasst",
            )
        )
        withdrawn = health_lab.execute_write(
            withdrawal, expected_plan=health_lab.preview_write(withdrawal).fingerprint
        )
        category_withdrawal = ReviseIntakeReasonCategory(
            IntakeReasonCategoryWithdraw(
                category_receipt.result.logical_id,
                renamed_receipt.result.revision_id,
                "Nicht mehr gebraucht",
            )
        )
        withdrawn_category = health_lab.execute_write(
            category_withdrawal,
            expected_plan=health_lab.preview_write(category_withdrawal).fingerprint,
        )
        category_restore = ReviseIntakeReasonCategory(
            IntakeReasonCategoryRestore(
                category_receipt.result.logical_id,
                withdrawn_category.result.revision_id,
                "Schmerz",
            )
        )
        health_lab.execute_write(
            category_restore, expected_plan=health_lab.preview_write(category_restore).fingerprint
        )
        restore = ReviseAsNeededIntake(
            AsNeededIntakeRestore(
                intake_receipt.result.logical_id,
                withdrawn.result.revision_id,
                taken_at,
                Decimal("300"),
                category_receipt.result.logical_id,
            )
        )
        health_lab.execute_write(
            restore, expected_plan=health_lab.preview_write(restore).fingerprint
        )
        audit = health_lab.load_medication_audit(intake_receipt.result.logical_id)

    assert old_day.as_needed_intakes[0].reason_category_name == "Kopfschmerz"
    assert new_day.as_needed_intakes[0].reason_category_name == "Schmerz"
    assert new_day.as_needed_intakes[0].medication_name == "Ibuprofen"
    assert new_day.as_needed_intakes[0].amount == Decimal("200")
    assert blocked_plan.approval.status.value == "blocked"
    assert audit.revisions[-1].state == "active"


def test_as_needed_plan_entry_ids_must_be_explicitly_valid_and_unique() -> None:
    entry_id = MedicationPlanEntryId("a" * 32)
    with pytest.raises(ConfigurationError):
        AsNeededMedication("Ibuprofen", Decimal("400"), "mg", entry_id="not-an-id")  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError):
        ReviseMedicationRegime(
            MedicationRegimeCreate(
                datetime.fromisoformat("2024-03-01T00:00:00+01:00"),
                "Europe/Berlin",
                (),
                (
                    AsNeededMedication("Ibuprofen", Decimal("400"), "mg", entry_id=entry_id),
                    AsNeededMedication("Paracetamol", Decimal("500"), "mg", entry_id=entry_id),
                ),
            )
        )
