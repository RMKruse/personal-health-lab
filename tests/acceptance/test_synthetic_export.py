import json
from pathlib import Path
from statistics import correlation
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

from personal_health_lab.application import DataMode, HealthLab, RuntimeConfig
from personal_health_lab.synthetic_export import GenerationOptions, generate_export


def test_same_scenario_version_and_seed_generate_identical_fixture(tmp_path: Path) -> None:
    first = generate_export("lag-signal-v1", 20260712, tmp_path / "first")
    second = generate_export("lag-signal-v1", 20260712, tmp_path / "second")

    assert first.export_path.read_bytes() == second.export_path.read_bytes()
    assert first.metadata_path.read_bytes() == second.metadata_path.read_bytes()
    assert first.checksums_path.read_bytes() == second.checksums_path.read_bytes()
    assert json.loads(first.metadata_path.read_text(encoding="utf-8"))["schema_version"] == "1.0"


@pytest.mark.parametrize(
    ("scenario_id", "expected_lag_days"),
    [("lag-signal-v1", 1), ("null-v1", None)],
)
def test_scenarios_contain_365_measurement_local_days_and_supported_healthkit_types(
    scenario_id: str,
    expected_lag_days: int | None,
    tmp_path: Path,
) -> None:
    fixture = generate_export(scenario_id, 42, tmp_path / scenario_id)

    with ZipFile(fixture.export_path) as archive:
        root = ElementTree.fromstring(archive.read("apple_health_export/export.xml"))
    records = root.findall("Record")
    records_by_type = {
        data_type: [record for record in records if record.attrib["type"] == data_type]
        for data_type in {
            "HKQuantityTypeIdentifierActiveEnergyBurned",
            "HKQuantityTypeIdentifierRestingHeartRate",
        }
    }
    local_days = {record.attrib["startDate"][:10] for record in records}
    metadata = json.loads(fixture.metadata_path.read_text(encoding="utf-8"))

    assert len(local_days) == 365
    assert len(records_by_type["HKQuantityTypeIdentifierActiveEnergyBurned"]) == 4 * 365
    assert len(records_by_type["HKQuantityTypeIdentifierRestingHeartRate"]) == 365
    assert metadata["signal"]["expected_lag_days"] == expected_lag_days


def test_noise_and_missing_values_are_configurable_and_recorded(tmp_path: Path) -> None:
    fixture = generate_export(
        "null-v1",
        73,
        tmp_path,
        options=GenerationOptions(
            noise_standard_deviation=0.0,
            missing_active_energy_probability=0.5,
        ),
    )

    with ZipFile(fixture.export_path) as archive:
        root = ElementTree.fromstring(archive.read("apple_health_export/export.xml"))
    active_records = [
        record
        for record in root.findall("Record")
        if record.attrib["type"] == "HKQuantityTypeIdentifierActiveEnergyBurned"
    ]
    resting_values = [
        record.attrib["value"]
        for record in root.findall("Record")
        if record.attrib["type"] == "HKQuantityTypeIdentifierRestingHeartRate"
    ]
    metadata = json.loads(fixture.metadata_path.read_text(encoding="utf-8"))

    assert 0 < len(active_records) < 4 * 365
    assert set(resting_values) == {"62.00"}
    assert metadata["generation_options"] == {
        "missing_active_energy_probability": 0.5,
        "missing_resting_heart_rate_probability": 0.0,
        "noise_standard_deviation": 0.0,
    }


def test_export_contains_berlin_dst_and_travel_timezone_fixtures(tmp_path: Path) -> None:
    fixture = generate_export("null-v1", 42, tmp_path)

    with ZipFile(fixture.export_path) as archive:
        root = ElementTree.fromstring(archive.read("apple_health_export/export.xml"))
    resting_records = {
        record.attrib["startDate"][:10]: record.attrib["startDate"][-5:]
        for record in root.findall("Record")
        if record.attrib["type"] == "HKQuantityTypeIdentifierRestingHeartRate"
    }

    assert resting_records | {
        "2024-03-30": "+0100",
        "2024-04-01": "+0200",
        "2024-05-06": "-0400",
        "2024-09-16": "+0900",
        "2024-10-26": "+0200",
        "2024-10-28": "+0100",
    } == resting_records


def _lag_one_correlation(export_path: Path) -> float:
    with ZipFile(export_path) as archive:
        root = ElementTree.fromstring(archive.read("apple_health_export/export.xml"))
    active_by_day: dict[str, float] = {}
    resting_by_day: dict[str, float] = {}
    for record in root.findall("Record"):
        local_day = record.attrib["startDate"][:10]
        if record.attrib["type"] == "HKQuantityTypeIdentifierActiveEnergyBurned":
            active_by_day[local_day] = active_by_day.get(local_day, 0.0) + float(
                record.attrib["value"]
            )
        elif record.attrib["type"] == "HKQuantityTypeIdentifierRestingHeartRate":
            resting_by_day[local_day] = float(record.attrib["value"])
    local_days = sorted(active_by_day)
    return correlation(
        [active_by_day[day] for day in local_days[:-1]],
        [resting_by_day[day] for day in local_days[1:]],
    )


def test_lag_signal_is_measurable_and_null_scenario_has_no_built_in_signal(tmp_path: Path) -> None:
    signal = generate_export("lag-signal-v1", 42, tmp_path / "signal")
    null = generate_export("null-v1", 42, tmp_path / "null")

    assert _lag_one_correlation(signal.export_path) < -0.8
    assert abs(_lag_one_correlation(null.export_path)) < 0.15


def test_generator_refuses_to_write_inside_a_healthlab_store(tmp_path: Path) -> None:
    real_store = tmp_path / "real"
    config = RuntimeConfig(
        mode=DataMode.REAL,
        synthetic_store=tmp_path / "synthetic",
        real_store=real_store,
    )
    with HealthLab.open(config):
        pass

    with pytest.raises(ValueError, match="Datenspeicher"):
        generate_export("null-v1", 42, real_store / "fixture")

    assert not (real_store / "fixture").exists()
