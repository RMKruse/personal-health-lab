from pathlib import Path

from personal_health_lab.application import (
    DataMode,
    HealthLab,
    OverviewSelection,
    OverviewStatus,
    RuntimeConfig,
)


def test_fresh_store_loads_a_typed_empty_overview(tmp_path: Path) -> None:
    config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "synthetic",
        real_store=tmp_path / "real",
    )

    with HealthLab.open(config) as health_lab:
        overview = health_lab.load_overview(OverviewSelection())

    assert overview.status is OverviewStatus.EMPTY
    assert overview.schema_version == "1.0"
    assert overview.message == "Keine Gesundheitsdaten vorhanden."
