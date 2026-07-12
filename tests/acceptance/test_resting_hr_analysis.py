from pathlib import Path

import pytest

from personal_health_lab.application import (
    AnalysisDefinitionId,
    AnalysisStatus,
    DataMode,
    HealthLab,
    ModelMaturityStatus,
    OverviewSelection,
    RestingHeartRateAnalysisConfig,
    RuntimeConfig,
)
from personal_health_lab.synthetic_export import generate_export


def test_signal_scenario_runs_as_a_pinned_deterministic_lag_analysis(tmp_path: Path) -> None:
    package = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    newer_package = generate_export("lag-signal-v1", 43, tmp_path / "newer-fixture")
    runtime = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "store",
        real_store=tmp_path / "real-store",
    )
    analysis = RestingHeartRateAnalysisConfig(
        analysis_definition_id=AnalysisDefinitionId("lag-signal-v1")
    )

    with HealthLab.open(runtime) as health_lab:
        imported = health_lab.import_health_export(package.export_path)
        first = health_lab.run_resting_hr_analysis(analysis)
        first_overview = health_lab.load_overview(OverviewSelection())
        second = health_lab.run_resting_hr_analysis(analysis)
        second_overview = health_lab.load_overview(OverviewSelection())

    assert first.status is AnalysisStatus.COMPLETED
    assert first.snapshot_ref == imported.snapshot_ref
    assert first.analysis_definition_id == analysis.analysis_definition_id
    assert first.model_maturity is ModelMaturityStatus.EXPLORATORY
    assert first.result_ref is not None
    assert second.status is AnalysisStatus.COMPLETED
    assert second.snapshot_ref == first.snapshot_ref

    first_result = first_overview.resting_hr_analysis
    second_result = second_overview.resting_hr_analysis
    assert first_result is not None
    assert second_result is not None
    assert first_result.model_maturity == "exploratory"
    assert len(first_result.lag_associations) == 7
    assert [item.lag_days for item in first_result.lag_associations] == list(range(1, 8))
    assert all(item.exposure_unit.value == "kcal" for item in first_result.lag_associations)
    assert all(item.outcome_unit.value == "count/min" for item in first_result.lag_associations)
    assert all(
        item.estimate_per_personal_standard_deviation
        == pytest.approx(
            item.estimate_per_100_kcal * first_result.personal_standard_deviation_kcal / 100.0
        )
        for item in first_result.lag_associations
    )
    assert first_result.lag_associations[0].estimate_per_100_kcal == pytest.approx(-0.9, abs=0.15)
    assert all(abs(item.estimate_per_100_kcal) < 0.25 for item in first_result.lag_associations[1:])
    assert first_result.cumulative_association.estimate_per_100_kcal < -0.6
    assert [item.estimate_per_100_kcal for item in first_result.lag_associations] == pytest.approx(
        [item.estimate_per_100_kcal for item in second_result.lag_associations], abs=1e-12
    )

    with HealthLab.open(runtime) as health_lab:
        newer_import = health_lab.import_health_export(newer_package.export_path)
        newer_overview = health_lab.load_overview(OverviewSelection())

    assert newer_import.snapshot_ref != first.snapshot_ref
    assert newer_overview.resting_hr_analysis is None
