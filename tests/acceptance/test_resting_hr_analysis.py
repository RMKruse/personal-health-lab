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
from personal_health_lab.synthetic_export import GenerationOptions, generate_export


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
    assert first.model_maturity is ModelMaturityStatus.ROBUST
    assert first.result_ref is not None
    assert second.status is AnalysisStatus.COMPLETED
    assert second.snapshot_ref == first.snapshot_ref

    first_result = first_overview.resting_hr_analysis
    second_result = second_overview.resting_hr_analysis
    assert first_result is not None
    assert second_result is not None
    assert first_result.model_maturity == "robust"
    assert first_result.methodology.bootstrap_method == "moving_block"
    assert first_result.methodology.block_length_days == 7
    assert first_result.methodology.resample_count > 0
    assert first_result.diagnostics.model_readiness == "robust"
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
    lag_one = first_result.lag_associations[0]
    assert lag_one.pointwise_interval.lower_per_100_kcal < -0.9
    assert lag_one.pointwise_interval.upper_per_100_kcal > -0.9
    assert lag_one.simultaneous_band is not None
    assert (
        lag_one.simultaneous_band.lower_per_100_kcal
        <= lag_one.pointwise_interval.lower_per_100_kcal
    )
    assert (
        lag_one.simultaneous_band.upper_per_100_kcal
        >= lag_one.pointwise_interval.upper_per_100_kcal
    )
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


def test_null_scenario_does_not_present_a_stable_association(tmp_path: Path) -> None:
    package = generate_export("null-v1", 42, tmp_path / "fixture")
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
        result = health_lab.load_overview(OverviewSelection()).resting_hr_analysis

    assert receipt.status is AnalysisStatus.COMPLETED
    assert result is not None
    assert result.diagnostics.association_guardrail == "simultaneous_band_includes_zero"
    assert all(
        item.simultaneous_band is not None
        and item.simultaneous_band.lower_per_100_kcal
        <= 0
        <= item.simultaneous_band.upper_per_100_kcal
        for item in result.lag_associations
    )


def test_analysis_reports_insufficient_and_unstable_inputs_with_stable_diagnostics(
    tmp_path: Path,
) -> None:
    sparse = generate_export(
        "null-v1",
        42,
        tmp_path / "sparse",
        options=GenerationOptions(
            missing_active_energy_probability=0.999,
            missing_resting_heart_rate_probability=0.999,
        ),
    )
    constant = generate_export(
        "null-v1",
        42,
        tmp_path / "constant",
        options=GenerationOptions(resting_heart_rate_noise_standard_deviation=0.0),
    )
    incomplete = generate_export(
        "null-v1",
        42,
        tmp_path / "incomplete",
        options=GenerationOptions(missing_active_energy_probability=0.1),
    )

    receipts = []
    for name, package in (
        ("sparse", sparse),
        ("constant", constant),
        ("incomplete", incomplete),
    ):
        runtime = RuntimeConfig(
            mode=DataMode.SYNTHETIC,
            synthetic_store=tmp_path / f"{name}-store",
            real_store=tmp_path / f"{name}-real-store",
        )
        with HealthLab.open(runtime) as health_lab:
            health_lab.import_health_export(package.export_path)
            receipts.append(
                health_lab.run_resting_hr_analysis(
                    RestingHeartRateAnalysisConfig(AnalysisDefinitionId("lag-signal-v1"))
                )
            )

    assert receipts[0].status is AnalysisStatus.INSUFFICIENT_DATA
    assert receipts[0].diagnostics == ("insufficient_complete_days",)
    assert receipts[1].status is AnalysisStatus.UNSTABLE
    assert receipts[1].diagnostics == ("constant_outcome",)
    assert receipts[2].status is AnalysisStatus.COMPLETED
    assert receipts[2].model_maturity is ModelMaturityStatus.EXPLORATORY
    assert "model_readiness_exploratory" in receipts[2].diagnostics
