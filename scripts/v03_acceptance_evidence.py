from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import cast

ROOT = Path(__file__).parents[1]
MATRIX = ROOT / "tests/acceptance/v03_matrix.toml"
V02_MATRIX = ROOT / "tests/acceptance/v02_matrix.toml"
METHOD_SOURCES = {
    "activity-derivation/v1": ROOT / "src/personal_health_lab/storage/_store.py",
    "cow-migration/v1": ROOT / "src/personal_health_lab/storage/_store.py",
    "full-snapshot-import/v1": ROOT / "src/personal_health_lab/storage/_store.py",
    "manual-snapshot/v1": ROOT / "src/personal_health_lab/storage/_store.py",
    "metadata-backup/v1": ROOT / "src/personal_health_lab/storage/_store.py",
    "restore-activate/v1": ROOT / "src/personal_health_lab/recovery/__init__.py",
    "restore-source-import/v1": ROOT / "src/personal_health_lab/recovery/__init__.py",
    "restore-start/v1": ROOT / "src/personal_health_lab/recovery/__init__.py",
}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _row_hash(row: dict[str, object]) -> str:
    return _sha256_bytes(
        json.dumps(row, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    )


def _v02_evidence_hash() -> str:
    script = ROOT / "scripts/v02_acceptance_evidence.py"
    spec = importlib.util.spec_from_file_location("v02_acceptance_evidence", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    evidence = module.build_evidence()
    return _sha256_bytes((json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode())


def _validate_matrix(matrix: dict[str, object]) -> None:
    sys.path.insert(0, str(ROOT / "tests/acceptance"))
    from v03_matrix_validator import validate_matrix

    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    validate_matrix(
        matrix,
        root=ROOT,
        collected_node_ids={line for line in collected.stdout.splitlines() if "::" in line},
    )


def build_evidence() -> dict[str, object]:
    matrix = cast(dict[str, object], tomllib.loads(MATRIX.read_text(encoding="utf-8")))
    _validate_matrix(matrix)
    contracts = cast(list[dict[str, object]], matrix["contract"])
    cases = cast(list[dict[str, object]], matrix["case"])
    fixtures = cast(list[dict[str, object]], matrix["fixture"])
    commit = os.environ.get("GITHUB_SHA") or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    repository = os.environ.get("GITHUB_REPOSITORY")
    server = os.environ.get("GITHUB_SERVER_URL")
    run_url = (
        f"{server}/{repository}/actions/runs/{run_id}"
        if server and repository and run_id != "local"
        else None
    )
    gate_statuses = {
        "linux": os.environ.get("V03_LINUX_GATE_STATUS", "success"),
        "macos": os.environ.get("V03_MACOS_GATE_STATUS", "success"),
    }
    missing = [name for name, status in gate_statuses.items() if status != "success"]
    matrix_text = MATRIX.read_text(encoding="utf-8")
    missing.extend(method_id for method_id in METHOD_SOURCES if method_id not in matrix_text)
    return {
        "schema_version": 1,
        "release": "v0.3",
        "git_commit": commit,
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "environment_lock_sha256": _sha256(ROOT / "uv.lock"),
        "v02_matrix_sha256": _sha256(V02_MATRIX),
        "v02_evidence_sha256": _v02_evidence_hash(),
        "v03_matrix_sha256": _sha256(MATRIX),
        "contracts": {str(row["id"]): _row_hash(row) for row in contracts},
        "cases": {str(row["id"]): _row_hash(row) for row in cases},
        "fixtures": {str(row["id"]): str(row["sha256"]) for row in fixtures},
        "methods": {
            method_id: {
                "source": str(path.relative_to(ROOT)),
                "sha256": _sha256(path),
            }
            for method_id, path in sorted(METHOD_SOURCES.items())
        },
        "ci": {
            "run_id": run_id,
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local"),
            "run_url": run_url,
            "gates": [
                {"name": name, "runner": runner, "status": gate_statuses[name]}
                for name, runner in (("linux", "ubuntu-latest"), ("macos", "macos-latest"))
            ],
        },
        "missing": sorted(missing),
    }


def main() -> None:
    evidence = build_evidence()
    if evidence["missing"]:
        raise SystemExit(f"incomplete V0.3 evidence: {evidence['missing']}")
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "v03-acceptance-evidence.json"
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
