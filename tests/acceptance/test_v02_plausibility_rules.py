from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
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
from personal_health_lab.data_quality import resolve_sources
from personal_health_lab.storage import (
    ExportFact,
    MeasurementVersionFact,
    ResolvedMeasurement,
    SourceOccurrenceFact,
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


def test_personal_range_uses_previous_effective_days_and_keeps_earlier_findings(
    tmp_path: Path,
) -> None:
    history = [60.0, 61.0] * 14
    package = _package(
        tmp_path / "personal-range.zip",
        [
            (
                "HKQuantityTypeIdentifierRestingHeartRate",
                "count/min",
                value,
                f"hr-{index}",
            )
            for index, value in enumerate([*history, 64.0, 67.0])
        ],
        export_date=datetime(2024, 2, 15, tzinfo=UTC),
    )

    with HealthLab.open(_config(tmp_path)) as health_lab:
        receipt = _execute_import(health_lab, package)
        review = health_lab.load_data_review(DataReviewSelection())
        details = sorted(
            (health_lab.load_data_review_case(case.case_id) for case in review.cases),
            key=lambda detail: detail.measured_at or datetime.min.replace(tzinfo=UTC),
        )

    first_lower = 60.5 - 3.5 * 0.5 / 0.6745
    first_upper = 60.5 + 3.5 * 0.5 / 0.6745
    second_lower = 61.0 - 3.5 / 0.6745
    second_upper = 61.0 + 3.5 / 0.6745
    assert receipt.anomaly_count == 2
    assert [detail.effective_value for detail in details] == [64.0, 67.0]
    assert [reason.code.value for detail in details for reason in detail.reasons] == [
        "above_personal_upper_bound",
        "above_personal_upper_bound",
    ]
    assert math.isclose(details[0].reasons[0].lower_bound, first_lower)
    assert math.isclose(details[0].reasons[0].upper_bound or 0.0, first_upper)
    assert math.isclose(details[1].reasons[0].lower_bound, second_lower)
    assert math.isclose(details[1].reasons[0].upper_bound or 0.0, second_upper)


def test_personal_range_is_inclusive_and_skips_unready_inputs(tmp_path: Path) -> None:
    lower = 100.0 - 3.5 * 10.0 / 0.6745
    upper = 100.0 + 3.5 * 10.0 / 0.6745
    scenarios = (
        ("lower", [90.0] * 14 + [110.0] * 14 + [lower], 0),
        ("upper", [90.0] * 14 + [110.0] * 14 + [upper], 0),
        ("outside", [90.0] * 14 + [110.0] * 14 + [math.nextafter(upper, math.inf)], 1),
        ("short", [60.0, 61.0] * 13 + [60.0, 64.0], 0),
        ("missing-current", [60.0, 61.0] * 14, 0),
        ("zero-mad", [60.0] * 28 + [100.0], 0),
    )
    for name, values, expected_anomalies in scenarios:
        root = tmp_path / name
        root.mkdir()
        package = _package(
            root / "health.zip",
            [
                (
                    "HKQuantityTypeIdentifierRestingHeartRate",
                    "count/min",
                    value,
                    f"{name}-{index}",
                )
                for index, value in enumerate(values)
            ],
            export_date=datetime(2024, 3, 1, tzinfo=UTC),
        )
        with HealthLab.open(_config(root)) as health_lab:
            receipt = _execute_import(health_lab, package)
        assert receipt.anomaly_count == expected_anomalies

    energy_root = tmp_path / "active-energy"
    energy_root.mkdir()
    energy = _package(
        energy_root / "health.zip",
        [
            ("HKQuantityTypeIdentifierActiveEnergyBurned", "kcal", value, f"energy-{index}")
            for index, value in enumerate([400.0] * 28 + [1_000_000.0])
        ],
        export_date=datetime(2024, 3, 1, tzinfo=UTC),
    )
    with HealthLab.open(_config(energy_root)) as health_lab:
        assert _execute_import(health_lab, energy).anomaly_count == 0


def test_personal_range_uses_corrections_instead_of_source_values() -> None:
    export = ExportFact("export", datetime(2024, 2, 1, tzinfo=UTC))
    versions = tuple(
        MeasurementVersionFact(
            measurement_version_id=f"version-{index}",
            logical_measurement_id=f"logical-{index}",
            canonical_type="apple_resting_heart_rate",
            canonical_unit="count/min",
            canonical_value=70.0 if index < 28 else 100.0,
            source_start_utc=(datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index)).isoformat(),
            source_end_utc=(datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index)).isoformat(),
            source_updated_at_utc=(
                datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index)
            ).isoformat(),
            source_version="1",
            source_name="Test Watch",
            device="Test Device",
            strong_source_id_hash=f"source-{index}",
            measurement_local_date=date(2024, 1, 1) + timedelta(days=index),
        )
        for index in range(29)
    )
    occurrences = tuple(
        SourceOccurrenceFact(
            "export", index + 1, version.logical_measurement_id, version.measurement_version_id
        )
        for index, version in enumerate(versions)
    )
    corrections = tuple(
        ResolvedMeasurement(
            logical_measurement_id=f"logical-{index}",
            selected_measurement_version_id=f"version-{index}",
            disposition="included_correction",
            effective_value=60.0,
            canonical_unit="count/min",
            effective_value_source="correction",
            effective_decision_id=f"decision-{index}",
            correction_decision_id=f"decision-{index}",
            source_deletion_decision_id=None,
            conflict_resolution_decision_id=None,
        )
        for index in range(14)
    )

    result = resolve_sources(
        occurrences=occurrences,
        versions=versions,
        exports=(export,),
        previous_measurements=corrections,
        previous_review_cases=(),
        governing_export_id="export",
        imported_measurement_version_ids=("version-28",),
    )

    assert result.anomaly_count == 1
    assert len(result.review_cases) == 1


def test_personal_range_checks_only_the_effective_value_for_each_day() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    values = [60.0, 61.0] * 14 + [251.0, 63.0]
    versions = tuple(
        MeasurementVersionFact(
            measurement_version_id=f"version-{index}",
            logical_measurement_id=f"logical-{index}",
            canonical_type="apple_resting_heart_rate",
            canonical_unit="count/min",
            canonical_value=value,
            source_start_utc=(
                start + timedelta(days=min(index, 28), hours=index == 29)
            ).isoformat(),
            source_end_utc=(start + timedelta(days=min(index, 28), hours=index == 29)).isoformat(),
            source_updated_at_utc=(
                start + timedelta(days=min(index, 28), hours=index == 29)
            ).isoformat(),
            source_version="1",
            source_name="Test Watch",
            device="Test Device",
            strong_source_id_hash=f"source-{index}",
            measurement_local_date=date(2024, 1, 1) + timedelta(days=min(index, 28)),
        )
        for index, value in enumerate(values)
    )
    occurrences = tuple(
        SourceOccurrenceFact(
            "export", index + 1, version.logical_measurement_id, version.measurement_version_id
        )
        for index, version in enumerate(versions)
    )

    result = resolve_sources(
        occurrences=occurrences,
        versions=versions,
        exports=(ExportFact("export", datetime(2024, 2, 1, tzinfo=UTC)),),
        previous_measurements=(),
        previous_review_cases=(),
        governing_export_id="export",
        imported_measurement_version_ids=("version-28", "version-29"),
    )

    assert result.anomaly_count == 1
    assert len(result.review_cases) == 1


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
