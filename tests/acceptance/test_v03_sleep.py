import json
from datetime import date, timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from personal_health_lab.adapters.cli._cli import main
from personal_health_lab.application import (
    DataMode,
    HealthLab,
    ImportHealthExport,
    RuntimeConfig,
    SleepCategory,
    SleepObservationStatus,
    SleepSourceClass,
    SnapshotDateSelection,
    UnsupportedContentCategory,
)


def _package(path: Path, xml: str) -> Path:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    return path


def _record(
    category: str,
    start: str,
    end: str,
    source_name: str = "Apple Watch",
    device: str = "Apple Watch",
) -> str:
    return (
        f'<Record type="HKCategoryTypeIdentifierSleepAnalysis" value="{category}" '
        f'sourceName="{source_name}" sourceVersion="1" device="{device}" '
        f'creationDate="{end}" startDate="{start}" endDate="{end}"/>'
    )


def test_sleep_import_preserves_categories_offsets_overlap_and_source_eligibility(
    tmp_path: Path,
) -> None:
    xml = (Path(__file__).parents[1] / "fixtures/v03/sleep-edges.xml").read_text()
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(_package(tmp_path / "sleep.zip", xml))
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        sleep = health_lab.load_sleep_days(
            SnapshotDateSelection(start_date=date(2024, 1, 2), end_date=date(2024, 1, 4))
        )

    assert len(sleep.accepted_intervals) == 5
    assert {item.source_class for item in sleep.rejected_intervals} == {
        SleepSourceClass.IPHONE,
        SleepSourceClass.MANUAL,
    }
    deprecated = next(
        item
        for item in sleep.accepted_intervals
        if item.original_category == "HKCategoryValueSleepAnalysisAsleep"
    )
    assert deprecated.canonical_category is SleepCategory.ASLEEP_UNSPECIFIED
    assert deprecated.source_start.utcoffset() == timedelta(hours=1)
    assert deprecated.source_end.utcoffset() == timedelta(hours=1)
    day = sleep.days[1]
    assert day.day == date(2024, 1, 3)
    assert day.status is SleepObservationStatus.PARTIAL
    assert day.primary_episode is not None
    assert day.primary_episode.stage_ambiguous == timedelta(minutes=30)
    assert day.primary_episode.asleep_awake_conflict == timedelta(minutes=30)
    assert day.quality.accepted_interval_count == 5
    assert day.quality.rejected_interval_count == 2
    assert [item.rejected_interval_count for item in day.quality.source_counts] == [
        0,
        1,
        1,
        0,
        0,
    ]
    assert sleep.days[0].status is SleepObservationStatus.UNOBSERVED
    assert sleep.days[2].status is SleepObservationStatus.UNOBSERVED


@pytest.mark.v02_adapter("cli", "SleepObservationStatus")
def test_sleep_days_cli_uses_the_shared_selection(tmp_path: Path, capsys) -> None:
    xml = "".join(
        (
            '<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>',
            _record(
                "HKCategoryValueSleepAnalysisAsleepREM",
                "2024-01-02 23:00:00 +0100",
                "2024-01-03 06:00:00 +0100",
            ),
            "</HealthData>",
        )
    )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(_package(tmp_path / "sleep.zip", xml))
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
        "sleep-days",
        "--start-date",
        "2024-01-03",
        "--end-date",
        "2024-01-03",
    ]
    assert main(arguments) == 0
    assert "Schlafnächte" in capsys.readouterr().out
    assert main([*arguments, "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["kind"] == "sleep_days"
    assert output["selection"]["start_date"] == "2024-01-03"
    assert output["days"][0]["primary_episode"]["observed_sleep_seconds"] == 25200


def test_sleep_reimport_versions_stages_and_catalogs_invalid_forms(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    original = "".join(
        (
            '<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>',
            _record(
                "HKCategoryValueSleepAnalysisAsleep",
                "2024-01-02 23:00:00 +0100",
                "2024-01-03 06:00:00 +0100",
            ),
            "</HealthData>",
        )
    )
    corrected = original.replace(
        "HKCategoryValueSleepAnalysisAsleep",
        "HKCategoryValueSleepAnalysisAsleepCore",
    ).replace("2024-01-03 06:00:00 +0100\" start", "2024-01-03 07:00:00 +0100\" start")
    invalid = "".join(
        (
            '<HealthData><Record type="HKCategoryTypeIdentifierSleepAnalysis" '
            'value="HKCategoryValueSleepAnalysisDreaming" sourceName="" sourceVersion="" '
            'device="" creationDate="2024-01-03 07:00:00 +0100" '
            'startDate="2024-01-02 23:00:00 +0100" endDate="2024-01-03 06:00:00 +0100"/>',
            '<Record type="HKCategoryTypeIdentifierSleepAnalysis" '
            'value="HKCategoryValueSleepAnalysisAsleepREM" unit="count" sourceName="" '
            'sourceVersion="" device="" creationDate="2024-01-03 07:00:00 +0100" '
            'startDate="2024-01-02 23:00:00 +0100" endDate="2024-01-03 06:00:00 +0100"/>',
            "</HealthData>",
        )
    )
    with HealthLab.open(config) as health_lab:
        first = health_lab.execute_write(
            request := ImportHealthExport(_package(tmp_path / "first.zip", original)),
            expected_plan=health_lab.preview_write(request).fingerprint,
        )
        second = health_lab.execute_write(
            request := ImportHealthExport(_package(tmp_path / "second.zip", corrected)),
            expected_plan=health_lab.preview_write(request).fingerprint,
        )
        duplicate = health_lab.execute_write(
            request := ImportHealthExport(tmp_path / "second.zip"),
            expected_plan=health_lab.preview_write(request).fingerprint,
        )
        invalid_receipt = health_lab.execute_write(
            request := ImportHealthExport(_package(tmp_path / "invalid.zip", invalid)),
            expected_plan=health_lab.preview_write(request).fingerprint,
        )
        sleep = health_lab.load_sleep_days(
            SnapshotDateSelection(start_date=date(2024, 1, 3), end_date=date(2024, 1, 3))
        )
        details = health_lab.load_import_details(invalid_receipt.result.import_id)

    assert first.result.logical_measurement_count == 1
    assert second.result.logical_measurement_count == 1
    assert duplicate.result.record_count == 0
    assert duplicate.result.measurement_version_count == 2
    assert len(sleep.accepted_intervals) == 2
    assert [item.canonical_category for item in sleep.accepted_intervals if item.is_selected] == [
        SleepCategory.ASLEEP_CORE
    ]
    assert {item.category for item in details.unsupported_content} == {
        UnsupportedContentCategory.SLEEP_VALUE,
        UnsupportedContentCategory.UNIT,
    }
