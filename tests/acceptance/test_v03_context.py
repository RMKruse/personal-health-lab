from datetime import date
from pathlib import Path
from zipfile import ZipFile

from personal_health_lab.application import (
    ContextCoverageStartCreate,
    ContextStressOrigin,
    CustomContextLabelCreate,
    CustomContextPeriodCreate,
    DailyStressCreate,
    DataMode,
    HealthLab,
    IllnessCategoryCreate,
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

    assert period.logical_id
    assert overlap_plan.approval.status.value == "blocked"
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
