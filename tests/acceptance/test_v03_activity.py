import json
from datetime import date, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import duckdb
import pytest

from personal_health_lab.adapters.cli import main
from personal_health_lab.application import (
    ActivityMetric,
    CreateActivityDerivationVersion,
    DataMode,
    DataReviewCaseKind,
    DataReviewSelection,
    HealthLab,
    ImportHealthExport,
    ResolveDataReviewCase,
    RuntimeConfig,
    SnapshotDateSelection,
    SourceConflictResolution,
    SourceConflictStrategy,
)


def _package(path: Path, xml: str) -> Path:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    return path


def test_activity_import_preserves_typed_samples_and_assigns_cross_midnight_value_to_start_day(
    tmp_path: Path,
) -> None:
    xml = """
    <HealthData>
      <ExportDate value="2024-01-03 12:00:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierAppleExerciseTime" unit="s" value="120"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 23:02:00 +0100"
        startDate="2024-01-02 23:00:00 +0100" endDate="2024-01-03 00:02:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="42"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 20:01:00 +0100"
        startDate="2024-01-02 20:00:00 +0100" endDate="2024-01-02 20:01:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierDistanceWalkingRunning" unit="mi" value="1"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 21:01:00 +0100"
        startDate="2024-01-02 21:00:00 +0100" endDate="2024-01-02 21:01:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierActiveEnergyBurned" unit="kcal" value="10"
        sourceName="iPhone" sourceVersion="1" device="iPhone"
        creationDate="2024-01-02 22:01:00 +0100"
        startDate="2024-01-02 22:00:00 +0100" endDate="2024-01-02 22:01:00 +0100"/>
    </HealthData>
    """
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(_package(tmp_path / "activity.zip", xml))
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        activity = health_lab.load_activity_days(
            SnapshotDateSelection(start_date=date(2024, 1, 2), end_date=date(2024, 1, 3))
        )

    assert {item.data_type for item in activity.measurements} == set(ActivityMetric)
    exercise_time = next(
        item for item in activity.measurements if item.data_type is ActivityMetric.EXERCISE_TIME
    )
    assert exercise_time.measurement_local_day == date(2024, 1, 2)
    assert exercise_time.source_end.date() == date(2024, 1, 3)
    day = activity.days[0]
    assert day.day == date(2024, 1, 2)
    assert day.exercise_time.value == 2
    assert day.step_count.value == 42
    assert day.walking_running_distance.value == 1.609344
    assert day.active_energy.value is None
    assert (
        next(
            item.source_class.value
            for item in activity.measurements
            if item.data_type is ActivityMetric.ACTIVE_ENERGY
        )
        == "iphone"
    )
    assert activity.days[1].exercise_time.value is None


def test_activity_days_cli_projects_the_shared_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    xml = """
    <HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="0"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 20:01:00 +0100"
        startDate="2024-01-02 20:00:00 +0100" endDate="2024-01-02 20:01:00 +0100"/>
    </HealthData>
    """
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(_package(tmp_path / "activity.zip", xml))
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    arguments = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(config.synthetic_store),
        "--real-store",
        str(config.real_store),
        "activity-days",
        "--start-date",
        "2024-01-02",
        "--end-date",
        "2024-01-03",
    ]
    assert main(arguments) == 0
    assert "Aktivitätstage" in capsys.readouterr().out
    assert main([*arguments, "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["kind"] == "activity_days"
    assert output["selection"]["start_date"] == "2024-01-02"
    assert output["days"][0]["step_count"]["value"] == 0
    assert output["days"][1]["step_count"]["value"] is None
    assert output["coverage_gap_minutes"] == 240
    assert output["days"][0]["is_complete"] is False
    assert output["measurements"][0]["suppression_reason"] is None


def test_same_metric_same_source_class_overlap_opens_a_review_case(tmp_path: Path) -> None:
    xml = """
    <HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="42"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 20:30:00 +0100"
        startDate="2024-01-02 20:00:00 +0100" endDate="2024-01-02 20:30:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="10"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 20:45:00 +0100"
        startDate="2024-01-02 20:15:00 +0100" endDate="2024-01-02 20:45:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierActiveEnergyBurned" unit="kcal" value="5"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 20:45:00 +0100"
        startDate="2024-01-02 20:15:00 +0100" endDate="2024-01-02 20:45:00 +0100"/>
    </HealthData>
    """
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(_package(tmp_path / "activity.zip", xml))
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        review = health_lab.load_data_review(
            DataReviewSelection(DataReviewCaseKind.SOURCE_CONFLICT)
        )
        activity = health_lab.load_activity_days(SnapshotDateSelection())
        resolution = ResolveDataReviewCase(
            review.cases[0].case_id,
            SourceConflictResolution(
                SourceConflictStrategy.PREFER,
                preferred_version_id=review.cases[0].candidate_version_ids[0],
            ),
        )
        health_lab.execute_write(
            resolution, expected_plan=health_lab.preview_write(resolution).fingerprint
        )
        resolved_review = health_lab.load_data_review(
            DataReviewSelection(DataReviewCaseKind.SOURCE_CONFLICT)
        )

    assert len(review.cases) == 1
    assert len(review.cases[0].candidate_version_ids) == 2
    assert resolved_review.cases == ()
    assert activity.status.value == "provisional"
    assert activity.days[0].step_count.value == 52
    assert activity.days[0].active_energy.value == 5


def test_point_activity_sample_does_not_open_an_overlap_review(tmp_path: Path) -> None:
    xml = """
    <HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="1"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 20:30:00 +0100"
        startDate="2024-01-02 20:00:00 +0100" endDate="2024-01-02 20:30:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="2"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 20:15:00 +0100"
        startDate="2024-01-02 20:15:00 +0100" endDate="2024-01-02 20:15:00 +0100"/>
    </HealthData>
    """
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(_package(tmp_path / "activity.zip", xml))
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        review = health_lab.load_data_review(
            DataReviewSelection(DataReviewCaseKind.SOURCE_CONFLICT)
        )

    assert review.cases == ()


def test_negative_activity_value_remains_canonical_and_opens_plausibility_review(
    tmp_path: Path,
) -> None:
    xml = """
    <HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="-1"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 20:01:00 +0100"
        startDate="2024-01-02 20:00:00 +0100" endDate="2024-01-02 20:01:00 +0100"/>
    </HealthData>
    """
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(_package(tmp_path / "activity.zip", xml))
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        review = health_lab.load_data_review(DataReviewSelection(DataReviewCaseKind.PLAUSIBILITY))
        activity = health_lab.load_activity_days(SnapshotDateSelection())

    assert len(review.cases) == 1
    assert activity.measurements[0].value == -1
    assert activity.days[0].step_count.value == -1


def test_activity_days_use_shared_watch_coverage_and_whole_interval_iphone_fallback(
    tmp_path: Path,
) -> None:
    xml = """
    <HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="1"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 08:10:00 +0100"
        startDate="2024-01-02 08:00:00 +0100" endDate="2024-01-02 08:10:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierActiveEnergyBurned" unit="kcal" value="1"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 16:10:00 +0100"
        startDate="2024-01-02 16:00:00 +0100" endDate="2024-01-02 16:10:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="2"
        sourceName="iPhone" sourceVersion="1" device="iPhone"
        creationDate="2024-01-02 10:10:00 +0100"
        startDate="2024-01-02 10:00:00 +0100" endDate="2024-01-02 10:10:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="4"
        sourceName="iPhone" sourceVersion="1" device="iPhone"
        creationDate="2024-01-02 07:10:00 +0100"
        startDate="2024-01-02 07:00:00 +0100" endDate="2024-01-02 07:10:00 +0100"/>
    </HealthData>
    """
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(_package(tmp_path / "coverage.zip", xml))
        receipt = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        activity = health_lab.load_activity_days(SnapshotDateSelection())

    snapshot = config.active_store / "parquet/snapshots" / str(receipt.result.snapshot_ref)
    with duckdb.connect() as query:
        persisted_values = query.execute(
            "SELECT watch_value, iphone_value, effective_value, derivation_contract_id, "
            "snapshot_id, derived_at_utc "
            "FROM read_parquet(?) WHERE metric = 'step_count'",
            (str(snapshot / "activity_days.parquet"),),
        ).fetchone()
        persisted_segments = query.execute(
            "SELECT coverage_kind FROM read_parquet(?) ORDER BY start_utc",
            (str(snapshot / "activity_coverage_segments.parquet"),),
        ).fetchall()

    day = activity.days[0]
    assert day.step_count.watch_value == 1
    assert day.step_count.iphone_value == 2
    assert day.step_count.value == 3
    assert day.is_complete is False
    assert "unobserved_coverage" in day.incomplete_reasons
    assert day.coverage_segments[0].source_start.hour == 0
    assert day.coverage_segments[-1].source_end.hour == 0
    assert [segment.kind for segment in day.coverage_segments] == [
        "unobserved",
        "watch",
        "unobserved",
        "iphone_fallback",
        "unobserved",
        "watch",
        "unobserved",
    ]
    assert sorted(
        item.suppression_reason for item in activity.measurements if item.suppression_reason
    ) == [
        "iphone_outside_watch_gap",
    ]
    assert persisted_values is not None
    assert persisted_values[:5] == (
        1,
        2,
        3,
        "activity-day/v1",
        str(receipt.result.snapshot_ref),
    )
    assert datetime.fromisoformat(persisted_values[5]).tzinfo is not None
    assert [row[0] for row in persisted_segments] == [
        segment.kind.value for segment in day.coverage_segments
    ]


def test_activity_derivation_threshold_is_versioned_and_keeps_old_snapshot_readable(
    tmp_path: Path,
) -> None:
    xml = """
    <HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="1"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 08:10:00 +0100"
        startDate="2024-01-02 08:00:00 +0100" endDate="2024-01-02 08:10:00 +0100"/>
      <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="1"
        sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
        creationDate="2024-01-02 09:10:00 +0100"
        startDate="2024-01-02 09:00:00 +0100" endDate="2024-01-02 09:10:00 +0100"/>
    </HealthData>
    """
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        import_request = ImportHealthExport(_package(tmp_path / "settings.zip", xml))
        imported = health_lab.execute_write(
            import_request, expected_plan=health_lab.preview_write(import_request).fingerprint
        )
        old_snapshot = imported.result.snapshot_ref
        change = CreateActivityDerivationVersion(coverage_gap_minutes=30)
        receipt = health_lab.execute_write(
            change, expected_plan=health_lab.preview_write(change).fingerprint
        )
        old = health_lab.load_activity_days(SnapshotDateSelection(snapshot_ref=old_snapshot))
        current = health_lab.load_activity_days(SnapshotDateSelection())

    assert receipt.result.snapshot_ref != old_snapshot
    assert old.coverage_gap_minutes == 240
    assert current.coverage_gap_minutes == 30
    assert len(old.measurements) == len(current.measurements) == 2
