from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path

from personal_health_lab.adapters._config import load_runtime_config
from personal_health_lab.application import (
    ConfigurationError,
    DataMode,
    HealthLab,
    OverviewSelection,
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
    return parser


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
                receipt = health_lab.import_health_export(parsed.package)
            else:
                overview = health_lab.load_overview(OverviewSelection())
    except ConfigurationError as error:
        _parser().error(str(error))

    if parsed.command == "import" and parsed.as_json:
        print(
            json.dumps(
                {
                    "diagnostics": receipt.diagnostics,
                    "import_id": str(receipt.import_id),
                    "operation_id": str(receipt.operation_id),
                    "package_hash": receipt.package_hash,
                    "record_count": receipt.record_count,
                    "schema_version": "1.0",
                    "snapshot_ref": str(receipt.snapshot_ref) if receipt.snapshot_ref else None,
                    "status": receipt.status.value,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif parsed.command == "import":
        print(f"Health-Exportimport: {receipt.status.value}")
        print(f"Importierte Records: {receipt.record_count}")
    elif parsed.as_json:
        print(
            json.dumps(
                {
                    "daily_series": [
                        {
                            "data_type": series.data_type.value,
                            "unit": series.unit.value,
                            "values": [
                                {"day": value.day.isoformat(), "value": value.value}
                                for value in series.values
                            ],
                        }
                        for series in overview.daily_series
                    ],
                    "message": overview.message,
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
    return 0
