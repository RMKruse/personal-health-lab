from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
    ValidationError,
)

import personal_health_lab.application as application

_SCHEMA = json.loads(
    files("personal_health_lab.adapters.cli")
    .joinpath("schemas/input-3.0.schema.json")
    .read_text(encoding="utf-8")
)


def load_write_request(
    source: Path | str,
    *,
    expected_type: str | None = None,
    expected_intent: str | None = None,
) -> application.WriteRequest:
    try:
        document = json.loads(
            sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise application.ConfigurationError("Ungültiges JSON-3.0-Eingabedokument.") from error
    return decode_write_request(
        document, expected_type=expected_type, expected_intent=expected_intent
    )


def decode_write_request(
    document: object,
    *,
    expected_type: str | None = None,
    expected_intent: str | None = None,
) -> application.WriteRequest:
    try:
        Draft202012Validator(_SCHEMA, format_checker=FormatChecker()).validate(document)
    except ValidationError as error:
        raise application.ConfigurationError("Ungültiges JSON-3.0-Eingabedokument.") from error
    assert isinstance(document, dict)
    typed = cast(dict[str, object], document)
    if expected_type is not None and typed["type"] != expected_type:
        raise application.ConfigurationError("JSON-Auftrag passt nicht zum CLI-Unterbefehl.")
    if expected_intent is not None and typed["intent"] != expected_intent:
        raise application.ConfigurationError("JSON-Intent passt nicht zum CLI-Unterbefehl.")
    try:
        return _decode(typed)
    except application.ConfigurationError:
        raise
    except (AssertionError, KeyError, TypeError, ValueError) as error:
        raise application.ConfigurationError("Ungültiger JSON-3.0-Schreibauftrag.") from error


def _only(document: dict[str, object], *fields: str) -> None:
    expected = {"schema_version", "type", "intent", *fields}
    if document.keys() != expected:
        raise application.ConfigurationError("JSON-Schreibauftrag enthält ungültige Felder.")


def _id(value: object) -> application.ContextLogicalId:
    return application.ContextLogicalId(str(value))


def _context_revision(value: object) -> application.ContextRevisionId:
    return application.ContextRevisionId(str(value))


def _medication_id(value: object) -> application.MedicationLogicalId:
    return application.MedicationLogicalId(str(value))


def _medication_revision(value: object) -> application.MedicationRevisionId:
    return application.MedicationRevisionId(str(value))


def _context_intent(
    document: dict[str, object],
    create: type[Any],
    revise: type[Any],
    withdraw: type[Any],
    restore: type[Any],
    value_fields: tuple[str, ...],
    convert: Callable[[dict[str, object]], tuple[Any, ...]],
) -> Any:
    intent = str(document["intent"])
    if intent == "create":
        _only(document, *value_fields)
        return create(*convert(document))
    if intent == "withdraw":
        _only(document, "logical_id", "expected_revision_id", "reason")
        return withdraw(
            _id(document["logical_id"]),
            _context_revision(document["expected_revision_id"]),
            str(document["reason"]),
        )
    _only(document, "logical_id", "expected_revision_id", *value_fields)
    args = (
        _id(document["logical_id"]),
        _context_revision(document["expected_revision_id"]),
        *convert(document),
    )
    return (revise if intent == "revise" else restore)(*args)


def _actual_intakes(values: object) -> tuple[application.MedicationActualIntake, ...]:
    assert isinstance(values, list)
    return tuple(
        application.MedicationActualIntake(
            datetime.fromisoformat(str(item["taken_at"])), Decimal(str(item["amount"]))
        )
        for item in values
        if isinstance(item, dict)
    )


def _regime_value(document: dict[str, object]) -> tuple[Any, ...]:
    doses = document["scheduled_doses"]
    as_needed = document["as_needed_medications"]
    assert isinstance(doses, list) and isinstance(as_needed, list)
    return (
        datetime.fromisoformat(str(document["starts_at"])),
        str(document["timezone"]),
        tuple(
            application.ScheduledDose(
                str(item["medication_name"]),
                Decimal(str(item["amount"])),
                str(item["unit"]),
                time.fromisoformat(str(item["local_time"])),
                frozenset(application.Weekday(str(day)) for day in item["weekdays"]),
            )
            for item in doses
            if isinstance(item, dict)
        ),
        tuple(
            application.AsNeededMedication(
                str(item["medication_name"]),
                Decimal(str(item["amount"])),
                str(item["unit"]),
                tuple(
                    _medication_id(value) for value in item.get("preferred_reason_category_ids", [])
                ),
                (
                    None
                    if item.get("entry_id") is None
                    else application.MedicationPlanEntryId(str(item["entry_id"]))
                ),
            )
            for item in as_needed
            if isinstance(item, dict)
        ),
    )


def _decode(document: dict[str, object]) -> application.WriteRequest:
    request_type = str(document["type"])
    intent = str(document["intent"])
    value: Any
    cls: Any
    if request_type == "create_activity_derivation_version":
        _only(document, "coverage_gap_minutes")
        if intent != "create":
            raise application.ConfigurationError("Aktivitätsableitungen werden nur angelegt.")
        return application.CreateActivityDerivationVersion(
            int(str(document["coverage_gap_minutes"]))
        )
    context_specs: dict[str, tuple[Any, ...]] = {
        "revise_context_coverage_start": (
            application.ReviseContextCoverageStart,
            application.ContextCoverageStartCreate,
            application.ContextCoverageStartRevise,
            application.ContextCoverageStartWithdraw,
            application.ContextCoverageStartRestore,
            ("start_date",),
            lambda row: (date.fromisoformat(str(row["start_date"])),),
        ),
        "revise_illness_category": (
            application.ReviseIllnessCategory,
            application.IllnessCategoryCreate,
            application.IllnessCategoryRevise,
            application.IllnessCategoryWithdraw,
            application.IllnessCategoryRestore,
            ("name",),
            lambda row: (str(row["name"]),),
        ),
        "revise_custom_context_label": (
            application.ReviseCustomContextLabel,
            application.CustomContextLabelCreate,
            application.CustomContextLabelRevise,
            application.CustomContextLabelWithdraw,
            application.CustomContextLabelRestore,
            ("name",),
            lambda row: (str(row["name"]),),
        ),
        "revise_illness_period": (
            application.ReviseIllnessPeriod,
            application.IllnessPeriodCreate,
            application.IllnessPeriodRevise,
            application.IllnessPeriodWithdraw,
            application.IllnessPeriodRestore,
            ("category_logical_id", "start_date", "end_date", "severity"),
            lambda row: (
                _id(row["category_logical_id"]),
                date.fromisoformat(str(row["start_date"])),
                None if row["end_date"] is None else date.fromisoformat(str(row["end_date"])),
                application.IllnessSeverity(str(row["severity"])),
            ),
        ),
        "revise_daily_stress": (
            application.ReviseDailyStress,
            application.DailyStressCreate,
            application.DailyStressRevise,
            application.DailyStressWithdraw,
            application.DailyStressRestore,
            ("day", "level"),
            lambda row: (
                date.fromisoformat(str(row["day"])),
                application.StressLevel(str(row["level"])),
            ),
        ),
        "revise_custom_context_period": (
            application.ReviseCustomContextPeriod,
            application.CustomContextPeriodCreate,
            application.CustomContextPeriodRevise,
            application.CustomContextPeriodWithdraw,
            application.CustomContextPeriodRestore,
            ("label_logical_id", "start_date", "end_date", "note"),
            lambda row: (
                _id(row["label_logical_id"]),
                date.fromisoformat(str(row["start_date"])),
                None if row["end_date"] is None else date.fromisoformat(str(row["end_date"])),
                None if row["note"] is None else str(row["note"]),
            ),
        ),
    }
    if request_type in context_specs:
        wrapper, create, revise, withdraw, restore, fields, convert = context_specs[request_type]
        return cast(
            application.WriteRequest,
            wrapper(_context_intent(document, create, revise, withdraw, restore, fields, convert)),
        )
    if request_type == "revise_medication_regime":
        fields = ("starts_at", "timezone", "scheduled_doses", "as_needed_medications")
        if intent == "create":
            _only(document, *fields)
            value = application.MedicationRegimeCreate(*_regime_value(document))
        elif intent == "withdraw":
            _only(document, "logical_id", "expected_revision_id", "reason")
            value = application.MedicationRegimeWithdraw(
                _medication_id(document["logical_id"]),
                _medication_revision(document["expected_revision_id"]),
                str(document["reason"]),
            )
        else:
            _only(document, "logical_id", "expected_revision_id", *fields)
            cls = (
                application.MedicationRegimeRevise
                if intent == "revise"
                else application.MedicationRegimeRestore
            )
            value = cls(
                _medication_id(document["logical_id"]),
                _medication_revision(document["expected_revision_id"]),
                *_regime_value(document),
            )
        return application.ReviseMedicationRegime(value)
    if request_type == "revise_medication_deviation":
        if intent == "create":
            _only(document, "regime_logical_id", "scheduled_at", "actual_intakes")
            value = application.MedicationDeviationCreate(
                _medication_id(document["regime_logical_id"]),
                datetime.fromisoformat(str(document["scheduled_at"])),
                _actual_intakes(document["actual_intakes"]),
            )
        elif intent == "withdraw":
            _only(document, "logical_id", "expected_revision_id", "reason")
            value = application.MedicationDeviationWithdraw(
                _medication_id(document["logical_id"]),
                _medication_revision(document["expected_revision_id"]),
                str(document["reason"]),
            )
        else:
            _only(
                document,
                "logical_id",
                "expected_revision_id",
                "regime_logical_id",
                "scheduled_at",
                "actual_intakes",
            )
            cls = (
                application.MedicationDeviationRevise
                if intent == "revise"
                else application.MedicationDeviationRestore
            )
            value = cls(
                _medication_id(document["logical_id"]),
                _medication_revision(document["expected_revision_id"]),
                _medication_id(document["regime_logical_id"]),
                datetime.fromisoformat(str(document["scheduled_at"])),
                _actual_intakes(document["actual_intakes"]),
            )
        return application.ReviseMedicationDeviation(value)
    if request_type == "revise_as_needed_intake":
        if intent == "create":
            _only(
                document,
                "regime_logical_id",
                "entry_id",
                "taken_at",
                "amount",
                "reason_category_logical_id",
            )
            value = application.AsNeededIntakeCreate(
                _medication_id(document["regime_logical_id"]),
                application.MedicationPlanEntryId(str(document["entry_id"])),
                datetime.fromisoformat(str(document["taken_at"])),
                Decimal(str(document["amount"])),
                None
                if document["reason_category_logical_id"] is None
                else _medication_id(document["reason_category_logical_id"]),
            )
        elif intent == "withdraw":
            _only(document, "logical_id", "expected_revision_id", "reason")
            value = application.AsNeededIntakeWithdraw(
                _medication_id(document["logical_id"]),
                _medication_revision(document["expected_revision_id"]),
                str(document["reason"]),
            )
        else:
            _only(
                document,
                "logical_id",
                "expected_revision_id",
                "regime_logical_id",
                "entry_id",
                "taken_at",
                "amount",
                "reason_category_logical_id",
            )
            cls = (
                application.AsNeededIntakeRevise
                if intent == "revise"
                else application.AsNeededIntakeRestore
            )
            value = cls(
                _medication_id(document["logical_id"]),
                _medication_revision(document["expected_revision_id"]),
                _medication_id(document["regime_logical_id"]),
                application.MedicationPlanEntryId(str(document["entry_id"])),
                datetime.fromisoformat(str(document["taken_at"])),
                Decimal(str(document["amount"])),
                None
                if document["reason_category_logical_id"] is None
                else _medication_id(document["reason_category_logical_id"]),
            )
        return application.ReviseAsNeededIntake(value)
    if request_type == "revise_intake_reason_category":
        if intent == "create":
            _only(document, "name")
            value = application.IntakeReasonCategoryCreate(str(document["name"]))
        elif intent == "withdraw":
            _only(document, "logical_id", "expected_revision_id", "reason")
            value = application.IntakeReasonCategoryWithdraw(
                _medication_id(document["logical_id"]),
                _medication_revision(document["expected_revision_id"]),
                str(document["reason"]),
            )
        else:
            _only(document, "logical_id", "expected_revision_id", "name")
            cls = (
                application.IntakeReasonCategoryRevise
                if intent == "revise"
                else application.IntakeReasonCategoryRestore
            )
            value = cls(
                _medication_id(document["logical_id"]),
                _medication_revision(document["expected_revision_id"]),
                str(document["name"]),
            )
        return application.ReviseIntakeReasonCategory(value)
    raise application.ConfigurationError("Unbekannte JSON-Schreibauftragsvariante.")
