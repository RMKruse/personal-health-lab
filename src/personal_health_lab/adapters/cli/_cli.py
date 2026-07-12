from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from personal_health_lab.adapters._config import load_runtime_config
from personal_health_lab.application import (
    AnalysisDefinitionId,
    AnalysisStatus,
    ConfigurationError,
    DataMode,
    HealthLab,
    Overview,
    OverviewSelection,
    RestingHeartRateAnalysisConfig,
)


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
    cumulative = result.cumulative_association
    return {
        "analysis_definition_id": str(result.analysis_definition_id),
        "cumulative_association": {
            "direction": cumulative.direction.value,
            "estimate_per_100_kcal": cumulative.estimate_per_100_kcal,
            "estimate_per_personal_standard_deviation": (
                cumulative.estimate_per_personal_standard_deviation
            ),
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
            }
            for item in result.lag_associations
        ],
        "model_maturity": result.model_maturity,
        "personal_standard_deviation_kcal": result.personal_standard_deviation_kcal,
        "snapshot_ref": str(result.snapshot_id),
    }


def main(args: Sequence[str] | None = None) -> int:
    parsed = _parser().parse_args(args)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
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
                overview = health_lab.load_overview(OverviewSelection())
            else:
                overview = health_lab.load_overview(OverviewSelection())
    except ConfigurationError as error:
        _parser().error(str(error))

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
                    "result_ref": (
                        str(analysis_receipt.result_ref) if analysis_receipt.result_ref else None
                    ),
                    "result": _analysis_json(overview),
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
                print(f"Lag {item.lag_days}: {item.estimate_per_100_kcal:.2f} bpm/100 kcal")
            cumulative = overview.resting_hr_analysis.cumulative_association
            print(f"Kumulativ: {cumulative.estimate_per_100_kcal:.2f} bpm/100 kcal")
    elif parsed.command == "import" and parsed.as_json:
        print(
            json.dumps(
                {
                    "diagnostics": import_receipt.diagnostics,
                    "import_id": str(import_receipt.import_id),
                    "operation_id": str(import_receipt.operation_id),
                    "package_hash": import_receipt.package_hash,
                    "package_record_count": import_receipt.package_record_count,
                    "record_count": import_receipt.record_count,
                    "logical_measurement_count": import_receipt.logical_measurement_count,
                    "measurement_version_count": import_receipt.measurement_version_count,
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
                        "snapshot_count": overview.snapshot_count,
                    },
                    "runtime_config": {
                        "mode": config.mode.value,
                        "real_store": "<redacted>",
                        "synthetic_store": "<redacted>",
                    },
                    "schema_version": overview.schema_version,
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
    return 0
