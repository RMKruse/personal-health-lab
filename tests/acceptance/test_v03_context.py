from datetime import date
from pathlib import Path
from zipfile import ZipFile

import duckdb
import pytest

from personal_health_lab.application import (
    ConfigurationError,
    ContextCoverageStartCreate,
    ContextCoverageStartRevise,
    ContextStressOrigin,
    CustomContextLabelCreate,
    CustomContextPeriodCreate,
    DailyStressCreate,
    DataMode,
    HealthLab,
    IllnessCategoryCreate,
    IllnessCategoryRestore,
    IllnessCategoryRevise,
    IllnessCategoryWithdraw,
    IllnessPeriodCreate,
    IllnessSeverity,
    ImportHealthExport,
    ManualContextRevisionPlan,
    ManualContextRevisionReceipt,
    ReviseContextCoverageStart,
    ReviseCustomContextLabel,
    ReviseCustomContextPeriod,
    ReviseDailyStress,
    ReviseIllnessCategory,
    ReviseIllnessPeriod,
    RuntimeConfig,
    SnapshotDateSelection,
    StressLevel,
    WriteApprovalStatus,
)


def _package(path: Path, value: int = 1) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            f"""<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
            <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="{value}"
            sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
            creationDate="2024-01-02 12:00:00 +0100"
            startDate="2024-01-02 12:00:00 +0100" endDate="2024-01-02 12:01:00 +0100"/>
            </HealthData>""",
        )
    return path


def test_context_coverage_start_publishes_an_immutable_snapshot_and_baseline(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(_package(tmp_path / "export.zip"))
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        request = ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 2)))
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        assert isinstance(plan.details, ManualContextRevisionPlan)
        assert isinstance(receipt.result, ManualContextRevisionReceipt)
        daily = health_lab.load_daily_context(
            SnapshotDateSelection(start_date=date(2024, 1, 1), end_date=date(2024, 1, 3))
        )
        records = health_lab.load_context_records()
        audit = health_lab.load_context_audit(receipt.result.logical_id)
        later_import = ImportHealthExport(_package(tmp_path / "later-export.zip", value=2))
        health_lab.execute_write(
            later_import, expected_plan=health_lab.preview_write(later_import).fingerprint
        )
        carried_records = health_lab.load_context_records()

    snapshot = config.active_store / "parquet/snapshots" / str(receipt.result.snapshot_ref)
    with duckdb.connect() as query:
        persisted = query.execute(
            "SELECT day, illness_severity, stress_level FROM read_parquet(?) "
            "WHERE day BETWEEN DATE '2024-01-01' AND DATE '2024-01-03' ORDER BY day",
            (str(snapshot / "daily_context.parquet"),),
        ).fetchall()

    assert receipt.result.snapshot_ref != plan.details.base_snapshot_ref
    assert [item.illness_origin.value for item in daily.days] == [
        "unknown",
        "assumed_none",
        "assumed_none",
    ]
    assert [item.stress_origin.value for item in daily.days] == [
        "unknown",
        "assumed_average",
        "assumed_average",
    ]
    assert records.coverage_start is not None
    assert carried_records.coverage_start == records.coverage_start
    assert len(audit.revisions) == 1
    assert persisted == [
        (date(2024, 1, 2), None, "average"),
        (date(2024, 1, 3), None, "average"),
    ]


def test_no_change_is_terminal_without_writer_or_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    create = ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 2)))
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(_package(tmp_path / "no-change.zip"))
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        first = health_lab.preview_write(create)
        receipt = health_lab.execute_write(create, expected_plan=first.fingerprint)
        assert isinstance(receipt.result, ManualContextRevisionReceipt)
        request = ReviseContextCoverageStart(
            ContextCoverageStartRevise(
                receipt.result.logical_id,
                receipt.result.revision_id,
                date(2024, 1, 2),
            )
        )
        unchanged = health_lab.preview_write(request)
        assert unchanged.approval.status is WriteApprovalStatus.NO_CHANGE

        def fail_writer(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("no_change acquired the writer")

        monkeypatch.setattr(
            "personal_health_lab.application._application.LocalStore.open_writer", fail_writer
        )
        with pytest.raises(ConfigurationError, match="nicht ausgeführt"):
            health_lab.execute_write(request, expected_plan=unchanged.fingerprint)


def test_illness_periods_project_active_categories_and_reject_same_category_overlap(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(_package(tmp_path / "export.zip"))
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        category_request = ReviseIllnessCategory(IllnessCategoryCreate("Reiseübelkeit"))
        category = health_lab.execute_write(
            category_request, expected_plan=health_lab.preview_write(category_request).fingerprint
        ).result
        period_request = ReviseIllnessPeriod(
            IllnessPeriodCreate(
                category.logical_id, date(2024, 1, 1), date(2024, 1, 2), IllnessSeverity.MODERATE
            )
        )
        period = health_lab.execute_write(
            period_request, expected_plan=health_lab.preview_write(period_request).fingerprint
        ).result
        overlap = ReviseIllnessPeriod(
            IllnessPeriodCreate(category.logical_id, date(2024, 1, 2), None, IllnessSeverity.MILD)
        )
        daily = health_lab.load_daily_context(
            SnapshotDateSelection(start_date=date(2024, 1, 1), end_date=date(2024, 1, 3))
        )
        overlap_plan = health_lab.preview_write(overlap)
        other_category_request = ReviseIllnessCategory(IllnessCategoryCreate("Migräne"))
        other_category = health_lab.execute_write(
            other_category_request,
            expected_plan=health_lab.preview_write(other_category_request).fingerprint,
        ).result
        different_category_overlap = ReviseIllnessPeriod(
            IllnessPeriodCreate(
                other_category.logical_id,
                date(2024, 1, 2),
                None,
                IllnessSeverity.SEVERE,
            )
        )
        different_category_plan = health_lab.preview_write(different_category_overlap)
        health_lab.execute_write(
            different_category_overlap, expected_plan=different_category_plan.fingerprint
        )

    assert period.logical_id
    assert overlap_plan.approval.status.value == "blocked"
    assert different_category_plan.approval.status is WriteApprovalStatus.READY
    assert [item.illness_origin.value for item in daily.days] == ["observed", "observed", "unknown"]
    assert [item.highest_illness_severity for item in daily.days] == [
        IllnessSeverity.MODERATE,
        IllnessSeverity.MODERATE,
        None,
    ]


def test_daily_stress_and_custom_contexts_share_the_revision_snapshot_contract(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(_package(tmp_path / "export.zip"))
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        coverage = ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 1)))
        health_lab.execute_write(
            coverage, expected_plan=health_lab.preview_write(coverage).fingerprint
        )
        stress = ReviseDailyStress(DailyStressCreate(date(2024, 1, 2), StressLevel.AVERAGE))
        health_lab.execute_write(stress, expected_plan=health_lab.preview_write(stress).fingerprint)
        label = ReviseCustomContextLabel(CustomContextLabelCreate("  Nachtarbeit  "))
        label_receipt = health_lab.execute_write(
            label, expected_plan=health_lab.preview_write(label).fingerprint
        )
        period = ReviseCustomContextPeriod(
            CustomContextPeriodCreate(
                label_receipt.result.logical_id,
                date(2024, 1, 1),
                None,
                "  nach Bereitschaft  ",
            )
        )
        health_lab.execute_write(period, expected_plan=health_lab.preview_write(period).fingerprint)
        conflicting = ReviseCustomContextPeriod(
            CustomContextPeriodCreate(
                label_receipt.result.logical_id, date(2024, 1, 2), date(2024, 1, 2), None
            )
        )
        daily = health_lab.load_daily_context(
            SnapshotDateSelection(start_date=date(2024, 1, 1), end_date=date(2024, 1, 3))
        )
        conflicting_plan = health_lab.preview_write(conflicting)

    assert [item.stress_origin for item in daily.days] == [
        ContextStressOrigin.ASSUMED_AVERAGE,
        ContextStressOrigin.OBSERVED,
        ContextStressOrigin.ASSUMED_AVERAGE,
    ]
    assert [item.stress_level for item in daily.days] == [
        StressLevel.AVERAGE,
        StressLevel.AVERAGE,
        StressLevel.AVERAGE,
    ]
    assert [item.custom_context_labels for item in daily.days] == [
        ("Nachtarbeit",),
        ("Nachtarbeit",),
        ("Nachtarbeit",),
    ]
    assert conflicting_plan.approval.status.value == "blocked"


def test_context_catalog_revision_withdrawal_and_restore_are_audited(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(_package(tmp_path / "export.zip"))
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        created_request = ReviseIllnessCategory(IllnessCategoryCreate("Erkältung"))
        created = health_lab.execute_write(
            created_request,
            expected_plan=health_lab.preview_write(created_request).fingerprint,
        ).result
        renamed_request = ReviseIllnessCategory(
            IllnessCategoryRevise(created.logical_id, created.revision_id, "Infekt")
        )
        renamed = health_lab.execute_write(
            renamed_request,
            expected_plan=health_lab.preview_write(renamed_request).fingerprint,
        ).result
        withdrawn_request = ReviseIllnessCategory(
            IllnessCategoryWithdraw(created.logical_id, renamed.revision_id, "Korrektur")
        )
        withdrawn = health_lab.execute_write(
            withdrawn_request,
            expected_plan=health_lab.preview_write(withdrawn_request).fingerprint,
        ).result
        assert not health_lab.load_context_records().illness_categories
        collision_request = ReviseIllnessCategory(IllnessCategoryCreate("Infekt"))
        assert (
            health_lab.preview_write(collision_request).approval.status
            is WriteApprovalStatus.BLOCKED
        )
        restored_request = ReviseIllnessCategory(
            IllnessCategoryRestore(created.logical_id, withdrawn.revision_id, "Infekt")
        )
        restored_plan = health_lab.preview_write(restored_request)
        health_lab.execute_write(restored_request, expected_plan=restored_plan.fingerprint)
        records = health_lab.load_context_records()
        audit = health_lab.load_context_audit(created.logical_id)

    assert records.illness_categories[0].name == "Infekt"
    assert [revision.state for revision in audit.revisions] == [
        "active",
        "active",
        "withdrawn",
        "active",
    ]


def test_open_context_period_is_bound_to_its_snapshot(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(_package(tmp_path / "export.zip"))
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        label_request = ReviseCustomContextLabel(CustomContextLabelCreate("Nachtschicht"))
        label = health_lab.execute_write(
            label_request, expected_plan=health_lab.preview_write(label_request).fingerprint
        ).result
        period_request = ReviseCustomContextPeriod(
            CustomContextPeriodCreate(label.logical_id, date(2024, 1, 1), None, None)
        )
        period = health_lab.execute_write(
            period_request, expected_plan=health_lab.preview_write(period_request).fingerprint
        ).result
        selection = SnapshotDateSelection(
            period.snapshot_ref, start_date=date(2024, 1, 1), end_date=date(2024, 1, 3)
        )
        before = health_lab.load_daily_context(selection)
        later = ReviseDailyStress(DailyStressCreate(date(2024, 1, 2), StressLevel.HIGH))
        health_lab.execute_write(later, expected_plan=health_lab.preview_write(later).fingerprint)
        after = health_lab.load_daily_context(selection)

    assert before == after
    assert [item.custom_context_labels for item in before.days] == [
        ("Nachtschicht",),
        ("Nachtschicht",),
        ("Nachtschicht",),
    ]


def test_illness_and_custom_context_catalogs_are_explicitly_split(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(_package(tmp_path / "export.zip"))
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        illness_request = ReviseIllnessCategory(IllnessCategoryCreate("Reise"))
        illness = health_lab.execute_write(
            illness_request, expected_plan=health_lab.preview_write(illness_request).fingerprint
        ).result
        label_request = ReviseCustomContextLabel(CustomContextLabelCreate("Reise"))
        health_lab.execute_write(
            label_request, expected_plan=health_lab.preview_write(label_request).fingerprint
        )
        rename = ReviseIllnessCategory(
            IllnessCategoryRevise(illness.logical_id, illness.revision_id, "Reisekrankheit")
        )
        health_lab.execute_write(rename, expected_plan=health_lab.preview_write(rename).fingerprint)
        records = health_lab.load_context_records()

    assert [item.name for item in records.illness_categories] == ["Reisekrankheit"]
    assert [item.name for item in records.custom_labels] == ["Reise"]
