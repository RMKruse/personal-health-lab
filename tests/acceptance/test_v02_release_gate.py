import inspect
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast, get_args, get_type_hints

import pytest

import personal_health_lab.application as application
from personal_health_lab.application import (
    ConfigurationError,
    DataCorrection,
    DataMode,
    HealthLab,
    LocalMeasurementExclusion,
    ResolveDataReviewCase,
    RevokeDataReviewDecision,
    RuntimeConfig,
    WritePlanDetails,
    WriteRequest,
    WriteResult,
)


def _names(union: object) -> set[str]:
    return {member.__name__ for member in get_args(union)}


def test_public_application_surface_is_exact_and_closed(tmp_path: Path) -> None:
    requests = _names(WriteRequest)
    plans = _names(WritePlanDetails)
    results = _names(WriteResult)
    variant_contract = {
        "AbortMetadataRestore": ({"AbortMetadataRestorePlan"}, {"MetadataRestoreReceipt"}),
        "BeginMetadataRestore": ({"MetadataRestorePlan"}, {"MetadataRestoreReceipt"}),
        "ConfirmDataReviewBatch": ({"DataReviewBatchPlan"}, {"WriteBatchDecisionReceipt"}),
        "CreateMetadataBackup": ({"MetadataBackupPlan"}, {"MetadataBackupReceipt"}),
        "CreatePlausibilityRuleVersion": (
            {"PlausibilityRuleVersionPlan"},
            {"PlausibilityRuleVersionReceipt"},
        ),
        "ImportHealthExport": ({"ImportHealthExportPlan"}, {"ImportReceipt"}),
        "MigrateStore": ({"StoreMigrationPlan"}, {"StoreMigrationReceipt"}),
        "ResolveDataReviewCase": ({"DataReviewDecisionPlan"}, {"WriteDecisionReceipt"}),
        "RevokeDataReviewDecision": (
            {"DataReviewDecisionPlan", "DataReviewBatchRevokePlan"},
            {"WriteDecisionReceipt", "WriteBatchDecisionReceipt"},
        ),
        "RollbackMigration": ({"RollbackMigrationPlan"}, {"RollbackMigrationReceipt"}),
        "RunHistoricalReview": ({"HistoricalReviewPlan"}, {"HistoricalReviewReceipt"}),
        "RunRestingHeartRateAnalysis": (
            {"RestingHeartRateAnalysisPlan"},
            {"AnalysisReceipt"},
        ),
    }
    assert set(variant_contract) <= requests
    assert {
        plan for expected_plans, _ in variant_contract.values() for plan in expected_plans
    } <= plans
    assert {
        result for _, expected_results in variant_contract.values() for result in expected_results
    } <= results - {"WriteNotStarted", "WriteNoChange"}
    samples = {
        "AbortMetadataRestore": [application.AbortMetadataRestore()],
        "BeginMetadataRestore": [application.BeginMetadataRestore(tmp_path / "backup.sqlite3")],
        "ConfirmDataReviewBatch": [
            application.ConfirmDataReviewBatch(application.DataReviewSelection())
        ],
        "CreateMetadataBackup": [application.CreateMetadataBackup(tmp_path / "metadata.sqlite3")],
        "CreatePlausibilityRuleVersion": [
            application.CreatePlausibilityRuleVersion(
                application.CanonicalHealthType.APPLE_RESTING_HEART_RATE,
                application.PlausibilityRuleSpecification(
                    application.CanonicalUnit.BEATS_PER_MINUTE
                ),
                datetime(2024, 1, 1, tzinfo=UTC),
            )
        ],
        "ImportHealthExport": [application.ImportHealthExport(tmp_path / "export.zip")],
        "MigrateStore": [application.MigrateStore()],
        "ResolveDataReviewCase": [
            application.ResolveDataReviewCase("case", application.DataConfirmation())
        ],
        "RevokeDataReviewDecision": [
            application.RevokeDataReviewDecision(
                application.SingleDecisionTarget(application.DataReviewDecisionId("a" * 32)),
                "erneut prüfen",
            ),
            application.RevokeDataReviewDecision(
                application.BatchDecisionTarget(application.DataReviewBatchActionId("b" * 32)),
                "erneut prüfen",
            ),
        ],
        "RollbackMigration": [application.RollbackMigration()],
        "RunHistoricalReview": [
            application.RunHistoricalReview(
                application.CanonicalHealthType.APPLE_RESTING_HEART_RATE,
                date(2024, 1, 1),
                date(2024, 1, 2),
            )
        ],
        "RunRestingHeartRateAnalysis": [
            application.RunRestingHeartRateAnalysis(
                application.AnalysisDefinitionId("lag-signal-v2")
            )
        ],
    }
    config = RuntimeConfig(
        mode=DataMode.REAL,
        synthetic_store=tmp_path / "synthetic",
        real_store=tmp_path / "real",
    )
    with HealthLab.open(config) as health_lab:
        for request_name, request_samples in samples.items():
            actual_plans = set()
            for request in request_samples:
                plan = health_lab.preview_write(request)
                actual_plans.add(type(plan.details).__name__)
                result = health_lab.execute_write(request, expected_plan=plan.fingerprint).result
                assert type(result).__name__ in (
                    variant_contract[request_name][1] | {"WriteNotStarted"}
                )
            assert actual_plans == variant_contract[request_name][0]
    assert _names(get_type_hints(ResolveDataReviewCase)["resolution"]) == {
        "DataConfirmation",
        "DataCorrection",
        "LocalMeasurementExclusion",
        "LocalWorkoutExclusion",
        "SourceConflictResolution",
        "SourceDeletionResolution",
        "SourceValueAcceptance",
        "WorkoutCorrection",
    }
    assert _names(get_type_hints(RevokeDataReviewDecision)["target"]) == {
        "BatchDecisionTarget",
        "SingleDecisionTarget",
    }
    for request_type in (DataCorrection, LocalMeasurementExclusion, RevokeDataReviewDecision):
        reason = inspect.signature(request_type).parameters["reason"]
        assert get_type_hints(request_type)["reason"] is str
        assert reason.default is inspect.Parameter.empty
    assert "WriteNotStarted" in results
    assert {name for name in HealthLab.__dict__ if not name.startswith("_")} == {
        "execute_write",
        "load_activity_days",
        "load_activity_settings",
        "load_analysis_catalog",
        "load_context_audit",
        "load_context_records",
        "load_daily_context",
        "load_data_review",
        "load_data_review_case",
        "load_import_details",
        "load_migration_diagnostics",
        "load_medication_audit",
        "load_medication_days",
        "load_medication_plan",
        "load_overview",
        "load_plausibility_rules",
        "load_recovery_status",
        "load_sleep_days",
        "load_weight_nutrition",
        "load_workouts",
        "load_workspace_status",
        "open",
        "preview_write",
    }


def test_unknown_public_union_variants_are_rejected(tmp_path: Path) -> None:
    config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "synthetic",
        real_store=tmp_path / "real",
    )
    with HealthLab.open(config) as health_lab, pytest.raises(ConfigurationError):
        health_lab.preview_write(cast(WriteRequest, object()))
    with pytest.raises(ConfigurationError):
        ResolveDataReviewCase(cast(Any, "case"), cast(Any, object()))
    with pytest.raises(ConfigurationError):
        RevokeDataReviewDecision(cast(Any, object()), "begründet")


def test_adapter_parity_registry_is_exact_and_closed() -> None:
    assert {
        "AbortMetadataRestore",
        "BeginMetadataRestore",
        "ConfirmDataReviewBatch",
        "CreateMetadataBackup",
        "CreatePlausibilityRuleVersion",
        "ImportHealthExport",
        "MigrateStore",
        "ResolveDataReviewCase",
        "RevokeDataReviewDecision",
        "RollbackMigration",
        "RunHistoricalReview",
        "RunRestingHeartRateAnalysis",
    } <= _names(WriteRequest)


def test_opening_an_existing_healthlab_is_read_only(tmp_path: Path) -> None:
    config = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "synthetic",
        real_store=tmp_path / "real",
    )
    with HealthLab.open(config):
        pass
    before = {
        path.relative_to(config.active_store): path.read_bytes()
        for path in config.active_store.rglob("*")
        if path.is_file()
    }

    with HealthLab.open(config):
        pass

    assert {
        path.relative_to(config.active_store): path.read_bytes()
        for path in config.active_store.rglob("*")
        if path.is_file()
    } == before
