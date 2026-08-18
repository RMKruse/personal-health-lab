import copy
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from v04_matrix_validator import validate_matrix

_ROOT = Path(__file__).parents[2]
_MATRIX = Path(__file__).with_name("v04_matrix.toml")


def _matrix() -> dict[str, object]:
    with _MATRIX.open("rb") as source:
        return tomllib.load(source)


def _collected_node_ids() -> set[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return {line for line in result.stdout.splitlines() if "::" in line}


def test_v04_matrix_is_well_formed_and_fully_registered() -> None:
    validate_matrix(_matrix(), root=_ROOT, collected_node_ids=_collected_node_ids())


def test_v04_matrix_rejects_open_ids_cases_and_hash_references() -> None:
    matrix = _matrix()
    collected_node_ids = _collected_node_ids()
    mutations = []

    missing_definition = copy.deepcopy(matrix)
    missing_definition["analysis_definition_ids"].pop()
    mutations.append(missing_definition)

    duplicate_case = copy.deepcopy(matrix)
    duplicate_case["case"][1]["id"] = duplicate_case["case"][0]["id"]
    mutations.append(duplicate_case)

    changed_hash = copy.deepcopy(matrix)
    changed_hash["hash_reference"][0]["sha256"] = "0" * 64
    mutations.append(changed_hash)

    open_hash_reference = copy.deepcopy(matrix)
    open_hash_reference["case"][0]["hash_references"].append("V04-H-UNKNOWN-V1")
    mutations.append(open_hash_reference)

    missing_hash_reference = copy.deepcopy(matrix)
    missing_hash_reference["case"][0]["hash_references"].clear()
    mutations.append(missing_hash_reference)

    missing_runner = copy.deepcopy(matrix)
    missing_runner["case"][0]["runner"] = "tests/acceptance/not_real.py::test_not_real"
    mutations.append(missing_runner)

    for invalid in mutations:
        with pytest.raises(AssertionError):
            validate_matrix(invalid, root=_ROOT, collected_node_ids=collected_node_ids)
