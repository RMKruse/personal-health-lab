from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from personal_health_lab.adapters._config import load_runtime_config
from personal_health_lab.application import (
    AnalysisDefinitionId,
    AnalysisProvenance,
    AnalysisStatus,
    AssociationInterval,
    ConfigurationError,
    DataMode,
    HealthLab,
    ImportStatus,
    Overview,
    OverviewSelection,
    RestingHeartRateAnalysisConfig,
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
    analysis = commands.add_parser("analyze", help="Verzögerungsprofil analysieren")
    analysis.add_argument("--definition", default="lag-signal-v1")
    analysis.add_argument("--start-date", type=date.fromisoformat)
    analysis.add_argument("--end-date", type=date.fromisoformat)
    analysis.add_argument("--json", action="store_true", dest="as_json")
    return parser


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


def main(args: Sequence[str] | None = None) -> int:
    parsed = _parser().parse_args(args)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        config = load_runtime_config(
            explicit_mode=parsed.mode,
            explicit_synthetic_store=parsed.synthetic_store,
            explicit_real_store=parsed.real_store,
            config_file=parsed.config,
        )
        with HealthLab.open(config) as health_lab:
            if parsed.command == "import":
                import_receipt = health_lab.import_health_export(parsed.package)
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
        print(
            "Konfiguration: "
            + json.dumps(runtime_config, ensure_ascii=False, sort_keys=True)
        )
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
        print(
            json.dumps(
                {
                    "anomaly_count": import_receipt.anomaly_count,
                    "diagnostics": import_receipt.diagnostics,
                    "import_id": str(import_receipt.import_id),
                    "operation_id": str(import_receipt.operation_id),
                    "package_hash": import_receipt.package_hash,
                    "package_record_count": import_receipt.package_record_count,
                    "record_count": import_receipt.record_count,
                    "logical_measurement_count": import_receipt.logical_measurement_count,
                    "measurement_version_count": import_receipt.measurement_version_count,
                    "runtime_config": runtime_config,
                    "schema_version": "1.0",
                    "snapshot_ref": (
                        str(import_receipt.snapshot_ref) if import_receipt.snapshot_ref else None
                    ),
                    "status": import_receipt.status.value,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif parsed.command == "import":
        print(f"Health-Exportimport: {import_receipt.status.value}")
        print(f"Importierte Records: {import_receipt.record_count}")
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
    if parsed.command == "import" and import_receipt.status not in {
        ImportStatus.COMMITTED,
        ImportStatus.DUPLICATE,
    }:
        return 3
    return 0
