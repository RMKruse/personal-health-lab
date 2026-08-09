from datetime import date
from pathlib import Path
from zipfile import ZipFile

from personal_health_lab.application import (
    ContextCoverageStartCreate,
    DataMode,
    HealthLab,
    ImportHealthExport,
    ManualContextRevisionPlan,
    ManualContextRevisionReceipt,
    ReviseContextCoverageStart,
    RuntimeConfig,
    SnapshotDateSelection,
)


def _package(path: Path) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            """<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
            <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="1"
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
    assert len(audit.revisions) == 1
