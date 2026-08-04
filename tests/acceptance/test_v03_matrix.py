import copy
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from v03_matrix_validator import validate_matrix

_ROOT = Path(__file__).parents[2]
_MATRIX = Path(__file__).with_name("v03_matrix.toml")


def _matrix() -> dict[str, object]:
    with _MATRIX.open("rb") as source:
        return tomllib.load(source)


def _collected_node_ids() -> set[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return {line for line in result.stdout.splitlines() if "::" in line}


def test_v03_matrix_is_well_formed_and_fully_registered() -> None:
    validate_matrix(_matrix(), root=_ROOT, collected_node_ids=_collected_node_ids())


def test_v03_matrix_rejects_open_references_and_hashes() -> None:
    matrix = _matrix()
    collected_node_ids = _collected_node_ids()
    mutations = []

    duplicate = copy.deepcopy(matrix)
    duplicate["case"][1]["id"] = duplicate["case"][0]["id"]
    mutations.append(duplicate)

    short_runner = copy.deepcopy(matrix)
    short_runner["case"][0]["runner"] = "test_import_details"
    mutations.append(short_runner)

    missing_runner = copy.deepcopy(matrix)
    missing_runner["case"][0]["runner"] = "tests/acceptance/not_real.py::test_not_real"
    mutations.append(missing_runner)

    missing_evidence = copy.deepcopy(matrix)
    missing_evidence["case"][0]["evidence"] = ["positive"]
    mutations.append(missing_evidence)

    unknown_platform = copy.deepcopy(matrix)
    unknown_platform["case"][0]["platform"] = "windows"
    mutations.append(unknown_platform)

    changed_fixture = copy.deepcopy(matrix)
    changed_fixture["fixture"][0]["sha256"] = "0" * 64
    mutations.append(changed_fixture)

    for invalid in mutations:
        with pytest.raises(AssertionError):
            validate_matrix(invalid, root=_ROOT, collected_node_ids=collected_node_ids)
