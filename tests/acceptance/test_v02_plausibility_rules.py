from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo
from zoneinfo import ZoneInfo

import pytest

from personal_health_lab.application import (
    ConfigurationError,
    CreatePlausibilityRuleVersion,
    DataConfirmation,
    DataMode,
    DataQualityStatus,
    DataReviewCaseKind,
    DataReviewCycleStatus,
    DataReviewSelection,
    HealthLab,
    HealthLabError,
    ImportHealthExport,
    ImportReceipt,
    OverviewSelection,
    PlausibilityRuleSpecification,
    PlausibilityRuleVersionReceipt,
    ResolveDataReviewCase,
    RevokeDataReviewDecision,
    RunHistoricalReview,
    RuntimeConfig,
    SingleDecisionTarget,
    WriteNotStarted,
    WriteNotStartedStatus,
)
from personal_health_lab.data_quality import evaluate_plausibility_cases, resolve_sources
from personal_health_lab.health_data import CanonicalHealthType, CanonicalUnit
from personal_health_lab.storage import (
    ExportFact,
    LocalStore,
    MeasurementVersionFact,
    PlausibilityRuleRecord,
    ResolvedMeasurement,
    SourceOccurrenceFact,
    StoreError,
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
    measurement_step: timedelta = timedelta(days=1),
) -> Path:
    rows = []
    for index, (source_type, unit, value, sync_id) in enumerate(records):
        measured_at = measurement_start + measurement_step * index
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


def test_changed_anomaly_evidence_opens_a_new_case_for_the_same_measurement_and_rule() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    target = MeasurementVersionFact(
        "target-version",
        "target-logical",
        "apple_resting_heart_rate",
        "count/min",
        100.0,
        (start + timedelta(days=28)).isoformat(),
        (start + timedelta(days=28)).isoformat(),
        (start + timedelta(days=28)).isoformat(),
        "1",
        "Test Watch",
        "Test Device",
        "target-source",
        date(2024, 1, 29),
    )
    rule = PlausibilityRuleRecord(
        "same-rule-version",
        "apple_resting_heart_rate",
        "count/min",
        20.0,
        250.0,
        True,
        None,
        start,
    )

    def cases_for(baseline: tuple[float, ...]):
        history = tuple(
            MeasurementVersionFact(
                f"history-version-{index}",
                f"history-logical-{index}",
                "apple_resting_heart_rate",
                "count/min",
                value,
                (start + timedelta(days=index)).isoformat(),
                (start + timedelta(days=index)).isoformat(),
                (start + timedelta(days=index)).isoformat(),
                "1",
                "Test Watch",
                "Test Device",
                f"history-source-{index}",
                date(2024, 1, 1) + timedelta(days=index),
            )
            for index, value in enumerate(baseline)
        )
        versions = (*history, target)
        measurements = tuple(
            ResolvedMeasurement(
                item.logical_measurement_id,
                item.measurement_version_id,
                "included_source",
                item.canonical_value,
                item.canonical_unit,
                "source",
                None,
                None,
                None,
                None,
            )
            for item in versions
        )
        return evaluate_plausibility_cases(
            versions, measurements, (target.measurement_version_id,), (rule,)
        )

    first = cases_for(tuple(60.0 if index % 2 == 0 else 61.0 for index in range(28)))[0]
    changed = cases_for(tuple(70.0 if index % 2 == 0 else 72.0 for index in range(28)))[0]

    assert changed.measurement_version_id == first.measurement_version_id
    assert changed.rule_version_id == first.rule_version_id
    assert changed.evidence_fingerprint != first.evidence_fingerprint
    assert changed.review_case_id != first.review_case_id


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
        [("HKQuantityTypeIdentifierUVExposure", "count", 1000, "uv-1")],
        export_date=datetime(2024, 2, 1, tzinfo=UTC),
    )
    second = _package(
        tmp_path / "unknown-2.zip",
        [("HKQuantityTypeIdentifierUVExposure", "count", 2000, "uv-2")],
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
    assert detail.source_type == "HKQuantityTypeIdentifierUVExposure"
    assert detail.reasons == ()
    assert [cycle.status for cycle in review.cycles] == [
        DataReviewCycleStatus.OPEN,
        DataReviewCycleStatus.CLOSED,
    ]
    assert review.status is DataQualityStatus.PROVISIONAL


def test_plausibility_rule_versions_are_typed_immutable_and_time_bound(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    monday = datetime(2024, 2, 12, tzinfo=datetime.now().astimezone().tzinfo)

    with HealthLab.open(config) as health_lab:
        initial = health_lab.load_plausibility_rules()
        resting = next(
            rule
            for rule in initial.rules
            if rule.data_type is CanonicalHealthType.APPLE_RESTING_HEART_RATE
        )
        original = resting.versions[0]
        assert original.effective_from is None

        for unit, lower, upper in (
            (CanonicalUnit.BEATS_PER_MINUTE, math.nan, 200),
            (CanonicalUnit.BEATS_PER_MINUTE, 30, math.inf),
            (CanonicalUnit.KILOCALORIE, 30, 200),
            (CanonicalUnit.BEATS_PER_MINUTE, 200, 30),
        ):
            with pytest.raises(ConfigurationError):
                CreatePlausibilityRuleVersion(
                    CanonicalHealthType.APPLE_RESTING_HEART_RATE,
                    PlausibilityRuleSpecification(unit, lower, upper),
                    monday,
                )
        assert health_lab.load_plausibility_rules() == initial

        request = CreatePlausibilityRuleVersion(
            CanonicalHealthType.APPLE_RESTING_HEART_RATE,
            PlausibilityRuleSpecification(
                CanonicalUnit.BEATS_PER_MINUTE,
                30,
                200,
                personal_range_enabled=True,
            ),
            monday,
        )
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        assert isinstance(receipt.result, PlausibilityRuleVersionReceipt)
        assert receipt.result.status == "committed"

        changed = health_lab.load_plausibility_rules()
        resting = next(
            rule
            for rule in changed.rules
            if rule.data_type is CanonicalHealthType.APPLE_RESTING_HEART_RATE
        )
        assert resting.versions[0] == original
        assert resting.active_version.specification == request.specification
        assert resting.active_version.effective_from == monday
        assert resting.active_version.effective_timezone is not None
        assert resting.active_version.effective_offset_minutes is not None
        assert resting.recommendation.specification != resting.active_version.specification

        adopt = CreatePlausibilityRuleVersion(
            resting.data_type,
            resting.recommendation.specification,
            monday + timedelta(days=7),
            resting.recommendation.recommendation_id,
        )
        plan = health_lab.preview_write(adopt)
        receipt = health_lab.execute_write(adopt, expected_plan=plan.fingerprint)
        assert isinstance(receipt.result, PlausibilityRuleVersionReceipt)
        resting = next(
            rule
            for rule in health_lab.load_plausibility_rules().rules
            if rule.data_type is CanonicalHealthType.APPLE_RESTING_HEART_RATE
        )
        assert resting.active_version.recommendation_id == "builtin-plausibility/v1"


def test_rule_plan_fingerprint_binds_the_named_timezone(tmp_path: Path) -> None:
    config = _config(tmp_path)
    specification = PlausibilityRuleSpecification(CanonicalUnit.BEATS_PER_MINUTE, 30, 200)
    previewed = CreatePlausibilityRuleVersion(
        CanonicalHealthType.APPLE_RESTING_HEART_RATE,
        specification,
        datetime(2024, 2, 12, tzinfo=ZoneInfo("Europe/Berlin")),
    )
    changed = CreatePlausibilityRuleVersion(
        CanonicalHealthType.APPLE_RESTING_HEART_RATE,
        specification,
        datetime(2024, 2, 12, tzinfo=timezone(timedelta(hours=1))),
    )

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(previewed)
        receipt = health_lab.execute_write(changed, expected_plan=plan.fingerprint)
        versions = next(
            rule.versions
            for rule in health_lab.load_plausibility_rules().rules
            if rule.data_type is CanonicalHealthType.APPLE_RESTING_HEART_RATE
        )

    assert isinstance(receipt.result, WriteNotStarted)
    assert receipt.result.status is WriteNotStartedStatus.PLAN_CHANGED
    assert len(versions) == 1


def test_rule_version_and_required_recheck_commit_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    package = _package(
        tmp_path / "atomic.zip",
        [("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 25, "current")],
        export_date=datetime(2024, 2, 15, tzinfo=UTC),
        measurement_start=datetime(2024, 2, 13, 7, tzinfo=UTC),
    )
    with HealthLab.open(config) as health_lab:
        _execute_import(health_lab, package)
        before = health_lab.load_plausibility_rules()
        request = CreatePlausibilityRuleVersion(
            CanonicalHealthType.APPLE_RESTING_HEART_RATE,
            PlausibilityRuleSpecification(CanonicalUnit.BEATS_PER_MINUTE, 30, 200),
            datetime(2024, 2, 12, tzinfo=timezone(timedelta(hours=1))),
        )
        plan = health_lab.preview_write(request)
        monkeypatch.setattr(
            LocalStore,
            "_stage_review_snapshot",
            lambda *args, **kwargs: (_ for _ in ()).throw(StoreError("stage failed")),
        )
        with pytest.raises(HealthLabError, match="nicht gespeichert"):
            health_lab.execute_write(request, expected_plan=plan.fingerprint)

    with HealthLab.open(config) as health_lab:
        assert health_lab.load_plausibility_rules() == before


def test_deactivation_and_reactivation_leave_a_time_bound_gap(tmp_path: Path) -> None:
    config = _config(tmp_path)
    zone = datetime.now().astimezone().tzinfo
    deactivated_at = datetime(2024, 2, 12, tzinfo=zone)
    reactivated_at = datetime(2024, 2, 19, tzinfo=zone)

    with HealthLab.open(config) as health_lab:
        deactivate = CreatePlausibilityRuleVersion(
            CanonicalHealthType.APPLE_RESTING_HEART_RATE,
            PlausibilityRuleSpecification(CanonicalUnit.BEATS_PER_MINUTE),
            deactivated_at,
        )
        plan = health_lab.preview_write(deactivate)
        health_lab.execute_write(deactivate, expected_plan=plan.fingerprint)

        reactivate = CreatePlausibilityRuleVersion(
            CanonicalHealthType.APPLE_RESTING_HEART_RATE,
            PlausibilityRuleSpecification(CanonicalUnit.BEATS_PER_MINUTE, 20, 250),
            reactivated_at,
        )
        plan = health_lab.preview_write(reactivate)
        health_lab.execute_write(reactivate, expected_plan=plan.fingerprint)
        versions = next(
            rule.versions
            for rule in health_lab.load_plausibility_rules().rules
            if rule.data_type is CanonicalHealthType.APPLE_RESTING_HEART_RATE
        )

    assert versions[-2].specification.active is False
    assert versions[-1].specification.active is True
    assert versions[-2].effective_from == deactivated_at
    assert versions[-1].effective_from == reactivated_at


def test_imports_use_the_stored_rule_timeline_without_backfilling_inactive_weeks(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    zone = timezone(timedelta(hours=1))

    with HealthLab.open(config) as health_lab:
        for effective_from, lower in (
            (datetime(2024, 2, 12, tzinfo=zone), 30.0),
            (datetime(2024, 2, 19, tzinfo=zone), None),
            (datetime(2024, 2, 26, tzinfo=zone), 30.0),
        ):
            request = CreatePlausibilityRuleVersion(
                CanonicalHealthType.APPLE_RESTING_HEART_RATE,
                PlausibilityRuleSpecification(
                    CanonicalUnit.BEATS_PER_MINUTE,
                    lower,
                    200.0 if lower is not None else None,
                ),
                effective_from,
            )
            plan = health_lab.preview_write(request)
            health_lab.execute_write(request, expected_plan=plan.fingerprint)

        package = _package(
            tmp_path / "timeline.zip",
            [
                ("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 10, "inactive"),
                ("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 10, "active"),
            ],
            export_date=datetime(2024, 3, 1, tzinfo=UTC),
            measurement_start=datetime(2024, 2, 20, 7, tzinfo=UTC),
            measurement_step=timedelta(days=7),
        )
        receipt = _execute_import(health_lab, package)
        review = health_lab.load_data_review(DataReviewSelection())

    assert receipt.anomaly_count == 1
    assert len(review.cases) == 1


def test_historical_review_pins_its_basis_and_reuses_only_identical_confirmations(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    zone = timezone(timedelta(hours=1))
    inactive_at = datetime(2024, 2, 12, tzinfo=zone)
    active_at = datetime(2024, 2, 26, tzinfo=zone)

    with HealthLab.open(config) as health_lab:
        for effective_from, lower in ((inactive_at, None), (active_at, 30.0)):
            change = CreatePlausibilityRuleVersion(
                CanonicalHealthType.APPLE_RESTING_HEART_RATE,
                PlausibilityRuleSpecification(
                    CanonicalUnit.BEATS_PER_MINUTE,
                    lower,
                    200.0 if lower is not None else None,
                ),
                effective_from,
            )
            change_plan = health_lab.preview_write(change)
            change_receipt = health_lab.execute_write(change, expected_plan=change_plan.fingerprint)
        assert isinstance(change_receipt.result, PlausibilityRuleVersionReceipt)
        rule_version_id = change_receipt.result.rule_version_id

        package = _package(
            tmp_path / "inactive.zip",
            [("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 10, "inactive")],
            export_date=datetime(2024, 3, 1, tzinfo=UTC),
            measurement_start=datetime(2024, 2, 20, 7, tzinfo=UTC),
        )
        imported = _execute_import(health_lab, package)
        assert health_lab.load_data_review(DataReviewSelection()).cases == ()

        request = RunHistoricalReview(
            CanonicalHealthType.APPLE_RESTING_HEART_RATE,
            date(2024, 2, 19),
            date(2024, 2, 25),
            rule_version_id,
        )
        plan = health_lab.preview_write(request)
        assert plan.details.base_snapshot_ref == imported.snapshot_ref
        assert plan.details.rule_version_id == rule_version_id
        assert plan.details.start_date == date(2024, 2, 19)
        assert plan.details.end_date == date(2024, 2, 25)

        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        review = health_lab.load_data_review(DataReviewSelection())
        assert receipt.result.status == "committed"
        assert len(review.cases) == 1
        first = review.cases[0]
        historical_cycle = review.cycles[-1]
        assert historical_cycle.kind.value == "historical"
        assert historical_cycle.base_snapshot_ref == imported.snapshot_ref
        assert historical_cycle.rule_version_id == rule_version_id

        confirm = ResolveDataReviewCase(first.case_id, DataConfirmation())
        confirm_plan = health_lab.preview_write(confirm)
        confirmation_receipt = health_lab.execute_write(
            confirm, expected_plan=confirm_plan.fingerprint
        )
        assert hasattr(confirmation_receipt.result, "decision_id")

        repeated_plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=repeated_plan.fingerprint)
        repeated_review = health_lab.load_data_review(DataReviewSelection())
        assert repeated_review.cases == ()
        reused_cycle = repeated_review.cycles[-1]
        assert reused_cycle.status is DataReviewCycleStatus.CLOSED

        revoke = RevokeDataReviewDecision(
            SingleDecisionTarget(confirmation_receipt.result.decision_id),
            "confirmation withdrawn",
        )
        revoke_plan = health_lab.preview_write(revoke)
        health_lab.execute_write(revoke, expected_plan=revoke_plan.fingerprint)
        revoked_review = health_lab.load_data_review(DataReviewSelection())
        assert revoked_review.cases[0].case_id == first.case_id
        reopened_cycles = {
            cycle.cycle_id: cycle
            for cycle in revoked_review.cycles
            if cycle.cycle_id in {historical_cycle.cycle_id, reused_cycle.cycle_id}
        }
        assert len(reopened_cycles) == 2
        assert all(
            cycle.status is DataReviewCycleStatus.OPEN and cycle.open_case_count == 1
            for cycle in reopened_cycles.values()
        )

        reconfirm = ResolveDataReviewCase(first.case_id, DataConfirmation())
        reconfirm_plan = health_lab.preview_write(reconfirm)
        health_lab.execute_write(reconfirm, expected_plan=reconfirm_plan.fingerprint)

        assert health_lab.load_data_review(DataReviewSelection()).cases == ()

        changed_package = _package(
            tmp_path / "inactive-changed.zip",
            [("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 9, "inactive")],
            export_date=datetime(2024, 4, 1, tzinfo=UTC),
            measurement_start=datetime(2024, 2, 20, 7, tzinfo=UTC),
        )
        _execute_import(health_lab, changed_package)
        changed_plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=changed_plan.fingerprint)
        changed_review = health_lab.load_data_review(DataReviewSelection())
        assert len(changed_review.cases) == 1
        changed_case = changed_review.cases[0]
        assert changed_case.measurement_version_id != first.measurement_version_id
        assert changed_case.case_id != first.case_id

        changed_confirmation = ResolveDataReviewCase(changed_case.case_id, DataConfirmation())
        changed_confirmation_plan = health_lab.preview_write(changed_confirmation)
        health_lab.execute_write(
            changed_confirmation, expected_plan=changed_confirmation_plan.fingerprint
        )

        different_rule = RunHistoricalReview(
            request.data_type,
            request.start_date,
            request.end_date,
            "fixed-plausibility/v1",
        )
        different_plan = health_lab.preview_write(different_rule)
        health_lab.execute_write(different_rule, expected_plan=different_plan.fingerprint)
        reopened = health_lab.load_data_review(DataReviewSelection())

    assert len(reopened.cases) == 1
    assert reopened.cases[0].rule_version_id == "fixed-plausibility/v1"
    assert reopened.cases[0].case_id != first.case_id


def test_import_rule_change_and_historical_cycles_close_independently(tmp_path: Path) -> None:
    config = _config(tmp_path)
    package = _package(
        tmp_path / "three-cycles.zip",
        [
            ("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 10, "old"),
            ("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 25, "current"),
        ],
        export_date=datetime(2024, 2, 15, tzinfo=UTC),
        measurement_start=datetime(2024, 1, 1, 7, tzinfo=UTC),
        measurement_step=timedelta(days=43),
    )

    with HealthLab.open(config) as health_lab:
        _execute_import(health_lab, package)
        change = CreatePlausibilityRuleVersion(
            CanonicalHealthType.APPLE_RESTING_HEART_RATE,
            PlausibilityRuleSpecification(CanonicalUnit.BEATS_PER_MINUTE, 30, 200),
            datetime(2024, 2, 12, tzinfo=timezone(timedelta(hours=1))),
        )
        change_plan = health_lab.preview_write(change)
        change_result = health_lab.execute_write(
            change, expected_plan=change_plan.fingerprint
        ).result
        assert isinstance(change_result, PlausibilityRuleVersionReceipt)

        historical = RunHistoricalReview(
            CanonicalHealthType.APPLE_RESTING_HEART_RATE,
            date(2024, 1, 1),
            date(2024, 1, 1),
            change_result.rule_version_id,
        )
        historical_plan = health_lab.preview_write(historical)
        health_lab.execute_write(historical, expected_plan=historical_plan.fingerprint)
        review = health_lab.load_data_review(DataReviewSelection())
        open_cycles = [cycle for cycle in review.cycles if cycle.status.value == "open"]
        assert [cycle.kind.value for cycle in open_cycles] == [
            "import",
            "rule_version",
            "historical",
        ]

        historical_case = next(
            case
            for case in review.cases
            if case.rule_version_id == change_result.rule_version_id
            and health_lab.load_data_review_case(case.case_id).measured_at.date()
            == date(2024, 1, 1)
        )
        confirmation = ResolveDataReviewCase(historical_case.case_id, DataConfirmation())
        confirmation_plan = health_lab.preview_write(confirmation)
        health_lab.execute_write(confirmation, expected_plan=confirmation_plan.fingerprint)
        after = health_lab.load_data_review(DataReviewSelection())

    assert [cycle.status.value for cycle in after.cycles[-3:]] == [
        "open",
        "open",
        "closed",
    ]


def test_rule_change_reevaluates_only_measurements_since_its_local_week_boundary(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    package = _package(
        tmp_path / "current-week.zip",
        [("HKQuantityTypeIdentifierRestingHeartRate", "count/min", 25, "current")],
        export_date=datetime(2024, 2, 15, tzinfo=UTC),
        measurement_start=datetime(2024, 2, 13, 7, tzinfo=UTC),
    )
    with HealthLab.open(config) as health_lab:
        imported = _execute_import(health_lab, package)
        request = CreatePlausibilityRuleVersion(
            CanonicalHealthType.APPLE_RESTING_HEART_RATE,
            PlausibilityRuleSpecification(CanonicalUnit.BEATS_PER_MINUTE, 30, 200),
            datetime(2024, 2, 12, tzinfo=timezone(timedelta(hours=1))),
        )
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        review = health_lab.load_data_review(DataReviewSelection())

    assert imported.anomaly_count == 0
    assert isinstance(receipt.result, PlausibilityRuleVersionReceipt)
    assert receipt.result.snapshot_ref != imported.snapshot_ref
    assert len(review.cases) == 1
    assert review.cases[0].rule_version_id == receipt.result.rule_version_id


def test_rule_change_ignores_non_effective_conflict_candidates(
    tmp_path: Path, source_conflict_package: Callable[[Path], Path]
) -> None:
    config = _config(tmp_path)
    with HealthLab.open(config) as health_lab:
        _execute_import(health_lab, source_conflict_package(tmp_path / "conflict.zip"))
        request = CreatePlausibilityRuleVersion(
            CanonicalHealthType.APPLE_RESTING_HEART_RATE,
            PlausibilityRuleSpecification(CanonicalUnit.BEATS_PER_MINUTE, 20, 60.5),
            datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        )
        plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=plan.fingerprint)
        review = health_lab.load_data_review(DataReviewSelection())

    assert [case.kind for case in review.cases] == [DataReviewCaseKind.SOURCE_CONFLICT]
