import fcntl
import json
import platform
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path
from zipfile import ZipFile

import pytest
from streamlit.testing.v1 import AppTest

from personal_health_lab.adapters.cli import main as cli_main
from personal_health_lab.application import (
    AnalysisDefinitionId,
    CreateMetadataBackup,
    DataMode,
    FeatureNotAvailableError,
    HealthLab,
    HealthLabError,
    ImportHealthExport,
    RunRestingHeartRateAnalysis,
    RuntimeConfig,
)
from personal_health_lab.synthetic_export import GenerationOptions, generate_export


@pytest.mark.v02_adapter(
    "streamlit",
    "ContextAudit",
    "ContextRecords",
    "DailyContext",
    "ManualContextRevisionPlan",
    "ManualContextRevisionReceipt",
    "MedicationRegimePlan",
    "MedicationRegimeReceipt",
    "MedicationDeviationPlan",
    "MedicationDeviationReceipt",
    "AsNeededIntakePlan",
    "AsNeededIntakeReceipt",
    "IntakeReasonCategoryPlan",
    "IntakeReasonCategoryReceipt",
    "IllnessRevisionPlan",
    "NoChangeStatus",
    "ReviseContextCoverageStart",
    "ReviseCustomContextLabel",
    "ReviseCustomContextPeriod",
    "ReviseDailyStress",
    "ReviseIllnessCategory",
    "ReviseIllnessPeriod",
    "ReviseMedicationRegime",
    "ReviseMedicationDeviation",
    "ReviseAsNeededIntake",
    "ReviseIntakeReasonCategory",
    "WriteNoChange",
)
def test_streamlit_loads_import_details_through_the_application_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xml = (Path(__file__).parents[1] / "fixtures/v03/unsupported-import-content.xml").read_text(
        encoding="utf-8"
    )
    package = tmp_path / "unsupported.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        receipt = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(config.synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(config.real_store))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()
    app.radio[0].set_value("Kerndaten").run()
    next(item for item in app.text_input if item.label == "Import-ID").set_value(
        str(receipt.result.import_id)
    )
    next(button for button in app.button if button.label == "Importdetails laden").click().run()

    assert not app.exception
    assert any(item.value == "Importdetails" for item in app.subheader)
    assert any("Kanonische Records im Paket: 1" in item.value for item in app.caption)
    assert app.dataframe


def test_streamlit_exposes_as_needed_write_lifecycles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    fixture = generate_export("null-v1", 42, tmp_path / "fixture")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(config.synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(config.real_store))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()
    app.radio[0].set_value("Kontext & Medikamente").run()

    actions = [item for item in app.selectbox if item.label.endswith("Aktion")]
    assert len(actions) == 2
    assert all(item.options == ["create", "revise", "withdraw", "restore"] for item in actions)
    next(item for item in app.text_input if item.label == "Name des Einnahmegrunds").set_value(
        "Kopfschmerz"
    )
    next(button for button in app.button if button.label == "Einnahmegrund prüfen").click().run()
    next(
        button
        for button in app.button
        if button.label == "Medikamentenschreibvorgang ausführen"
    ).click().run()

    with HealthLab.open(config) as health_lab:
        categories = health_lab.load_medication_plan().intake_reason_categories
    assert tuple(item.name for item in categories) == ("Kopfschmerz",)


@pytest.mark.v02_adapter("streamlit", "SleepObservationStatus")
def test_streamlit_loads_sleep_days_through_the_application_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "sleep.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            """
            <HealthData>
              <ExportDate value="2024-01-03 12:00:00 +0100"/>
              <Record type="HKCategoryTypeIdentifierSleepAnalysis"
                value="HKCategoryValueSleepAnalysisAsleepCore" sourceName="Apple Watch"
                sourceVersion="1" device="Apple Watch" creationDate="2024-01-03 06:00:00 +0100"
                startDate="2024-01-02 23:00:00 +0100" endDate="2024-01-03 06:00:00 +0100"/>
            </HealthData>
            """,
        )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(config.synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(config.real_store))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()
    app.radio[0].set_value("Kerndaten").run()
    next(button for button in app.button if button.label == "Schlaf laden").click().run()

    assert not app.exception
    assert any(item.value == "Schlaf" for item in app.subheader)
    assert app.dataframe


def test_streamlit_loads_activity_days_through_the_application_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "activity.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            """
            <HealthData>
              <ExportDate value="2024-01-03 12:00:00 +0100"/>
              <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="42"
                sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
                creationDate="2024-01-02 20:01:00 +0100"
                startDate="2024-01-02 20:00:00 +0100" endDate="2024-01-02 20:01:00 +0100"/>
            </HealthData>
            """,
        )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(config.synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(config.real_store))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()
    app.radio[0].set_value("Kerndaten").run()
    next(button for button in app.button if button.label == "Aktivität laden").click().run()

    assert not app.exception
    assert any(item.value == "Aktivität" for item in app.subheader)
    assert app.dataframe
    assert {
        "Logische Messung",
        "Quellaktualisierung",
        "Quellversion",
        "Disposition",
        "Ausgewählt",
    } <= set(app.dataframe[-1].value.columns)


@pytest.mark.v02_adapter("streamlit", "WeightDayStatus")
def test_streamlit_renders_the_complete_weight_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xml = (Path(__file__).parents[1] / "fixtures/v03/weight-edges.xml").read_text(encoding="utf-8")
    package = tmp_path / "weights.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(config.synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(config.real_store))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()
    app.radio[0].set_value("Kerndaten").run()
    next(item for item in app.date_input if item.label == "Gewicht von").set_value(date(2024, 1, 1))
    next(item for item in app.date_input if item.label == "Gewicht bis").set_value(date(2024, 1, 6))
    next(button for button in app.button if button.label == "Gewicht laden").click().run()

    assert not app.exception
    assert any(item.value == "Gewicht" for item in app.subheader)
    assert any("Status: provisional" in item.value for item in app.caption)
    daily = app.dataframe[0].value
    measurements = app.dataframe[1].value
    nutrition_days = app.dataframe[2].value
    nutrition_samples = app.dataframe[3].value
    assert daily["Status"].tolist() == [
        "observed",
        "missing",
        "observed",
        "observed",
        "ambiguous",
        "missing",
    ]
    assert len(measurements) == 7
    assert measurements["Originaleinheit"].tolist()[2] == "lb"
    assert nutrition_days["Energie (kcal)"].tolist()[0] == 100
    assert nutrition_days["Energie (kcal)"].isna().tolist()[1]
    assert nutrition_days["Protein (g)"].tolist()[0] == 20
    assert nutrition_days["Kohlenhydrate (g)"].tolist()[1] == 30
    assert nutrition_days["Gesamtfett (g)"].tolist()[2] == 10
    assert nutrition_days["Energie-Datentyp"].tolist()[0] == "dietary_energy_consumed"
    assert nutrition_days["Energie-Qualität"].tolist()[0] == "reviewed"
    assert nutrition_days["Energie-Logische Messungen"].tolist()[0]
    assert nutrition_days["Energie-Messungsversionen"].tolist()[0]
    assert len(nutrition_samples) == 5
    assert "dietary_biotin" in nutrition_samples["Datentyp"].tolist()


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

    execute = next(button for button in app.button if button.label == "Ruhepulsanalyse ausführen")
    execute.click().run()

    assert any("Analysestatus: insufficient_data" in item.value for item in app.warning)
    assert not app.get("vega_lite_chart")


@pytest.mark.v02_adapter(
    "streamlit",
    "MigrateStore",
    "RollbackMigration",
    "StoreMigrationPlan",
    "RollbackMigrationPlan",
    "StoreMigrationReceipt",
    "RollbackMigrationReceipt",
    "MigrationDiagnostics",
    "MigrationStatus",
)
def test_streamlit_focuses_migration_in_restricted_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = tmp_path / "migration-store"
    config = RuntimeConfig(DataMode.SYNTHETIC, store, tmp_path / "real")
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "migration-fixture")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(fixture.export_path)
        imported = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
        analysis = RunRestingHeartRateAnalysis(AnalysisDefinitionId("lag-signal-v2"))
        health_lab.execute_write(
            analysis, expected_plan=health_lab.preview_write(analysis).fingerprint
        )
    snapshot_ref = str(imported.result.snapshot_ref)
    with sqlite3.connect(store / "metadata.sqlite3") as metadata:
        metadata.execute("UPDATE store_identity SET schema_version = 2 WHERE singleton = 1")
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(store),
        "--real-store",
        str(tmp_path / "real"),
    ]
    assert cli_main([*common, "migrate", "--json"]) == 0
    cli_migration_plan = json.loads(capsys.readouterr().out)
    assert (
        cli_main(
            [
                *common,
                "migrate",
                "--json",
                "--execute",
                "--expect-plan",
                cli_migration_plan["fingerprint"],
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert cli_main([*common, "overview", "--json"]) == 0
    cli_overview = json.loads(capsys.readouterr().out)
    assert any(item["freshness"] == "stale" for item in cli_overview["analysis_history"])
    assert cli_main([*common, "rollback-migration", "--json"]) == 0
    cli_rollback_plan = json.loads(capsys.readouterr().out)
    assert (
        cli_main(
            [
                *common,
                "rollback-migration",
                "--json",
                "--execute",
                "--expect-plan",
                cli_rollback_plan["fingerprint"],
            ]
        )
        == 0
    )
    capsys.readouterr()
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()

    assert not app.exception
    assert any(item.value == "Datenspeichermigration" for item in app.subheader)
    assert any("Schema: 2 → 11" in item.value for item in app.caption)
    assert any("Snapshot-Schritte: -" in item.value for item in app.caption)
    assert any("Snapshot-Stichtag: " in item.value for item in app.caption)
    assert any("2 → 3, 3 → 4, 4 → 5, 5 → 6" in item.value for item in app.caption)
    assert any(f"Betroffene Snapshots: {snapshot_ref}" in item.value for item in app.caption)
    assert any("Bestehende Analysen werden stale: true" in item.value for item in app.caption)
    assert not app.file_uploader
    next(
        button for button in app.button if button.label == "Datenspeichermigration ausführen"
    ).click().run()

    assert not app.exception
    assert any(item.value == "Status: ready" for item in app.markdown)
    assert any("stale" in item.value for item in app.caption)
    assert any(item.value == "Migrationsrollback" for item in app.subheader)
    assert any(
        f"Wiederhergestellter Snapshot: {snapshot_ref}" in item.value for item in app.caption
    )
    next(button for button in app.button if button.label == "Letzte Migration zurückrollen").click()
    app.run()

    assert not app.exception
    assert any(item.value == "Datenspeichermigration" for item in app.subheader)


@pytest.mark.v02_adapter("streamlit", "ConfigurationError")
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


@pytest.mark.v02_adapter("streamlit", "FeatureNotAvailableError", "HealthLabError")
@pytest.mark.parametrize("error_type", (FeatureNotAvailableError, HealthLabError))
def test_streamlit_translates_application_errors(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[HealthLabError],
) -> None:
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setattr(
        HealthLab,
        "open",
        lambda _config: (_ for _ in ()).throw(error_type("unavailable")),
    )
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()

    assert not app.exception
    assert app.error[0].value == (
        "HealthLab-Konfiguration oder lokaler Datenspeicher ist ungültig."
    )


@pytest.mark.v02_adapter(
    "streamlit",
    "CreateMetadataBackup",
    "MetadataBackupPlan",
    "MetadataBackupReceipt",
    "MetadataBackupStatus",
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
    app.run(timeout=10)

    assert target.is_file()
    assert any("Metadatensicherung: completed" in item.value for item in app.success)


@pytest.mark.v02_adapter(
    "streamlit",
    "BeginMetadataRestore",
    "AbortMetadataRestore",
    "MetadataRestorePlan",
    "AbortMetadataRestorePlan",
    "MetadataRestoreReceipt",
    "MetadataRestoreStatus",
    "RecoveryStatus",
)
def test_streamlit_focuses_pending_metadata_restore_and_can_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = tmp_path / "private" / "metadata.sqlite3"
    backup.parent.mkdir()
    source_config = RuntimeConfig(
        DataMode.REAL, tmp_path / "source-synthetic", tmp_path / "source-real"
    )
    with HealthLab.open(source_config) as health_lab:
        request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    target = tmp_path / "target-real"
    monkeypatch.setenv("HEALTHLAB_MODE", "real")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(tmp_path / "target-synthetic"))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(target))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()
    next(
        item for item in app.text_input if item.label == "Metadatensicherung wiederherstellen"
    ).set_value(str(backup))
    next(button for button in app.button if button.label == "Wiederherstellung prüfen").click()
    app.run()

    assert not app.exception
    assert any("Sicherungs-ID:" in item.value for item in app.caption)
    next(button for button in app.button if button.label == "Wiederherstellung beginnen").click()
    app.run()

    assert not app.exception
    assert any(item.value == "Metadatenwiederherstellung" for item in app.subheader)
    assert any("Status: pending" in item.value for item in app.caption)
    assert any(item.label == "Apple-Health-Export" for item in app.file_uploader)
    assert all(button.label != "Ruhepulsanalyse prüfen" for button in app.button)
    next(button for button in app.button if button.label == "Wiederherstellung abbrechen").click()
    app.run()

    assert not app.exception
    assert not target.exists()
    assert any("Wiederherstellung: aborted" in item.value for item in app.success)


def test_streamlit_completes_restore_through_the_shared_import_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = generate_export("null-v1", 54, tmp_path / "fixture")
    backup = tmp_path / "private" / "metadata.sqlite3"
    backup.parent.mkdir()
    source_config = RuntimeConfig(
        DataMode.REAL, tmp_path / "source-synthetic", tmp_path / "source-real"
    )
    with HealthLab.open(source_config) as health_lab:
        import_request = ImportHealthExport(fixture.export_path)
        health_lab.execute_write(
            import_request,
            expected_plan=health_lab.preview_write(import_request).fingerprint,
        )
        backup_request = CreateMetadataBackup(backup)
        health_lab.execute_write(
            backup_request,
            expected_plan=health_lab.preview_write(backup_request).fingerprint,
        )
    target = tmp_path / "target-real"
    monkeypatch.setenv("HEALTHLAB_MODE", "real")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(tmp_path / "target-synthetic"))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(target))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()
    next(
        item for item in app.text_input if item.label == "Metadatensicherung wiederherstellen"
    ).set_value(str(backup))
    next(button for button in app.button if button.label == "Wiederherstellung prüfen").click()
    app.run()
    next(button for button in app.button if button.label == "Wiederherstellung beginnen").click()
    app.run()

    app.file_uploader[0].set_value(
        ("apple-health-export.zip", fixture.export_path.read_bytes(), "application/zip")
    )
    next(button for button in app.button if button.label == "Health-Export prüfen").click()
    app.run(timeout=10)
    assert not app.exception
    assert any("Methode restore-activate/v1" in item.value for item in app.caption)
    next(
        button
        for button in app.button
        if button.label in {"Vorschau ausführen", "Bestätigen und ausführen"}
    ).click()
    app.run(timeout=10)

    assert not app.exception
    assert any("Importstatus: committed" in item.value for item in app.success)
    assert any(item.value == "Status: ready" for item in app.markdown)


@pytest.mark.v02_adapter(
    "streamlit",
    "CreatePlausibilityRuleVersion",
    "PlausibilityRuleVersionPlan",
    "PlausibilityRuleVersionReceipt",
    "PlausibilityRules",
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


@pytest.mark.v02_adapter(
    "streamlit", "RunHistoricalReview", "HistoricalReviewPlan", "HistoricalReviewReceipt"
)
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


@pytest.mark.v02_adapter(
    "streamlit",
    "ImportHealthExport",
    "ImportHealthExportPlan",
    "ImportReceipt",
    "ImportStatus",
    "RunRestingHeartRateAnalysis",
    "RestingHeartRateAnalysisPlan",
    "AnalysisReceipt",
    "AnalysisStatus",
    "Overview",
    "OverviewStatus",
    "WorkspaceStatus",
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
    app.button[1].click().run(timeout=10)

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

    execute = next(button for button in app.button if button.label == "Ruhepulsanalyse ausführen")
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
    execute = next(button for button in app.button if button.label == "Ruhepulsanalyse ausführen")
    execute.click().run(timeout=10)

    assert any("Analysestatus: reused" in message.value for message in app.success)

    app.date_input[0].set_value(date(2024, 1, 1))
    app.date_input[1].set_value(date(2024, 6, 30))
    next(button for button in app.button if button.label == "Ruhepulsanalyse prüfen").click().run()
    execute = next(button for button in app.button if button.label == "Ruhepulsanalyse ausführen")
    execute.click().run(timeout=10)

    assert not app.exception
    assert app.markdown[0].value == "Status: provisional"
    assert any("Letztes robustes Ergebnis" in item.value for item in app.warning)


def test_streamlit_page_change_discards_pending_preview(
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

    assert any(item.value == "Schreibvorschau" for item in app.subheader)

    app.radio[0].set_value("Datenprüfung").run()

    assert not app.exception
    assert all(item.value != "Schreibvorschau" for item in app.subheader)


def test_streamlit_maps_unstable_analysis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.mark.v02_adapter("streamlit", "WriteNotStarted", "WriteNotStartedStatus")
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


@pytest.mark.v02_adapter(
    "streamlit",
    "CapacityReason",
    "CapacityStatus",
    "FileVaultReason",
    "FileVaultStatus",
    "PersonBindingStatus",
    "WriteApprovalStatus",
)
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


@pytest.mark.v02_adapter("streamlit", "DataReviewAction", "DataReviewCycleStatus")
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


@pytest.mark.v02_adapter("streamlit", "ReviewReasonCode")
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


@pytest.mark.v02_adapter("streamlit", "ConfirmDataReviewBatch", "DataReviewBatchPlan")
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
    app.run(timeout=10)

    assert not app.exception
    assert app.dataframe
    assert any(button.label == "Datenprüfentscheidung ausführen" for button in app.button)


@pytest.mark.v02_adapter(
    "streamlit",
    "ResolveDataReviewCase",
    "RevokeDataReviewDecision",
    "DataReviewDecisionPlan",
    "DataReviewBatchRevokePlan",
    "WriteDecisionReceipt",
    "WriteBatchDecisionReceipt",
    "DataReview",
    "DataReviewCaseDetail",
    "DataReviewAction",
    "DataReviewCycleStatus",
)
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


@pytest.mark.v02_adapter(
    "cli",
    "DataQualityStatus",
    "DataStatusReasonCode",
    "ModelMaturityStatus",
    "ReproducibilityStatus",
)
@pytest.mark.v02_adapter(
    "streamlit",
    "DataQualityStatus",
    "DataStatusReasonCode",
    "ModelMaturityStatus",
    "ReproducibilityStatus",
)
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
        assert (
            cli_main(
                [
                    *common,
                    *command,
                    "--json",
                    "--execute",
                    "--expect-plan",
                    plan["fingerprint"],
                ]
            )
            == 0
        )
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
    next(button for button in app.button if button.label == "Health-Export prüfen").click().run(
        timeout=10
    )
    next(button for button in app.button if button.label == "Vorschau ausführen").click().run(
        timeout=10
    )
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
