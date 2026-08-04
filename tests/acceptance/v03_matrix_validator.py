from __future__ import annotations

import hashlib
import importlib.util
import re
from collections import Counter
from collections.abc import Collection
from pathlib import Path
from typing import cast

_EVIDENCE = {
    "positive",
    "negative",
    "boundary",
    "transition",
    "fault",
    "parity",
    "security",
    "reproducibility",
}
_PLATFORMS = {"all", "linux", "macos"}
_LEVELS = {"application", "adapter", "meta", "platform"}


def _rows(matrix: dict[str, object], name: str) -> list[dict[str, object]]:
    rows = matrix.get(name)
    assert isinstance(rows, list), f"missing V0.3 matrix section: {name}"
    assert all(isinstance(row, dict) for row in rows), f"invalid {name} row"
    return cast(list[dict[str, object]], rows)


def _ids(rows: list[dict[str, object]], prefix: str) -> set[str]:
    identifiers = [row.get("id") for row in rows]
    assert all(
        isinstance(identifier, str)
        and re.fullmatch(rf"{prefix}[A-Z0-9]+(?:-[A-Z0-9]+)*-V[1-9][0-9]*", identifier)
        for identifier in identifiers
    ), f"open or invalid {prefix} identifier"
    assert len(identifiers) == len(set(identifiers)), f"duplicate {prefix} identifier"
    return cast(set[str], set(identifiers))


def _references(row: dict[str, object], field: str) -> set[str]:
    values = row.get(field)
    assert isinstance(values, list) and all(isinstance(value, str) for value in values)
    return cast(set[str], set(values))


def validate_matrix(
    matrix: dict[str, object], *, root: Path, collected_node_ids: Collection[str]
) -> None:
    assert matrix.get("schema_version") == 1
    assert matrix.get("release") == "v0.3-delta"
    contracts = _rows(matrix, "contract")
    fixtures = _rows(matrix, "fixture")
    cases = _rows(matrix, "case")
    contract_ids = _ids(contracts, "V03-C-")
    fixture_ids = _ids(fixtures, "V03-F-")
    _ids(cases, "V03-A-")
    all_ids = [str(row["id"]) for rows in (contracts, fixtures, cases) for row in rows]
    assert len(all_ids) == len(set(all_ids)), "duplicate V0.3 matrix ID"

    required_evidence: dict[str, set[str]] = {}
    for contract in contracts:
        contract_id = str(contract["id"])
        assert isinstance(contract.get("source"), str) and contract["source"]
        assert isinstance(contract.get("statement"), str) and contract["statement"]
        evidence = _references(contract, "evidence")
        assert evidence and evidence <= _EVIDENCE, f"invalid evidence in {contract_id}"
        required_evidence[contract_id] = evidence

    root = root.resolve()
    for fixture in fixtures:
        fixture_id = str(fixture["id"])
        assert fixture.get("platform") in _PLATFORMS
        digest = fixture.get("sha256")
        assert isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        artifact = fixture.get("artifact")
        generator = fixture.get("generator")
        assert (artifact is None) != (generator is None), (
            f"{fixture_id} needs exactly one artifact or generator"
        )
        if artifact is not None:
            assert isinstance(artifact, str)
            path = (root / artifact).resolve()
            assert path.is_relative_to(root) and path.is_file(), f"missing fixture {fixture_id}"
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, (
                f"fixture hash changed: {fixture_id}"
            )
        else:
            assert isinstance(generator, str) and generator
            seed = fixture.get("seed")
            options = fixture.get("options")
            assert isinstance(seed, int)
            assert isinstance(options, dict)
            module_name, separator, function_name = generator.rpartition(".")
            assert separator and module_name and function_name
            module_path = root.joinpath(*module_name.split(".")).with_suffix(".py")
            spec = importlib.util.spec_from_file_location(module_name, module_path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            generated = getattr(module, function_name)(seed=seed, options=options)
            assert isinstance(generated, str)
            assert hashlib.sha256(generated.encode()).hexdigest() == digest, (
                f"generated fixture hash changed: {fixture_id}"
            )

    required_case_fields = {
        "id",
        "contracts",
        "fixtures",
        "runner",
        "initial_state",
        "operation",
        "level",
        "platform",
        "expected_result",
        "expected_effect",
        "forbidden_side_effects",
        "evidence",
    }
    delivered_evidence = {contract_id: set() for contract_id in contract_ids}
    workflow = (root / ".github/workflows/quality.yml").read_text(encoding="utf-8")
    runners: set[str] = set()
    for case in cases:
        case_id = str(case["id"])
        assert required_case_fields <= case.keys(), f"incomplete case row {case_id}"
        referenced_contracts = _references(case, "contracts")
        referenced_fixtures = _references(case, "fixtures")
        evidence = _references(case, "evidence")
        assert referenced_contracts and referenced_contracts <= contract_ids
        assert referenced_fixtures <= fixture_ids
        assert evidence and evidence <= _EVIDENCE
        assert case["level"] in _LEVELS
        platform = case["platform"]
        assert platform in _PLATFORMS
        if platform != "all":
            assert re.search(rf"(?m)^  {platform}:$", workflow), (
                f"no CI job for {platform} case {case_id}"
            )
        runner = case["runner"]
        assert isinstance(runner, str) and re.fullmatch(
            r"tests/[a-zA-Z0-9_./-]+\.py::test_[a-zA-Z0-9_]+", runner
        ), f"runner must be a full Pytest node ID: {case_id}"
        assert runner not in runners, f"duplicate V0.3 runner: {runner}"
        runners.add(runner)
        for contract_id in referenced_contracts:
            delivered_evidence[contract_id].update(evidence)

    assert set().union(*(set(_references(case, "fixtures")) for case in cases)) == fixture_ids, (
        "unreferenced V0.3 fixture"
    )
    for contract_id, required in required_evidence.items():
        missing = required - delivered_evidence[contract_id]
        assert required <= delivered_evidence[contract_id], (
            f"missing evidence for {contract_id}: {sorted(missing)}"
        )

    collected = Counter(node_id.split("[", 1)[0] for node_id in collected_node_ids)
    missing_runners = runners - collected.keys()
    duplicate_runners = {runner: collected[runner] for runner in runners if collected[runner] > 1}
    assert not missing_runners and not duplicate_runners, (
        "invalid V0.3 runner registry; "
        f"missing={sorted(missing_runners)}, duplicate={duplicate_runners}"
    )
