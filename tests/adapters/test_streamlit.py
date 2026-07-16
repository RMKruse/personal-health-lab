import platform
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from personal_health_lab.application import DataMode, HealthLab, ImportHealthExport, RuntimeConfig
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
    assert any(item.value == "Status: empty" for item in app.markdown)
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


def test_streamlit_projects_plausibility_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(tmp_path / "synthetic"))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()

    assert not app.exception
    assert any(expander.label == "Plausibilitätsregeln" for expander in app.expander)
    assert any("apple_resting_heart_rate" in item.value for item in app.caption)


def test_streamlit_shows_the_pinned_historical_review_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    synthetic_store = tmp_path / "synthetic"
    config = RuntimeConfig(DataMode.SYNTHETIC, synthetic_store, tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        snapshot_ref = receipt.result.snapshot_ref

    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"
    app = AppTest.from_file(str(app_path)).run()

    next(item for item in app.date_input if item.label == "Historische Prüfung von").set_value(
        date(2024, 1, 1)
    )
    next(item for item in app.date_input if item.label == "Historische Prüfung bis").set_value(
        date(2024, 1, 31)
    )
    next(button for button in app.button if button.label == "Historische Prüfung planen").click()
    app.run()

    assert not app.exception
    assert any(
        f"Gepinnte Basis: {snapshot_ref}" in item.value
        and "Zeitraum 2024-01-01 bis 2024-01-31" in item.value
        for item in app.caption
    )
    assert any(button.label == "Historische Prüfung ausführen" for button in app.button)


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
    assert any(item.value == "Schreibvorschau" for item in app.subheader)
    assert any(item.value == "Status: empty" for item in app.markdown)
    assert len(app.code[0].value) == 64

    app.button[2].click().run()

    assert not app.exception
    assert all(item.value != "Schreibvorschau" for item in app.subheader)
    assert app.markdown[0].value == "Status: empty"

    app.file_uploader[0].set_value(
        ("apple-health-export.zip", fixture.export_path.read_bytes(), "application/zip")
    )
    app.button[0].click().run()
    app.button[1].click().run()

    assert not app.exception
    assert any("Importstatus: committed" in message.value for message in app.success)
    assert app.markdown[0].value == "Status: ready"
    assert [item.label for item in app.date_input] == [
        "Von",
        "Bis",
        "Historische Prüfung von",
        "Historische Prüfung bis",
    ]
    assert len(app.get("vega_lite_chart")) == 2

    app.button[1].click().run(timeout=10)

    assert not app.exception
    assert any("Analysestatus: completed" in message.value for message in app.success)
    assert [heading.value for heading in app.subheader] == [
        "Datenprüfung",
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


def test_streamlit_renders_the_shared_real_import_confirmation_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("HEALTHLAB_MODE", "real")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(tmp_path / "synthetic"))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()
    app.file_uploader[0].set_value(
        ("apple-health-export.zip", fixture.export_path.read_bytes(), "application/zip")
    )
    app.button[0].click().run()

    assert not app.exception
    values = [item.value for item in (*app.markdown, *app.caption, *app.warning)]
    assert any(
        "real_import_same_person" in value and "filevault_unknown" in value for value in values
    )
    assert any("FileVault: unknown" in value for value in values)
    assert any("Kapazität: ready" in value for value in values)
    assert any("full-snapshot-import/v1" in value for value in values)
    assert all(str(tmp_path) not in value for value in values)
    assert any("Datenspeicher-ID:" in value and "unbound" in value for value in values)
    assert any(button.label == "Bestätigen und ausführen" for button in app.button)


def test_streamlit_projects_source_conflicts_from_the_shared_data_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_conflict_package: Callable[[Path], Path],
) -> None:
    synthetic_store = tmp_path / "synthetic"
    real_store = tmp_path / "real"
    package = source_conflict_package(tmp_path / "conflict.zip")
    config = RuntimeConfig(DataMode.SYNTHETIC, synthetic_store, real_store)
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=plan.fingerprint)

    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(real_store))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()

    assert not app.exception
    assert any(item.value == "Datenprüfung" for item in app.subheader)
    assert any("Datenstatus: provisional" in item.value for item in app.caption)
    assert any(
        "source_conflict" in item.value
        and "Aktionen prefer, split" in item.value
        and "Kandidaten" in item.value
        for item in app.warning
    )
    assert any("Details:" in item.value for item in app.caption)


def test_streamlit_projects_the_personal_range_finding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    personal_range_package: Callable[[Path], Path],
) -> None:
    synthetic_store = tmp_path / "synthetic"
    config = RuntimeConfig(DataMode.SYNTHETIC, synthetic_store, tmp_path / "real")
    package = personal_range_package(tmp_path / "personal.zip")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=plan.fingerprint)

    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"
    app = AppTest.from_file(str(app_path)).run()

    assert not app.exception
    assert any("above_personal_upper_bound" in item.value for item in app.caption)


def test_streamlit_can_plan_a_data_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    personal_range_package: Callable[[Path], Path],
) -> None:
    synthetic_store = tmp_path / "synthetic"
    config = RuntimeConfig(DataMode.SYNTHETIC, synthetic_store, tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(personal_range_package(tmp_path / "personal.zip"))
        plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=plan.fingerprint)

    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"
    app = AppTest.from_file(str(app_path)).run()

    next(item for item in app.number_input if item.label == "Korrekturwert").set_value(61)
    next(item for item in app.text_input if item.label == "Korrekturgrund").set_value(
        "falscher Wert"
    )
    next(button for button in app.button if button.label == "Korrektur prüfen").click().run()

    assert not app.exception
    assert any(button.label == "Datenprüfentscheidung ausführen" for button in app.button)
