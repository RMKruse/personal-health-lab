from datetime import date
from pathlib import Path

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
