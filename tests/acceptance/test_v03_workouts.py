import json
from datetime import date
from pathlib import Path
from zipfile import ZipFile

import pytest

from personal_health_lab.adapters.cli import main
from personal_health_lab.application import (
    DataMode,
    HealthLab,
    ImportHealthExport,
    ResolveDataReviewCase,
    RuntimeConfig,
    SnapshotDateSelection,
    WorkoutCorrection,
)


def test_workouts_keep_types_durations_and_overlap_status(tmp_path: Path) -> None:
    package = tmp_path / "workouts.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            """
            <HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
              <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30"
                durationUnit="min" totalDistance="2" totalDistanceUnit="km"
                totalEnergyBurned="200" totalEnergyBurnedUnit="kcal" sourceName="Apple Watch"
                sourceVersion="1" device="Apple Watch" creationDate="2024-01-02 20:31:00 +0100"
                startDate="2024-01-02 20:00:00 +0100" endDate="2024-01-02 20:30:00 +0100"/>
              <Workout workoutActivityType="com.example.unknown" sourceName="iPhone"
                sourceVersion="1" device="iPhone" creationDate="2024-01-02 20:46:00 +0100"
                startDate="2024-01-02 20:15:00 +0100" endDate="2024-01-02 20:45:00 +0100"/>
            </HealthData>
            """,
        )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        projection = health_lab.load_workouts(
            SnapshotDateSelection(start_date=date(2024, 1, 2), end_date=date(2024, 1, 2))
        )

    assert [item.original_activity_type for item in projection.workouts] == [
        "HKWorkoutActivityTypeRunning",
        "com.example.unknown",
    ]
    assert projection.workouts[0].effective_duration_minutes == 30
    assert projection.workouts[1].effective_duration_minutes == 30
    assert projection.workouts[0].review_case_ids == projection.workouts[1].review_case_ids
    assert projection.status.value == "provisional"
    assert projection.aggregates[0].distance_kilometers == 2


def test_workout_correction_persists_through_data_review(tmp_path: Path) -> None:
    package = tmp_path / "workouts.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            """<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
            <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="90"
              durationUnit="min" sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
              creationDate="2024-01-02 20:31:00 +0100" startDate="2024-01-02 20:00:00 +0100"
              endDate="2024-01-02 20:30:00 +0100"/></HealthData>""",
        )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        workout = health_lab.load_workouts(SnapshotDateSelection()).workouts[0]
        correction = ResolveDataReviewCase(
            workout.review_case_ids[0],
            WorkoutCorrection(workout.workout_version_id, 30, None, None, "source typo"),
        )
        health_lab.execute_write(
            correction, expected_plan=health_lab.preview_write(correction).fingerprint
        )
        corrected = health_lab.load_workouts(SnapshotDateSelection())
        assert corrected.workouts[0].effective_duration_minutes == 30
        assert corrected.status.value == "reviewed"
        assert not corrected.workouts[0].review_case_ids


def test_workouts_cli_serializes_the_shared_projection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "workouts.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            """<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
            <Workout workoutActivityType="HKWorkoutActivityTypeYoga" sourceName="Apple Watch"
              sourceVersion="1" device="Apple Watch" creationDate="2024-01-02 21:01:00 +0100"
              startDate="2024-01-02 20:00:00 +0100" endDate="2024-01-02 21:00:00 +0100"/>
            </HealthData>""",
        )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    args = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(config.synthetic_store),
        "--real-store",
        str(config.real_store),
        "workouts",
        "--json",
    ]

    assert main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["kind"] == "workouts"
    assert output["workouts"][0]["effective_duration_minutes"] == 60
