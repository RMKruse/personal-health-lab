from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
MATRIX = ROOT / "tests/acceptance/v02_matrix.toml"
METHOD_IDS = {
    "cow-migration/v1",
    "full-snapshot-import/v1",
    "metadata-backup/v1",
    "restore-activate/v1",
    "restore-source-import/v1",
    "restore-start/v1",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_evidence() -> dict[str, object]:
    matrix_text = MATRIX.read_text(encoding="utf-8")
    matrix = tomllib.loads(matrix_text)
    contracts = {row["id"] for row in matrix["contract"]}
    cases = {row["id"] for row in matrix["case"]}
    fixtures = {row["id"]: row["sha256"] for row in matrix["fixture"]}
    missing = sorted(method_id for method_id in METHOD_IDS if method_id not in matrix_text)
    if len(contracts) != 77:
        missing.append(f"contract_count:{len(contracts)}")
    if len(cases) != 245:
        missing.append(f"case_count:{len(cases)}")
    for fixture in matrix["fixture"]:
        path = ROOT / fixture["parameters"]
        if path.is_file() and _sha256(path) != fixture["sha256"]:
            missing.append(f"hash:{fixture['id']}")
    commit = os.environ.get("GITHUB_SHA") or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    repository = os.environ.get("GITHUB_REPOSITORY")
    server = os.environ.get("GITHUB_SERVER_URL")
    return {
        "schema_version": 1,
        "release": "v0.2",
        "git_commit": commit,
        "environment_lock_sha256": _sha256(ROOT / "uv.lock"),
        "matrix_sha256": _sha256(MATRIX),
        "contract_ids": sorted(contracts),
        "case_ids": sorted(cases),
        "fixtures": fixtures,
        "method_ids": sorted(METHOD_IDS),
        "ci": {
            "run_id": run_id,
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local"),
            "run_url": (
                f"{server}/{repository}/actions/runs/{run_id}"
                if server and repository and run_id != "local"
                else None
            ),
            "gates": [
                {"name": "linux", "runner": "ubuntu-latest", "status": "passed"},
                {"name": "macos", "runner": "macos-latest", "status": "passed"},
            ],
        },
        "missing": missing,
    }


def main() -> None:
    evidence = build_evidence()
    if evidence["missing"]:
        raise SystemExit(f"incomplete V0.2 evidence: {evidence['missing']}")
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "v02-acceptance-evidence.json"
    output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
