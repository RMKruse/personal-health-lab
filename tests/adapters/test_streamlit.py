from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from personal_health_lab.synthetic_export import generate_export


def test_streamlit_shows_the_same_empty_overview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(tmp_path / "synthetic"))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()

    assert not app.exception
    assert app.title[0].value == "HealthLab Übersicht"
    assert app.markdown[0].value == "Status: empty"
    assert app.info[0].value == "Keine Gesundheitsdaten vorhanden."

    app.button[1].click().run()

    assert any("Analysestatus: insufficient_data" in item.value for item in app.warning)
    assert not app.get("vega_lite_chart")


def test_streamlit_translates_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEALTHLAB_MODE", "mixed")
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()

    assert not app.exception
    assert app.error[0].value == (
        "HealthLab-Konfiguration oder lokaler Datenspeicher ist ungültig."
    )


def test_streamlit_shows_imported_daily_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(tmp_path / "synthetic"))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()
    app.file_uploader[0].set_value(
        ("apple-health-export.zip", fixture.export_path.read_bytes(), "application/zip")
    )
    app.button[0].click().run()

    assert not app.exception
    assert any("Importstatus: committed" in message.value for message in app.success)
    assert app.markdown[0].value == "Status: ready"
    assert [item.label for item in app.date_input] == ["Von", "Bis"]
    assert len(app.get("vega_lite_chart")) == 2

    app.button[1].click().run(timeout=10)

    assert not app.exception
    assert any("Analysestatus: completed" in message.value for message in app.success)
    assert [heading.value for heading in app.subheader] == [
        "Aktive Energie (kcal)",
        "Apple-Ruhepuls (count/min)",
        "Verzögerungsprofil",
    ]
    assert len(app.get("vega_lite_chart")) == 3
    assert app.metric[0].label == "Kumulativer Zusammenhang je 100 kcal"
    assert any("Dunkelblau: punktweises Intervall" in caption.value for caption in app.caption)
    assert any("Modellreife: robust" in caption.value for caption in app.caption)
    assert any("Run " in caption.value and "Ergebnis " in caption.value for caption in app.caption)
    assert any(
        "Konfiguration " in caption.value and "Environment " in caption.value
        for caption in app.caption
    )
    assert any("Commit " in caption.value and "Diff " in caption.value for caption in app.caption)
    assert any("Moving-Block-Bootstrap" in caption.value for caption in app.caption)

    app.date_input[0].set_value(date(2024, 1, 1))
    app.date_input[1].set_value(date(2024, 6, 30))
    app.button[1].click().run(timeout=10)

    assert not app.exception
    assert app.markdown[0].value == "Status: provisional"
    assert any("Letztes belastbares Ergebnis" in item.value for item in app.warning)
