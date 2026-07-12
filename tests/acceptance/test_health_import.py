import os
import signal
import time
from datetime import UTC, date, datetime, timedelta
from multiprocessing import get_context
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from personal_health_lab.application import (
    DataMode,
    HealthLab,
    ImportStatus,
    OverviewSelection,
    OverviewStatus,
    RuntimeConfig,
)
from personal_health_lab.health_data import CanonicalHealthType, CanonicalUnit
from personal_health_lab.synthetic_export import generate_export


def _export(path: Path, records: str) -> Path:
    xml = f'<?xml version="1.0"?><HealthData>{records}</HealthData>'
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    return path


def _record(
    data_type: str,
    value: float,
    start: str,
    end: str,
    *,
    source_version: str = "1",
) -> str:
    unit = "kcal" if data_type.endswith("ActiveEnergyBurned") else "count/min"
    return (
        f'<Record type="{data_type}" sourceName="Test Watch" '
        f'sourceVersion="{source_version}" device="Test Device" unit="{unit}" '
        f'creationDate="{end}" startDate="{start}" endDate="{end}" '
        f'value="{value}"/>'
    )


def _import_in_process(config: RuntimeConfig, package_path: Path) -> None:
    while True:
        with HealthLab.open(config) as health_lab:
            receipt = health_lab.import_health_export(package_path)
        if receipt.status is not ImportStatus.STORE_BUSY:
            return


def _large_export(path: Path, count: int = 50_000) -> Path:
    start = datetime(2024, 1, 2, tzinfo=UTC)
    records = "".join(
        _record(
            "HKQuantityTypeIdentifierActiveEnergyBurned",
            1,
            (start + timedelta(seconds=index)).strftime("%Y-%m-%d %H:%M:%S %z"),
            (start + timedelta(seconds=index + 1)).strftime("%Y-%m-%d %H:%M:%S %z"),
        )
        for index in range(count)
    )
    return _export(path, records)


def test_valid_synthetic_export_is_published_as_daily_health_data(tmp_path: Path) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "synthetic-store",
        real_store=tmp_path / "real-store",
    )

    with HealthLab.open(config) as health_lab:
        receipt = health_lab.import_health_export(fixture.export_path)
        overview = health_lab.load_overview(OverviewSelection())

    assert receipt.status is ImportStatus.COMMITTED
    assert receipt.snapshot_ref is not None
    assert receipt.record_count == 1_825
    assert overview.status is OverviewStatus.READY
    assert {series.data_type for series in overview.daily_series} == {
        CanonicalHealthType.ACTIVE_ENERGY,
        CanonicalHealthType.APPLE_RESTING_HEART_RATE,
    }

    active_energy = next(
        series
        for series in overview.daily_series
        if series.data_type is CanonicalHealthType.ACTIVE_ENERGY
    )
    resting_heart_rate = next(
        series
        for series in overview.daily_series
        if series.data_type is CanonicalHealthType.APPLE_RESTING_HEART_RATE
    )
    assert active_energy.unit is CanonicalUnit.KILOCALORIE
    assert resting_heart_rate.unit is CanonicalUnit.BEATS_PER_MINUTE
    assert len(active_energy.values) == len(resting_heart_rate.values) == 365

    dst_value = next(value for value in active_energy.values if value.day == date(2024, 3, 31))
    new_york_value = next(
        value for value in resting_heart_rate.values if value.day == date(2024, 5, 6)
    )
    tokyo_value = next(
        value for value in resting_heart_rate.values if value.day == date(2024, 9, 16)
    )
    assert round(dst_value.value, 2) == 284.17
    assert {timestamp.utcoffset().total_seconds() for timestamp in dst_value.source_starts} == {
        7_200.0
    }
    assert new_york_value.value == 62.4
    assert new_york_value.source_starts[0].utcoffset().total_seconds() == -14_400.0
    assert tokyo_value.value == 62.68
    assert tokyo_value.source_starts[0].utcoffset().total_seconds() == 32_400.0
    assert new_york_value.source_names == ("HealthLab Synthetic Apple Watch",)


def test_cumulative_exports_are_idempotent_and_preserve_measurement_versions(
    tmp_path: Path,
) -> None:
    active_type = "HKQuantityTypeIdentifierActiveEnergyBurned"
    resting_type = "HKQuantityTypeIdentifierRestingHeartRate"
    active = _record(
        active_type,
        100,
        "2024-01-01 08:00:00 +0100",
        "2024-01-01 08:30:00 +0100",
    )
    resting_v1 = _record(
        resting_type,
        60,
        "2024-01-01 07:00:00 +0100",
        "2024-01-01 07:01:00 +0100",
    )
    resting_v2 = _record(
        resting_type,
        61,
        "2024-01-01 07:00:00 +0100",
        "2024-01-01 07:01:00 +0100",
        source_version="2",
    )
    new_active = _record(
        active_type,
        50,
        "2024-01-01 12:00:00 +0100",
        "2024-01-01 12:30:00 +0100",
    )
    base = _export(tmp_path / "base.zip", active + resting_v1)
    expanded = _export(tmp_path / "expanded.zip", active + resting_v2 + new_active)
    config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "synthetic-store",
        real_store=tmp_path / "real-store",
    )

    with HealthLab.open(config) as health_lab:
        first = health_lab.import_health_export(base)
        duplicate = health_lab.import_health_export(base)
        cumulative = health_lab.import_health_export(expanded)
        overview = health_lab.load_overview(OverviewSelection())

    assert first.status is ImportStatus.COMMITTED
    assert duplicate.status is ImportStatus.DUPLICATE
    assert duplicate.snapshot_ref == first.snapshot_ref
    assert duplicate.record_count == 0
    assert cumulative.status is ImportStatus.COMMITTED
    assert cumulative.snapshot_ref != first.snapshot_ref
    assert cumulative.record_count == 2
    assert cumulative.package_record_count == 3
    assert cumulative.logical_measurement_count == 3
    assert cumulative.measurement_version_count == 4
    assert overview.import_count == 3
    assert overview.package_count == 2
    assert overview.snapshot_count == 2
    assert overview.logical_measurement_count == 3
    assert overview.measurement_version_count == 4

    values = {series.data_type: series.values[0] for series in overview.daily_series}
    assert values[CanonicalHealthType.ACTIVE_ENERGY].value == 150
    preferred_resting = values[CanonicalHealthType.APPLE_RESTING_HEART_RATE]
    assert preferred_resting.value == 61
    assert preferred_resting.source_versions == ("2",)
    assert tuple(timestamp.isoformat() for timestamp in preferred_resting.source_updated_ats) == (
        "2024-01-01T06:01:00+00:00",
    )
    assert len(preferred_resting.measurement_version_ids) == 1


def test_interrupted_import_keeps_snapshot_and_is_quarantined_on_restart(
    tmp_path: Path,
) -> None:
    base = _export(
        tmp_path / "base.zip",
        _record(
            "HKQuantityTypeIdentifierRestingHeartRate",
            60,
            "2024-01-01 07:00:00 +0100",
            "2024-01-01 07:01:00 +0100",
        ),
    )
    interrupted = _large_export(tmp_path / "interrupted.zip")
    config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "synthetic-store",
        real_store=tmp_path / "real-store",
    )
    with HealthLab.open(config) as health_lab:
        committed = health_lab.import_health_export(base)
        before = health_lab.load_overview(OverviewSelection())

    process = get_context("spawn").Process(
        target=_import_in_process,
        args=(config, interrupted),
    )
    process.start()
    deadline = time.monotonic() + 10
    busy = None
    before_busy = None
    while time.monotonic() < deadline:
        with HealthLab.open(config) as health_lab:
            before_attempt = health_lab.load_overview(OverviewSelection())
            candidate = health_lab.import_health_export(base)
        if candidate.status is ImportStatus.STORE_BUSY:
            busy = candidate
            before_busy = before_attempt
            os.kill(process.pid, signal.SIGSTOP)
            break
        time.sleep(0.01)
    if busy is None and process.is_alive():
        process.kill()
        process.join(timeout=5)
    assert busy is not None
    assert before_busy is not None
    try:
        with HealthLab.open(config) as health_lab:
            during = health_lab.load_overview(OverviewSelection())
    finally:
        os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=5)
    assert not process.is_alive()
    assert busy.status is ImportStatus.STORE_BUSY
    assert during == before_busy

    with HealthLab.open(config) as health_lab:
        after = health_lab.load_overview(OverviewSelection())

    assert after.daily_series == before.daily_series
    assert after.import_count == before_busy.import_count
    assert after.snapshot_count == before_busy.snapshot_count
    assert after.quarantined_import_count == before_busy.quarantined_import_count + 1
    assert committed.snapshot_ref is not None
