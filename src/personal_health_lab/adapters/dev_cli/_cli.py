from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from personal_health_lab.synthetic_export import GenerationOptions, generate_export

_SCENARIOS = ("lag-signal-v1", "null-v1")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="healthlab-dev")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="Synthetisches Health-Exportpaket erzeugen")
    generate.add_argument("--scenario", required=True, choices=_SCENARIOS)
    generate.add_argument("--seed", required=True, type=int)
    generate.add_argument("--destination", required=True, type=Path)
    generate.add_argument("--noise-standard-deviation", type=float, default=0.7)
    generate.add_argument("--missing-active-energy-probability", type=float, default=0.0)
    generate.add_argument("--missing-resting-heart-rate-probability", type=float, default=0.0)
    generate.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _read_checksums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        digest, filename = line.split("  ", maxsplit=1)
        checksums[filename] = digest
    return checksums


def main(args: Sequence[str] | None = None) -> int:
    parser = _parser()
    parsed = parser.parse_args(args)
    try:
        options = GenerationOptions(
            noise_standard_deviation=parsed.noise_standard_deviation,
            missing_active_energy_probability=parsed.missing_active_energy_probability,
            missing_resting_heart_rate_probability=(
                parsed.missing_resting_heart_rate_probability
            ),
        )
        fixture = generate_export(
            parsed.scenario,
            parsed.seed,
            parsed.destination,
            options=options,
        )
    except ValueError as error:
        parser.error(str(error))
    metadata: object = json.loads(fixture.metadata_path.read_text(encoding="utf-8"))
    checksums = _read_checksums(fixture.checksums_path)
    if parsed.as_json:
        print(
            json.dumps(
                {
                    "checksums": checksums,
                    "files": {
                        "export": str(fixture.export_path),
                        "metadata": str(fixture.metadata_path),
                    },
                    "metadata": metadata,
                    "schema_version": "1.0",
                    "status": "generated",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(f"Szenario erzeugt: {fixture.scenario_id} (Seed {fixture.seed})")
        print(f"Export: {fixture.export_path}")
        print(f"Metadaten: {fixture.metadata_path}")
        print(fixture.metadata_path.read_text(encoding="utf-8"), end="")
        print("Prüfsummen:")
        print(fixture.checksums_path.read_text(encoding="ascii"), end="")
    return 0
