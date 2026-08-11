import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import duckdb
import pytest

from personal_health_lab.application import (
    AsNeededIntakeCreate,
    AsNeededIntakeRestore,
    AsNeededIntakeRevise,
    AsNeededIntakeWithdraw,
    AsNeededMedication,
    ConfigurationError,
    ContextCoverageStartCreate,
    DataMode,
    HealthLab,
    HealthLabError,
    ImportHealthExport,
    IntakeReasonCategoryCreate,
    IntakeReasonCategoryPlan,
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
    ReviseContextCoverageStart,
    ReviseIntakeReasonCategory,
    ReviseMedicationDeviation,
    ReviseMedicationRegime,
    RuntimeConfig,
    ScheduledDose,
    SnapshotDateSelection,
    Weekday,
)


def _package(path: Path, *, step_count: int = 1) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            '<HealthData><ExportDate value="2024-04-01 12:00:00 +0200"/>'
            f'<Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="{step_count}" '
            'sourceName="Apple Watch" sourceVersion="1" device="Apple Watch" '
            'creationDate="2024-04-01 12:00:00 +0200" startDate="2024-04-01 12:00:00 +0200" '
            'endDate="2024-04-01 12:01:00 +0200"/></HealthData>',
        )
    return path


def test_snapshot_lineage_is_flat_validated_and_reuses_immutable_sources(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(_package(tmp_path / "export.zip"))
        imported_receipt = health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        category = ReviseIntakeReasonCategory(IntakeReasonCategoryCreate("Schmerz"))
        category_plan = health_lab.preview_write(category)
        category_receipt = health_lab.execute_write(
            category, expected_plan=category_plan.fingerprint
        )

    snapshots = config.active_store / "parquet" / "snapshots"
    parent = snapshots / str(imported_receipt.result.snapshot_ref)
    snapshot = snapshots / str(category_receipt.result.snapshot_ref)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    lineage = str(snapshot / "derivation_lineage.parquet").replace("'", "''")
    rows = duckdb.sql(f"SELECT * FROM read_parquet('{lineage}')").fetchall()

    for unchanged in (
        "measurement_versions.parquet",
        "resolved_measurements.parquet",
    ):
        assert os.path.samefile(parent / unchanged, snapshot / unchanged)
    assert manifest["snapshot_schema_version"] == 6
    assert {
        "resolved-measurement/v1",
        "resolved-workout/v1",
        "weight-nutrition-day/v1",
        "sleep-episode/v1",
        "sleep-night/v1",
        "activity-day/v1",
        "activity-coverage/v1",
        "workout-feature/v1",
        "daily-context/v1",
        "medication-context/v1",
    } == set(manifest["derivation_contract_ids"])
    snapshot_as_of = datetime.fromisoformat(manifest["snapshot_binding"]["snapshot_as_of"])
    assert isinstance(category_plan.details, IntakeReasonCategoryPlan)
    assert snapshot_as_of == category_plan.details.medication_as_of
    assert manifest["snapshot_binding"] == {
        "context_as_of_date": snapshot_as_of.astimezone(ZoneInfo("Europe/Berlin"))
        .date()
        .isoformat(),
        "context_timezone": "Europe/Berlin",
        "manual_revision_ids": [str(category_receipt.result.revision_id)],
        "medication_as_of": manifest["snapshot_binding"]["snapshot_as_of"],
        "snapshot_as_of": manifest["snapshot_binding"]["snapshot_as_of"],
        "source_version_ids": sorted({row[4] for row in rows if len(row[4]) == 64}),
    }
    resolved_row = next(row for row in rows if row[1] == "resolved_measurement")
    assert resolved_row[1:3] == ("resolved_measurement", "resolved-measurement/v1")
    assert {row[1] for row in rows} == {
        "resolved_measurement",
        "activity_day",
        "activity_coverage",
        "medication_context",
    }
    assert {row[6] for row in rows} == {str(category_receipt.result.snapshot_ref)}
    assert all(datetime.fromisoformat(row[7]).tzinfo is not None for row in rows)
    assert set(
        next(entry for entry in manifest["files"] if entry["name"] == "derivation_lineage.parquet")[
            "contract_ids"
        ]
    ) == set(manifest["derivation_contract_ids"])
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        assert metadata.execute(
            "SELECT snapshot_as_of, context_timezone, context_as_of_date, medication_as_of "
            "FROM snapshot_contract_bindings WHERE snapshot_id = ?",
            (str(category_receipt.result.snapshot_ref),),
        ).fetchone() == (
            manifest["snapshot_binding"]["snapshot_as_of"],
            "Europe/Berlin",
            manifest["snapshot_binding"]["context_as_of_date"],
            manifest["snapshot_binding"]["medication_as_of"],
        )


def test_import_carries_every_effective_manual_revision_binding_forward(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    taken_at = datetime.fromisoformat("2024-04-01T08:00:00+02:00")
    with HealthLab.open(config) as health_lab:
        first = ImportHealthExport(_package(tmp_path / "first.zip"))
        health_lab.execute_write(first, expected_plan=health_lab.preview_write(first).fingerprint)
        context = ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 1)))
        health_lab.execute_write(
            context, expected_plan=health_lab.preview_write(context).fingerprint
        )
        category = ReviseIntakeReasonCategory(IntakeReasonCategoryCreate("Schmerz"))
        category_receipt = health_lab.execute_write(
            category, expected_plan=health_lab.preview_write(category).fingerprint
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
                (
                    AsNeededMedication(
                        "Ibuprofen",
                        Decimal("400"),
                        "mg",
                        (category_receipt.result.logical_id,),
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
                taken_at,
                (MedicationActualIntake(taken_at, Decimal("50")),),
            )
        )
        health_lab.execute_write(
            deviation, expected_plan=health_lab.preview_write(deviation).fingerprint
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
        health_lab.execute_write(intake, expected_plan=health_lab.preview_write(intake).fingerprint)
        second = ImportHealthExport(_package(tmp_path / "second.zip", step_count=2))
        imported = health_lab.execute_write(
            second, expected_plan=health_lab.preview_write(second).fingerprint
        )
        day = health_lab.load_medication_days(
            SnapshotDateSelection(imported.result.snapshot_ref, taken_at.date(), taken_at.date())
        ).days[0]

    assert day.as_needed_intakes[0].reason_category_name == "Schmerz"
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        revision_ids = {
            str(row[0])
            for table in (
                "manual_context_snapshot_bindings",
                "medication_snapshot_bindings",
                "medication_deviation_snapshot_bindings",
                "intake_reason_category_snapshot_bindings",
                "as_needed_intake_snapshot_bindings",
            )
            for row in metadata.execute(
                f"SELECT revision_id FROM {table} WHERE snapshot_id = ?",
                (str(imported.result.snapshot_ref),),
            )
        }
    manifest = json.loads(
        (
            config.active_store
            / "parquet/snapshots"
            / str(imported.result.snapshot_ref)
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert set(manifest["snapshot_binding"]["manual_revision_ids"]) == revision_ids


def test_manual_snapshot_fault_keeps_revision_audit_and_activation_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index, fault_point in enumerate(
        (
            "snapshot.before_move/v1",
            "snapshot.after_move/v1",
            "snapshot.before_sqlite_commit/v1",
        )
    ):
        root = tmp_path / str(index)
        config = RuntimeConfig(DataMode.SYNTHETIC, root / "store", root / "real")
        with HealthLab.open(config) as health_lab:
            imported = ImportHealthExport(_package(root / "export.zip"))
            receipt = health_lab.execute_write(
                imported, expected_plan=health_lab.preview_write(imported).fingerprint
            )
            request = ReviseIntakeReasonCategory(IntakeReasonCategoryCreate("Schmerz"))
            plan = health_lab.preview_write(request)

            def fail_at(_root: Path, point: str, target_fault_point: str = fault_point) -> None:
                if point == target_fault_point:
                    raise RuntimeError("fault")

            monkeypatch.setattr(
                "personal_health_lab.storage._store._publication_fault_point", fail_at
            )
            with pytest.raises(RuntimeError, match="fault"):
                health_lab.execute_write(request, expected_plan=plan.fingerprint)

        with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
            assert metadata.execute("SELECT snapshot_id FROM active_snapshot").fetchone() == (
                str(receipt.result.snapshot_ref),
            )
            assert metadata.execute(
                "SELECT count(*) FROM intake_reason_category_revisions"
            ).fetchone() == (0,)
            assert metadata.execute("SELECT count(*) FROM audit_events").fetchone() == (1,)


def test_manifest_manual_revision_binding_must_match_sqlite(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(_package(tmp_path / "export.zip"))
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        category = ReviseIntakeReasonCategory(IntakeReasonCategoryCreate("Schmerz"))
        receipt = health_lab.execute_write(
            category, expected_plan=health_lab.preview_write(category).fingerprint
        )

    snapshot = config.active_store / "parquet/snapshots" / str(receipt.result.snapshot_ref)
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    manifest["snapshot_binding"]["manual_revision_ids"] = []
    manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    (snapshot / "manifest.json").write_bytes(manifest_bytes)
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        metadata.execute(
            "UPDATE dataset_snapshots SET manifest_sha256 = ? WHERE snapshot_id = ?",
            (hashlib.sha256(manifest_bytes).hexdigest(), snapshot.name),
        )

    with (
        pytest.raises(HealthLabError, match="Datenspeicher konnte nicht geöffnet"),
        HealthLab.open(config),
    ):
        pass


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
