from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from personal_health_lab.application import (
    DataMode,
    DataQualityStatus,
    DataReviewCaseKind,
    DataReviewCycleStatus,
    DataReviewSelection,
    HealthLab,
    ImportHealthExport,
    ImportReceipt,
    OverviewSelection,
    RuntimeConfig,
)


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "synthetic-store",
        real_store=tmp_path / "real-store",
    )


def _package(
    path: Path,
    records: list[tuple[str, str, float, str]],
    *,
    export_date: datetime,
    measurement_start: datetime = datetime(2024, 1, 1, 7, tzinfo=UTC),
) -> Path:
    rows = []
    for index, (source_type, unit, value, sync_id) in enumerate(records):
        measured_at = measurement_start + timedelta(days=index)
        rows.append(
            f'<Record type="{source_type}" sourceName="Test Watch" sourceVersion="1" '
            f'device="Test Device" unit="{unit}" '
            f'creationDate="{measured_at:%Y-%m-%d %H:%M:%S} +0000" '
            f'startDate="{measured_at:%Y-%m-%d %H:%M:%S} +0000" '
            f'endDate="{measured_at:%Y-%m-%d %H:%M:%S} +0000" value="{value}">'
            f'<MetadataEntry key="HKMetadataKeySyncIdentifier" value="{sync_id}"/>'
            "</Record>"
        )
    xml = (
        '<?xml version="1.0"?><HealthData>'
        f'<ExportDate value="{export_date:%Y-%m-%d %H:%M:%S} +0000"/>'
        f"{''.join(rows)}</HealthData>"
    )
    entry = ZipInfo("apple_health_export/export.xml", date_time=(1980, 1, 1, 0, 0, 0))
    entry.compress_type = ZIP_DEFLATED
    with ZipFile(path, "w") as archive:
        archive.writestr(entry, xml)
    return path


def _execute_import(health_lab: HealthLab, package: Path) -> ImportReceipt:
    request = ImportHealthExport(package)
    plan = health_lab.preview_write(request)
    receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
    assert isinstance(receipt.result, ImportReceipt)
    return receipt.result


def test_fixed_rules_are_inclusive_and_keep_flagged_source_values(tmp_path: Path) -> None:
    package = _package(
        tmp_path / "bounds.zip",
        [
            ("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 20, "hr-20"),
            ("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 250, "hr-250"),
            ("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 19, "hr-19"),
            ("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 251, "hr-251"),
            ("HKQuantityTypeIdentifierActiveEnergyBurned", "kcal", 0, "energy-0"),
            ("HKQuantityTypeIdentifierActiveEnergyBurned", "kcal", -1, "energy-negative"),
            ("HKQuantityTypeIdentifierActiveEnergyBurned", "kcal", 1_000_000, "energy-high"),
        ],
        export_date=datetime(2024, 2, 1, tzinfo=UTC),
    )

    with HealthLab.open(_config(tmp_path)) as health_lab:
        receipt = _execute_import(health_lab, package)
        review = health_lab.load_data_review(DataReviewSelection())
        details = tuple(health_lab.load_data_review_case(case.case_id) for case in review.cases)
        overview = health_lab.load_overview(OverviewSelection())

    assert receipt.anomaly_count == 3
    assert review.status is DataQualityStatus.PROVISIONAL
    assert review.cycles[-1].status is DataReviewCycleStatus.OPEN
    identities = {
        (case.measurement_version_id, case.rule_version_id, case.evidence_fingerprint)
        for case in review.cases
    }
    assert len(identities) == len(review.cases)
    assert len({case.evidence_fingerprint for case in review.cases}) == 3
    assert {detail.effective_value for detail in details} == {19, 251, -1}
    assert {detail.effective_value_source for detail in details} == {"source"}
    assert {reason.code for detail in details for reason in detail.reasons} == {
        "below_fixed_lower_bound",
        "above_fixed_upper_bound",
    }
    assert any(value.value == -1 for series in overview.daily_series for value in series.values)


def test_clean_import_cycle_closes_immediately(tmp_path: Path) -> None:
    package = _package(
        tmp_path / "clean.zip",
        [
            ("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 60, "hr-clean"),
            ("HKQuantityTypeIdentifierActiveEnergyBurned", "kcal", 400, "energy-clean"),
        ],
        export_date=datetime(2024, 2, 1, tzinfo=UTC),
    )

    with HealthLab.open(_config(tmp_path)) as health_lab:
        receipt = _execute_import(health_lab, package)
        review = health_lab.load_data_review(DataReviewSelection())

    assert receipt.anomaly_count == 0
    assert review.status is DataQualityStatus.REVIEWED
    assert review.cases == ()
    assert review.cycles[-1].status is DataReviewCycleStatus.CLOSED


def test_late_measurement_uses_the_rule_for_its_measurement_time(tmp_path: Path) -> None:
    config = _config(tmp_path)
    newer = _package(
        tmp_path / "newer.zip",
        [("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 60, "newer")],
        export_date=datetime(2024, 3, 1, tzinfo=UTC),
    )
    late = _package(
        tmp_path / "late.zip",
        [
            ("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 60, "newer"),
            ("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 10, "late"),
        ],
        export_date=datetime(2024, 4, 1, tzinfo=UTC),
        measurement_start=datetime(2023, 12, 1, 7, tzinfo=UTC),
    )

    with HealthLab.open(config) as health_lab:
        _execute_import(health_lab, newer)
        receipt = _execute_import(health_lab, late)
        review = health_lab.load_data_review(DataReviewSelection())

    assert receipt.anomaly_count == 1
    assert len(review.cases) == 1
    assert review.cases[0].rule_version_id == "fixed-plausibility/v1"


def test_unknown_source_type_is_cataloged_and_requests_a_rule_once(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = _package(
        tmp_path / "unknown-1.zip",
        [("HKQuantityTypeIdentifierStepCount", "count", 1000, "steps-1")],
        export_date=datetime(2024, 2, 1, tzinfo=UTC),
    )
    second = _package(
        tmp_path / "unknown-2.zip",
        [("HKQuantityTypeIdentifierStepCount", "count", 2000, "steps-2")],
        export_date=datetime(2024, 3, 1, tzinfo=UTC),
    )

    with HealthLab.open(config) as health_lab:
        first_receipt = _execute_import(health_lab, first)
        second_receipt = _execute_import(health_lab, second)
        review = health_lab.load_data_review(DataReviewSelection())
        detail = health_lab.load_data_review_case(review.cases[0].case_id)

    assert first_receipt.status.value == "committed"
    assert second_receipt.status.value == "committed"
    assert len(review.cases) == 1
    assert review.cases[0].kind is DataReviewCaseKind.RULE_DEFINITION
    assert detail.source_type == "HKQuantityTypeIdentifierStepCount"
    assert detail.reasons == ()
    assert [cycle.status for cycle in review.cycles] == [
        DataReviewCycleStatus.OPEN,
        DataReviewCycleStatus.CLOSED,
    ]
    assert review.status is DataQualityStatus.PROVISIONAL
