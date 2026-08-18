import fcntl
import json
import platform
import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from streamlit.testing.v1 import AppTest

from personal_health_lab.adapters.cli import main as cli_main
from personal_health_lab.application import (
    ActivityDays,
    ActivitySettings,
    AnalysisDefinitionId,
    ContextAudit,
    ContextCoverageStartCreate,
    ContextLogicalId,
    ContextRecords,
    CreateMetadataBackup,
    DailyContext,
    DataMode,
    FeatureNotAvailableError,
    HealthLab,
    HealthLabError,
    IllnessCategoryCreate,
    IllnessCategoryWithdraw,
    ImportDetails,
    ImportHealthExport,
    ImportId,
    IntakeReasonCategoryCreate,
    IntakeReasonCategoryWithdraw,
    LocalWorkoutExclusion,
    ManualContextRevisionReceipt,
    MedicationAudit,
    MedicationDays,
    MedicationLogicalId,
    MedicationPlan,
    ResolveDataReviewCase,
    ReviseContextCoverageStart,
    ReviseIllnessCategory,
    ReviseIntakeReasonCategory,
    RunRestingHeartRateAnalysis,
    RuntimeConfig,
    SleepDays,
    SnapshotDateSelection,
    SnapshotSelection,
    WeightNutrition,
    WorkoutCorrection,
    Workouts,
    WriteNotStarted,
    WriteNotStartedStatus,
    WritePlan,
    WriteRequest,
)
from personal_health_lab.synthetic_export import GenerationOptions, generate_export


def test_streamlit_uses_typed_v03_forms_without_raw_request_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(tmp_path / "synthetic"))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"
    app = AppTest.from_file(str(app_path)).run()
    app.radio[0].set_value("Kontext & Medikamente").run()

    assert all(item.label != "JSON-3.0-Schreibauftrag" for item in app.text_area)
    family = next(item for item in app.selectbox if item.label == "Schreibauftragsfamilie")
    for request_type, _route, _fields in _V03_WRITES:
        family.set_value(request_type).run()
        labels = {
            item.label
            for item in (*app.selectbox, *app.text_input, *app.date_input, *app.number_input)
        }
        assert labels & {
            "Revisionsabsicht",
            "Abdeckungsschwelle (Minuten)",
            "Abdeckungsbeginn",
            "Name",
            "Tag",
            "Beginn",
            "Regimebeginn (ISO 8601)",
            "Geplanter Zeitpunkt (ISO 8601)",
            "Einnahmezeitpunkt (ISO 8601)",
        }


def test_streamlit_keeps_busy_v03_preview_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(tmp_path / "synthetic"))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    package = tmp_path / "seed.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            """<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
            <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="1"
            sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
            creationDate="2024-01-02 12:00:00 +0100"
            startDate="2024-01-02 12:00:00 +0100"
            endDate="2024-01-02 12:01:00 +0100"/></HealthData>""",
        )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )

    def busy(*_args: object, **_kwargs: object) -> object:
        return type("Receipt", (), {"result": WriteNotStarted(WriteNotStartedStatus.STORE_BUSY)})()

    monkeypatch.setattr(HealthLab, "execute_write", busy)
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"
    app = AppTest.from_file(str(app_path)).run()
    app.radio[0].set_value("Kontext & Medikamente").run()
    next(
        button for button in app.button if button.label == "V0.3-Schreibauftrag prüfen"
    ).click().run()
    next(
        button for button in app.button if button.label == "V0.3-Schreibauftrag ausführen"
    ).click().run()

    assert not app.exception
    assert any("Datenspeicher ist belegt" in item.value for item in app.warning)
    assert any(button.label == "V0.3-Schreibauftrag ausführen" for button in app.button)


@pytest.mark.parametrize("root_kind", ("context", "medication"))
def test_streamlit_restores_withdrawn_roots_from_historical_audit(
    root_kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "seed.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            """<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
            <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="1"
            sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
            creationDate="2024-01-02 12:00:00 +0100"
            startDate="2024-01-02 12:00:00 +0100"
            endDate="2024-01-02 12:01:00 +0100"/></HealthData>""",
        )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(package)
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        if root_kind == "context":
            created_request = ReviseIllnessCategory(IllnessCategoryCreate("Infekt"))
        else:
            created_request = ReviseIntakeReasonCategory(IntakeReasonCategoryCreate("Schmerz"))
        created = health_lab.execute_write(
            created_request,
            expected_plan=health_lab.preview_write(created_request).fingerprint,
        ).result
        historical_snapshot = created.snapshot_ref
        if root_kind == "context":
            withdrawn_request = ReviseIllnessCategory(
                IllnessCategoryWithdraw(created.logical_id, created.revision_id, "Korrektur")
            )
        else:
            withdrawn_request = ReviseIntakeReasonCategory(
                IntakeReasonCategoryWithdraw(created.logical_id, created.revision_id, "Korrektur")
            )
        withdrawn = health_lab.execute_write(
            withdrawn_request,
            expected_plan=health_lab.preview_write(withdrawn_request).fingerprint,
        ).result

    observed: list[WriteRequest] = []
    original_preview = HealthLab.preview_write

    def capture(health_lab: HealthLab, request: WriteRequest) -> WritePlan:
        observed.append(request)
        return original_preview(health_lab, request)

    monkeypatch.setattr(HealthLab, "preview_write", capture)
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(config.synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(config.real_store))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"
    app = AppTest.from_file(str(app_path)).run()
    app.radio[0].set_value("Kontext & Medikamente").run()
    if root_kind == "context":
        next(
            item for item in app.text_input if item.label == "Kontext-Snapshot-ID (optional)"
        ).set_value(str(historical_snapshot))
        next(button for button in app.button if button.label == "Kontext laden").click().run()
        next(button for button in app.button if button.label == "Kontext-Audit laden").click().run()
        request_type = "revise_illness_category"
    else:
        next(
            item for item in app.text_input if item.label == "Medikamenten-Snapshot-ID (optional)"
        ).set_value(str(historical_snapshot))
        next(
            button for button in app.button if button.label == "Medikamentenplan laden"
        ).click().run()
        next(
            button for button in app.button if button.label == "Medikamentenaudit laden"
        ).click().run()
        request_type = "revise_intake_reason_category"
    next(item for item in app.selectbox if item.label == "Schreibauftragsfamilie").set_value(
        request_type
    ).run()
    next(item for item in app.selectbox if item.label == "Revisionsabsicht").set_value(
        "restore"
    ).run()
    next(item for item in app.text_input if item.label == "Name").set_value("Wieder aktiv")
    next(
        button for button in app.button if button.label == "V0.3-Schreibauftrag prüfen"
    ).click().run()

    request_class = ReviseIllnessCategory if root_kind == "context" else ReviseIntakeReasonCategory
    restored = next(item for item in reversed(observed) if isinstance(item, request_class))
    assert not app.exception
    assert restored.intent.logical_id == withdrawn.logical_id
    assert restored.intent.expected_revision_id == withdrawn.revision_id


@pytest.mark.v03_adapter("cli", "WorkoutCorrection", "LocalWorkoutExclusion")
@pytest.mark.v03_adapter("streamlit", "WorkoutCorrection", "LocalWorkoutExclusion")
@pytest.mark.parametrize("resolution_kind", ("correction", "exclusion"))
def test_cli_and_streamlit_build_the_same_workout_resolution(
    resolution_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = tmp_path / "workout.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            """<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
            <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="90"
            durationUnit="min" sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
            creationDate="2024-01-02 20:31:00 +0100" startDate="2024-01-02 20:00:00 +0100"
            endDate="2024-01-02 20:30:00 +0100"/></HealthData>""",
        )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(package)
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        workout = health_lab.load_workouts(SnapshotDateSelection()).workouts[0]
    observed: list[ResolveDataReviewCase] = []
    original_preview = HealthLab.preview_write

    def capture_preview(health_lab: HealthLab, request: WriteRequest) -> WritePlan:
        if isinstance(request, ResolveDataReviewCase):
            observed.append(request)
        return original_preview(health_lab, request)

    monkeypatch.setattr(HealthLab, "preview_write", capture_preview)
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(config.synthetic_store),
        "--real-store",
        str(config.real_store),
        "review-resolve",
        str(workout.review_case_ids[0]),
        "--measurement-version",
        str(workout.workout_version_id),
        "--reason",
        "source typo",
        "--json",
    ]
    command = (
        [*common, "--workout-correction", "--correct", "30"]
        if resolution_kind == "correction"
        else [*common, "--workout-exclusion"]
    )
    assert cli_main(command) == 0
    capsys.readouterr()
    cli_request = observed[-1]

    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(config.synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(config.real_store))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"
    app = AppTest.from_file(str(app_path)).run()
    next(item for item in app.text_input if item.label == "Korrekturgrund").set_value("source typo")
    if resolution_kind == "correction":
        next(
            item for item in app.number_input if item.label == "Wirksame Dauer (Minuten)"
        ).set_value(30)
        next(
            button for button in app.button if button.label == "Training korrigieren"
        ).click().run()
    else:
        next(
            button for button in app.button if button.label == "Training lokal ausschließen"
        ).click().run()
    streamlit_request = observed[-1]

    assert not app.exception
    assert type(streamlit_request.resolution) is type(cli_request.resolution)
    assert streamlit_request == cli_request
    assert isinstance(streamlit_request.resolution, WorkoutCorrection | LocalWorkoutExclusion)


_V03_WRITES = (
    (
        "revise_context_coverage_start",
        ("context", "coverage-start", "create"),
        {"start_date": "2024-01-01"},
    ),
    (
        "revise_illness_category",
        ("context", "illness-category", "create"),
        {"name": "Erkältung"},
    ),
    (
        "revise_custom_context_label",
        ("context", "custom-label", "create"),
        {"name": "Reise"},
    ),
    (
        "revise_illness_period",
        ("context", "illness-period", "create"),
        {
            "category_logical_id": "a" * 32,
            "start_date": "2024-01-01",
            "end_date": None,
            "severity": "mild",
        },
    ),
    (
        "revise_daily_stress",
        ("context", "daily-stress", "create"),
        {"day": "2024-01-01", "level": "average"},
    ),
    (
        "revise_custom_context_period",
        ("context", "custom-period", "create"),
        {
            "label_logical_id": "a" * 32,
            "start_date": "2024-01-01",
            "end_date": None,
            "note": None,
        },
    ),
    (
        "revise_medication_regime",
        ("medication", "regime", "create"),
        {
            "starts_at": "2024-01-01T00:00:00+01:00",
            "timezone": "Europe/Berlin",
            "scheduled_doses": [],
            "as_needed_medications": [],
        },
    ),
    (
        "revise_medication_deviation",
        ("medication", "deviation", "create"),
        {
            "regime_logical_id": "b" * 32,
            "scheduled_at": "2024-01-01T08:00:00+01:00",
            "actual_intakes": [],
        },
    ),
    (
        "revise_as_needed_intake",
        ("medication", "as-needed-intake", "create"),
        {
            "regime_logical_id": "b" * 32,
            "entry_id": "c" * 32,
            "taken_at": "2024-01-01T08:00:00+01:00",
            "amount": "1",
            "reason_category_logical_id": None,
        },
    ),
    (
        "revise_intake_reason_category",
        ("medication", "intake-reason-category", "create"),
        {"name": "Kopfschmerz"},
    ),
    (
        "create_activity_derivation_version",
        ("activity-settings", "create-version"),
        {"coverage_gap_minutes": 180},
    ),
)

_V03_INTENT_NAMES = (
    "ContextCoverageStartCreate",
    "ContextCoverageStartRevise",
    "ContextCoverageStartWithdraw",
    "ContextCoverageStartRestore",
    "IllnessCategoryCreate",
    "IllnessCategoryRevise",
    "IllnessCategoryWithdraw",
    "IllnessCategoryRestore",
    "CustomContextLabelCreate",
    "CustomContextLabelRevise",
    "CustomContextLabelWithdraw",
    "CustomContextLabelRestore",
    "IllnessPeriodCreate",
    "IllnessPeriodRevise",
    "IllnessPeriodWithdraw",
    "IllnessPeriodRestore",
    "DailyStressCreate",
    "DailyStressRevise",
    "DailyStressWithdraw",
    "DailyStressRestore",
    "CustomContextPeriodCreate",
    "CustomContextPeriodRevise",
    "CustomContextPeriodWithdraw",
    "CustomContextPeriodRestore",
    "MedicationRegimeCreate",
    "MedicationRegimeRevise",
    "MedicationRegimeWithdraw",
    "MedicationRegimeRestore",
    "MedicationDeviationCreate",
    "MedicationDeviationRevise",
    "MedicationDeviationWithdraw",
    "MedicationDeviationRestore",
    "AsNeededIntakeCreate",
    "AsNeededIntakeRevise",
    "AsNeededIntakeWithdraw",
    "AsNeededIntakeRestore",
    "IntakeReasonCategoryCreate",
    "IntakeReasonCategoryRevise",
    "IntakeReasonCategoryWithdraw",
    "IntakeReasonCategoryRestore",
)


def _v03_write_variants() -> tuple[tuple[str, tuple[str, ...], str, dict[str, object]], ...]:
    variants: list[tuple[str, tuple[str, ...], str, dict[str, object]]] = []
    for request_type, route, fields in _V03_WRITES:
        variants.append((request_type, route, "create", fields))
        if request_type == "create_activity_derivation_version":
            continue
        logical_id = "a" * 32
        revision_id = "c" * 32
        variants.append(
            (
                request_type,
                (*route[:-1], "withdraw"),
                "withdraw",
                {
                    "logical_id": logical_id,
                    "expected_revision_id": revision_id,
                    "reason": "Korrektur",
                },
            )
        )
        for intent in ("revise", "restore"):
            variants.append(
                (
                    request_type,
                    (*route[:-1], intent),
                    intent,
                    {"logical_id": logical_id, "expected_revision_id": revision_id, **fields},
                )
            )
    return tuple(variants)


def _prime_v03_targets(app: AppTest, request_type: str, intent: str) -> None:
    target = SimpleNamespace(
        logical_id="a" * 32,
        revision_id="c" * 32,
        name="Ziel",
        start_date=date(2024, 1, 1),
    )
    category = SimpleNamespace(logical_id="a" * 32, revision_id="c" * 32, name="Kategorie")
    entry = SimpleNamespace(entry_id="c" * 32, name="Bedarf")
    regime_id = "a" * 32 if request_type == "revise_medication_regime" else "b" * 32
    regime = SimpleNamespace(
        logical_id=regime_id,
        revision_id="c" * 32,
        starts_at=datetime.fromisoformat("2024-01-01T00:00:00+01:00"),
        timezone="Europe/Berlin",
        scheduled_doses=(),
        as_needed_medications=(entry,),
    )
    groups = {
        "revise_context_coverage_start": "coverage_start",
        "revise_illness_category": "illness_categories",
        "revise_illness_period": "illness_periods",
        "revise_daily_stress": "daily_stress",
        "revise_custom_context_label": "custom_labels",
        "revise_custom_context_period": "custom_periods",
    }
    records = SimpleNamespace(
        coverage_start=None,
        illness_categories=(category,),
        illness_periods=(),
        daily_stress=(),
        custom_labels=(category,),
        custom_periods=(),
    )
    field = groups.get(request_type)
    if field == "coverage_start":
        records.coverage_start = target
    elif field is not None:
        setattr(records, field, (target,))
    app.session_state["context_records"] = records
    app.session_state["medication_plan"] = SimpleNamespace(
        regimes=(regime,), intake_reason_categories=(target,)
    )
    if intent == "restore" and field is not None:
        app.session_state["context_audit"] = SimpleNamespace(
            logical_id="a" * 32,
            revisions=(
                SimpleNamespace(
                    revision_id="c" * 32,
                    previous_revision_id=None,
                    object_kind={
                        "revise_context_coverage_start": "context_coverage_start",
                        "revise_illness_category": "illness_category",
                        "revise_illness_period": "illness_period",
                        "revise_daily_stress": "daily_stress",
                        "revise_custom_context_label": "custom_context_label",
                        "revise_custom_context_period": "custom_context_period",
                    }[request_type],
                    state="withdrawn",
                    start_date=date(2024, 1, 1),
                ),
            ),
        )
    if request_type in {"revise_medication_deviation", "revise_as_needed_intake"} or (
        intent == "restore" and request_type.startswith("revise_medication")
    ):
        revision_fields = {
            "state": "withdrawn" if intent == "restore" else "active",
            "revision_id": "c" * 32,
            "previous_revision_id": None,
        }
        if request_type == "revise_medication_deviation":
            revision_fields["scheduled_at"] = "2024-01-01T08:00:00+01:00"
        elif request_type == "revise_as_needed_intake":
            revision_fields["entry_id"] = "c" * 32
        else:
            revision_fields["starts_at"] = "2024-01-01T00:00:00+01:00"
        app.session_state["medication_audit"] = SimpleNamespace(
            logical_id="a" * 32, revisions=(SimpleNamespace(**revision_fields),)
        )
    app.run()


def _fill_v03_form(app: AppTest, document: dict[str, object], *, prime: bool = True) -> None:
    request_type = str(document["type"])
    intent = str(document["intent"])
    if prime:
        _prime_v03_targets(app, request_type, intent)
    next(item for item in app.selectbox if item.label == "Schreibauftragsfamilie").set_value(
        request_type
    ).run()
    if request_type != "create_activity_derivation_version":
        next(item for item in app.selectbox if item.label == "Revisionsabsicht").set_value(
            intent
        ).run()
    text_fields = {
        "Name": "name",
        "Regimebeginn (ISO 8601)": "starts_at",
        "IANA-Zeitzone": "timezone",
        "Geplanter Zeitpunkt (ISO 8601)": "scheduled_at",
        "Einnahmezeitpunkt (ISO 8601)": "taken_at",
        "Menge": "amount",
        "Rücknahmegrund": "reason",
    }
    for label, key in text_fields.items():
        if key in document:
            next(item for item in app.text_input if item.label == label).set_value(
                str(document[key])
            )
    if "coverage_gap_minutes" in document:
        next(
            item for item in app.number_input if item.label == "Abdeckungsschwelle (Minuten)"
        ).set_value(int(document["coverage_gap_minutes"]))
    date_fields = {
        "Abdeckungsbeginn": "start_date",
        "Tag": "day",
        "Beginn": "start_date",
    }
    for label, key in date_fields.items():
        if key in document:
            matches = [item for item in app.date_input if item.label == label]
            if matches:
                matches[0].set_value(date.fromisoformat(str(document[key])))
    if "severity" in document:
        next(item for item in app.selectbox if item.label == "Schwere").set_value(
            document["severity"]
        )
    if "level" in document:
        next(item for item in app.selectbox if item.label == "Stressstufe").set_value(
            document["level"]
        )


@pytest.mark.v03_adapter(
    "cli",
    "ActivityDerivationPlan",
    "CreateActivityDerivationVersion",
    "ManualContextRevisionPlan",
    "MedicationRevisionPlan",
    "ReviseAsNeededIntake",
    "ReviseContextCoverageStart",
    "ReviseCustomContextLabel",
    "ReviseCustomContextPeriod",
    "ReviseDailyStress",
    "ReviseIllnessCategory",
    "ReviseIllnessPeriod",
    "ReviseIntakeReasonCategory",
    "ReviseMedicationDeviation",
    "ReviseMedicationRegime",
    *_V03_INTENT_NAMES,
)
@pytest.mark.v03_adapter(
    "streamlit",
    "ActivityDerivationPlan",
    "CreateActivityDerivationVersion",
    "ManualContextRevisionPlan",
    "MedicationRevisionPlan",
    "ReviseAsNeededIntake",
    "ReviseContextCoverageStart",
    "ReviseCustomContextLabel",
    "ReviseCustomContextPeriod",
    "ReviseDailyStress",
    "ReviseIllnessCategory",
    "ReviseIllnessPeriod",
    "ReviseIntakeReasonCategory",
    "ReviseMedicationDeviation",
    "ReviseMedicationRegime",
    *_V03_INTENT_NAMES,
)
@pytest.mark.parametrize(("request_type", "route", "intent", "fields"), _v03_write_variants())
def test_cli_and_streamlit_preview_the_same_v03_write(
    request_type: str,
    route: tuple[str, ...],
    intent: str,
    fields: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = tmp_path / "seed.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            """<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
            <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="1"
            sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
            creationDate="2024-01-02 12:00:00 +0100"
            startDate="2024-01-02 12:00:00 +0100" endDate="2024-01-02 12:01:00 +0100"/>
            </HealthData>""",
        )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        seed = ImportHealthExport(package)
        health_lab.execute_write(seed, expected_plan=health_lab.preview_write(seed).fingerprint)
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(tmp_path / "synthetic"))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    document = {
        "schema_version": "3.0",
        "type": request_type,
        "intent": intent,
        **fields,
    }
    source = tmp_path / f"{request_type}.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    observed: list[tuple[WriteRequest, WritePlan]] = []
    original_preview = HealthLab.preview_write

    def capture_preview(health_lab: HealthLab, request: WriteRequest) -> WritePlan:
        plan = original_preview(health_lab, request)
        observed.append((request, plan))
        return plan

    monkeypatch.setattr(HealthLab, "preview_write", capture_preview)
    assert cli_main([*route, "--input", str(source), "--json"]) in {0, 3}
    cli_output = json.loads(capsys.readouterr().out)
    cli_request, cli_plan = observed[-1]

    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"
    app = AppTest.from_file(str(app_path)).run()
    app.radio[0].set_value("Kontext & Medikamente").run()
    _fill_v03_form(app, document)
    next(button for button in app.button if button.label == "V0.3-Schreibauftrag prüfen").click()
    app.run()
    streamlit_request, streamlit_plan = next(
        item for item in reversed(observed) if type(item[0]) is type(cli_request)
    )

    assert not app.exception
    assert streamlit_request == cli_request
    assert streamlit_plan.fingerprint == cli_plan.fingerprint
    assert streamlit_plan.approval.status == cli_plan.approval.status
    assert streamlit_plan.details == cli_plan.details
    assert streamlit_plan.diagnostics == cli_plan.diagnostics
    assert cli_output["fingerprint"] == str(streamlit_plan.fingerprint)
    assert any(item.value == str(streamlit_plan.fingerprint) for item in app.code)


@pytest.mark.v03_adapter(
    "streamlit",
    "ImportDetails",
    "ContextAudit",
    "ContextRecords",
    "DailyContext",
    "ManualContextRevisionPlan",
    "ManualContextRevisionReceipt",
    "MedicationRevisionPlan",
    "MedicationRevisionReceipt",
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
@pytest.mark.v02_adapter(
    "streamlit",
    "AsNeededIntakePlan",
    "AsNeededIntakeReceipt",
    "IllnessRevisionPlan",
    "IntakeReasonCategoryPlan",
    "IntakeReasonCategoryReceipt",
    "MedicationDeviationPlan",
    "MedicationDeviationReceipt",
    "MedicationRegimePlan",
    "MedicationRegimeReceipt",
    "NoChangeStatus",
    "WriteNoChange",
)
def test_streamlit_loads_import_details_through_the_application_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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
    calls: list[tuple[ImportId, ImportDetails]] = []
    original = HealthLab.load_import_details

    def capture(health_lab: HealthLab, import_id: ImportId) -> ImportDetails:
        projection = original(health_lab, import_id)
        calls.append((import_id, projection))
        return projection

    monkeypatch.setattr(HealthLab, "load_import_details", capture)
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(config.synthetic_store),
        "--real-store",
        str(config.real_store),
    ]
    assert cli_main([*common, "import-details", str(receipt.result.import_id), "--json"]) == 0
    capsys.readouterr()
    cli_call = calls[-1]
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
    assert calls[-1] == cli_call
    assert any(item.value == "Importdetails" for item in app.subheader)
    assert any("Kanonische Records im Paket: 1" in item.value for item in app.caption)
    assert app.dataframe


@pytest.mark.v03_adapter("cli", "ContextAudit", "ContextRecords", "DailyContext")
@pytest.mark.v03_adapter("streamlit", "ContextAudit", "ContextRecords", "DailyContext")
def test_cli_and_streamlit_load_identical_context_projections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = tmp_path / "context.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            """<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
            <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="1"
            sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
            creationDate="2024-01-02 12:00:00 +0100"
            startDate="2024-01-02 12:00:00 +0100" endDate="2024-01-02 12:01:00 +0100"/>
            </HealthData>""",
        )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(package)
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        request = ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 1)))
        receipt = health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    assert isinstance(receipt.result, ManualContextRevisionReceipt)
    logical_id = ContextLogicalId(str(receipt.result.logical_id))
    daily_calls: list[tuple[SnapshotDateSelection, DailyContext]] = []
    record_calls: list[tuple[SnapshotSelection, ContextRecords]] = []
    audit_calls: list[tuple[ContextLogicalId, ContextAudit]] = []
    original_daily = HealthLab.load_daily_context
    original_records = HealthLab.load_context_records
    original_audit = HealthLab.load_context_audit
    active_selection = SnapshotSelection()

    def capture_daily(health_lab: HealthLab, selection: SnapshotDateSelection) -> DailyContext:
        result = original_daily(health_lab, selection)
        daily_calls.append((selection, result))
        return result

    def capture_records(
        health_lab: HealthLab, selection: SnapshotSelection = active_selection
    ) -> ContextRecords:
        result = original_records(health_lab, selection)
        record_calls.append((selection, result))
        return result

    def capture_audit(health_lab: HealthLab, target: ContextLogicalId) -> ContextAudit:
        result = original_audit(health_lab, target)
        audit_calls.append((target, result))
        return result

    monkeypatch.setattr(HealthLab, "load_daily_context", capture_daily)
    monkeypatch.setattr(HealthLab, "load_context_records", capture_records)
    monkeypatch.setattr(HealthLab, "load_context_audit", capture_audit)
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(config.synthetic_store),
        "--real-store",
        str(config.real_store),
        "context",
    ]
    assert cli_main([*common, "days", "--json"]) == 0
    capsys.readouterr()
    assert cli_main([*common, "records", "--json"]) == 0
    capsys.readouterr()
    assert cli_main([*common, "audit", str(logical_id), "--json"]) == 0
    capsys.readouterr()
    cli_daily, cli_records, cli_audit = daily_calls[-1], record_calls[-1], audit_calls[-1]

    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(config.synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(config.real_store))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"
    app = AppTest.from_file(str(app_path)).run()
    app.radio[0].set_value("Kontext & Medikamente").run()
    next(button for button in app.button if button.label == "Kontext laden").click().run()

    assert not app.exception
    assert daily_calls[-1] == cli_daily
    assert record_calls[-1] == cli_records
    assert audit_calls[-1] == cli_audit
    assert len(app.dataframe) >= 3


@pytest.mark.v03_adapter("cli", "NoChangeStatus", "WriteNoChange")
@pytest.mark.v03_adapter("streamlit", "NoChangeStatus", "WriteNoChange")
def test_cli_and_streamlit_treat_no_change_as_terminal_without_a_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = tmp_path / "no-change.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            """<HealthData><ExportDate value="2024-01-03 12:00:00 +0100"/>
            <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="1"
            sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
            creationDate="2024-01-02 12:00:00 +0100"
            startDate="2024-01-02 12:00:00 +0100" endDate="2024-01-02 12:01:00 +0100"/>
            </HealthData>""",
        )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(package)
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        create = ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 1)))
        receipt = health_lab.execute_write(
            create, expected_plan=health_lab.preview_write(create).fingerprint
        )
    assert isinstance(receipt.result, ManualContextRevisionReceipt)
    snapshot_before = receipt.result.snapshot_ref
    document = {
        "schema_version": "3.0",
        "type": "revise_context_coverage_start",
        "intent": "revise",
        "logical_id": str(receipt.result.logical_id),
        "expected_revision_id": str(receipt.result.revision_id),
        "start_date": "2024-01-01",
    }
    source = tmp_path / "unchanged.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(config.synthetic_store),
        "--real-store",
        str(config.real_store),
        "context",
        "coverage-start",
        "revise",
        "--input",
        str(source),
        "--json",
    ]
    assert cli_main(common) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["approval"]["status"] == "no_change"
    assert cli_main([*common, "--execute", "--expect-plan", plan["fingerprint"]]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "write_plan"

    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(config.synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(config.real_store))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"
    app = AppTest.from_file(str(app_path)).run()
    app.radio[0].set_value("Kontext & Medikamente").run()
    next(button for button in app.button if button.label == "Kontext laden").click().run()
    _fill_v03_form(app, document, prime=False)
    next(button for button in app.button if button.label == "V0.3-Schreibauftrag prüfen").click()
    app.run()

    execute = next(
        button for button in app.button if button.label == "V0.3-Schreibauftrag ausführen"
    )
    assert execute.disabled
    assert any("Freigabe: no_change" in item.value for item in app.caption)
    with HealthLab.open(config) as health_lab:
        assert health_lab.load_context_records().snapshot_ref == snapshot_before


@pytest.mark.v03_adapter(
    "streamlit",
    "ActivityDerivationPlan",
    "ActivityDerivationReceipt",
    "ActivitySettings",
    "CreateActivityDerivationVersion",
)
def test_streamlit_traces_activity_settings_and_structured_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(tmp_path / "synthetic"))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(tmp_path / "real"))
    calls: list[ActivitySettings] = []
    original = HealthLab.load_activity_settings

    def capture(health_lab: HealthLab) -> ActivitySettings:
        projection = original(health_lab)
        calls.append(projection)
        return projection

    monkeypatch.setattr(HealthLab, "load_activity_settings", capture)
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    assert cli_main([*common, "activity-settings", "show", "--json"]) == 0
    capsys.readouterr()
    cli_projection = calls[-1]
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()
    app.radio[0].set_value("Kontext & Medikamente").run()
    next(item for item in app.selectbox if item.label == "Schreibauftragsfamilie").set_value(
        "create_activity_derivation_version"
    ).run()
    next(
        item for item in app.number_input if item.label == "Abdeckungsschwelle (Minuten)"
    ).set_value(180)
    next(button for button in app.button if button.label == "V0.3-Schreibauftrag prüfen").click()
    app.run()

    assert not app.exception
    assert calls[-1] == cli_projection
    assert any(item.value == "Aktivitätsableitung" for item in app.subheader)
    assert any("Freigabe:" in item.value for item in app.caption)
    assert next(item for item in app.selectbox if item.label == "Schreibauftragsfamilie").disabled
    assert next(
        item for item in app.number_input if item.label == "Abdeckungsschwelle (Minuten)"
    ).disabled

    app.radio[0].set_value("Kerndaten").run()
    app.radio[0].set_value("Kontext & Medikamente").run()

    assert not next(
        item for item in app.selectbox if item.label == "Schreibauftragsfamilie"
    ).disabled
    assert all(button.label != "V0.3-Schreibauftrag ausführen" for button in app.button)


@pytest.mark.v03_adapter("streamlit", "MedicationAudit", "MedicationDays", "MedicationPlan")
def test_streamlit_exposes_as_needed_write_lifecycles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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

    next(item for item in app.selectbox if item.label == "Schreibauftragsfamilie").set_value(
        "revise_intake_reason_category"
    ).run()
    next(item for item in app.text_input if item.label == "Name").set_value("Kopfschmerz")
    next(
        button for button in app.button if button.label == "V0.3-Schreibauftrag prüfen"
    ).click().run()
    next(
        button for button in app.button if button.label == "V0.3-Schreibauftrag ausführen"
    ).click().run()

    with HealthLab.open(config) as health_lab:
        categories = health_lab.load_medication_plan().intake_reason_categories
    assert tuple(item.name for item in categories) == ("Kopfschmerz",)
    plan_calls: list[tuple[SnapshotSelection, MedicationPlan]] = []
    day_calls: list[tuple[SnapshotDateSelection, MedicationDays]] = []
    audit_calls: list[tuple[MedicationLogicalId, MedicationAudit]] = []
    original_plan = HealthLab.load_medication_plan
    original_days = HealthLab.load_medication_days
    original_audit = HealthLab.load_medication_audit
    active_selection = SnapshotSelection()

    def capture_plan(
        health_lab: HealthLab, selection: SnapshotSelection = active_selection
    ) -> MedicationPlan:
        projection = original_plan(health_lab, selection)
        plan_calls.append((selection, projection))
        return projection

    def capture_days(
        health_lab: HealthLab, selection: SnapshotDateSelection
    ) -> MedicationDays:
        projection = original_days(health_lab, selection)
        day_calls.append((selection, projection))
        return projection

    def capture_audit(
        health_lab: HealthLab, logical_id: MedicationLogicalId
    ) -> MedicationAudit:
        projection = original_audit(health_lab, logical_id)
        audit_calls.append((logical_id, projection))
        return projection

    monkeypatch.setattr(HealthLab, "load_medication_plan", capture_plan)
    monkeypatch.setattr(HealthLab, "load_medication_days", capture_days)
    monkeypatch.setattr(HealthLab, "load_medication_audit", capture_audit)
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(config.synthetic_store),
        "--real-store",
        str(config.real_store),
        "medication",
    ]
    assert cli_main([*common, "plan", "--json"]) == 0
    capsys.readouterr()
    cli_plan = plan_calls[-1]
    assert cli_main([*common, "days", "--json"]) == 0
    capsys.readouterr()
    cli_days = day_calls[-1]
    assert cli_main([*common, "audit", str(categories[0].logical_id), "--json"]) == 0
    capsys.readouterr()
    cli_audit = audit_calls[-1]
    next(button for button in app.button if button.label == "Medikamentenplan laden").click().run()
    next(item for item in app.selectbox if item.label == "Medikamenten-Auditziel").set_value(
        categories[0]
    )
    next(button for button in app.button if button.label == "Medikamentenaudit laden").click().run()
    assert not app.exception
    assert plan_calls[-1] == cli_plan
    assert day_calls[-1] == cli_days
    assert audit_calls[-1] == cli_audit


@pytest.mark.v02_adapter("streamlit", "SleepObservationStatus")
@pytest.mark.v03_adapter("streamlit", "SleepDays")
def test_streamlit_loads_sleep_days_through_the_application_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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
    calls: list[tuple[SnapshotDateSelection, SleepDays]] = []
    original = HealthLab.load_sleep_days

    def capture(health_lab: HealthLab, selection: SnapshotDateSelection) -> SleepDays:
        projection = original(health_lab, selection)
        calls.append((selection, projection))
        return projection

    monkeypatch.setattr(HealthLab, "load_sleep_days", capture)
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(config.synthetic_store),
        "--real-store",
        str(config.real_store),
    ]
    assert cli_main([*common, "sleep-days", "--json"]) == 0
    capsys.readouterr()
    cli_call = calls[-1]
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(config.synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(config.real_store))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()
    app.radio[0].set_value("Kerndaten").run()
    next(button for button in app.button if button.label == "Schlaf laden").click().run()

    assert not app.exception
    assert calls[-1] == cli_call
    assert any(item.value == "Schlaf" for item in app.subheader)
    assert app.dataframe


@pytest.mark.v03_adapter("streamlit", "ActivityDays", "Workouts")
def test_streamlit_loads_activity_days_through_the_application_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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
              <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30"
                durationUnit="min" sourceName="Apple Watch" sourceVersion="1"
                device="Apple Watch" creationDate="2024-01-02 21:31:00 +0100"
                startDate="2024-01-02 21:00:00 +0100"
                endDate="2024-01-02 21:30:00 +0100"/>
            </HealthData>
            """,
        )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        request = ImportHealthExport(package)
        health_lab.execute_write(
            request, expected_plan=health_lab.preview_write(request).fingerprint
        )
    activity_calls: list[tuple[SnapshotDateSelection, ActivityDays]] = []
    workout_calls: list[tuple[SnapshotDateSelection, Workouts]] = []
    original_activity = HealthLab.load_activity_days
    original_workouts = HealthLab.load_workouts

    def capture_activity(
        health_lab: HealthLab, selection: SnapshotDateSelection
    ) -> ActivityDays:
        projection = original_activity(health_lab, selection)
        activity_calls.append((selection, projection))
        return projection

    def capture_workouts(health_lab: HealthLab, selection: SnapshotDateSelection) -> Workouts:
        projection = original_workouts(health_lab, selection)
        workout_calls.append((selection, projection))
        return projection

    monkeypatch.setattr(HealthLab, "load_activity_days", capture_activity)
    monkeypatch.setattr(HealthLab, "load_workouts", capture_workouts)
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(config.synthetic_store),
        "--real-store",
        str(config.real_store),
    ]
    assert cli_main([*common, "activity-days", "--json"]) == 0
    capsys.readouterr()
    cli_activity = activity_calls[-1]
    assert cli_main([*common, "workouts", "--json"]) == 0
    capsys.readouterr()
    cli_workouts = workout_calls[-1]
    monkeypatch.setenv("HEALTHLAB_MODE", "synthetic")
    monkeypatch.setenv("HEALTHLAB_SYNTHETIC_STORE", str(config.synthetic_store))
    monkeypatch.setenv("HEALTHLAB_REAL_STORE", str(config.real_store))
    app_path = Path(__file__).parents[2] / "src/personal_health_lab/adapters/streamlit/app.py"

    app = AppTest.from_file(str(app_path)).run()
    app.radio[0].set_value("Kerndaten").run()
    next(button for button in app.button if button.label == "Aktivität laden").click().run()
    next(button for button in app.button if button.label == "Training laden").click().run()

    assert not app.exception
    assert activity_calls[-1] == cli_activity
    assert workout_calls[-1] == cli_workouts
    assert cli_workouts[1].workouts
    assert any(item.value == "Aktivität" for item in app.subheader)
    assert app.dataframe
    assert any(
        {
            "Logische Messung",
            "Quellaktualisierung",
            "Quellversion",
            "Disposition",
            "Ausgewählt",
        }
        <= set(frame.value.columns)
        for frame in app.dataframe
    )


@pytest.mark.v02_adapter("streamlit", "WeightDayStatus")
@pytest.mark.v03_adapter("streamlit", "WeightNutrition")
def test_streamlit_renders_the_complete_weight_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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
    calls: list[tuple[SnapshotDateSelection, WeightNutrition]] = []
    original = HealthLab.load_weight_nutrition

    def capture(health_lab: HealthLab, selection: SnapshotDateSelection) -> WeightNutrition:
        projection = original(health_lab, selection)
        calls.append((selection, projection))
        return projection

    monkeypatch.setattr(HealthLab, "load_weight_nutrition", capture)
    common = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(config.synthetic_store),
        "--real-store",
        str(config.real_store),
        "weight-nutrition",
        "--start-date",
        "2024-01-01",
        "--end-date",
        "2024-01-06",
        "--json",
    ]
    assert cli_main(common) == 0
    capsys.readouterr()
    cli_call = calls[-1]
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
    assert calls[-1] == cli_call
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
    assert any("Schema: 2 → 12" in item.value for item in app.caption)
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

    app.radio[0].set_value("Kontext & Medikamente").run()
    next(item for item in app.selectbox if item.label == "Schreibauftragsfamilie").set_value(
        "create_activity_derivation_version"
    ).run()
    next(
        item for item in app.number_input if item.label == "Abdeckungsschwelle (Minuten)"
    ).set_value(180)
    next(button for button in app.button if button.label == "V0.3-Schreibauftrag prüfen").click()
    app.run()
    assert any(button.label == "V0.3-Schreibauftrag ausführen" for button in app.button)

    app.radio[0].set_value("Kerndaten").run()
    app.radio[0].set_value("Kontext & Medikamente").run()
    assert all(button.label != "V0.3-Schreibauftrag ausführen" for button in app.button)


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
