import fcntl
import json
import platform
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from personal_health_lab.adapters.cli import main as cli_main
from personal_health_lab.application import (
    DataMode,
    HealthLab,
    ImportHealthExport,
    RuntimeConfig,
)
from personal_health_lab.synthetic_export import GenerationOptions, generate_export


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

    next(button for button in app.button if button.label == "Ruhepulsanalyse prüfen").click().run()

    assert any(button.label == "Ruhepulsanalyse ausführen" for button in app.button)
    assert any("Gepinnter Snapshot: -" in item.value for item in app.caption)

    execute = next(
        button for button in app.button if button.label == "Ruhepulsanalyse ausführen"
    )
    execute.click().run()

    assert any("Analysestatus: insufficient_data" in item.value for item in app.warning)
    assert not app.get("vega_lite_chart")


def test_streamlit_focuses_migration_in_restricted_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "migration-store"
    config = RuntimeConfig(DataMode.SYNTHETIC, store, tmp_path / "real")
    fixture = generate_export("null-v1", 42, tmp_path / "migration-fixture")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        imported = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    snapshot_ref = str(imported.result.snapshot_ref)
    with sqlite3.connect(store / "metadata.sqlite3") as metadata:
        metadata.execute("UPDATE store_identity SET schema_version = 2 WHERE singleton = 1")
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()

    assert not app.exception
    assert any(item.value == "Datenspeichermigration" for item in app.subheader)
    assert any("Schema: 2 → 4" in item.value for item in app.caption)
    assert any("2 → 3, 3 → 4" in item.value for item in app.caption)
    assert any(f"Betroffene Snapshots: {snapshot_ref}" in item.value for item in app.caption)
    assert any("Bestehende Analysen werden stale: true" in item.value for item in app.caption)
    assert not app.file_uploader
    next(
        button for button in app.button if button.label == "Datenspeichermigration ausführen"
    ).click().run()

    assert not app.exception
    assert any(item.value == "Status: ready" for item in app.markdown)


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


def test_streamlit_plans_and_executes_metadata_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "private" / "metadata.sqlite3"
    target.parent.mkdir()
    monkeypatch.setenv("HEALTHLAB_MODE", "real")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(tmp_path / "synthetic"))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"
    app = AppTest.from_file(str(app_path)).run()

    next(
        item for item in app.text_input if item.label == "Zieldatei für Metadatensicherung"
    ).set_value(str(target))
    next(button for button in app.button if button.label == "Metadatensicherung prüfen").click()
    app.run()

    assert not app.exception
    assert any("Audit-Höchststand: 0" in item.value for item in app.caption)
    assert all(str(tmp_path) not in item.value for item in app.caption)
    next(button for button in app.button if button.label == "Metadatensicherung ausführen").click()
    app.run()

    assert target.is_file()
    assert any("Metadatensicherung: completed" in item.value for item in app.success)


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

    next(button for button in app.button if button.label == "Ruhepulsanalyse prüfen").click().run()

    assert any(button.label == "Ruhepulsanalyse ausführen" for button in app.button)
    assert any("Gepinnter Snapshot:" in item.value for item in app.caption)
    assert app.date_input[0].disabled
    assert app.date_input[1].disabled

    next(
        button
        for button in app.button
        if button.label == "Analysevorschau verwerfen und bearbeiten"
    ).click().run()

    assert not app.date_input[0].disabled
    assert not app.date_input[1].disabled
    next(button for button in app.button if button.label == "Ruhepulsanalyse prüfen").click().run()

    execute = next(
        button for button in app.button if button.label == "Ruhepulsanalyse ausführen"
    )
    execute.click().run(timeout=10)

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

    next(button for button in app.button if button.label == "Ruhepulsanalyse prüfen").click().run()
    execute = next(
        button for button in app.button if button.label == "Ruhepulsanalyse ausführen"
    )
    execute.click().run(timeout=10)

    assert any("Analysestatus: reused" in message.value for message in app.success)

    app.date_input[0].set_value(date(2024, 1, 1))
    app.date_input[1].set_value(date(2024, 6, 30))
    next(button for button in app.button if button.label == "Ruhepulsanalyse prüfen").click().run()
    execute = next(
        button for button in app.button if button.label == "Ruhepulsanalyse ausführen"
    )
    execute.click().run(timeout=10)

    assert not app.exception
    assert app.markdown[0].value == "Status: provisional"
    assert any("Letztes robustes Ergebnis" in item.value for item in app.warning)


def test_streamlit_maps_unstable_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = generate_export(
        "null-v1",
        42,
        tmp_path / "fixture",
        options=GenerationOptions(resting_heart_rate_noise_standard_deviation=0.0),
    )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=plan.fingerprint)
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(config.synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(config.real_store))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()
    next(button for button in app.button if button.label == "Ruhepulsanalyse prüfen").click().run()
    next(
        button for button in app.button if button.label == "Ruhepulsanalyse ausführen"
    ).click().run(timeout=10)

    assert not app.exception
    assert any("Analysestatus: unstable" in message.value for message in app.warning)


def test_streamlit_maps_store_busy_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synthetic_store = tmp_path / "synthetic"
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"
    app = AppTest.from_file(str(app_path)).run()
    next(button for button in app.button if button.label == "Ruhepulsanalyse prüfen").click().run()

    with (synthetic_store / ".writer.lock").open("a+b") as writer_lock:
        fcntl.flock(writer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        next(
            button for button in app.button if button.label == "Ruhepulsanalyse ausführen"
        ).click().run()

    assert not app.exception
    assert any("Analysestatus: store_busy" in message.value for message in app.warning)


def test_streamlit_maps_plan_changed_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = generate_export("null-v1", 42, tmp_path / "first")
    second = generate_export("null-v1", 43, tmp_path / "second")
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(first.export_path)
        plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=plan.fingerprint)
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(config.synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(config.real_store))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"
    app = AppTest.from_file(str(app_path)).run()
    next(button for button in app.button if button.label == "Ruhepulsanalyse prüfen").click().run()

    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(second.export_path)
        plan = health_lab.preview_write(request)
        health_lab.execute_write(request, expected_plan=plan.fingerprint)
    next(
        button for button in app.button if button.label == "Ruhepulsanalyse ausführen"
    ).click().run()

    assert not app.exception
    assert any("Analysestatus: plan_changed" in message.value for message in app.warning)


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


def test_streamlit_previews_the_application_materialized_batch(
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

    next(button for button in app.button if button.label == "Sammelbestätigung prüfen").click()
    app.run()

    assert not app.exception
    assert app.dataframe
    assert any(button.label == "Datenprüfentscheidung ausführen" for button in app.button)


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


def test_cli_and_streamlit_project_analysis_status_axes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = generate_export("lag-signal-v1", 42, tmp_path / "first")
    second = generate_export("lag-signal-v1", 43, tmp_path / "second")
    cli_store = tmp_path / "cli-synthetic"

    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(cli_store),
        "--real-store",
        str(tmp_path / "cli-real"),
    ]

    def execute_cli(command: list[str]) -> dict[str, object]:
        assert cli_main([*common, *command, "--json"]) == 0
        plan = json.loads(capsys.readouterr().out)
        assert cli_main(
            [
                *common,
                *command,
                "--json",
                "--execute",
                "--expect-plan",
                plan["fingerprint"],
            ]
        ) == 0
        return json.loads(capsys.readouterr().out)

    execute_cli(["import", str(first.export_path)])
    execute_cli(["review-confirm-batch", "--note", "geprüft"])
    execute_cli(["analyze"])
    assert cli_main([*common, "overview", "--json"]) == 0
    cli_current = json.loads(capsys.readouterr().out)["resting_hr_analysis"]
    assert cli_current["freshness"] == "current"
    assert cli_current["data_status"] == "reviewed"
    assert cli_current["model_maturity"] == "robust"
    assert cli_current["reproducibility"] in {"reproducible", "local_development"}

    execute_cli(["import", str(second.export_path)])
    assert cli_main([*common, "overview", "--json"]) == 0
    cli_overview = json.loads(capsys.readouterr().out)
    assert cli_overview["resting_hr_analysis"] is None
    assert cli_overview["analysis_history"][0]["freshness"] == "stale"
    assert cli_overview["analysis_history"][0]["data_status"] == "reviewed"

    streamlit_store = tmp_path / "streamlit-synthetic"
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(streamlit_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "streamlit-real"))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"
    app = AppTest.from_file(str(app_path)).run()

    app.file_uploader[0].set_value(
        ("apple-health-export.zip", first.export_path.read_bytes(), "application/zip")
    )
    next(button for button in app.button if button.label == "Health-Export prüfen").click().run()
    next(button for button in app.button if button.label == "Vorschau ausführen").click().run()
    next(
        button for button in app.button if button.label == "Sammelbestätigung prüfen"
    ).click().run()
    next(
        button for button in app.button if button.label == "Datenprüfentscheidung ausführen"
    ).click().run()
    app = AppTest.from_file(str(app_path)).run()
    next(button for button in app.button if button.label == "Ruhepulsanalyse prüfen").click().run()
    next(
        button for button in app.button if button.label == "Ruhepulsanalyse ausführen"
    ).click().run()

    assert not app.exception
    assert any(
        "current" in item.value
        and "Datenstatus: reviewed" in item.value
        and "Modellreife: robust" in item.value
        for item in app.caption
    )
