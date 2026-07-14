from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from personal_health_lab.adapters._config import load_runtime_config
from personal_health_lab.application import (
    AnalysisDefinitionId,
    AnalysisProvenance,
    AnalysisStatus,
    AssociationInterval,
    CapacityCheck,
    ConfigurationError,
    DataMode,
    DataReview,
    DataReviewCaseDetail,
    DataReviewCaseId,
    DataReviewSelection,
    FileVaultCheck,
    HealthLab,
    ImportHealthExport,
    ImportReceipt,
    ImportStatus,
    Overview,
    OverviewSelection,
    PlanFingerprint,
    RestingHeartRateAnalysisConfig,
    WorkspaceStatus,
    WriteApprovalStatus,
    WriteNotStarted,
    WritePlan,
    WriteReceipt,
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
    review = commands.add_parser("review", help="Offene Datenprüffälle laden")
    review.add_argument("--json", action="store_true", dest="as_json")
    return parser


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

    return {
        "cases": [
            {
                "case_id": str(case.case_id),
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
                "cycle_id": str(cycle.cycle_id),
                "open_case_count": cycle.open_case_count,
                "snapshot_ref": str(cycle.snapshot_ref),
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
    return {
        "approval": {"status": plan.approval.status.value},
        "confirmations": tuple(item.value for item in plan.confirmations),
        "details": {
            "package_hash": plan.details.package_hash,
            "package_size": plan.details.package_size,
        },
        "diagnostics": plan.diagnostics,
        "fingerprint": str(plan.fingerprint),
        "kind": "write_plan",
        "preflight": {
            "capacity": _capacity_json(plan.preflight.capacity),
            "filevault": _filevault_json(plan.preflight.filevault),
        },
        "request": {"package": "<redacted>", "type": "import_health_export"},
        "runtime_config": dict(runtime_config),
        "schema_version": "2.0",
        "workspace": _workspace_json(workspace),
    }


def _write_receipt_json(
    receipt: WriteReceipt, runtime_config: Mapping[str, object]
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
            "confirmations": tuple(
                item.value for item in receipt.final_preflight.confirmations
            ),
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
        "Datenspeicher-ID: "
        f"{workspace.store_id or '-'} · Bindung: {workspace.person_binding.value}"
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
    if parsed.command == "import":
        if parsed.execute and not parsed.as_json:
            parser.error("--execute ist nur zusammen mit --json zulässig.")
        if parsed.execute != (parsed.expect_plan is not None):
            parser.error("--execute und --expect-plan müssen gemeinsam angegeben werden.")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
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
            elif parsed.command == "analyze":
                analysis_receipt = health_lab.run_resting_hr_analysis(
                    RestingHeartRateAnalysisConfig(
                        analysis_definition_id=AnalysisDefinitionId(parsed.definition),
                        start_date=parsed.start_date,
                        end_date=parsed.end_date,
                    )
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
        print(
            json.dumps(
                {
                    "analysis_definition_id": str(analysis_receipt.analysis_definition_id),
                    "analysis_run_id": str(analysis_receipt.analysis_run_id),
                    "diagnostics": analysis_receipt.diagnostics,
                    "model_maturity": (
                        None
                        if analysis_receipt.model_maturity is None
                        else analysis_receipt.model_maturity.value
                    ),
                    "operation_id": str(analysis_receipt.operation_id),
                    "provenance": _provenance_json(analysis_receipt.provenance),
                    "result_ref": (
                        str(analysis_receipt.result_ref) if analysis_receipt.result_ref else None
                    ),
                    "result": _analysis_json(overview),
                    "runtime_config": runtime_config,
                    "schema_version": "1.0",
                    "snapshot_ref": (
                        str(analysis_receipt.snapshot_ref)
                        if analysis_receipt.snapshot_ref
                        else None
                    ),
                    "status": analysis_receipt.status.value,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif parsed.command == "analyze":
        print(f"Ruhepulsanalyse: {analysis_receipt.status.value}")
        print(
            "Modellreife: "
            f"{analysis_receipt.model_maturity.value if analysis_receipt.model_maturity else '-'}"
        )
        if overview.resting_hr_analysis is not None:
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
            print(f"{detail.case.case_id}: {detail.case.kind.value} ({detail.source_type or '-'})")
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
    if parsed.command == "analyze" and analysis_receipt.status not in {
        AnalysisStatus.COMPLETED,
        AnalysisStatus.REUSED,
    }:
        return 3
    if parsed.command == "import":
        if import_write_receipt is None:
            return 3 if import_plan.approval.status is WriteApprovalStatus.BLOCKED else 0
        result = import_write_receipt.result
        if isinstance(result, WriteNotStarted) or result.status not in {
            ImportStatus.COMMITTED,
            ImportStatus.DUPLICATE,
        }:
            return 3
    return 0
