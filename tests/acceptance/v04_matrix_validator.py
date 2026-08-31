import hashlib
import re
from collections.abc import Collection
from pathlib import Path
from typing import cast

import personal_health_lab.application as application
from personal_health_lab.analysis import analysis_definitions

_DEFINITION_IDS = {
    "rhr-activity-lag-1-7-v1",
    "rhr-activity-lag-1-30-v1",
    "weight-core-7-14-30-90-v1",
    "rhr-weight-association-7-14-30-90-v1",
}
_CONTRACT_IDS = {
    "V04-C-ANALYSIS-ARTIFACT-SCHEMAS-V1",
    "V04-C-ANALYSIS-CATALOG-V1",
    "V04-C-ANALYSIS-INPUT-BUNDLE-V1",
    "V04-C-ANALYSIS-INPUT-SCHEMA2-V1",
    "V04-C-ANALYSIS-INTEGRITY-REPRODUCIBILITY-V1",
    "V04-C-ANALYSIS-LEGACY-MIGRATION-V1",
    "V04-C-ANALYSIS-PUBLICATION-V1",
    "V04-C-ANALYSIS-PROJECTION-COMPATIBILITY-V1",
    "V04-C-ANALYSIS-RESULT-SELECTION-V1",
    "V04-C-ANALYSIS-RESULT-UNAVAILABLE-V1",
    "V04-C-ANALYSIS-REUSE-IDENTITY-V1",
    "V04-C-ANALYSIS-RUN-HISTORY-V1",
    "V04-C-ANALYSIS-RUN-ATOMICITY-V1",
    "V04-C-ANALYSIS-STATUS-AXES-V1",
    "V04-C-HISTORICAL-WRITE-LOCK-V1",
    "V04-C-LONG-LAG-INDEPENDENCE-V1",
    "V04-C-LONG-LAG-PROFILE-V1",
    "V04-C-MATRIX-VALIDATION-V1",
    "V04-C-RUN-ANALYSIS-PLAN-V1",
    "V04-C-RUN-ANALYSIS-RECHECK-V1",
    "V04-C-RUN-ANALYSIS-START-V1",
    "V04-C-RUN-ANALYSIS-LEGACY-V1",
    "V04-C-SNAPSHOT-SELECTION-V1",
    "V04-C-SHORT-LAG-ACTIVITY-DESIGN-V1",
    "V04-C-SHORT-LAG-BOOTSTRAP-UNCERTAINTY-V1",
    "V04-C-SHORT-LAG-CONTEXT-SENSITIVITY-V1",
    "V04-C-SHORT-LAG-MATURITY-V1",
    "V04-C-SHORT-LAG-POINT-FIT-V1",
    "V04-C-SHORT-LAG-SYNTHETIC-CASES-V1",
    "V04-C-SHORT-LAG-TECHNICAL-OUTCOMES-V1",
    "V04-C-V03-COMPATIBILITY-V1",
}
_CASE_IDS = {
    "V04-A-ANALYSIS-ARTIFACT-READ-V1",
    "V04-A-ANALYSIS-CATALOG-V1",
    "V04-A-ANALYSIS-INPUT-BUNDLE-V1",
    "V04-A-ANALYSIS-LEGACY-MIGRATION-V1",
    "V04-A-ANALYSIS-PUBLICATION-V1",
    "V04-A-ANALYSIS-PUBLICATION-VALIDATION-V1",
    "V04-A-ANALYSIS-PROJECTION-COMPATIBILITY-V1",
    "V04-A-ANALYSIS-REPRODUCIBILITY-V1",
    "V04-A-ANALYSIS-RESULT-FAMILIES-V1",
    "V04-A-ANALYSIS-REUSE-INTEGRITY-V1",
    "V04-A-ANALYSIS-REUSE-V1",
    "V04-A-ANALYSIS-RUN-ATOMICITY-V1",
    "V04-A-ANALYSIS-RUN-HISTORY-V1",
    "V04-A-ANALYSIS-SELECTION-FAILURES-V1",
    "V04-A-ANALYSIS-LATEST-RUN-V1",
    "V04-A-HISTORICAL-WRITE-LOCK-V1",
    "V04-A-LONG-LAG-PROFILE-V1",
    "V04-A-MATRIX-VALIDATION-V1",
    "V04-A-RUN-ANALYSIS-LEGACY-V1",
    "V04-A-RUN-ANALYSIS-NONSTART-V1",
    "V04-A-RUN-ANALYSIS-PLAN-V1",
    "V04-A-RUN-ANALYSIS-REQUEST-V1",
    "V04-A-SNAPSHOT-SELECTION-V1",
    "V04-A-SNAPSHOT-UNAVAILABLE-V1",
    "V04-A-SHORT-LAG-POINT-FIT-V1",
    "V04-A-SHORT-LAG-BOOTSTRAP-FAILURE-V1",
    "V04-A-SHORT-LAG-CONTEXT-SENSITIVITY-V1",
    "V04-A-SHORT-LAG-MATURITY-BOUNDARY-V1",
    "V04-A-SHORT-LAG-MISSINGNESS-V1",
    "V04-A-SHORT-LAG-NULL-V1",
    "V04-A-SHORT-LAG-SCALING-RANK-V1",
    "V04-A-SHORT-LAG-UNCERTAINTY-SIGNAL-V1",
    "V04-A-V03-COMPATIBILITY-V1",
}
_HASH_IDS = {
    "V04-H-ANALYSIS-ARTIFACT-SCHEMAS-V1",
    "V04-H-ANALYSIS-DEFINITIONS-V1",
    "V04-H-ANALYSIS-PROJECTIONS-V1",
    "V04-H-LAG-CALIBRATION-V1",
    "V04-H-OUTCOME-ASSOCIATION-CALIBRATION-V1",
    "V04-H-WEIGHT-CALIBRATION-V1",
}


def _rows(matrix: dict[str, object], name: str) -> list[dict[str, object]]:
    rows = matrix.get(name)
    assert isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
    return cast(list[dict[str, object]], rows)


def _references(row: dict[str, object], field: str) -> set[str]:
    references = row.get(field)
    assert isinstance(references, list) and all(isinstance(item, str) for item in references)
    reference_set = set(cast(list[str], references))
    assert len(reference_set) == len(references)
    return reference_set


def _ids(rows: list[dict[str, object]], prefix: str) -> set[str]:
    identifiers = {row.get("id") for row in rows}
    assert all(
        isinstance(identifier, str) and re.fullmatch(rf"{prefix}[A-Z0-9-]+-V1", identifier)
        for identifier in identifiers
    )
    assert len(identifiers) == len(rows)
    return cast(set[str], identifiers)


def validate_matrix(
    matrix: dict[str, object], *, root: Path, collected_node_ids: Collection[str]
) -> None:
    assert matrix.get("schema_version") == 1
    assert matrix.get("release") == "v0.4-delta"
    assert _references(matrix, "analysis_definition_ids") == _DEFINITION_IDS
    assert _references(matrix, "read_operations") == {
        "load_analysis_catalog",
        "load_analysis_result",
        "load_analysis_runs",
        "load_snapshot_catalog",
    }
    assert _references(matrix, "projection_variants") == {
        "AnalysisCatalog",
        "AnalysisRuns",
        "ProjectionUnavailable",
        "RhrActivityLag1To30Result",
        "RhrActivityLag1To7Result",
        "RhrWeightAssociationResult",
        "SnapshotCatalog",
        "WeightCoreResult",
    }
    assert _references(matrix, "write_request_variants") == {"RunAnalysis"}
    assert _references(matrix, "write_plan_variants") == {
        "HistoricalModeWritePlan",
        "RunAnalysisPlan",
    }
    assert {str(item.analysis_definition_id) for item in analysis_definitions()} == _DEFINITION_IDS
    assert hasattr(application.HealthLab, "load_analysis_catalog")
    assert hasattr(application.HealthLab, "load_analysis_result")
    assert hasattr(application.HealthLab, "load_analysis_runs")
    assert hasattr(application.HealthLab, "load_snapshot_catalog")
    assert hasattr(application, "AnalysisCatalog")
    assert hasattr(application, "AnalysisRuns")
    assert hasattr(application, "ProjectionUnavailable")
    assert hasattr(application, "SnapshotCatalog")
    assert hasattr(application, "RhrActivityLag1To30Result")
    assert hasattr(application, "RhrActivityLag1To7Result")
    assert hasattr(application, "RhrWeightAssociationResult")
    assert hasattr(application, "WeightCoreResult")
    assert hasattr(application, "HistoricalModeWritePlan")
    assert hasattr(application, "RunAnalysis")
    assert hasattr(application, "RunAnalysisPlan")

    contracts = _rows(matrix, "contract")
    hash_references = _rows(matrix, "hash_reference")
    cases = _rows(matrix, "case")
    assert _ids(contracts, "V04-C-") == _CONTRACT_IDS
    assert _ids(hash_references, "V04-H-") == _HASH_IDS
    assert _ids(cases, "V04-A-") == _CASE_IDS

    for contract in contracts:
        assert isinstance(contract.get("source"), str) and contract["source"]
        assert isinstance(contract.get("statement"), str) and contract["statement"]

    root = root.resolve()
    for reference in hash_references:
        artifact = reference.get("artifact")
        digest = reference.get("sha256")
        assert isinstance(artifact, str)
        assert isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        path = (root / artifact).resolve()
        assert path.is_relative_to(root) and path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest

    required_case_fields = {
        "id",
        "contracts",
        "hash_references",
        "runner",
        "level",
        "platform",
        "expected_result",
        "forbidden_side_effects",
    }
    runners: set[str] = set()
    referenced_contracts: set[str] = set()
    referenced_hashes: set[str] = set()
    for case in cases:
        assert required_case_fields <= case.keys()
        contracts = _references(case, "contracts")
        assert contracts and contracts <= _CONTRACT_IDS
        referenced_contracts.update(contracts)
        hashes = _references(case, "hash_references")
        assert hashes and hashes <= _HASH_IDS
        referenced_hashes.update(hashes)
        assert case["level"] in {"application", "meta"}
        assert case["platform"] == "all"
        runner = case["runner"]
        assert isinstance(runner, str) and re.fullmatch(
            r"tests/[a-zA-Z0-9_./-]+\.py::test_[a-zA-Z0-9_]+", runner
        )
        runners.add(runner)

    assert referenced_contracts == _CONTRACT_IDS
    assert referenced_hashes == _HASH_IDS
    collected = set(collected_node_ids)
    assert not runners - collected, f"unregistered V0.4 runners: {sorted(runners - collected)}"
