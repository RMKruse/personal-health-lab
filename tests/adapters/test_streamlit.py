from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


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
