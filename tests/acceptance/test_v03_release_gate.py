from importlib.resources import files
from typing import get_args, get_type_hints

import pytest

import personal_health_lab.application as application
from personal_health_lab.application import (
    ConfigurationError,
    HealthLab,
    WritePlanDetails,
    WriteRequest,
    WriteResult,
)


def _names(union: object) -> set[str]:
    return {item.__name__ for item in get_args(union)}


def test_v03_public_application_surface_is_exact_and_closed() -> None:
    assert _names(WriteRequest) == {
        "AbortMetadataRestore",
        "BeginMetadataRestore",
        "ConfirmDataReviewBatch",
        "CreateActivityDerivationVersion",
        "CreateMetadataBackup",
        "CreatePlausibilityRuleVersion",
        "ImportHealthExport",
        "MigrateStore",
        "ResolveDataReviewCase",
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
        "RevokeDataReviewDecision",
        "RollbackMigration",
        "RunHistoricalReview",
        "RunRestingHeartRateAnalysis",
    }
    assert _names(WritePlanDetails) == {
        "AbortMetadataRestorePlan",
        "ActivityDerivationPlan",
        "DataReviewBatchPlan",
        "DataReviewBatchRevokePlan",
        "DataReviewDecisionPlan",
        "HistoricalReviewPlan",
        "HistoricalModeWritePlan",
        "ImportHealthExportPlan",
        "ManualContextRevisionPlan",
        "MedicationRevisionPlan",
        "MetadataBackupPlan",
        "MetadataRestorePlan",
        "PlausibilityRuleVersionPlan",
        "RestingHeartRateAnalysisPlan",
        "RollbackMigrationPlan",
        "StoreMigrationPlan",
    }
    assert _names(WriteResult) == {
        "ActivityDerivationReceipt",
        "AnalysisReceipt",
        "HistoricalReviewReceipt",
        "ImportReceipt",
        "ManualContextRevisionReceipt",
        "MedicationRevisionReceipt",
        "MetadataBackupReceipt",
        "MetadataRestoreReceipt",
        "PlausibilityRuleVersionReceipt",
        "RollbackMigrationReceipt",
        "StoreMigrationReceipt",
        "WriteBatchDecisionReceipt",
        "WriteDecisionReceipt",
        "WriteNoChange",
        "WriteNotStarted",
    }
    assert {name for name in HealthLab.__dict__ if name.startswith("load_")} >= {
        "load_activity_days",
        "load_activity_settings",
        "load_context_audit",
        "load_context_records",
        "load_daily_context",
        "load_import_details",
        "load_medication_audit",
        "load_medication_days",
        "load_medication_plan",
        "load_sleep_days",
        "load_weight_nutrition",
        "load_workouts",
    }


@pytest.mark.parametrize(
    "request_name",
    [
        "ReviseContextCoverageStart",
        "ReviseIllnessCategory",
        "ReviseCustomContextLabel",
        "ReviseIllnessPeriod",
        "ReviseDailyStress",
        "ReviseCustomContextPeriod",
        "ReviseMedicationRegime",
        "ReviseMedicationDeviation",
        "ReviseAsNeededIntake",
        "ReviseIntakeReasonCategory",
    ],
)
def test_v03_revision_requests_have_four_closed_intents(request_name: str) -> None:
    intent = get_type_hints(getattr(application, request_name))["intent"]
    prefix = request_name.removeprefix("Revise")
    assert {item.__name__.removeprefix(prefix) for item in get_args(intent)} == {
        "Create",
        "Restore",
        "Revise",
        "Withdraw",
    }


def test_v03_unknown_request_variant_is_rejected(tmp_path) -> None:
    config = application.RuntimeConfig(
        application.DataMode.SYNTHETIC,
        tmp_path / "synthetic",
        tmp_path / "real",
    )
    with HealthLab.open(config) as health_lab, pytest.raises(ConfigurationError):
        health_lab.preview_write(object())  # type: ignore[arg-type]


def test_json_3_is_the_only_v03_runtime_contract() -> None:
    schemas = files("personal_health_lab.adapters.cli").joinpath("schemas")
    assert not schemas.joinpath("output-2.0.schema.json").is_file()
    assert schemas.joinpath("input-3.0.schema.json").is_file()
    assert schemas.joinpath("output-3.0.schema.json").is_file()
