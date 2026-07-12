from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from personal_health_lab.application import DataMode, HealthLab, RuntimeConfig
from personal_health_lab.synthetic_export import generate_export


def test_streamlit_shows_the_same_empty_overview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(tmp_path / "synthetic"))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    app_path = (
        Path(__file__).parents[2]
        / "src/personal_health_lab/adapters/streamlit/app.py"
    )

    app = AppTest.from_file(str(app_path)).run()

    assert not app.exception
    assert app.title[0].value == "HealthLab Übersicht"
    assert app.markdown[0].value == "Status: empty"
    assert app.info[0].value == "Keine Gesundheitsdaten vorhanden."


def test_streamlit_translates_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEALTHLAB_MODE", "mixed")
    app_path = (
        Path(__file__).parents[2]
        / "src/personal_health_lab/adapters/streamlit/app.py"
    )

    app = AppTest.from_file(str(app_path)).run()

    assert not app.exception
    assert app.error[0].value == (
        "HealthLab-Konfiguration oder lokaler Datenspeicher ist ungültig."
    )


def test_streamlit_shows_imported_daily_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "synthetic",
        real_store=tmp_path / "real",
    )
    with HealthLab.open(config) as health_lab:
        health_lab.import_health_export(fixture.export_path)
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(config.synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(config.real_store))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()

    assert not app.exception
    assert [heading.value for heading in app.subheader] == [
        "Aktive Energie (kcal)",
        "Apple-Ruhepuls (count/min)",
    ]
    assert len(app.get("vega_lite_chart")) == 2
