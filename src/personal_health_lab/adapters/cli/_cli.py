from __future__ import annotations

import argparse
import json
import logging
import pydoc
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path

from personal_health_lab.adapters._config import load_runtime_config
from personal_health_lab.application import (
    AnalysisDefinitionId,
    AnalysisProvenance,
    AnalysisReceipt,
    AnalysisStatus,
    AssociationInterval,
    BatchDecisionTarget,
    CanonicalHealthType,
    CanonicalUnit,
    CapacityCheck,
    ConfigurationError,
    ConfirmDataReviewBatch,
    CreatePlausibilityRuleVersion,
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
    ImportHealthExport,
    ImportHealthExportPlan,
    ImportReceipt,
    ImportStatus,
    LocalMeasurementExclusion,
    MeasurementVersionId,
    Overview,
    OverviewSelection,
    PlanFingerprint,
    PlausibilityRules,
    PlausibilityRuleSpecification,
    PlausibilityRuleVersionPlan,
    PlausibilityRuleVersionReceipt,
    ResolveDataReviewCase,
    RestingHeartRateAnalysisPlan,
    RevokeDataReviewDecision,
    RunHistoricalReview,
    RunRestingHeartRateAnalysis,
    SingleDecisionTarget,
    SourceConflictResolution,
    SourceConflictStrategy,
    SourceDeletionResolution,
    SourceDeletionVerdict,
    SourceValueAcceptance,
    WorkspaceStatus,
    WriteApprovalStatus,
    WriteBatchDecisionReceipt,
    WriteDecisionReceipt,
    WriteNotStarted,
    WritePlan,
    WriteReceipt,
    WriteRequest,
)


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
    analysis = commands.add_parser("analyze", help="Verzögerungsprofil analysieren")
    analysis.add_argument("--definition", default="lag-signal-v1")
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
    resolve.add_argument("--unit", type=CanonicalUnit, choices=tuple(CanonicalUnit))
    resolve.add_argument("--exclude-local", action="store_true")
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


def _analysis_json(overview: Overview) -> dict[str, object] | None:
    result = overview.resting_hr_analysis
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
        },
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
        "methodology": {
            "block_length_days": result.methodology.block_length_days,
            "bootstrap_method": result.methodology.bootstrap_method,
            "interval_level": result.methodology.interval_level,
            "minimum_observations": result.methodology.minimum_observations,
            "random_seed": result.methodology.random_seed,
            "resample_count": result.methodology.resample_count,
            "ridge_penalty": result.methodology.ridge_penalty,
            "robust_observations": result.methodology.robust_observations,
        },
        "personal_standard_deviation_kcal": result.personal_standard_deviation_kcal,
        "provenance": _provenance_json(result.provenance),
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
        "schema_version": "2.0",
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
        "schema_version": "2.0",
    }


def _print_write_plan(plan: WritePlan, workspace: WorkspaceStatus) -> None:
    print(f"Schreibvorschau: {plan.approval.status.value}")
    print(f"Plan-Fingerprint: {plan.fingerprint}")
    print(
        f"Datenspeicher-ID: {workspace.store_id or '-'} · Bindung: {workspace.person_binding.value}"
    )
    print("Bestätigungen: " + (", ".join(plan.confirmations) or "-"))
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
        "historical-review",
        "import",
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
        (parsed.correct is not None and (parsed.unit is None or not parsed.reason))
        or (parsed.exclude_local and (parsed.case_id is None or not parsed.reason))
        or ((parsed.correct is not None or parsed.exclude_local or parsed.accept_source)
            and parsed.measurement_version is None)
        or (parsed.case_id is None and parsed.correct is None)
    ):
        parser.error("Messung, Einheit, Prüffall oder Pflichtgrund fehlt.")
    if parsed.command == "review-revoke" and (
        (parsed.decision_id is None) == (parsed.batch_action is None)
    ):
        parser.error("Genau eine Entscheidungs- oder Sammelaktions-ID muss angegeben werden.")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    decision_request: WriteRequest
    try:
        config = load_runtime_config(
            explicit_mode=parsed.mode,
            explicit_synthetic_store=parsed.synthetic_store,
            explicit_real_store=parsed.real_store,
            config_file=parsed.config,
        )
        with HealthLab.open(config) as health_lab:
            workspace_status = health_lab.load_workspace_status()
            if parsed.command == "import":
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
                            DataCorrection(
                                MeasurementVersionId(parsed.measurement_version),
                                parsed.correct,
                                parsed.unit,
                                parsed.reason,
                                parsed.note,
                            )
                            if parsed.correct is not None
                            else (
                                LocalMeasurementExclusion(
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
                                            else MeasurementVersionId(
                                                parsed.preferred_version
                                            ),
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
                    if (
                        decision_plan.approval.status is not WriteApprovalStatus.BLOCKED
                        and input(
                            f"{decision_plan.details.count} Datenprüffälle bestätigen? [j/N] "
                        ).strip().lower()
                        in {"j", "ja"}
                    ):
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
                    if (
                        analysis_plan.approval.status is not WriteApprovalStatus.BLOCKED
                        and input("Ruhepulsanalyse ausführen? [j/N] ").strip().lower()
                        in {"j", "ja"}
                    ):
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
    if parsed.command == "analyze" and parsed.as_json:
        output = (
            _write_plan_json(analysis_plan, runtime_config, workspace_status)
            if analysis_write_receipt is None
            else _write_receipt_json(
                analysis_write_receipt,
                runtime_config,
                (
                    _analysis_json(overview)
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
    elif parsed.command in {
        "review-confirm-batch",
        "review-resolve",
        "review-revoke",
    } and parsed.as_json:
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
                    "schema_version": "2.0",
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
                    "schema_version": "2.0",
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
                    "resting_hr_analysis": _analysis_json(overview),
                    "provenance": {
                        "import_count": overview.import_count,
                        "logical_measurement_count": overview.logical_measurement_count,
                        "measurement_version_count": overview.measurement_version_count,
                        "package_count": overview.package_count,
                        "quarantined_import_count": overview.quarantined_import_count,
                        "snapshot_count": overview.snapshot_count,
                    },
                    "runtime_config": runtime_config,
                    "schema_version": overview.schema_version,
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
        if overview.resting_hr_analysis is not None:
            estimate = overview.resting_hr_analysis.cumulative_association
            print(f"Kumulativer Zusammenhang: {estimate.estimate_per_100_kcal:.2f} bpm/100 kcal")
    if parsed.command in write_commands:
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
