from dataclasses import replace
from pathlib import Path

from personal_health_lab.application import (
    AnalysisDefinitionId,
    DataMode,
    HealthLab,
    RestingHeartRateAnalysisConfig,
    RuntimeConfig,
    SnapshotRef,
)
from personal_health_lab.synthetic_export import generate_export


def test_reuse_requires_every_reproduction_identity_to_match(tmp_path: Path) -> None:
    package = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    runtime = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "store",
        real_store=tmp_path / "real-store",
    )
    with HealthLab.open(runtime) as health_lab:
        health_lab.import_health_export(package.export_path)
        receipt = health_lab.run_resting_hr_analysis(
            RestingHeartRateAnalysisConfig(AnalysisDefinitionId("lag-signal-v1"))
        )

    provenance = receipt.provenance
    assert provenance is not None
    changed_candidates = (
        replace(provenance, snapshot_id=SnapshotRef("different")),
        replace(provenance, config_hash="different"),
        replace(provenance, config_schema_version="different"),
        replace(provenance, analysis_definition_id=AnalysisDefinitionId("different")),
        replace(provenance, code_commit="different"),
        replace(provenance, code_dirty=not provenance.code_dirty),
        replace(
            provenance,
            code_diff_hash=None if provenance.code_diff_hash else "different",
        ),
        replace(provenance, environment_lock_hash="different"),
    )

    assert all(candidate.reuse_key != provenance.reuse_key for candidate in changed_candidates)
    assert len({candidate.reuse_key for candidate in changed_candidates}) == len(changed_candidates)
