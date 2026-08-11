from __future__ import annotations

import argparse
import json
import logging
import pydoc
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from personal_health_lab.adapters._config import load_runtime_config
from personal_health_lab.application import (
    AbortMetadataRestore,
    AbortMetadataRestorePlan,
    ActivityDays,
    AnalysisDefinitionId,
    AnalysisProvenance,
    AnalysisReceipt,
    AnalysisStatus,
    AsNeededIntakeAuditRevision,
    AsNeededIntakeCreate,
    AsNeededIntakeRestore,
    AsNeededIntakeRevise,
    AsNeededIntakeWithdraw,
    AssociationInterval,
    BatchDecisionTarget,
    BeginMetadataRestore,
    CanonicalHealthType,
    CanonicalUnit,
    CapacityCheck,
    ConfigurationError,
    ConfirmDataReviewBatch,
    ContextLogicalId,
    ContextRecords,
    CreateMetadataBackup,
    CreatePlausibilityRuleVersion,
    DailyActivityMetric,
    DailyContext,
    DailyNutritionFeature,
    DataConfirmation,
    DataCorrection,
    DataMode,
    DataReview,
    DataReviewBatchActionId,
    DataReviewBatchPlan,
    DataReviewBatchRevokePlan,
    DataReviewCaseDetail,
    DataReviewCaseId,
    DataReviewCaseKind,
    DataReviewDecisionId,
    DataReviewDecisionPlan,
    DataReviewSelection,
    FileVaultCheck,
    HealthLab,
    HistoricalReviewPlan,
    HistoricalReviewReceipt,
    ImportDetails,
    ImportHealthExport,
    ImportHealthExportPlan,
    ImportId,
    ImportReceipt,
    ImportStatus,
    IntakeReasonCategoryAuditRevision,
    IntakeReasonCategoryCreate,
    IntakeReasonCategoryRestore,
    IntakeReasonCategoryRevise,
    IntakeReasonCategoryWithdraw,
    LocalMeasurementExclusion,
    LocalWorkoutExclusion,
    MeasurementVersionId,
    MedicationAuditRevision,
    MedicationDeviationAuditRevision,
    MedicationLogicalId,
    MedicationPlanEntryId,
    MedicationRevisionId,
    MetadataBackupPlan,
    MetadataBackupReceipt,
    MetadataRestorePlan,
    MetadataRestoreReceipt,
    MigrateStore,
    OverviewSelection,
    PlanFingerprint,
    PlausibilityRules,
    PlausibilityRuleSpecification,
    PlausibilityRuleVersionPlan,
    PlausibilityRuleVersionReceipt,
    RecoveryStatus,
    ResolveDataReviewCase,
    RestingHeartRateAnalysisPlan,
    RestingHeartRateAnalysisResult,
    ReviseAsNeededIntake,
    ReviseIntakeReasonCategory,
    RevokeDataReviewDecision,
    RollbackMigration,
    RollbackMigrationPlan,
    RollbackMigrationReceipt,
    RunHistoricalReview,
    RunRestingHeartRateAnalysis,
    SingleDecisionTarget,
    SleepDays,
    SleepEpisode,
    SleepInterval,
    SnapshotDateSelection,
    SnapshotRef,
    SourceConflictResolution,
    SourceConflictStrategy,
    SourceDeletionResolution,
    SourceDeletionVerdict,
    SourceValueAcceptance,
    StoreMigrationPlan,
    StoreMigrationReceipt,
    WeightNutrition,
    WorkoutCorrection,
    Workouts,
    WorkspaceStatus,
    WriteApprovalStatus,
    WriteBatchDecisionReceipt,
    WriteDecisionReceipt,
    WriteNotStarted,
    WritePlan,
    WriteReceipt,
    WriteRequest,
)

_OUTPUT_SCHEMA_VERSION = "3.0"


def _medication_audit_revision_json(
    item: (
        MedicationAuditRevision
        | MedicationDeviationAuditRevision
        | AsNeededIntakeAuditRevision
        | IntakeReasonCategoryAuditRevision
    ),
) -> dict[str, object]:
    if isinstance(item, MedicationDeviationAuditRevision):
        return {
            "revision_id": str(item.revision_id),
            "previous_revision_id": None
            if item.previous_revision_id is None
            else str(item.previous_revision_id),
            "state": item.state,
            "regime_logical_id": str(item.regime_logical_id),
            "scheduled_at": item.scheduled_at.isoformat(),
            "actual_intakes": [
                {"taken_at": intake.taken_at.isoformat(), "amount": str(intake.amount)}
                for intake in item.actual_intakes
            ],
        }
    if isinstance(item, AsNeededIntakeAuditRevision):
        return {
            "revision_id": str(item.revision_id),
            "previous_revision_id": None
            if item.previous_revision_id is None
            else str(item.previous_revision_id),
            "state": item.state,
            "regime_logical_id": str(item.regime_logical_id),
            "entry_id": str(item.entry_id),
            "taken_at": item.taken_at.isoformat(),
            "amount": str(item.amount),
            "reason_category_logical_id": None
            if item.reason_category_logical_id is None
            else str(item.reason_category_logical_id),
        }
    if isinstance(item, IntakeReasonCategoryAuditRevision):
        return {
            "revision_id": str(item.revision_id),
            "previous_revision_id": None
            if item.previous_revision_id is None
            else str(item.previous_revision_id),
            "state": item.state,
            "name": item.name,
        }
    return {
        "revision_id": str(item.revision_id),
        "previous_revision_id": None
        if item.previous_revision_id is None
        else str(item.previous_revision_id),
        "starts_at": item.starts_at.isoformat(),
        "timezone": item.timezone,
    }


def _provenance_json(provenance: AnalysisProvenance | None) -> dict[str, object] | None:
    if provenance is None:
        return None
    return {
        "analysis_definition_id": str(provenance.analysis_definition_id),
        "analysis_run_id": str(provenance.analysis_run_id),
        "code_commit": provenance.code_commit,
        "code_diff_hash": provenance.code_diff_hash,
        "code_dirty": provenance.code_dirty,
        "config_hash": provenance.config_hash,
        "config_schema_version": provenance.config_schema_version,
        "environment_lock_hash": provenance.environment_lock_hash,
        "result_ref": None if provenance.result_id is None else str(provenance.result_id),
        "snapshot_ref": str(provenance.snapshot_id),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="healthlab")
    parser.add_argument("--mode", type=DataMode, choices=tuple(DataMode))
    parser.add_argument("--synthetic-store", type=Path)
    parser.add_argument("--real-store", type=Path)
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    overview = commands.add_parser("overview", help="Aktuelle Übersicht laden")
    overview.add_argument("--json", action="store_true", dest="as_json")
    import_command = commands.add_parser("import", help="Apple-Health-Export importieren")
    import_command.add_argument("package", type=Path)
    import_command.add_argument("--json", action="store_true", dest="as_json")
    import_command.add_argument("--execute", action="store_true")
    import_command.add_argument("--expect-plan", type=PlanFingerprint)
    import_details = commands.add_parser("import-details", help="Importdetails laden")
    import_details.add_argument("import_id", type=ImportId)
    import_details.add_argument("--json", action="store_true", dest="as_json")
    weight = commands.add_parser("weight-nutrition", help="Gewicht und Ernährung laden")
    weight.add_argument("--snapshot", type=SnapshotRef)
    weight.add_argument("--start-date", type=date.fromisoformat)
    weight.add_argument("--end-date", type=date.fromisoformat)
    weight.add_argument("--json", action="store_true", dest="as_json")
    sleep = commands.add_parser("sleep-days", help="Schlafnächte laden")
    sleep.add_argument("--snapshot", type=SnapshotRef)
    sleep.add_argument("--start-date", type=date.fromisoformat)
    sleep.add_argument("--end-date", type=date.fromisoformat)
    sleep.add_argument("--json", action="store_true", dest="as_json")
    activity = commands.add_parser("activity-days", help="Aktivitätstage laden")
    activity.add_argument("--snapshot", type=SnapshotRef)
    activity.add_argument("--start-date", type=date.fromisoformat)
    activity.add_argument("--end-date", type=date.fromisoformat)
    activity.add_argument("--json", action="store_true", dest="as_json")
    workouts = commands.add_parser("workouts", help="Trainingseinheiten laden")
    workouts.add_argument("--snapshot", type=SnapshotRef)
    workouts.add_argument("--start-date", type=date.fromisoformat)
    workouts.add_argument("--end-date", type=date.fromisoformat)
    workouts.add_argument("--json", action="store_true", dest="as_json")
    context = commands.add_parser("context", help="Manuellen Kontext laden")
    context_commands = context.add_subparsers(dest="context_command", required=True)
    context_days = context_commands.add_parser("days", help="Täglichen Kontext laden")
    context_days.add_argument("--snapshot", type=SnapshotRef)
    context_days.add_argument("--start-date", type=date.fromisoformat)
    context_days.add_argument("--end-date", type=date.fromisoformat)
    context_days.add_argument("--json", action="store_true", dest="as_json")
    context_records = context_commands.add_parser("records", help="Wirksame Kontextdaten laden")
    context_records.add_argument("--snapshot", type=SnapshotRef)
    context_records.add_argument("--json", action="store_true", dest="as_json")
    context_audit = context_commands.add_parser("audit", help="Kontextaudit laden")
    context_audit.add_argument("logical_id", type=ContextLogicalId)
    context_audit.add_argument("--json", action="store_true", dest="as_json")
    medication = commands.add_parser("medication", help="Medikamentenplan laden")
    medication_commands = medication.add_subparsers(dest="medication_command", required=True)
    medication_days = medication_commands.add_parser("days", help="Tägliche Dosisvorkommen laden")
    medication_days.add_argument("--snapshot", type=SnapshotRef)
    medication_days.add_argument("--start-date", type=date.fromisoformat)
    medication_days.add_argument("--end-date", type=date.fromisoformat)
    medication_days.add_argument("--json", action="store_true", dest="as_json")
    medication_plan = medication_commands.add_parser("plan", help="Regimeplan laden")
    medication_plan.add_argument("--snapshot", type=SnapshotRef)
    medication_plan.add_argument("--json", action="store_true", dest="as_json")
    medication_audit = medication_commands.add_parser("audit", help="Regimeaudit laden")
    medication_audit.add_argument("logical_id", type=MedicationLogicalId)
    medication_audit.add_argument("--json", action="store_true", dest="as_json")
    medication_reason = medication_commands.add_parser(
        "reason", help="Einnahmegrund anlegen oder revidieren"
    )
    medication_reason.set_defaults(as_json=False)
    reason_actions = medication_reason.add_subparsers(dest="medication_action", required=True)
    reason_create = reason_actions.add_parser("create")
    reason_create.add_argument("name")
    for action in ("revise", "restore"):
        command = reason_actions.add_parser(action)
        command.add_argument("logical_id", type=MedicationLogicalId)
        command.add_argument("expected_revision_id", type=MedicationRevisionId)
        command.add_argument("name")
    reason_withdraw = reason_actions.add_parser("withdraw")
    reason_withdraw.add_argument("logical_id", type=MedicationLogicalId)
    reason_withdraw.add_argument("expected_revision_id", type=MedicationRevisionId)
    reason_withdraw.add_argument("--reason", required=True)
    medication_intake = medication_commands.add_parser(
        "intake", help="Bedarfseinnahme anlegen oder revidieren"
    )
    medication_intake.set_defaults(as_json=False)
    intake_actions = medication_intake.add_subparsers(dest="medication_action", required=True)
    intake_create = intake_actions.add_parser("create")
    intake_create.add_argument("regime_logical_id", type=MedicationLogicalId)
    intake_create.add_argument("entry_id", type=MedicationPlanEntryId)
    intake_create.add_argument("taken_at", type=datetime.fromisoformat)
    intake_create.add_argument("amount", type=Decimal)
    intake_create.add_argument("--reason-category", type=MedicationLogicalId)
    for action in ("revise", "restore"):
        command = intake_actions.add_parser(action)
        command.add_argument("logical_id", type=MedicationLogicalId)
        command.add_argument("expected_revision_id", type=MedicationRevisionId)
        command.add_argument("taken_at", type=datetime.fromisoformat)
        command.add_argument("amount", type=Decimal)
        command.add_argument("--reason-category", type=MedicationLogicalId)
    intake_withdraw = intake_actions.add_parser("withdraw")
    intake_withdraw.add_argument("logical_id", type=MedicationLogicalId)
    intake_withdraw.add_argument("expected_revision_id", type=MedicationRevisionId)
    intake_withdraw.add_argument("--reason", required=True)
    analysis = commands.add_parser("analyze", help="Verzögerungsprofil analysieren")
    analysis.add_argument("--definition", default="lag-signal-v2")
    analysis.add_argument("--start-date", type=date.fromisoformat)
    analysis.add_argument("--end-date", type=date.fromisoformat)
    analysis.add_argument("--json", action="store_true", dest="as_json")
    analysis.add_argument("--execute", action="store_true")
    analysis.add_argument("--expect-plan", type=PlanFingerprint)
    review = commands.add_parser("review", help="Offene Datenprüffälle laden")
    review.add_argument("--json", action="store_true", dest="as_json")
    rules = commands.add_parser("rules", help="Plausibilitätsregeln laden")
    rules.add_argument("--json", action="store_true", dest="as_json")
    rule = commands.add_parser("rule", help="Neue Plausibilitätsregelversion anlegen")
    rule.add_argument("data_type", type=CanonicalHealthType, choices=tuple(CanonicalHealthType))
    rule.add_argument("--unit", required=True, type=CanonicalUnit, choices=tuple(CanonicalUnit))
    rule.add_argument("--lower", type=float)
    rule.add_argument("--upper", type=float)
    rule.add_argument("--personal", action="store_true")
    rule.add_argument("--effective-from", required=True, type=datetime.fromisoformat)
    rule.add_argument("--recommendation")
    rule.add_argument("--json", action="store_true", dest="as_json")
    rule.add_argument("--execute", action="store_true")
    rule.add_argument("--expect-plan", type=PlanFingerprint)
    historical = commands.add_parser("historical-review", help="Historische Datenprüfung ausführen")
    historical.add_argument(
        "data_type", type=CanonicalHealthType, choices=tuple(CanonicalHealthType)
    )
    historical.add_argument("--start-date", required=True, type=date.fromisoformat)
    historical.add_argument("--end-date", required=True, type=date.fromisoformat)
    historical.add_argument("--rule-version")
    historical.add_argument("--json", action="store_true", dest="as_json")
    historical.add_argument("--execute", action="store_true")
    historical.add_argument("--expect-plan", type=PlanFingerprint)
    resolve = commands.add_parser("review-resolve", help="Datenprüffall auflösen")
    resolve.add_argument("case_id", nargs="?", type=DataReviewCaseId)
    resolve.add_argument("--deletion-verdict", type=SourceDeletionVerdict)
    resolve.add_argument("--conflict-strategy", type=SourceConflictStrategy)
    resolve.add_argument("--confirm", action="store_true")
    resolve.add_argument("--correct", type=float)
    resolve.add_argument("--workout-correction", action="store_true")
    resolve.add_argument("--distance", type=float)
    resolve.add_argument("--energy", type=float)
    resolve.add_argument("--unit", type=CanonicalUnit, choices=tuple(CanonicalUnit))
    resolve.add_argument("--exclude-local", action="store_true")
    resolve.add_argument("--workout-exclusion", action="store_true")
    resolve.add_argument("--accept-source", action="store_true")
    resolve.add_argument("--measurement-version")
    resolve.add_argument("--reason")
    resolve.add_argument("--preferred-version")
    resolve.add_argument("--note")
    resolve.add_argument("--json", action="store_true", dest="as_json")
    resolve.add_argument("--execute", action="store_true")
    resolve.add_argument("--expect-plan", type=PlanFingerprint)
    batch = commands.add_parser("review-confirm-batch", help="Datenprüffälle gesammelt bestätigen")
    batch.add_argument("--kind", type=DataReviewCaseKind, choices=tuple(DataReviewCaseKind))
    batch.add_argument("--note")
    batch.add_argument("--json", action="store_true", dest="as_json")
    batch.add_argument("--execute", action="store_true")
    batch.add_argument("--expect-plan", type=PlanFingerprint)
    revoke = commands.add_parser("review-revoke", help="Datenprüfentscheidung widerrufen")
    revoke.add_argument("decision_id", nargs="?", type=DataReviewDecisionId)
    revoke.add_argument("--batch-action", type=DataReviewBatchActionId)
    revoke.add_argument("--reason", required=True)
    revoke.add_argument("--json", action="store_true", dest="as_json")
    revoke.add_argument("--execute", action="store_true")
    revoke.add_argument("--expect-plan", type=PlanFingerprint)
    backup = commands.add_parser("backup", help="Metadaten punktgenau sichern")
    backup.add_argument("target", type=Path)
    backup.add_argument("--json", action="store_true", dest="as_json")
    backup.add_argument("--execute", action="store_true")
    backup.add_argument("--expect-plan", type=PlanFingerprint)
    migration = commands.add_parser("migrate", help="Datenspeicherschema migrieren")
    migration.add_argument("--json", action="store_true", dest="as_json")
    migration.add_argument("--execute", action="store_true")
    migration.add_argument("--expect-plan", type=PlanFingerprint)
    rollback = commands.add_parser("rollback-migration", help="Letzte Migration zurückrollen")
    rollback.add_argument("--json", action="store_true", dest="as_json")
    rollback.add_argument("--execute", action="store_true")
    rollback.add_argument("--expect-plan", type=PlanFingerprint)
    restore = commands.add_parser("restore", help="Metadaten wiederherstellen")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--json", action="store_true", dest="as_json")
    restore.add_argument("--execute", action="store_true")
    restore.add_argument("--expect-plan", type=PlanFingerprint)
    abort_restore = commands.add_parser(
        "abort-restore", help="Ausstehende Wiederherstellung abbrechen"
    )
    abort_restore.add_argument("--json", action="store_true", dest="as_json")
    abort_restore.add_argument("--execute", action="store_true")
    abort_restore.add_argument("--expect-plan", type=PlanFingerprint)
    recovery_status = commands.add_parser("recovery-status", help="Wiederherstellungsstatus laden")
    recovery_status.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _plausibility_rules_json(rules: PlausibilityRules) -> dict[str, object]:
    return {
        "rules": [
            {
                "data_type": rule.data_type.value,
                "recommendation": {
                    "recommendation_id": rule.recommendation.recommendation_id,
                    "specification": _rule_specification_json(rule.recommendation.specification),
                },
                "versions": [
                    {
                        "created_at": version.created_at.isoformat(),
                        "effective_from": (
                            None
                            if version.effective_from is None
                            else version.effective_from.isoformat()
                        ),
                        "effective_offset_minutes": version.effective_offset_minutes,
                        "effective_timezone": version.effective_timezone,
                        "recommendation_id": version.recommendation_id,
                        "specification": _rule_specification_json(version.specification),
                        "version_id": version.version_id,
                    }
                    for version in rule.versions
                ],
            }
            for rule in rules.rules
        ]
    }


def _rule_specification_json(specification: PlausibilityRuleSpecification) -> dict[str, object]:
    return {
        "active": specification.active,
        "fixed_lower_bound": specification.fixed_lower_bound,
        "fixed_upper_bound": specification.fixed_upper_bound,
        "personal_range_enabled": specification.personal_range_enabled,
        "unit": specification.unit.value,
    }


def _data_review_json(
    review: DataReview, details: tuple[DataReviewCaseDetail, ...]
) -> dict[str, object]:
    detail_by_id = {detail.case.case_id: detail for detail in details}

    def effective_source(case_id: DataReviewCaseId) -> str | None:
        source = detail_by_id[case_id].effective_value_source
        return None if source is None else source.value

    def measured_at(case_id: DataReviewCaseId) -> str | None:
        value = detail_by_id[case_id].measured_at
        return None if value is None else value.isoformat()

    def canonical_unit(case_id: DataReviewCaseId) -> str | None:
        value = detail_by_id[case_id].canonical_unit
        return None if value is None else value.value

    return {
        "cases": [
            {
                "case_id": str(case.case_id),
                "allowed_actions": case.allowed_actions,
                "candidate_version_ids": tuple(str(item) for item in case.candidate_version_ids),
                "evidence_fingerprint": case.evidence_fingerprint,
                "kind": case.kind.value,
                "logical_measurement_id": (
                    None
                    if case.logical_measurement_id is None
                    else str(case.logical_measurement_id)
                ),
                "measurement_version_id": (
                    None
                    if case.measurement_version_id is None
                    else str(case.measurement_version_id)
                ),
                "rule_version_id": case.rule_version_id,
                "detail": {
                    "canonical_unit": canonical_unit(case.case_id),
                    "effective_value": detail_by_id[case.case_id].effective_value,
                    "effective_value_source": effective_source(case.case_id),
                    "measured_at": measured_at(case.case_id),
                    "reasons": [
                        {
                            "code": reason.code.value,
                            "lower_bound": reason.lower_bound,
                            "upper_bound": reason.upper_bound,
                            "unit": reason.unit,
                        }
                        for reason in detail_by_id[case.case_id].reasons
                    ],
                    "source_type": detail_by_id[case.case_id].source_type,
                },
            }
            for case in review.cases
        ],
        "selection": {"kind": None},
        "snapshot_ref": None if review.snapshot_ref is None else str(review.snapshot_ref),
        "status": review.status.value,
        "cycles": [
            {
                "base_snapshot_ref": (
                    None if cycle.base_snapshot_ref is None else str(cycle.base_snapshot_ref)
                ),
                "cycle_id": str(cycle.cycle_id),
                "end_date": None if cycle.end_date is None else cycle.end_date.isoformat(),
                "kind": cycle.kind.value,
                "open_case_count": cycle.open_case_count,
                "rule_version_id": cycle.rule_version_id,
                "snapshot_ref": str(cycle.snapshot_ref),
                "start_date": (None if cycle.start_date is None else cycle.start_date.isoformat()),
                "status": cycle.status.value,
            }
            for cycle in review.cycles
        ],
    }


def _analysis_json(result: RestingHeartRateAnalysisResult | None) -> dict[str, object] | None:
    if result is None:
        return None

    def interval_json(interval: AssociationInterval | None) -> dict[str, float] | None:
        if interval is None:
            return None
        return {
            "lower_per_100_kcal": interval.lower_per_100_kcal,
            "upper_per_100_kcal": interval.upper_per_100_kcal,
            "lower_per_personal_standard_deviation": (
                interval.lower_per_personal_standard_deviation
            ),
            "upper_per_personal_standard_deviation": (
                interval.upper_per_personal_standard_deviation
            ),
        }

    cumulative = result.cumulative_association
    return {
        "analysis_definition_id": str(result.analysis_definition_id),
        "completed_at": (None if result.completed_at is None else result.completed_at.isoformat()),
        "cumulative_association": {
            "direction": cumulative.direction.value,
            "estimate_per_100_kcal": cumulative.estimate_per_100_kcal,
            "estimate_per_personal_standard_deviation": (
                cumulative.estimate_per_personal_standard_deviation
            ),
            "pointwise_interval": interval_json(cumulative.pointwise_interval),
        },
        "diagnostics": {
            "association_guardrail": result.diagnostics.association_guardrail,
            "bootstrap_resamples": result.diagnostics.bootstrap_resamples,
            "bootstrap_successes": result.diagnostics.bootstrap_successes,
            "complete_days": result.diagnostics.complete_days,
            "feature_dependency": result.diagnostics.feature_dependency,
            "model_readiness": result.diagnostics.model_readiness,
            "input_completeness": result.diagnostics.input_completeness,
            "outcome_standard_deviation": result.diagnostics.outcome_standard_deviation,
            "maximum_time_series_gap_days": (result.diagnostics.maximum_time_series_gap_days),
        },
        "data_status": result.data_status.value,
        "status_facts_recorded": result.status_facts_recorded,
        "data_status_reasons": [
            {
                "code": reason.code.value,
                "evidence_ids": reason.evidence_ids,
            }
            for reason in result.data_status_reasons
        ],
        "freshness": result.freshness.value,
        "lag_associations": [
            {
                "direction": item.direction.value,
                "estimate_per_100_kcal": item.estimate_per_100_kcal,
                "estimate_per_personal_standard_deviation": (
                    item.estimate_per_personal_standard_deviation
                ),
                "exposure_unit": item.exposure_unit.value,
                "lag_days": item.lag_days,
                "outcome_unit": item.outcome_unit.value,
                "pointwise_interval": interval_json(item.pointwise_interval),
                "simultaneous_band": interval_json(item.simultaneous_band),
            }
            for item in result.lag_associations
        ],
        "model_maturity": result.model_maturity,
        "maturity_criteria": [
            {
                "code": criterion.code.value,
                "observed_value": criterion.observed_value,
                "passed": criterion.passed,
                "threshold": criterion.threshold,
            }
            for criterion in result.maturity_criteria
        ],
        "methodology": {
            "block_length_days": result.methodology.block_length_days,
            "bootstrap_method": result.methodology.bootstrap_method,
            "interval_level": result.methodology.interval_level,
            "max_feature_dependency": result.methodology.max_feature_dependency,
            "minimum_bootstrap_success_rate": (result.methodology.minimum_bootstrap_success_rate),
            "minimum_input_completeness": result.methodology.minimum_input_completeness,
            "minimum_observations": result.methodology.minimum_observations,
            "minimum_outcome_standard_deviation": (
                result.methodology.minimum_outcome_standard_deviation
            ),
            "maximum_time_series_gap_days": (result.methodology.maximum_time_series_gap_days),
            "random_seed": result.methodology.random_seed,
            "resample_count": result.methodology.resample_count,
            "ridge_penalty": result.methodology.ridge_penalty,
            "robust_observations": result.methodology.robust_observations,
        },
        "personal_standard_deviation_kcal": result.personal_standard_deviation_kcal,
        "provenance": _provenance_json(result.provenance),
        "reproducibility": result.reproducibility.value,
        "snapshot_ref": str(result.snapshot_id),
    }


def _workspace_json(status: WorkspaceStatus) -> dict[str, object]:
    return {
        "allowed_reads": status.allowed_reads,
        "allowed_writes": status.allowed_writes,
        "mode": status.mode.value,
        "person_binding": status.person_binding.value,
        "state": status.state.value,
        "store_id": None if status.store_id is None else str(status.store_id),
    }


def _import_details_json(
    details: ImportDetails, runtime_config: Mapping[str, object]
) -> dict[str, object]:
    counts = details.canonical_counts
    return {
        "canonical_counts": {
            "anomaly_count": counts.anomaly_count,
            "logical_measurement_count": counts.logical_measurement_count,
            "measurement_version_count": counts.measurement_version_count,
            "package_record_count": counts.package_record_count,
            "record_count": counts.record_count,
            "source_occurrence_count": counts.source_occurrence_count,
        },
        "import_id": str(details.import_id),
        "kind": "import_details",
        "runtime_config": dict(runtime_config),
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "unsupported_content": [
            {
                "category": item.category.value,
                "count": item.count,
                "external_identifier": item.external_identifier,
            }
            for item in details.unsupported_content
        ],
    }


def _weight_nutrition_json(
    projection: WeightNutrition,
    selection: SnapshotDateSelection,
    runtime_config: Mapping[str, object],
) -> dict[str, object]:
    def feature_json(feature: DailyNutritionFeature) -> dict[str, object]:
        return {
            "data_type": feature.data_type.value,
            "logical_measurement_ids": [str(value) for value in feature.logical_measurement_ids],
            "measurement_version_ids": [str(value) for value in feature.measurement_version_ids],
            "quality_status": feature.quality_status.value,
            "review_case_ids": [str(value) for value in feature.review_case_ids],
            "unit": feature.unit.value,
            "value": feature.value,
        }

    return {
        "days": [
            {
                "day": item.day.isoformat(),
                "logical_measurement_ids": [str(value) for value in item.logical_measurement_ids],
                "measurement_version_ids": [str(value) for value in item.measurement_version_ids],
                "quality_status": item.quality_status.value,
                "review_case_ids": [str(value) for value in item.review_case_ids],
                "status": item.status.value,
                "value_kg": item.value_kg,
            }
            for item in projection.days
        ],
        "kind": "weight_nutrition",
        "nutrition_days": [
            {
                "carbohydrates": feature_json(item.carbohydrates),
                "day": item.day.isoformat(),
                "energy": feature_json(item.energy),
                "protein": feature_json(item.protein),
                "total_fat": feature_json(item.total_fat),
            }
            for item in projection.nutrition_days
        ],
        "healthkit_nutrition_samples": [
            {
                "data_type": item.data_type.value,
                "device": item.device,
                "disposition": item.disposition,
                "effective_value": item.effective_value,
                "is_selected": item.is_selected,
                "logical_measurement_id": str(item.logical_measurement_id),
                "measurement_local_day": item.measurement_local_day.isoformat(),
                "measurement_version_id": str(item.measurement_version_id),
                "original_unit": item.original_unit,
                "original_value": item.original_value,
                "review_case_ids": [str(value) for value in item.review_case_ids],
                "source_end": item.source_end.isoformat(),
                "source_name": item.source_name,
                "source_start": item.source_start.isoformat(),
                "source_updated_at": item.source_updated_at.isoformat(),
                "source_version": item.source_version,
                "unit": item.unit.value,
                "value": item.value,
            }
            for item in projection.healthkit_nutrition_samples
        ],
        "runtime_config": dict(runtime_config),
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "selection": {
            "end_date": None if selection.end_date is None else selection.end_date.isoformat(),
            "snapshot_ref": (
                None if selection.snapshot_ref is None else str(selection.snapshot_ref)
            ),
            "start_date": (
                None if selection.start_date is None else selection.start_date.isoformat()
            ),
        },
        "snapshot_ref": None if projection.snapshot_ref is None else str(projection.snapshot_ref),
        "status": projection.status.value,
        "weight_measurements": [
            {
                "device": item.device,
                "disposition": item.disposition,
                "effective_value_kg": item.effective_value_kg,
                "is_selected": item.is_selected,
                "logical_measurement_id": str(item.logical_measurement_id),
                "measurement_local_day": item.measurement_local_day.isoformat(),
                "measurement_version_id": str(item.measurement_version_id),
                "original_unit": item.original_unit,
                "original_value": item.original_value,
                "review_case_ids": [str(value) for value in item.review_case_ids],
                "source_end": item.source_end.isoformat(),
                "source_name": item.source_name,
                "source_start": item.source_start.isoformat(),
                "source_updated_at": item.source_updated_at.isoformat(),
                "source_version": item.source_version,
                "value_kg": item.value_kg,
            }
            for item in projection.weight_measurements
        ],
    }


def _sleep_days_json(
    projection: SleepDays,
    selection: SnapshotDateSelection,
    runtime_config: Mapping[str, object],
) -> dict[str, object]:
    def duration(value: timedelta | None) -> float | None:
        return None if value is None else value.total_seconds()

    def episode(item: SleepEpisode) -> dict[str, object]:
        return {
            "asleep_awake_conflict_seconds": duration(item.asleep_awake_conflict),
            "asleep_core_seconds": duration(item.asleep_core),
            "asleep_deep_seconds": duration(item.asleep_deep),
            "asleep_rem_seconds": duration(item.asleep_rem),
            "asleep_unspecified_seconds": duration(item.asleep_unspecified),
            "detailed_stage_coverage_ratio": item.detailed_stage_coverage_ratio,
            "end": item.end.isoformat(),
            "first_observed_asleep": (
                None
                if item.first_observed_asleep is None
                else item.first_observed_asleep.isoformat()
            ),
            "in_bed_seconds": duration(item.in_bed),
            "interval_ids": [str(value) for value in item.interval_ids],
            "last_observed_asleep": (
                None if item.last_observed_asleep is None else item.last_observed_asleep.isoformat()
            ),
            "observed_awake_seconds": duration(item.observed_awake),
            "observed_coverage_ratio": item.observed_coverage_ratio,
            "observed_sleep_seconds": duration(item.observed_sleep),
            "removed_same_state_overlap_seconds": duration(item.removed_same_state_overlap),
            "stage_ambiguous_seconds": duration(item.stage_ambiguous),
            "start": item.start.isoformat(),
            "uncovered_gap_seconds": duration(item.uncovered_gap),
        }

    def interval(item: SleepInterval) -> dict[str, object]:
        return {
            "canonical_category": item.canonical_category.value,
            "device": item.device,
            "is_selected": item.is_selected,
            "logical_measurement_id": str(item.logical_measurement_id),
            "measurement_version_id": str(item.measurement_version_id),
            "original_category": item.original_category,
            "source_class": item.source_class.value,
            "source_end": item.source_end.isoformat(),
            "source_name": item.source_name,
            "source_start": item.source_start.isoformat(),
            "source_updated_at": item.source_updated_at.isoformat(),
            "source_version": item.source_version,
        }

    return {
        "accepted_intervals": [interval(item) for item in projection.accepted_intervals],
        "days": [
            {
                "day": item.day.isoformat(),
                "nap_count": item.nap_count,
                "nap_observed_sleep_seconds": duration(item.nap_observed_sleep),
                "naps": [episode(value) for value in item.naps],
                "primary_episode": (
                    None if item.primary_episode is None else episode(item.primary_episode)
                ),
                "quality": {
                    "accepted_interval_count": item.quality.accepted_interval_count,
                    "contributing_watch_source_count": item.quality.contributing_watch_source_count,
                    "derivation_version": item.quality.derivation_version,
                    "primary_selection_ambiguous": item.quality.primary_selection_ambiguous,
                    "rejected_interval_count": item.quality.rejected_interval_count,
                    "source_counts": [
                        {
                            "accepted_interval_count": value.accepted_interval_count,
                            "rejected_interval_count": value.rejected_interval_count,
                            "source_class": value.source_class.value,
                        }
                        for value in item.quality.source_counts
                    ],
                    "source_classifier_version": item.quality.source_classifier_version,
                },
                "status": item.status.value,
            }
            for item in projection.days
        ],
        "kind": "sleep_days",
        "rejected_intervals": [interval(item) for item in projection.rejected_intervals],
        "runtime_config": dict(runtime_config),
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "selection": {
            "end_date": None if selection.end_date is None else selection.end_date.isoformat(),
            "snapshot_ref": (
                None if selection.snapshot_ref is None else str(selection.snapshot_ref)
            ),
            "start_date": (
                None if selection.start_date is None else selection.start_date.isoformat()
            ),
        },
        "snapshot_ref": None if projection.snapshot_ref is None else str(projection.snapshot_ref),
    }


def _activity_days_json(
    projection: ActivityDays,
    selection: SnapshotDateSelection,
    runtime_config: Mapping[str, object],
) -> dict[str, object]:
    def metric(item: DailyActivityMetric) -> dict[str, object]:
        return {
            "data_type": item.data_type.value,
            "logical_measurement_ids": [str(value) for value in item.logical_measurement_ids],
            "measurement_version_ids": [str(value) for value in item.measurement_version_ids],
            "quality_status": item.quality_status.value,
            "review_case_ids": [str(value) for value in item.review_case_ids],
            "unit": item.unit.value,
            "value": item.value,
            "watch_value": item.watch_value,
            "iphone_value": item.iphone_value,
        }

    return {
        "days": [
            {
                "active_energy": metric(item.active_energy),
                "coverage_segments": [
                    {
                        "kind": segment.kind.value,
                        "source_start": segment.source_start.isoformat(),
                        "source_end": segment.source_end.isoformat(),
                    }
                    for segment in item.coverage_segments
                ],
                "day": item.day.isoformat(),
                "exercise_time": metric(item.exercise_time),
                "incomplete_reasons": list(item.incomplete_reasons),
                "is_complete": item.is_complete,
                "step_count": metric(item.step_count),
                "walking_running_distance": metric(item.walking_running_distance),
            }
            for item in projection.days
        ],
        "coverage_gap_minutes": projection.coverage_gap_minutes,
        "derivation_version": projection.derivation_version,
        "kind": "activity_days",
        "measurements": [
            {
                "data_type": item.data_type.value,
                "device": item.device,
                "disposition": item.disposition,
                "effective_value": item.effective_value,
                "is_selected": item.is_selected,
                "logical_measurement_id": str(item.logical_measurement_id),
                "measurement_local_day": item.measurement_local_day.isoformat(),
                "measurement_version_id": str(item.measurement_version_id),
                "original_unit": item.original_unit,
                "original_value": item.original_value,
                "review_case_ids": [str(value) for value in item.review_case_ids],
                "source_class": item.source_class.value,
                "source_end": item.source_end.isoformat(),
                "source_name": item.source_name,
                "source_start": item.source_start.isoformat(),
                "source_updated_at": item.source_updated_at.isoformat(),
                "source_version": item.source_version,
                "suppression_reason": item.suppression_reason,
                "unit": item.unit.value,
                "value": item.value,
            }
            for item in projection.measurements
        ],
        "runtime_config": dict(runtime_config),
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "selection": {
            "end_date": None if selection.end_date is None else selection.end_date.isoformat(),
            "snapshot_ref": (
                None if selection.snapshot_ref is None else str(selection.snapshot_ref)
            ),
            "start_date": (
                None if selection.start_date is None else selection.start_date.isoformat()
            ),
        },
        "snapshot_ref": None if projection.snapshot_ref is None else str(projection.snapshot_ref),
        "source_classifier_version": projection.source_classifier_version,
        "status": projection.status.value,
    }


def _workouts_json(
    projection: Workouts,
    selection: SnapshotDateSelection,
    runtime_config: Mapping[str, object],
) -> dict[str, object]:
    return {
        "aggregates": [
            {
                "active_energy_kilocalories": item.active_energy_kilocalories,
                "day": item.day.isoformat(),
                "distance_kilometers": item.distance_kilometers,
                "duration_minutes": item.duration_minutes,
                "original_activity_type": item.original_activity_type,
                "review_case_ids": [str(value) for value in item.review_case_ids],
                "workout_count": item.workout_count,
            }
            for item in projection.aggregates
        ],
        "kind": "workouts",
        "runtime_config": dict(runtime_config),
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "selection": {
            "end_date": None if selection.end_date is None else selection.end_date.isoformat(),
            "snapshot_ref": None if selection.snapshot_ref is None else str(selection.snapshot_ref),
            "start_date": None
            if selection.start_date is None
            else selection.start_date.isoformat(),
        },
        "snapshot_ref": None if projection.snapshot_ref is None else str(projection.snapshot_ref),
        "status": projection.status.value,
        "workouts": [
            {
                "active_energy_kilocalories": item.active_energy_kilocalories,
                "device": item.device,
                "distance_kilometers": item.distance_kilometers,
                "effective_duration_minutes": item.effective_duration_minutes,
                "is_selected": item.is_selected,
                "logical_workout_id": str(item.logical_workout_id),
                "measurement_local_day": item.measurement_local_day.isoformat(),
                "original_activity_type": item.original_activity_type,
                "reported_duration_minutes": item.reported_duration_minutes,
                "review_case_ids": [str(value) for value in item.review_case_ids],
                "source_end": item.source_end.isoformat(),
                "source_name": item.source_name,
                "source_start": item.source_start.isoformat(),
                "source_updated_at": item.source_updated_at.isoformat(),
                "source_version": item.source_version,
                "strong_source_id_hash": item.strong_source_id_hash,
                "workout_version_id": str(item.workout_version_id),
            }
            for item in projection.workouts
        ],
    }


def _daily_context_json(
    projection: DailyContext, selection: SnapshotDateSelection, runtime_config: Mapping[str, object]
) -> dict[str, object]:
    return {
        "context_as_of_date": None
        if projection.context_as_of_date is None
        else projection.context_as_of_date.isoformat(),
        "context_timezone": projection.context_timezone,
        "days": [
            {
                "day": item.day.isoformat(),
                "illness_origin": item.illness_origin.value,
                "highest_illness_severity": (
                    None
                    if item.highest_illness_severity is None
                    else item.highest_illness_severity.value
                ),
                "stress_origin": item.stress_origin.value,
                "stress_level": None if item.stress_level is None else item.stress_level.value,
                "custom_context_labels": list(item.custom_context_labels),
            }
            for item in projection.days
        ],
        "kind": "daily_context",
        "runtime_config": dict(runtime_config),
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "selection": {
            "end_date": None if selection.end_date is None else selection.end_date.isoformat(),
            "snapshot_ref": None if selection.snapshot_ref is None else str(selection.snapshot_ref),
            "start_date": None
            if selection.start_date is None
            else selection.start_date.isoformat(),
        },
        "snapshot_ref": None if projection.snapshot_ref is None else str(projection.snapshot_ref),
    }


def _context_records_json(
    projection: ContextRecords, runtime_config: Mapping[str, object]
) -> dict[str, object]:
    return {
        "coverage_start": None
        if projection.coverage_start is None
        else {
            "logical_id": str(projection.coverage_start.logical_id),
            "revision_id": str(projection.coverage_start.revision_id),
            "start_date": projection.coverage_start.start_date.isoformat(),
        },
        "custom_labels": [
            {
                "logical_id": str(item.logical_id),
                "revision_id": str(item.revision_id),
                "name": item.name,
            }
            for item in projection.custom_labels
        ],
        "custom_periods": [
            {
                "logical_id": str(item.logical_id),
                "revision_id": str(item.revision_id),
                "label_logical_id": str(item.label_logical_id),
                "start_date": item.start_date.isoformat(),
                "end_date": None if item.end_date is None else item.end_date.isoformat(),
                "note": item.note,
            }
            for item in projection.custom_periods
        ],
        "kind": "context_records",
        "runtime_config": dict(runtime_config),
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "snapshot_ref": None if projection.snapshot_ref is None else str(projection.snapshot_ref),
    }


def _recovery_status_json(
    status: RecoveryStatus,
    runtime_config: Mapping[str, object],
    workspace: WorkspaceStatus,
) -> dict[str, object]:
    return {
        "kind": "recovery_status",
        "recovery": {
            "audit_max_position": status.audit_max_position,
            "backup_id": str(status.backup_id),
            "migration_steps": status.migration_steps,
            "original_backup_sha256": status.original_backup_sha256,
            "restore_id": str(status.restore_id),
            "source_schema_version": status.source_schema_version,
            "status": status.status.value,
            "target_schema_version": status.target_schema_version,
            "working_copy_sha256": status.working_copy_sha256,
        },
        "runtime_config": dict(runtime_config),
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "workspace": _workspace_json(workspace),
    }


def _filevault_json(filevault: FileVaultCheck | None) -> dict[str, str | None] | None:
    if filevault is None:
        return None
    return {
        "status": filevault.status.value,
        "target_volume": filevault.target_volume,
        "reason": None if filevault.reason is None else filevault.reason.value,
    }


def _capacity_json(capacity: CapacityCheck | None) -> dict[str, object] | None:
    if capacity is None:
        return None
    return {
        "available_bytes": capacity.available_bytes,
        "estimate_bytes": capacity.estimate_bytes,
        "fragment_size": capacity.fragment_size,
        "method_id": capacity.method_id,
        "minimum_remaining_bytes": capacity.minimum_remaining_bytes,
        "reason": None if capacity.reason is None else capacity.reason.value,
        "required_bytes": capacity.required_bytes,
        "safety_margin_bytes": capacity.safety_margin_bytes,
        "status": capacity.status.value,
        "target_volume": capacity.target_volume,
    }


def _write_plan_json(
    plan: WritePlan,
    runtime_config: Mapping[str, object],
    workspace: WorkspaceStatus,
) -> dict[str, object]:
    details = plan.details
    if isinstance(details, ImportHealthExportPlan):
        detail_json: dict[str, object] = {
            "package_hash": details.package_hash,
            "package_size": details.package_size,
            "type": "import_health_export",
        }
        request_json: dict[str, object] = {
            "package": "<redacted>",
            "type": "import_health_export",
        }
    elif isinstance(details, MetadataBackupPlan):
        detail_json = {
            "audit_max_position": details.audit_max_position,
            "backup_id": str(details.backup_id),
            "canonical_content_sha256": details.canonical_content_sha256,
            "target_file": details.target_file,
            "type": "create_metadata_backup",
        }
        request_json = {"target": "<redacted>", "type": "create_metadata_backup"}
    elif isinstance(details, MetadataRestorePlan):
        detail_json = {
            "audit_max_position": details.audit_max_position,
            "backup_id": None if details.backup_id is None else str(details.backup_id),
            "migration_steps": details.migration_steps,
            "original_backup_sha256": details.original_backup_sha256,
            "restore_id": None if details.restore_id is None else str(details.restore_id),
            "source_schema_version": details.source_schema_version,
            "source_store_id": (
                None if details.source_store_id is None else str(details.source_store_id)
            ),
            "target_schema_version": details.target_schema_version,
            "type": "begin_metadata_restore",
            "working_copy_sha256": details.working_copy_sha256,
        }
        request_json = {"backup": "<redacted>", "type": "begin_metadata_restore"}
    elif isinstance(details, AbortMetadataRestorePlan):
        detail_json = {
            "backup_id": None if details.backup_id is None else str(details.backup_id),
            "restore_id": None if details.restore_id is None else str(details.restore_id),
            "type": "abort_metadata_restore",
        }
        request_json = {"type": "abort_metadata_restore"}
    elif isinstance(details, StoreMigrationPlan):
        detail_json = {
            "affected_snapshot_refs": tuple(str(item) for item in details.affected_snapshot_refs),
            "backup_file": details.backup_file,
            "existing_analyses_become_stale": details.existing_analyses_become_stale,
            "source_version": details.source_version,
            "steps": details.steps,
            "snapshot_source_version": details.snapshot_source_version,
            "snapshot_as_of": (
                None if details.snapshot_as_of is None else details.snapshot_as_of.isoformat()
            ),
            "snapshot_steps": details.snapshot_steps,
            "snapshot_target_version": details.snapshot_target_version,
            "target_version": details.target_version,
            "type": "migrate_store",
        }
        request_json = {"type": "migrate_store"}
    elif isinstance(details, RollbackMigrationPlan):
        detail_json = {
            "backup_file": details.backup_file,
            "backup_sha256": details.backup_sha256,
            "current_snapshot_ref": (
                None if details.current_snapshot_ref is None else str(details.current_snapshot_ref)
            ),
            "migration_operation_id": (
                None
                if details.migration_operation_id is None
                else str(details.migration_operation_id)
            ),
            "restored_snapshot_ref": (
                None
                if details.restored_snapshot_ref is None
                else str(details.restored_snapshot_ref)
            ),
            "source_version": details.source_version,
            "target_version": details.target_version,
            "type": "rollback_migration",
        }
        request_json = {"type": "rollback_migration"}
    elif isinstance(details, PlausibilityRuleVersionPlan):
        detail_json = {
            "active_snapshot_ref": (
                None if details.active_snapshot_ref is None else str(details.active_snapshot_ref)
            ),
            "previous_version_id": details.previous_version_id,
            "proposed_version_id": details.proposed_version_id,
            "type": "create_plausibility_rule_version",
        }
        request_json = {"type": "create_plausibility_rule_version"}
    elif isinstance(details, DataReviewDecisionPlan):
        detail_json = {
            "active_snapshot_ref": (
                None if details.active_snapshot_ref is None else str(details.active_snapshot_ref)
            ),
            "case_id": None if details.case_id is None else str(details.case_id),
            "type": "data_review_decision",
        }
        request_json = {"type": "data_review_decision"}
    elif isinstance(details, DataReviewBatchPlan):
        detail_json = {
            "active_snapshot_ref": (
                None if details.active_snapshot_ref is None else str(details.active_snapshot_ref)
            ),
            "selection": {
                "kind": None if details.selection.kind is None else details.selection.kind.value
            },
            "matches": [
                {
                    "case_id": str(item.case_id),
                    "kind": item.kind.value,
                    "measurement_version_id": (
                        None
                        if item.measurement_version_id is None
                        else str(item.measurement_version_id)
                    ),
                    "rule_version_id": item.rule_version_id,
                    "evidence_fingerprint": item.evidence_fingerprint,
                    "effective_value": item.effective_value,
                    "canonical_unit": (
                        None if item.canonical_unit is None else item.canonical_unit.value
                    ),
                }
                for item in details.matches
            ],
            "count": details.count,
            "type": "confirm_data_review_batch",
        }
        request_json = {"type": "confirm_data_review_batch"}
    elif isinstance(details, DataReviewBatchRevokePlan):
        detail_json = {
            "active_snapshot_ref": (
                None if details.active_snapshot_ref is None else str(details.active_snapshot_ref)
            ),
            "batch_action_id": str(details.batch_action_id),
            "decision_ids": tuple(str(item) for item in details.decision_ids),
            "count": details.count,
            "type": "revoke_data_review_batch",
        }
        request_json = {"type": "revoke_data_review_batch"}
    elif isinstance(details, HistoricalReviewPlan):
        detail_json = {
            "base_snapshot_ref": (
                None if details.base_snapshot_ref is None else str(details.base_snapshot_ref)
            ),
            "end_date": details.end_date.isoformat(),
            "rule_version_id": details.rule_version_id,
            "start_date": details.start_date.isoformat(),
            "type": "run_historical_review",
        }
        request_json = {"type": "run_historical_review"}
    elif isinstance(details, RestingHeartRateAnalysisPlan):
        detail_json = {
            "analysis_definition_id": str(details.analysis_definition_id),
            "base_snapshot_ref": (
                None if details.base_snapshot_ref is None else str(details.base_snapshot_ref)
            ),
            "end_date": None if details.end_date is None else details.end_date.isoformat(),
            "schema_version": details.schema_version,
            "start_date": None if details.start_date is None else details.start_date.isoformat(),
            "type": "run_resting_heart_rate_analysis",
        }
        request_json = {"type": "run_resting_heart_rate_analysis"}
    else:
        raise TypeError("Nicht unterstützte Schreibplandetails.")
    return {
        "approval": {"status": plan.approval.status.value},
        "confirmations": tuple(item.value for item in plan.confirmations),
        "details": detail_json,
        "diagnostics": plan.diagnostics,
        "fingerprint": str(plan.fingerprint),
        "kind": "write_plan",
        "preflight": {
            "capacity": _capacity_json(plan.preflight.capacity),
            "filevault": _filevault_json(plan.preflight.filevault),
        },
        "request": request_json,
        "runtime_config": dict(runtime_config),
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "workspace": _workspace_json(workspace),
    }


def _write_receipt_json(
    receipt: WriteReceipt,
    runtime_config: Mapping[str, object],
    analysis: dict[str, object] | None = None,
) -> dict[str, object]:
    result = receipt.result
    if isinstance(result, ImportReceipt):
        result_json: dict[str, object] = {
            "anomaly_count": result.anomaly_count,
            "diagnostics": result.diagnostics,
            "import_id": str(result.import_id),
            "logical_measurement_count": result.logical_measurement_count,
            "measurement_version_count": result.measurement_version_count,
            "package_hash": result.package_hash,
            "package_record_count": result.package_record_count,
            "record_count": result.record_count,
            "source_occurrence_count": result.source_occurrence_count,
            "snapshot_ref": str(result.snapshot_ref) if result.snapshot_ref else None,
            "status": result.status.value,
            "type": "import_health_export",
        }
    elif isinstance(result, MetadataBackupReceipt):
        result_json = {
            "audit_max_position": result.audit_max_position,
            "backup_id": str(result.backup_id),
            "canonical_content_sha256": result.canonical_content_sha256,
            "created_at_utc": result.created_at_utc.isoformat(),
            "diagnostics": result.diagnostics,
            "status": result.status.value,
            "target_file": result.target_file,
            "type": "create_metadata_backup",
        }
    elif isinstance(result, MetadataRestoreReceipt):
        result_json = {
            "backup_id": str(result.backup_id),
            "diagnostics": result.diagnostics,
            "original_backup_sha256": result.original_backup_sha256,
            "restore_id": str(result.restore_id),
            "status": result.status.value,
            "type": "metadata_restore",
            "working_copy_sha256": result.working_copy_sha256,
        }
    elif isinstance(result, StoreMigrationReceipt):
        result_json = {
            "backup_file": result.backup_file,
            "diagnostics": result.diagnostics,
            "source_version": result.source_version,
            "snapshot_source_version": result.snapshot_source_version,
            "snapshot_as_of": (
                None if result.snapshot_as_of is None else result.snapshot_as_of.isoformat()
            ),
            "snapshot_steps": result.snapshot_steps,
            "snapshot_target_version": result.snapshot_target_version,
            "status": result.status.value,
            "steps": result.steps,
            "target_version": result.target_version,
            "type": "migrate_store",
        }
    elif isinstance(result, RollbackMigrationReceipt):
        result_json = {
            "backup_file": result.backup_file,
            "diagnostics": result.diagnostics,
            "migration_operation_id": str(result.migration_operation_id),
            "restored_snapshot_ref": (
                None if result.restored_snapshot_ref is None else str(result.restored_snapshot_ref)
            ),
            "source_version": result.source_version,
            "status": result.status.value,
            "target_version": result.target_version,
            "type": "rollback_migration",
            "unreferenced_snapshot_ref": (
                None
                if result.unreferenced_snapshot_ref is None
                else str(result.unreferenced_snapshot_ref)
            ),
        }
    elif isinstance(result, PlausibilityRuleVersionReceipt):
        result_json = {
            "diagnostics": result.diagnostics,
            "rule_version_id": result.rule_version_id,
            "snapshot_ref": None if result.snapshot_ref is None else str(result.snapshot_ref),
            "status": result.status.value,
            "type": "create_plausibility_rule_version",
        }
    elif isinstance(result, WriteDecisionReceipt):
        result_json = {
            "decision_id": str(result.decision_id),
            "diagnostics": result.diagnostics,
            "snapshot_ref": str(result.snapshot_ref),
            "status": result.status.value,
            "type": "data_review_decision",
        }
    elif isinstance(result, WriteBatchDecisionReceipt):
        result_json = {
            "batch_action_id": str(result.batch_action_id),
            "decision_ids": tuple(str(item) for item in result.decision_ids),
            "diagnostics": result.diagnostics,
            "snapshot_ref": str(result.snapshot_ref),
            "status": result.status.value,
            "type": "data_review_batch",
        }
    elif isinstance(result, HistoricalReviewReceipt):
        result_json = {
            "cycle_id": str(result.cycle_id),
            "diagnostics": result.diagnostics,
            "open_case_count": result.open_case_count,
            "snapshot_ref": str(result.snapshot_ref),
            "status": result.status.value,
            "type": "run_historical_review",
        }
    elif isinstance(result, AnalysisReceipt):
        result_json = {
            "analysis": analysis,
            "analysis_definition_id": str(result.analysis_definition_id),
            "analysis_run_id": str(result.analysis_run_id),
            "diagnostics": result.diagnostics,
            "model_maturity": (
                None if result.model_maturity is None else result.model_maturity.value
            ),
            "provenance": _provenance_json(result.provenance),
            "result_ref": None if result.result_ref is None else str(result.result_ref),
            "snapshot_ref": None if result.snapshot_ref is None else str(result.snapshot_ref),
            "status": result.status.value,
            "type": "run_resting_heart_rate_analysis",
        }
    else:
        result_json = {
            "diagnostics": result.diagnostics,
            "status": result.status.value,
            "type": "write_not_started",
        }
    return {
        "diagnostics": receipt.diagnostics,
        "final_preflight": {
            "approval": {"status": receipt.final_preflight.approval.status.value},
            "confirmations": tuple(item.value for item in receipt.final_preflight.confirmations),
            "diagnostics": receipt.final_preflight.diagnostics,
            "capacity": _capacity_json(receipt.final_preflight.capacity),
            "filevault": _filevault_json(receipt.final_preflight.filevault),
        },
        "kind": "write_receipt",
        "operation_id": str(receipt.operation_id),
        "plan_fingerprint": str(receipt.plan_fingerprint),
        "result": result_json,
        "runtime_config": dict(runtime_config),
        "schema_version": _OUTPUT_SCHEMA_VERSION,
    }


def _print_write_plan(plan: WritePlan, workspace: WorkspaceStatus) -> None:
    print(f"Schreibvorschau: {plan.approval.status.value}")
    print(f"Plan-Fingerprint: {plan.fingerprint}")
    print(
        f"Datenspeicher-ID: {workspace.store_id or '-'} · Bindung: {workspace.person_binding.value}"
    )
    print("Bestätigungen: " + (", ".join(plan.confirmations) or "-"))
    if isinstance(plan.details, StoreMigrationPlan):
        migration = plan.details
        chain = (
            " -> ".join(
                [str(migration.steps[0][0]), *(str(target) for _, target in migration.steps)]
            )
            if migration.steps
            else "-"
        )
        print(f"Migrationskette: {chain}")
        snapshot_chain = (
            " -> ".join(
                [
                    str(migration.snapshot_steps[0][0]),
                    *(str(target) for _, target in migration.snapshot_steps),
                ]
            )
            if migration.snapshot_steps
            else "-"
        )
        print(f"Snapshot-Migrationskette: {snapshot_chain}")
        print(
            "Snapshot-Stichtag: "
            + (migration.snapshot_as_of.isoformat() if migration.snapshot_as_of else "-")
        )
        print(f"Migrationssicherung: {migration.backup_file or '-'}")
        print(
            "Betroffene Snapshots: "
            + (", ".join(str(item) for item in migration.affected_snapshot_refs) or "-")
        )
        print(
            "Bestehende Analysen werden veraltet: "
            + ("ja" if migration.existing_analyses_become_stale else "nein")
        )
    elif isinstance(plan.details, RollbackMigrationPlan):
        rollback = plan.details
        print(f"Zurückzurollende Migration: {rollback.migration_operation_id or '-'}")
        print(f"Migrationssicherung: {rollback.backup_file or '-'}")
        print(f"Schema: {rollback.source_version or '-'} -> {rollback.target_version or '-'}")
        print(f"Wiederhergestellter Snapshot: {rollback.restored_snapshot_ref or '-'}")
        print(f"Unreferenzierter Snapshot: {rollback.current_snapshot_ref or '-'}")
    elif isinstance(plan.details, MetadataRestorePlan):
        restore = plan.details
        print(f"Wiederherstellungs-ID: {restore.restore_id or '-'}")
        print(f"Sicherungs-ID: {restore.backup_id or '-'}")
        print(
            f"Sicherungsschema: {restore.source_schema_version or '-'} -> "
            f"{restore.target_schema_version}"
        )
        print(
            "Migrationsschritte: "
            + (
                ", ".join(f"{source} -> {target}" for source, target in restore.migration_steps)
                or "-"
            )
        )
    elif isinstance(plan.details, AbortMetadataRestorePlan):
        print(f"Wiederherstellungs-ID: {plan.details.restore_id or '-'}")
        print(f"Sicherungs-ID: {plan.details.backup_id or '-'}")
    filevault = plan.preflight.filevault
    print(
        "FileVault: "
        + (
            f"{filevault.status.value} · Ziel {filevault.target_volume}"
            + (f" · Grund {filevault.reason.value}" if filevault.reason else "")
            if filevault
            else "-"
        )
    )
    capacity = plan.preflight.capacity
    print(
        "Kapazität: "
        + (
            f"{capacity.status.value} · Ziel {capacity.target_volume} · "
            f"Methode {capacity.method_id} · Schätzung {capacity.estimate_bytes} · "
            f"Marge {capacity.safety_margin_bytes} · Mindestrest "
            f"{capacity.minimum_remaining_bytes} · Verfügbar {capacity.available_bytes}"
            if capacity
            else "-"
        )
    )
    print("Zulässige Aktionen: " + ", ".join(workspace.allowed_writes))
    print("Diagnosen: " + (", ".join(plan.diagnostics) or "-"))


def main(args: Sequence[str] | None = None) -> int:
    parser = _parser()
    parsed = parser.parse_args(args)
    write_commands = {
        "analyze",
        "abort-restore",
        "backup",
        "historical-review",
        "import",
        "migrate",
        "rollback-migration",
        "restore",
        "rule",
        "review-confirm-batch",
        "review-resolve",
        "review-revoke",
    }
    if parsed.command in write_commands:
        if parsed.execute and not parsed.as_json:
            parser.error("--execute ist nur zusammen mit --json zulässig.")
        if parsed.execute != (parsed.expect_plan is not None):
            parser.error("--execute und --expect-plan müssen gemeinsam angegeben werden.")
    if (
        parsed.command == "review-resolve"
        and sum(
            (
                parsed.deletion_verdict is not None,
                parsed.conflict_strategy is not None,
                parsed.confirm,
                parsed.correct is not None,
                parsed.exclude_local,
                parsed.accept_source,
            )
        )
        != 1
    ):
        parser.error("Genau eine Auflösungsart muss angegeben werden.")
    if parsed.command == "review-resolve" and (
        (
            parsed.correct is not None
            and ((not parsed.workout_correction and parsed.unit is None) or not parsed.reason)
        )
        or (parsed.exclude_local and (parsed.case_id is None or not parsed.reason))
        or (
            (parsed.correct is not None or parsed.exclude_local or parsed.accept_source)
            and parsed.measurement_version is None
        )
        or (parsed.case_id is None and parsed.correct is None)
    ):
        parser.error("Messung, Einheit, Prüffall oder Pflichtgrund fehlt.")
    if parsed.command == "review-revoke" and (
        (parsed.decision_id is None) == (parsed.batch_action is None)
    ):
        parser.error("Genau eine Entscheidungs- oder Sammelaktions-ID muss angegeben werden.")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    decision_request: WriteRequest
    medication_write_request: ReviseAsNeededIntake | ReviseIntakeReasonCategory
    try:
        config = load_runtime_config(
            explicit_mode=parsed.mode,
            explicit_synthetic_store=parsed.synthetic_store,
            explicit_real_store=parsed.real_store,
            config_file=parsed.config,
        )
        with HealthLab.open(config) as health_lab:
            workspace_status = health_lab.load_workspace_status()
            if parsed.command == "recovery-status":
                recovery_status = health_lab.load_recovery_status()
            elif parsed.command == "import-details":
                import_details = health_lab.load_import_details(parsed.import_id)
            elif parsed.command == "weight-nutrition":
                weight_selection = SnapshotDateSelection(
                    parsed.snapshot, parsed.start_date, parsed.end_date
                )
                weight_nutrition = health_lab.load_weight_nutrition(weight_selection)
            elif parsed.command == "sleep-days":
                sleep_selection = SnapshotDateSelection(
                    parsed.snapshot, parsed.start_date, parsed.end_date
                )
                sleep_days = health_lab.load_sleep_days(sleep_selection)
            elif parsed.command == "activity-days":
                activity_selection = SnapshotDateSelection(
                    parsed.snapshot, parsed.start_date, parsed.end_date
                )
                activity_days = health_lab.load_activity_days(activity_selection)
            elif parsed.command == "workouts":
                workout_selection = SnapshotDateSelection(
                    parsed.snapshot, parsed.start_date, parsed.end_date
                )
                workout_projection = health_lab.load_workouts(workout_selection)
            elif parsed.command == "context" and parsed.context_command == "days":
                context_selection = SnapshotDateSelection(
                    parsed.snapshot, parsed.start_date, parsed.end_date
                )
                daily_context = health_lab.load_daily_context(context_selection)
            elif parsed.command == "context" and parsed.context_command == "records":
                context_records = health_lab.load_context_records(parsed.snapshot)
            elif parsed.command == "context" and parsed.context_command == "audit":
                context_audit = health_lab.load_context_audit(parsed.logical_id)
            elif parsed.command == "medication" and parsed.medication_command == "days":
                medication_selection = SnapshotDateSelection(
                    parsed.snapshot, parsed.start_date, parsed.end_date
                )
                medication_days = health_lab.load_medication_days(medication_selection)
            elif parsed.command == "medication" and parsed.medication_command == "plan":
                medication_plan = health_lab.load_medication_plan(parsed.snapshot)
            elif parsed.command == "medication" and parsed.medication_command == "audit":
                medication_audit = health_lab.load_medication_audit(parsed.logical_id)
            elif parsed.command == "medication" and parsed.medication_command == "reason":
                reason_intent: (
                    IntakeReasonCategoryCreate
                    | IntakeReasonCategoryRevise
                    | IntakeReasonCategoryWithdraw
                    | IntakeReasonCategoryRestore
                )
                if parsed.medication_action == "create":
                    reason_intent = IntakeReasonCategoryCreate(parsed.name)
                elif parsed.medication_action == "withdraw":
                    reason_intent = IntakeReasonCategoryWithdraw(
                        parsed.logical_id, parsed.expected_revision_id, parsed.reason
                    )
                else:
                    intent_type = (
                        IntakeReasonCategoryRevise
                        if parsed.medication_action == "revise"
                        else IntakeReasonCategoryRestore
                    )
                    reason_intent = intent_type(
                        parsed.logical_id, parsed.expected_revision_id, parsed.name
                    )
                medication_write_request = ReviseIntakeReasonCategory(reason_intent)
                medication_write_plan = health_lab.preview_write(medication_write_request)
                medication_write_receipt = None
                _print_write_plan(medication_write_plan, workspace_status)
                if (
                    medication_write_plan.approval.status is not WriteApprovalStatus.BLOCKED
                    and input("Medikamentenschreibvorgang ausführen? [j/N] ").strip().lower()
                    in {"j", "ja"}
                ):
                    medication_write_receipt = health_lab.execute_write(
                        medication_write_request,
                        expected_plan=medication_write_plan.fingerprint,
                    )
            elif parsed.command == "medication" and parsed.medication_command == "intake":
                intake_intent: (
                    AsNeededIntakeCreate
                    | AsNeededIntakeRevise
                    | AsNeededIntakeWithdraw
                    | AsNeededIntakeRestore
                )
                if parsed.medication_action == "create":
                    intake_intent = AsNeededIntakeCreate(
                        parsed.regime_logical_id,
                        parsed.entry_id,
                        parsed.taken_at,
                        parsed.amount,
                        parsed.reason_category,
                    )
                elif parsed.medication_action == "withdraw":
                    intake_intent = AsNeededIntakeWithdraw(
                        parsed.logical_id, parsed.expected_revision_id, parsed.reason
                    )
                else:
                    intake_type = (
                        AsNeededIntakeRevise
                        if parsed.medication_action == "revise"
                        else AsNeededIntakeRestore
                    )
                    intake_intent = intake_type(
                        parsed.logical_id,
                        parsed.expected_revision_id,
                        parsed.taken_at,
                        parsed.amount,
                        parsed.reason_category,
                    )
                medication_write_request = ReviseAsNeededIntake(intake_intent)
                medication_write_plan = health_lab.preview_write(medication_write_request)
                medication_write_receipt = None
                _print_write_plan(medication_write_plan, workspace_status)
                if (
                    medication_write_plan.approval.status is not WriteApprovalStatus.BLOCKED
                    and input("Medikamentenschreibvorgang ausführen? [j/N] ").strip().lower()
                    in {"j", "ja"}
                ):
                    medication_write_receipt = health_lab.execute_write(
                        medication_write_request,
                        expected_plan=medication_write_plan.fingerprint,
                    )
            elif parsed.command == "restore":
                restore_request = BeginMetadataRestore(parsed.backup)
                restore_plan = health_lab.preview_write(restore_request)
                restore_write_receipt = None
                if parsed.execute:
                    assert parsed.expect_plan is not None
                    restore_write_receipt = health_lab.execute_write(
                        restore_request, expected_plan=parsed.expect_plan
                    )
                elif not parsed.as_json:
                    _print_write_plan(restore_plan, workspace_status)
                    if restore_plan.approval.status is not WriteApprovalStatus.BLOCKED and input(
                        "Metadatenwiederherstellung beginnen? [j/N] "
                    ).strip().lower() in {"j", "ja"}:
                        restore_write_receipt = health_lab.execute_write(
                            restore_request, expected_plan=restore_plan.fingerprint
                        )
            elif parsed.command == "abort-restore":
                abort_restore_request = AbortMetadataRestore()
                abort_restore_plan = health_lab.preview_write(abort_restore_request)
                abort_restore_write_receipt = None
                if parsed.execute:
                    assert parsed.expect_plan is not None
                    abort_restore_write_receipt = health_lab.execute_write(
                        abort_restore_request, expected_plan=parsed.expect_plan
                    )
                elif not parsed.as_json:
                    _print_write_plan(abort_restore_plan, workspace_status)
                    if (
                        abort_restore_plan.approval.status is not WriteApprovalStatus.BLOCKED
                        and input("Wiederherstellung abbrechen? [j/N] ").strip().lower()
                        in {"j", "ja"}
                    ):
                        abort_restore_write_receipt = health_lab.execute_write(
                            abort_restore_request,
                            expected_plan=abort_restore_plan.fingerprint,
                        )
            elif parsed.command == "import":
                import_request = ImportHealthExport(parsed.package)
                import_plan = health_lab.preview_write(import_request)
                import_write_receipt = None
                if parsed.execute:
                    assert parsed.expect_plan is not None
                    import_write_receipt = health_lab.execute_write(
                        import_request,
                        expected_plan=parsed.expect_plan,
                    )
                elif (
                    not parsed.as_json
                    and import_plan.approval.status is not WriteApprovalStatus.BLOCKED
                ):
                    _print_write_plan(import_plan, workspace_status)
                    if input("Health-Exportimport ausführen? [j/N] ").strip().lower() in {
                        "j",
                        "ja",
                    }:
                        import_write_receipt = health_lab.execute_write(
                            import_request,
                            expected_plan=import_plan.fingerprint,
                        )
            elif parsed.command == "migrate":
                migration_request = MigrateStore()
                migration_plan = health_lab.preview_write(migration_request)
                migration_write_receipt = None
                if parsed.execute:
                    assert parsed.expect_plan is not None
                    migration_write_receipt = health_lab.execute_write(
                        migration_request, expected_plan=parsed.expect_plan
                    )
                elif not parsed.as_json:
                    _print_write_plan(migration_plan, workspace_status)
                    if migration_plan.approval.status is not WriteApprovalStatus.BLOCKED and input(
                        "Datenspeichermigration ausführen? [j/N] "
                    ).strip().lower() in {"j", "ja"}:
                        migration_write_receipt = health_lab.execute_write(
                            migration_request, expected_plan=migration_plan.fingerprint
                        )
            elif parsed.command == "rollback-migration":
                rollback_request = RollbackMigration()
                rollback_plan = health_lab.preview_write(rollback_request)
                rollback_write_receipt = None
                if parsed.execute:
                    assert parsed.expect_plan is not None
                    rollback_write_receipt = health_lab.execute_write(
                        rollback_request, expected_plan=parsed.expect_plan
                    )
                elif not parsed.as_json:
                    _print_write_plan(rollback_plan, workspace_status)
                    if rollback_plan.approval.status is not WriteApprovalStatus.BLOCKED and input(
                        "Letzte Migration zurückrollen? [j/N] "
                    ).strip().lower() in {"j", "ja"}:
                        rollback_write_receipt = health_lab.execute_write(
                            rollback_request, expected_plan=rollback_plan.fingerprint
                        )
            elif parsed.command == "backup":
                backup_request = CreateMetadataBackup(parsed.target)
                backup_plan = health_lab.preview_write(backup_request)
                backup_write_receipt = None
                if parsed.execute:
                    assert parsed.expect_plan is not None
                    backup_write_receipt = health_lab.execute_write(
                        backup_request, expected_plan=parsed.expect_plan
                    )
                elif not parsed.as_json:
                    _print_write_plan(backup_plan, workspace_status)
                    if backup_plan.approval.status is not WriteApprovalStatus.BLOCKED and input(
                        "Metadatensicherung schreiben? [j/N] "
                    ).strip().lower() in {"j", "ja"}:
                        backup_write_receipt = health_lab.execute_write(
                            backup_request, expected_plan=backup_plan.fingerprint
                        )
            elif parsed.command == "rule":
                rule_request = CreatePlausibilityRuleVersion(
                    parsed.data_type,
                    PlausibilityRuleSpecification(
                        parsed.unit, parsed.lower, parsed.upper, parsed.personal
                    ),
                    parsed.effective_from,
                    parsed.recommendation,
                )
                rule_plan = health_lab.preview_write(rule_request)
                rule_write_receipt = None
                if parsed.execute:
                    assert parsed.expect_plan is not None
                    rule_write_receipt = health_lab.execute_write(
                        rule_request, expected_plan=parsed.expect_plan
                    )
                elif (
                    not parsed.as_json
                    and rule_plan.approval.status is not WriteApprovalStatus.BLOCKED
                    and input("Regelversion anlegen? [j/N] ").strip().lower() in {"j", "ja"}
                ):
                    rule_write_receipt = health_lab.execute_write(
                        rule_request, expected_plan=rule_plan.fingerprint
                    )
            elif parsed.command == "historical-review":
                historical_request = RunHistoricalReview(
                    parsed.data_type,
                    parsed.start_date,
                    parsed.end_date,
                    parsed.rule_version,
                )
                historical_plan = health_lab.preview_write(historical_request)
                historical_write_receipt = None
                if parsed.execute:
                    assert parsed.expect_plan is not None
                    historical_write_receipt = health_lab.execute_write(
                        historical_request, expected_plan=parsed.expect_plan
                    )
                elif (
                    not parsed.as_json
                    and historical_plan.approval.status is not WriteApprovalStatus.BLOCKED
                    and input("Historische Datenprüfung ausführen? [j/N] ").strip().lower()
                    in {"j", "ja"}
                ):
                    historical_write_receipt = health_lab.execute_write(
                        historical_request, expected_plan=historical_plan.fingerprint
                    )
            elif parsed.command == "review-resolve":
                decision_request = ResolveDataReviewCase(
                    parsed.case_id,
                    (
                        DataConfirmation(parsed.note)
                        if parsed.confirm
                        else (
                            WorkoutCorrection(
                                MeasurementVersionId(parsed.measurement_version),
                                parsed.correct,
                                parsed.distance,
                                parsed.energy,
                                parsed.reason,
                                parsed.note,
                            )
                            if parsed.workout_correction
                            else DataCorrection(
                                MeasurementVersionId(parsed.measurement_version),
                                parsed.correct,
                                parsed.unit,
                                parsed.reason,
                                parsed.note,
                            )
                            if parsed.correct is not None
                            else (
                                LocalWorkoutExclusion(
                                    MeasurementVersionId(parsed.measurement_version),
                                    parsed.reason,
                                    parsed.note,
                                )
                                if parsed.workout_exclusion
                                else LocalMeasurementExclusion(
                                    MeasurementVersionId(parsed.measurement_version),
                                    parsed.reason,
                                    parsed.note,
                                )
                                if parsed.exclude_local
                                else (
                                    SourceValueAcceptance(
                                        MeasurementVersionId(parsed.measurement_version),
                                        parsed.note,
                                    )
                                    if parsed.accept_source
                                    else (
                                        SourceDeletionResolution(
                                            parsed.deletion_verdict, parsed.note
                                        )
                                        if parsed.deletion_verdict is not None
                                        else SourceConflictResolution(
                                            parsed.conflict_strategy,
                                            None
                                            if parsed.preferred_version is None
                                            else MeasurementVersionId(parsed.preferred_version),
                                            parsed.note,
                                        )
                                    )
                                )
                            )
                        )
                    ),
                )
                decision_plan = health_lab.preview_write(decision_request)
                decision_write_receipt = None
                if parsed.execute:
                    assert parsed.expect_plan is not None
                    decision_write_receipt = health_lab.execute_write(
                        decision_request, expected_plan=parsed.expect_plan
                    )
                elif (
                    not parsed.as_json
                    and decision_plan.approval.status is not WriteApprovalStatus.BLOCKED
                    and input("Datenprüffall auflösen? [j/N] ").strip().lower() in {"j", "ja"}
                ):
                    decision_write_receipt = health_lab.execute_write(
                        decision_request, expected_plan=decision_plan.fingerprint
                    )
            elif parsed.command == "review-confirm-batch":
                decision_request = ConfirmDataReviewBatch(
                    DataReviewSelection(parsed.kind), parsed.note
                )
                decision_plan = health_lab.preview_write(decision_request)
                assert isinstance(decision_plan.details, DataReviewBatchPlan)
                decision_write_receipt = None
                if parsed.execute:
                    assert parsed.expect_plan is not None
                    decision_write_receipt = health_lab.execute_write(
                        decision_request, expected_plan=parsed.expect_plan
                    )
                elif not parsed.as_json:
                    _print_write_plan(decision_plan, workspace_status)
                    pydoc.pager(
                        "\n".join(
                            f"{item.case_id}: {item.effective_value} "
                            f"{item.canonical_unit.value if item.canonical_unit else '-'}"
                            for item in decision_plan.details.matches
                        )
                    )
                    if decision_plan.approval.status is not WriteApprovalStatus.BLOCKED and input(
                        f"{decision_plan.details.count} Datenprüffälle bestätigen? [j/N] "
                    ).strip().lower() in {"j", "ja"}:
                        decision_write_receipt = health_lab.execute_write(
                            decision_request, expected_plan=decision_plan.fingerprint
                        )
            elif parsed.command == "review-revoke":
                decision_request = RevokeDataReviewDecision(
                    (
                        SingleDecisionTarget(parsed.decision_id)
                        if parsed.decision_id is not None
                        else BatchDecisionTarget(parsed.batch_action)
                    ),
                    parsed.reason,
                )
                decision_plan = health_lab.preview_write(decision_request)
                decision_write_receipt = None
                if parsed.execute:
                    assert parsed.expect_plan is not None
                    decision_write_receipt = health_lab.execute_write(
                        decision_request, expected_plan=parsed.expect_plan
                    )
                elif (
                    not parsed.as_json
                    and decision_plan.approval.status is not WriteApprovalStatus.BLOCKED
                    and input("Datenprüfentscheidung widerrufen? [j/N] ").strip().lower()
                    in {"j", "ja"}
                ):
                    decision_write_receipt = health_lab.execute_write(
                        decision_request, expected_plan=decision_plan.fingerprint
                    )
            elif parsed.command == "rules":
                plausibility_rules = health_lab.load_plausibility_rules()
            elif parsed.command == "analyze":
                analysis_request = RunRestingHeartRateAnalysis(
                    analysis_definition_id=AnalysisDefinitionId(parsed.definition),
                    start_date=parsed.start_date,
                    end_date=parsed.end_date,
                )
                analysis_plan = health_lab.preview_write(analysis_request)
                analysis_write_receipt = None
                if parsed.execute:
                    assert parsed.expect_plan is not None
                    analysis_write_receipt = health_lab.execute_write(
                        analysis_request, expected_plan=parsed.expect_plan
                    )
                elif not parsed.as_json:
                    _print_write_plan(analysis_plan, workspace_status)
                    if analysis_plan.approval.status is not WriteApprovalStatus.BLOCKED and input(
                        "Ruhepulsanalyse ausführen? [j/N] "
                    ).strip().lower() in {"j", "ja"}:
                        analysis_write_receipt = health_lab.execute_write(
                            analysis_request, expected_plan=analysis_plan.fingerprint
                        )
                overview = health_lab.load_overview(
                    OverviewSelection(parsed.start_date, parsed.end_date)
                )
            elif parsed.command == "review":
                data_review = health_lab.load_data_review(DataReviewSelection())
                data_review_details = tuple(
                    health_lab.load_data_review_case(case.case_id) for case in data_review.cases
                )
            else:
                overview = health_lab.load_overview(OverviewSelection())
    except ConfigurationError as error:
        _parser().error(str(error))
    except Exception as error:
        logging.error("healthlab_failed error_class=%s", type(error).__name__)
        print("Technischer HealthLab-Fehler.", file=sys.stderr)
        return 1

    runtime_config = {
        "max_import_compression_ratio": config.max_import_compression_ratio,
        "max_import_entries": config.max_import_entries,
        "max_import_entry_bytes": config.max_import_entry_bytes,
        "max_import_package_bytes": config.max_import_package_bytes,
        "max_import_uncompressed_bytes": config.max_import_uncompressed_bytes,
        "mode": config.mode.value,
        "real_store": "<redacted>",
        "schema_version": config.schema_version,
        "synthetic_store": "<redacted>",
    }
    if not parsed.as_json:
        print("Konfiguration: " + json.dumps(runtime_config, ensure_ascii=False, sort_keys=True))
    if parsed.command == "import-details" and parsed.as_json:
        print(
            json.dumps(
                _import_details_json(import_details, runtime_config),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif parsed.command == "import-details":
        counts = import_details.canonical_counts
        print(f"Import-ID: {import_details.import_id}")
        print(f"Kanonische Records im Paket: {counts.package_record_count}")
        print(f"Neu importierte Records: {counts.record_count}")
        print(f"Logische Messungen: {counts.logical_measurement_count}")
        print(f"Messungsversionen: {counts.measurement_version_count}")
        print(f"Quellvorkommen: {counts.source_occurrence_count}")
        print(f"Anomalien: {counts.anomaly_count}")
        for content in import_details.unsupported_content:
            print(f"{content.category.value} · {content.external_identifier} · {content.count}")
    elif parsed.command == "weight-nutrition" and parsed.as_json:
        print(
            json.dumps(
                _weight_nutrition_json(weight_nutrition, weight_selection, runtime_config),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif parsed.command == "weight-nutrition":
        print("Gewicht und Ernährung")
        print(f"Snapshot: {weight_nutrition.snapshot_ref or '-'}")
        print(f"Datenstatus: {weight_nutrition.status.value}")
        for weight_day in weight_nutrition.days:
            print(
                f"{weight_day.day} · {weight_day.status.value} · "
                f"{weight_day.value_kg if weight_day.value_kg is not None else '-'} kg · "
                f"Qualität: {weight_day.quality_status.value} · Logische Messungen: "
                f"{', '.join(map(str, weight_day.logical_measurement_ids)) or '-'} · "
                f"Messungsversionen: "
                f"{', '.join(map(str, weight_day.measurement_version_ids)) or '-'} · "
                f"Prüffälle: {', '.join(map(str, weight_day.review_case_ids)) or '-'}"
            )
        for weight_measurement in weight_nutrition.weight_measurements:
            print(
                f"Messung {weight_measurement.measurement_version_id} · Logische Messung: "
                f"{weight_measurement.logical_measurement_id} · "
                f"{weight_measurement.value_kg} kg · Original: "
                f"{weight_measurement.original_value} {weight_measurement.original_unit} · "
                f"Effektiv: {weight_measurement.effective_value_kg} kg · "
                f"Disposition: {weight_measurement.disposition or '-'} · "
                f"Ausgewählt: {str(weight_measurement.is_selected).lower()} · "
                f"Lokaler Tag: {weight_measurement.measurement_local_day} · "
                f"Quelle: {weight_measurement.source_name} "
                f"{weight_measurement.source_version} · Gerät: {weight_measurement.device} · "
                f"Start: {weight_measurement.source_start.isoformat()} · "
                f"Ende: {weight_measurement.source_end.isoformat()} · "
                f"Erstellt: {weight_measurement.source_updated_at.isoformat()} · "
                f"Prüffälle: "
                f"{', '.join(map(str, weight_measurement.review_case_ids)) or '-'}"
            )
        for nutrition_day in weight_nutrition.nutrition_days:

            def feature_text(feature: DailyNutritionFeature) -> str:
                value = "-" if feature.value is None else str(feature.value)
                return f"{value} {feature.unit.value}"

            print(
                f"{nutrition_day.day} · Energie: {feature_text(nutrition_day.energy)} · "
                f"Protein: {feature_text(nutrition_day.protein)} · "
                f"Kohlenhydrate: {feature_text(nutrition_day.carbohydrates)} · "
                f"Gesamtfett: {feature_text(nutrition_day.total_fat)}"
            )
            for feature in (
                nutrition_day.energy,
                nutrition_day.protein,
                nutrition_day.carbohydrates,
                nutrition_day.total_fat,
            ):
                print(
                    f"{nutrition_day.day} · Ernährungsmerkmal: {feature.data_type.value} · "
                    f"Qualität: {feature.quality_status.value} · Logische Messungen: "
                    f"{', '.join(map(str, feature.logical_measurement_ids)) or '-'} · "
                    f"Messungsversionen: "
                    f"{', '.join(map(str, feature.measurement_version_ids)) or '-'} · "
                    f"Prüffälle: {', '.join(map(str, feature.review_case_ids)) or '-'}"
                )
        for nutrition_sample in weight_nutrition.healthkit_nutrition_samples:
            print(
                f"{nutrition_sample.data_type.value} · {nutrition_sample.value:g} "
                f"{nutrition_sample.unit.value} · Original: "
                f"{nutrition_sample.original_value} {nutrition_sample.original_unit} · "
                f"Effektiv: {nutrition_sample.effective_value} "
                f"{nutrition_sample.unit.value} · "
                f"Disposition: {nutrition_sample.disposition or '-'} · "
                f"Ausgewählt: {str(nutrition_sample.is_selected).lower()} · "
                f"Logische Messung: {nutrition_sample.logical_measurement_id} · "
                f"Messungsversion: {nutrition_sample.measurement_version_id} · "
                f"Lokaler Tag: {nutrition_sample.measurement_local_day} · "
                f"Quelle: {nutrition_sample.source_name} "
                f"{nutrition_sample.source_version} · Gerät: {nutrition_sample.device} · "
                f"Start: {nutrition_sample.source_start.isoformat()} · "
                f"Ende: {nutrition_sample.source_end.isoformat()} · "
                f"Erstellt: {nutrition_sample.source_updated_at.isoformat()} · "
                f"Prüffälle: "
                f"{', '.join(map(str, nutrition_sample.review_case_ids)) or '-'}"
            )
    elif parsed.command == "sleep-days" and parsed.as_json:
        print(
            json.dumps(
                _sleep_days_json(sleep_days, sleep_selection, runtime_config),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif parsed.command == "activity-days" and parsed.as_json:
        print(
            json.dumps(
                _activity_days_json(activity_days, activity_selection, runtime_config),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif parsed.command == "workouts" and parsed.as_json:
        print(
            json.dumps(
                _workouts_json(workout_projection, workout_selection, runtime_config),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif parsed.command == "context" and parsed.context_command == "days" and parsed.as_json:
        print(
            json.dumps(
                _daily_context_json(daily_context, context_selection, runtime_config),
                sort_keys=True,
            )
        )
    elif parsed.command == "context" and parsed.context_command == "records" and parsed.as_json:
        print(json.dumps(_context_records_json(context_records, runtime_config), sort_keys=True))
    elif parsed.command == "context" and parsed.context_command == "audit" and parsed.as_json:
        print(
            json.dumps(
                {
                    "kind": "context_audit",
                    "logical_id": str(context_audit.logical_id),
                    "revisions": [
                        {
                            "previous_revision_id": None
                            if item.previous_revision_id is None
                            else str(item.previous_revision_id),
                            "revision_id": str(item.revision_id),
                            "start_date": None
                            if item.start_date is None
                            else item.start_date.isoformat(),
                            "state": item.state,
                        }
                        for item in context_audit.revisions
                    ],
                    "runtime_config": dict(runtime_config),
                    "schema_version": _OUTPUT_SCHEMA_VERSION,
                },
                sort_keys=True,
            )
        )
    elif parsed.command == "medication" and parsed.medication_command == "days" and parsed.as_json:
        print(
            json.dumps(
                {
                    "kind": "medication_days",
                    "snapshot_ref": None
                    if medication_days.snapshot_ref is None
                    else str(medication_days.snapshot_ref),
                    "medication_as_of": None
                    if medication_days.medication_as_of is None
                    else medication_days.medication_as_of.isoformat(),
                    "days": [
                        {
                            "day": item.day.isoformat(),
                            "status": item.status,
                            "occurrences": [
                                {
                                    "medication_name": dose.medication_name,
                                    "amount": str(dose.amount),
                                    "unit": dose.unit,
                                    "scheduled_at": dose.scheduled_at.isoformat(),
                                    "status": dose.status,
                                    "actual_intakes": [
                                        {
                                            "taken_at": intake.taken_at.isoformat(),
                                            "amount": str(intake.amount),
                                        }
                                        for intake in dose.actual_intakes
                                    ],
                                }
                                for dose in item.occurrences
                            ],
                            "as_needed_intakes": [
                                {
                                    "logical_id": str(intake.logical_id),
                                    "taken_at": intake.taken_at.isoformat(),
                                    "medication_name": intake.medication_name,
                                    "amount": str(intake.amount),
                                    "unit": intake.unit,
                                    "reason_category_name": intake.reason_category_name,
                                }
                                for intake in item.as_needed_intakes
                            ],
                        }
                        for item in medication_days.days
                    ],
                    "runtime_config": dict(runtime_config),
                    "schema_version": _OUTPUT_SCHEMA_VERSION,
                },
                sort_keys=True,
            )
        )
    elif parsed.command == "medication" and parsed.medication_command == "plan" and parsed.as_json:
        print(
            json.dumps(
                {
                    "kind": "medication_plan",
                    "snapshot_ref": None
                    if medication_plan.snapshot_ref is None
                    else str(medication_plan.snapshot_ref),
                    "regimes": [
                        {
                            "logical_id": str(item.logical_id),
                            "revision_id": str(item.revision_id),
                            "starts_at": item.starts_at.isoformat(),
                            "timezone": item.timezone,
                            "scheduled_doses": [
                                {
                                    "medication_name": dose.medication_name,
                                    "amount": str(dose.amount),
                                    "unit": dose.unit,
                                    "local_time": dose.local_time.isoformat(),
                                    "weekdays": sorted(day.value for day in dose.weekdays),
                                }
                                for dose in item.scheduled_doses
                            ],
                            "as_needed_medications": [
                                {
                                    "entry_id": str(medication.entry_id),
                                    "medication_name": medication.medication_name,
                                    "amount": str(medication.amount),
                                    "unit": medication.unit,
                                    "preferred_reason_category_ids": [
                                        str(category_id)
                                        for category_id in medication.preferred_reason_category_ids
                                    ],
                                }
                                for medication in item.as_needed_medications
                            ],
                        }
                        for item in medication_plan.regimes
                    ],
                    "intake_reason_categories": [
                        {
                            "logical_id": str(category.logical_id),
                            "revision_id": str(category.revision_id),
                            "name": category.name,
                        }
                        for category in medication_plan.intake_reason_categories
                    ],
                    "runtime_config": dict(runtime_config),
                    "schema_version": _OUTPUT_SCHEMA_VERSION,
                },
                sort_keys=True,
            )
        )
    elif parsed.command == "medication" and parsed.medication_command == "audit" and parsed.as_json:
        print(
            json.dumps(
                {
                    "kind": "medication_audit",
                    "logical_id": str(medication_audit.logical_id),
                    "revisions": [
                        _medication_audit_revision_json(item) for item in medication_audit.revisions
                    ],
                    "runtime_config": dict(runtime_config),
                    "schema_version": _OUTPUT_SCHEMA_VERSION,
                },
                sort_keys=True,
            )
        )
    elif parsed.command == "context" and parsed.context_command == "days":
        print("Täglicher Kontext")
        for context_day in daily_context.days:
            print(
                f"{context_day.day} · Krankheit: {context_day.illness_origin.value} · "
                f"Schwere: {context_day.highest_illness_severity or '-'} · "
                f"Stress: {context_day.stress_origin.value} ({context_day.stress_level or '-'}) · "
                f"Kontexte: {', '.join(context_day.custom_context_labels) or '-'}"
            )
    elif parsed.command == "context" and parsed.context_command == "records":
        print("Kontextdaten")
        if context_records.coverage_start is not None:
            print(f"Abdeckungsbeginn: {context_records.coverage_start.start_date}")
        for label in context_records.custom_labels:
            print(f"Bezeichnung: {label.name} · {label.logical_id}")
    elif parsed.command == "context" and parsed.context_command == "audit":
        print("Kontextaudit")
        for revision in context_audit.revisions:
            print(f"{revision.revision_id} · {revision.state} · {revision.start_date or '-'}")
    elif parsed.command == "medication" and parsed.medication_command in {"reason", "intake"}:
        if medication_write_receipt is None:
            print("Medikamentenschreibvorgang nicht ausgeführt.")
        else:
            print(f"Medikamentenschreibvorgang: {medication_write_receipt.result.status.value}")
    elif parsed.command == "medication" and parsed.medication_command == "days":
        for medication_day in medication_days.days:
            print(
                f"{medication_day.day} · {medication_day.status} · "
                f"{len(medication_day.occurrences)} Dosen · "
                f"{len(medication_day.as_needed_intakes)} Bedarfeinnahmen"
            )
    elif parsed.command == "medication" and parsed.medication_command == "plan":
        for medication_regime in medication_plan.regimes:
            print(
                f"{medication_regime.starts_at.isoformat()} · {medication_regime.timezone} · "
                f"{len(medication_regime.scheduled_doses)} Dosen · "
                f"{len(medication_regime.as_needed_medications)} Bedarfsmedikationen"
            )
    elif parsed.command == "medication" and parsed.medication_command == "audit":
        for medication_revision in medication_audit.revisions:
            if isinstance(medication_revision, MedicationDeviationAuditRevision):
                print(
                    f"{medication_revision.revision_id} · {medication_revision.state} · "
                    f"{medication_revision.scheduled_at.isoformat()}"
                )
            elif isinstance(medication_revision, AsNeededIntakeAuditRevision):
                print(
                    f"{medication_revision.revision_id} · {medication_revision.state} · "
                    f"{medication_revision.taken_at.isoformat()}"
                )
            elif isinstance(medication_revision, IntakeReasonCategoryAuditRevision):
                print(f"{medication_revision.revision_id} · {medication_revision.state}")
            else:
                print(
                    f"{medication_revision.revision_id} · "
                    f"{medication_revision.starts_at.isoformat()}"
                )
    elif parsed.command == "workouts":
        print("Trainingseinheiten")
        print(
            f"Snapshot: {workout_projection.snapshot_ref or '-'} · "
            f"Status: {workout_projection.status.value}"
        )
        for workout in workout_projection.workouts:
            print(
                f"{workout.original_activity_type} · {workout.effective_duration_minutes} min · "
                f"Start: {workout.source_start.isoformat()} · "
                f"Ende: {workout.source_end.isoformat()}"
            )
    elif parsed.command == "activity-days":
        print("Aktivitätstage")
        print(
            f"Snapshot: {activity_days.snapshot_ref or '-'} · Status: {activity_days.status.value}"
        )

        def value_or_dash(value: float | None) -> float | str:
            return "-" if value is None else value

        for activity_day in activity_days.days:
            print(
                f"{activity_day.day} · "
                f"Trainingszeit: {value_or_dash(activity_day.exercise_time.value)} min · "
                f"Schritte: {value_or_dash(activity_day.step_count.value)} count · "
                f"Distanz: {value_or_dash(activity_day.walking_running_distance.value)} km · "
                f"Aktive Energie: {value_or_dash(activity_day.active_energy.value)} kcal"
            )
        for activity_measurement in activity_days.measurements:
            print(
                f"{activity_measurement.data_type.value} · "
                f"{activity_measurement.value} {activity_measurement.unit.value} · "
                f"Original: {activity_measurement.original_value} "
                f"{activity_measurement.original_unit} · "
                f"Quellenklasse: {activity_measurement.source_class.value} · "
                f"Start: {activity_measurement.source_start.isoformat()} · "
                f"Ende: {activity_measurement.source_end.isoformat()}"
            )
    elif parsed.command == "sleep-days":
        print("Schlafnächte")
        print(f"Snapshot: {sleep_days.snapshot_ref or '-'}")
        for day in sleep_days.days:
            episode = day.primary_episode
            print(
                f"{day.day} · {day.status.value} · Schlaf: "
                f"{'-' if episode is None else episode.observed_sleep} · "
                f"Nickerchen: {day.nap_count} ({day.nap_observed_sleep}) · "
                f"Akzeptiert: {day.quality.accepted_interval_count} · "
                f"Abgewiesen: {day.quality.rejected_interval_count}"
            )
            print(
                f"  Klassifikator: {day.quality.source_classifier_version} · "
                f"Ableitung: {day.quality.derivation_version} · "
                f"Watch-Quellen: {day.quality.contributing_watch_source_count} · "
                "Primärauswahl mehrdeutig: "
                f"{str(day.quality.primary_selection_ambiguous).lower()} · "
                "Quellenklassen: "
                + ", ".join(
                    f"{count.source_class.value} "
                    f"(akzeptiert {count.accepted_interval_count}, "
                    f"abgewiesen {count.rejected_interval_count})"
                    for count in day.quality.source_counts
                )
            )
            if episode is not None:
                print(
                    f"  Episode: {episode.start.isoformat()} bis {episode.end.isoformat()} · "
                    f"Wach: {episode.observed_awake} · Im Bett: {episode.in_bed} · "
                    f"Core: {episode.asleep_core} · Tief: {episode.asleep_deep} · "
                    f"REM: {episode.asleep_rem} · Unspezifiziert: {episode.asleep_unspecified} · "
                    f"Mehrdeutig: {episode.stage_ambiguous} · Lücke: {episode.uncovered_gap} · "
                    f"Konflikt: {episode.asleep_awake_conflict} · "
                    f"Abdeckung: {episode.observed_coverage_ratio} · "
                    f"Stufenabdeckung: {episode.detailed_stage_coverage_ratio}"
                )
            for nap in day.naps:
                print(
                    f"  Nickerchen: {nap.start.isoformat()} bis {nap.end.isoformat()} · "
                    f"Schlaf: {nap.observed_sleep} · Wach: {nap.observed_awake} · "
                    f"Im Bett: {nap.in_bed} · Core: {nap.asleep_core} · "
                    f"Tief: {nap.asleep_deep} · REM: {nap.asleep_rem} · "
                    f"Unspezifiziert: {nap.asleep_unspecified} · "
                    f"Mehrdeutig: {nap.stage_ambiguous} · Lücke: {nap.uncovered_gap} · "
                    f"Konflikt: {nap.asleep_awake_conflict} · "
                    f"Abdeckung: {nap.observed_coverage_ratio} · "
                    f"Stufenabdeckung: {nap.detailed_stage_coverage_ratio}"
                )
        for interval in (*sleep_days.accepted_intervals, *sleep_days.rejected_intervals):
            print(
                f"{interval.canonical_category.value} · Original: {interval.original_category} · "
                f"Quelle: {interval.source_class.value} · Ausgewählt: "
                f"{str(interval.is_selected).lower()} · {interval.source_start.isoformat()} "
                f"bis {interval.source_end.isoformat()} · {interval.source_name} · "
                f"{interval.source_version} · Aktualisiert: "
                f"{interval.source_updated_at.isoformat()} · {interval.device}"
            )
    elif parsed.command == "recovery-status" and parsed.as_json:
        print(
            json.dumps(
                _recovery_status_json(recovery_status, runtime_config, workspace_status),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif parsed.command == "recovery-status":
        print(f"Wiederherstellungsstatus: {recovery_status.status.value}")
        print(f"Wiederherstellungs-ID: {recovery_status.restore_id}")
        print(f"Sicherungs-ID: {recovery_status.backup_id}")
    elif parsed.command == "restore" and parsed.as_json:
        output = (
            _write_plan_json(restore_plan, runtime_config, workspace_status)
            if restore_write_receipt is None
            else _write_receipt_json(restore_write_receipt, runtime_config)
        )
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    elif parsed.command == "restore":
        if restore_write_receipt is None:
            _print_write_plan(restore_plan, workspace_status)
            print("Metadatenwiederherstellung nicht begonnen.")
        else:
            print(f"Metadatenwiederherstellung: {restore_write_receipt.result.status.value}")
    elif parsed.command == "abort-restore" and parsed.as_json:
        output = (
            _write_plan_json(abort_restore_plan, runtime_config, workspace_status)
            if abort_restore_write_receipt is None
            else _write_receipt_json(abort_restore_write_receipt, runtime_config)
        )
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    elif parsed.command == "abort-restore":
        if abort_restore_write_receipt is None:
            _print_write_plan(abort_restore_plan, workspace_status)
            print("Wiederherstellung nicht abgebrochen.")
        else:
            print(f"Wiederherstellung: {abort_restore_write_receipt.result.status.value}")
    elif parsed.command == "backup" and parsed.as_json:
        output = (
            _write_plan_json(backup_plan, runtime_config, workspace_status)
            if backup_write_receipt is None
            else _write_receipt_json(backup_write_receipt, runtime_config)
        )
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    elif parsed.command == "migrate" and parsed.as_json:
        output = (
            _write_plan_json(migration_plan, runtime_config, workspace_status)
            if migration_write_receipt is None
            else _write_receipt_json(migration_write_receipt, runtime_config)
        )
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    elif parsed.command == "migrate":
        if migration_write_receipt is None:
            _print_write_plan(migration_plan, workspace_status)
            print("Datenspeichermigration nicht ausgeführt.")
        else:
            print(f"Datenspeichermigration: {migration_write_receipt.result.status.value}")
    elif parsed.command == "rollback-migration" and parsed.as_json:
        output = (
            _write_plan_json(rollback_plan, runtime_config, workspace_status)
            if rollback_write_receipt is None
            else _write_receipt_json(rollback_write_receipt, runtime_config)
        )
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    elif parsed.command == "rollback-migration":
        if rollback_write_receipt is None:
            _print_write_plan(rollback_plan, workspace_status)
            print("Migrationsrollback nicht ausgeführt.")
        else:
            print(f"Migrationsrollback: {rollback_write_receipt.result.status.value}")
    elif parsed.command == "backup":
        if backup_write_receipt is None:
            _print_write_plan(backup_plan, workspace_status)
            print("Metadatensicherung nicht ausgeführt.")
        else:
            print(f"Metadatensicherung: {backup_write_receipt.result.status.value}")
    elif parsed.command == "analyze" and parsed.as_json:
        output = (
            _write_plan_json(analysis_plan, runtime_config, workspace_status)
            if analysis_write_receipt is None
            else _write_receipt_json(
                analysis_write_receipt,
                runtime_config,
                (
                    _analysis_json(overview.resting_hr_analysis)
                    if isinstance(analysis_write_receipt.result, AnalysisReceipt)
                    and analysis_write_receipt.result.status
                    in {AnalysisStatus.COMPLETED, AnalysisStatus.REUSED}
                    else None
                ),
            )
        )
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    elif parsed.command == "analyze":
        if analysis_plan.approval.status is WriteApprovalStatus.BLOCKED:
            _print_write_plan(analysis_plan, workspace_status)
        if analysis_write_receipt is None:
            print("Ruhepulsanalyse nicht ausgeführt.")
        elif isinstance(analysis_write_receipt.result, AnalysisReceipt):
            analysis_receipt = analysis_write_receipt.result
            print(f"Ruhepulsanalyse: {analysis_receipt.status.value}")
            print(
                "Modellreife: "
                + (
                    analysis_receipt.model_maturity.value
                    if analysis_receipt.model_maturity
                    else "-"
                )
            )
        else:
            print(f"Ruhepulsanalyse: {analysis_write_receipt.result.status.value}")
        if (
            analysis_write_receipt is not None
            and isinstance(analysis_write_receipt.result, AnalysisReceipt)
            and analysis_write_receipt.result.status
            in {AnalysisStatus.COMPLETED, AnalysisStatus.REUSED}
            and overview.resting_hr_analysis is not None
        ):
            for item in overview.resting_hr_analysis.lag_associations:
                band = item.simultaneous_band
                assert band is not None
                print(
                    f"Lag {item.lag_days}: {item.estimate_per_100_kcal:.2f} bpm/100 kcal "
                    f"(punktweise {item.pointwise_interval.lower_per_100_kcal:.2f} bis "
                    f"{item.pointwise_interval.upper_per_100_kcal:.2f}; simultan "
                    f"{band.lower_per_100_kcal:.2f} bis {band.upper_per_100_kcal:.2f})"
                )
            cumulative = overview.resting_hr_analysis.cumulative_association
            print(f"Kumulativ: {cumulative.estimate_per_100_kcal:.2f} bpm/100 kcal")
            diagnostics = overview.resting_hr_analysis.diagnostics
            print(
                f"Diagnosen: {diagnostics.complete_days} vollständige Tage; "
                f"Merkmalsabhängigkeit {diagnostics.feature_dependency}; "
                f"Bootstrap {diagnostics.bootstrap_successes}/{diagnostics.bootstrap_resamples}; "
                f"Guardrail {diagnostics.association_guardrail}"
            )
    elif parsed.command == "import" and parsed.as_json:
        output = (
            _write_plan_json(import_plan, runtime_config, workspace_status)
            if import_write_receipt is None
            else _write_receipt_json(import_write_receipt, runtime_config)
        )
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    elif parsed.command == "import":
        if import_plan.approval.status is WriteApprovalStatus.BLOCKED:
            _print_write_plan(import_plan, workspace_status)
        if import_write_receipt is None:
            print("Health-Exportimport nicht ausgeführt.")
        elif isinstance(import_write_receipt.result, ImportReceipt):
            print(f"Health-Exportimport: {import_write_receipt.result.status.value}")
            print(f"Importierte Records: {import_write_receipt.result.record_count}")
        else:
            print(f"Health-Exportimport: {import_write_receipt.result.status.value}")
    elif parsed.command == "rule" and parsed.as_json:
        output = (
            _write_plan_json(rule_plan, runtime_config, workspace_status)
            if rule_write_receipt is None
            else _write_receipt_json(rule_write_receipt, runtime_config)
        )
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    elif parsed.command == "rule":
        if rule_write_receipt is None:
            _print_write_plan(rule_plan, workspace_status)
        else:
            print(f"Regelversion: {rule_write_receipt.result.status.value}")
    elif parsed.command == "historical-review" and parsed.as_json:
        output = (
            _write_plan_json(historical_plan, runtime_config, workspace_status)
            if historical_write_receipt is None
            else _write_receipt_json(historical_write_receipt, runtime_config)
        )
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    elif parsed.command == "historical-review":
        if historical_write_receipt is None:
            _print_write_plan(historical_plan, workspace_status)
        else:
            print(f"Historische Datenprüfung: {historical_write_receipt.result.status.value}")
    elif (
        parsed.command
        in {
            "review-confirm-batch",
            "review-resolve",
            "review-revoke",
        }
        and parsed.as_json
    ):
        output = (
            _write_plan_json(decision_plan, runtime_config, workspace_status)
            if decision_write_receipt is None
            else _write_receipt_json(decision_write_receipt, runtime_config)
        )
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    elif parsed.command in {"review-confirm-batch", "review-resolve", "review-revoke"}:
        if decision_write_receipt is None:
            if parsed.command != "review-confirm-batch":
                if isinstance(decision_plan.details, DataReviewBatchPlan):
                    _print_write_plan(decision_plan, workspace_status)
                    pydoc.pager(
                        "\n".join(
                            f"{item.case_id}: {item.effective_value} "
                            f"{item.canonical_unit.value if item.canonical_unit else '-'}"
                            for item in decision_plan.details.matches
                        )
                    )
                else:
                    _print_write_plan(decision_plan, workspace_status)
        else:
            print(f"Datenprüfentscheidung: {decision_write_receipt.result.status.value}")
    elif parsed.command == "rules" and parsed.as_json:
        print(
            json.dumps(
                {
                    **_plausibility_rules_json(plausibility_rules),
                    "runtime_config": runtime_config,
                    "schema_version": _OUTPUT_SCHEMA_VERSION,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif parsed.command == "rules":
        for rule in plausibility_rules.rules:
            active = rule.active_version
            print(
                f"{rule.data_type.value}: {active.version_id} ab "
                f"{active.effective_from or '-'} · aktiv {active.specification.active}"
            )
    elif parsed.command == "review" and parsed.as_json:
        print(
            json.dumps(
                {
                    **_data_review_json(data_review, data_review_details),
                    "runtime_config": runtime_config,
                    "schema_version": _OUTPUT_SCHEMA_VERSION,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif parsed.command == "review":
        print(f"Datenstatus: {data_review.status.value}")
        print(f"Offene Datenprüffälle: {len(data_review.cases)}")
        for detail in data_review_details:
            case = detail.case
            source = detail.effective_value_source.value if detail.effective_value_source else "-"
            reasons = ", ".join(reason.code.value for reason in detail.reasons) or "-"
            print(
                f"{case.case_id}: {case.kind.value} · "
                f"Messung {case.logical_measurement_id or case.measurement_version_id or '-'} · "
                f"Regel {case.rule_version_id or '-'} · Evidenz {case.evidence_fingerprint} · "
                f"Aktionen {', '.join(case.allowed_actions) or '-'} · "
                f"Kandidaten {', '.join(map(str, case.candidate_version_ids)) or '-'}"
            )
            print(
                f"  Typ {detail.source_type or '-'} · Zeitpunkt {detail.measured_at or '-'} · "
                f"Wert {detail.effective_value} · Quelle {source} · Begründungen {reasons}"
            )
    elif parsed.as_json:
        print(
            json.dumps(
                {
                    "daily_series": [
                        {
                            "data_type": series.data_type.value,
                            "unit": series.unit.value,
                            "values": [
                                {
                                    "day": value.day.isoformat(),
                                    "measurement_version_ids": [
                                        str(version_id)
                                        for version_id in value.measurement_version_ids
                                    ],
                                    "source_updated_ats": [
                                        timestamp.isoformat()
                                        for timestamp in value.source_updated_ats
                                    ],
                                    "source_versions": value.source_versions,
                                    "value": value.value,
                                }
                                for value in series.values
                            ],
                        }
                        for series in overview.daily_series
                    ],
                    "message": overview.message,
                    "analysis_history": [
                        _analysis_json(result) for result in overview.analysis_history
                    ],
                    "last_reviewed_analysis": _analysis_json(overview.last_reviewed_analysis),
                    "resting_hr_analysis": _analysis_json(overview.resting_hr_analysis),
                    "provenance": {
                        "import_count": overview.import_count,
                        "logical_measurement_count": overview.logical_measurement_count,
                        "measurement_version_count": overview.measurement_version_count,
                        "package_count": overview.package_count,
                        "quarantined_import_count": overview.quarantined_import_count,
                        "snapshot_count": overview.snapshot_count,
                    },
                    "runtime_config": runtime_config,
                    "schema_version": _OUTPUT_SCHEMA_VERSION,
                    "selection": {
                        "end_date": (
                            overview.selection.end_date.isoformat()
                            if overview.selection.end_date
                            else None
                        ),
                        "start_date": (
                            overview.selection.start_date.isoformat()
                            if overview.selection.start_date
                            else None
                        ),
                    },
                    "status": overview.status.value,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(f"HealthLab Übersicht: {overview.status.value}")
        print(overview.message)
        if overview.resting_hr_analysis is None:
            print("Kein aktuelles Analyseergebnis.")
        else:
            estimate = overview.resting_hr_analysis.cumulative_association
            print(f"Kumulativer Zusammenhang: {estimate.estimate_per_100_kcal:.2f} bpm/100 kcal")
        for historical in overview.analysis_history:
            provenance = historical.provenance
            print(
                f"Historisch: {historical.freshness.value} · "
                f"Run {provenance.analysis_run_id if provenance else '-'} · "
                f"Snapshot {historical.snapshot_id} · "
                "Ausgeführt "
                f"{historical.completed_at.isoformat() if historical.completed_at else '-'}"
            )
    if parsed.command == "medication" and parsed.medication_command in {"reason", "intake"}:
        if medication_write_receipt is None:
            return 3 if medication_write_plan.approval.status is WriteApprovalStatus.BLOCKED else 0
        return 3 if isinstance(medication_write_receipt.result, WriteNotStarted) else 0
    if parsed.command in write_commands:
        if parsed.command == "restore":
            if restore_write_receipt is None:
                return 3 if restore_plan.approval.status is WriteApprovalStatus.BLOCKED else 0
            return 3 if isinstance(restore_write_receipt.result, WriteNotStarted) else 0
        if parsed.command == "abort-restore":
            if abort_restore_write_receipt is None:
                return 3 if abort_restore_plan.approval.status is WriteApprovalStatus.BLOCKED else 0
            return 3 if isinstance(abort_restore_write_receipt.result, WriteNotStarted) else 0
        if parsed.command == "migrate":
            if migration_write_receipt is None:
                return 3 if migration_plan.approval.status is WriteApprovalStatus.BLOCKED else 0
            return 3 if isinstance(migration_write_receipt.result, WriteNotStarted) else 0
        if parsed.command == "rollback-migration":
            if rollback_write_receipt is None:
                return 3 if rollback_plan.approval.status is WriteApprovalStatus.BLOCKED else 0
            return 3 if isinstance(rollback_write_receipt.result, WriteNotStarted) else 0
        if parsed.command == "backup":
            if backup_write_receipt is None:
                return 3 if backup_plan.approval.status is WriteApprovalStatus.BLOCKED else 0
            return 3 if isinstance(backup_write_receipt.result, WriteNotStarted) else 0
        if parsed.command == "analyze":
            if analysis_write_receipt is None:
                return 3 if analysis_plan.approval.status is WriteApprovalStatus.BLOCKED else 0
            result = analysis_write_receipt.result
            return (
                0
                if isinstance(result, AnalysisReceipt)
                and result.status in {AnalysisStatus.COMPLETED, AnalysisStatus.REUSED}
                else 3
            )
        if parsed.command == "rule":
            if rule_write_receipt is None:
                return 3 if rule_plan.approval.status is WriteApprovalStatus.BLOCKED else 0
            return 3 if isinstance(rule_write_receipt.result, WriteNotStarted) else 0
        if parsed.command == "historical-review":
            if historical_write_receipt is None:
                return 3 if historical_plan.approval.status is WriteApprovalStatus.BLOCKED else 0
            return 3 if isinstance(historical_write_receipt.result, WriteNotStarted) else 0
        if parsed.command in {"review-confirm-batch", "review-resolve", "review-revoke"}:
            if decision_write_receipt is None:
                return 3 if decision_plan.approval.status is WriteApprovalStatus.BLOCKED else 0
            return 3 if isinstance(decision_write_receipt.result, WriteNotStarted) else 0
        if import_write_receipt is None:
            return 3 if import_plan.approval.status is WriteApprovalStatus.BLOCKED else 0
        result = import_write_receipt.result
        if isinstance(result, WriteNotStarted) or result.status not in {
            ImportStatus.COMMITTED,
            ImportStatus.DUPLICATE,
        }:
            return 3
    return 0
