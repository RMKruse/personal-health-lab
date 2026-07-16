import fcntl
from datetime import date
from pathlib import Path

import pytest

from personal_health_lab.application import (
    AnalysisDefinitionId,
    AnalysisReceipt,
    AnalysisStatus,
    DataMode,
    HealthLab,
    ImportHealthExport,
    ImportReceipt,
    ModelMaturityStatus,
    OverviewSelection,
    OverviewStatus,
    RestingHeartRateAnalysisPlan,
    RunRestingHeartRateAnalysis,
    RuntimeConfig,
    WriteNotStarted,
    WriteNotStartedStatus,
)
from personal_health_lab.synthetic_export import GenerationOptions, generate_export


def _execute_import(health_lab: HealthLab, package_path: Path) -> ImportReceipt | WriteNotStarted:
    request = ImportHealthExport(package_path)
    plan = health_lab.preview_write(request)
    return health_lab.execute_write(request, expected_plan=plan.fingerprint).result


def _execute_analysis(
    health_lab: HealthLab, request: RunRestingHeartRateAnalysis
) -> AnalysisReceipt:
    plan = health_lab.preview_write(request)
    result = health_lab.execute_write(request, expected_plan=plan.fingerprint).result
    assert isinstance(result, AnalysisReceipt)
    return result


def test_analysis_preview_is_read_only_and_execution_rechecks_the_request(tmp_path: Path) -> None:
    runtime = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "store",
        real_store=tmp_path / "real-store",
    )
    request = RunRestingHeartRateAnalysis(AnalysisDefinitionId("lag-signal-v1"))

    with HealthLab.open(runtime) as health_lab:
        before = health_lab.load_overview(OverviewSelection())
        plan = health_lab.preview_write(request)
        after = health_lab.load_overview(OverviewSelection())
        changed = health_lab.execute_write(
            RunRestingHeartRateAnalysis(
                AnalysisDefinitionId("lag-signal-v1"), start_date=date(2024, 1, 1)
            ),
            expected_plan=plan.fingerprint,
        ).result
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint).result

    assert before == after
    assert isinstance(plan.details, RestingHeartRateAnalysisPlan)
    assert plan.details.base_snapshot_ref is None
    assert isinstance(changed, WriteNotStarted)
    assert changed.status is WriteNotStartedStatus.PLAN_CHANGED
    assert isinstance(receipt, AnalysisReceipt)
    assert receipt.status is AnalysisStatus.INSUFFICIENT_DATA


def test_analysis_store_busy_is_write_not_started(tmp_path: Path) -> None:
    runtime = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "store",
        real_store=tmp_path / "real-store",
    )
    request = RunRestingHeartRateAnalysis(AnalysisDefinitionId("lag-signal-v1"))

    with HealthLab.open(runtime) as health_lab:
        plan = health_lab.preview_write(request)
        with (runtime.active_store / ".writer.lock").open("a+b") as writer_lock:
            fcntl.flock(writer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = health_lab.execute_write(request, expected_plan=plan.fingerprint).result

    assert isinstance(result, WriteNotStarted)
    assert result.status is WriteNotStartedStatus.STORE_BUSY


def test_signal_scenario_runs_as_a_pinned_deterministic_lag_analysis(tmp_path: Path) -> None:
    package = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    newer_package = generate_export("lag-signal-v1", 43, tmp_path / "newer-fixture")
    runtime = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "store",
        real_store=tmp_path / "real-store",
    )
    analysis = RunRestingHeartRateAnalysis(
        analysis_definition_id=AnalysisDefinitionId("lag-signal-v1")
    )

    with HealthLab.open(runtime) as health_lab:
        imported = _execute_import(health_lab, package.export_path)
        first = _execute_analysis(health_lab, analysis)
        first_overview = health_lab.load_overview(OverviewSelection())

    with HealthLab.open(runtime) as health_lab:
        second = _execute_analysis(health_lab, analysis)
        second_overview = health_lab.load_overview(OverviewSelection())

    assert first.status is AnalysisStatus.COMPLETED
    assert first.snapshot_ref == imported.snapshot_ref
    assert first.analysis_definition_id == analysis.analysis_definition_id
    assert first.model_maturity is ModelMaturityStatus.ROBUST
    assert first.result_ref is not None
    assert first.provenance is not None
    assert len(first.provenance.config_hash) == 64
    assert len(first.provenance.code_commit) >= 40
    assert len(first.provenance.environment_lock_hash) == 64
    assert (first.provenance.code_diff_hash is not None) == first.provenance.code_dirty
    assert second.status is AnalysisStatus.REUSED
    assert second.analysis_run_id == first.analysis_run_id
    assert second.result_ref == first.result_ref
    assert second.snapshot_ref == first.snapshot_ref
    assert second.provenance == first.provenance

    first_result = first_overview.resting_hr_analysis
    second_result = second_overview.resting_hr_analysis
    assert first_result is not None
    assert second_result is not None
    assert first_overview.status is OverviewStatus.READY
    assert first_result.provenance == first.provenance
    assert second_result.provenance == second.provenance
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
        provisional_selection = OverviewSelection(
            start_date=date(2024, 1, 1), end_date=date(2024, 6, 30)
        )
        changed_config = _execute_analysis(
            health_lab,
            RunRestingHeartRateAnalysis(
                analysis_definition_id=analysis.analysis_definition_id,
                start_date=provisional_selection.start_date,
                end_date=provisional_selection.end_date,
            )
        )
        provisional_overview = health_lab.load_overview(provisional_selection)
        previous_snapshot_plan = health_lab.preview_write(analysis)
        newer_import = _execute_import(health_lab, newer_package.export_path)
        stale_execution = health_lab.execute_write(
            analysis, expected_plan=previous_snapshot_plan.fingerprint
        ).result
        newer_overview = health_lab.load_overview(OverviewSelection())
        changed_snapshot = _execute_analysis(health_lab, analysis)

    assert changed_config.status is AnalysisStatus.COMPLETED
    assert changed_config.model_maturity is ModelMaturityStatus.EXPLORATORY
    assert provisional_overview.status is OverviewStatus.PROVISIONAL
    assert provisional_overview.last_ready_analysis_provenance == first.provenance
    assert changed_config.analysis_run_id != first.analysis_run_id
    assert changed_config.provenance is not None
    assert changed_config.provenance.config_hash != first.provenance.config_hash
    assert changed_config.provenance.snapshot_id == first.provenance.snapshot_id
    assert (
        changed_config.provenance.analysis_definition_id == first.provenance.analysis_definition_id
    )
    assert changed_config.provenance.code_commit == first.provenance.code_commit
    assert changed_config.provenance.code_dirty == first.provenance.code_dirty
    assert changed_config.provenance.code_diff_hash == first.provenance.code_diff_hash
    assert changed_config.provenance.environment_lock_hash == first.provenance.environment_lock_hash

    assert newer_import.snapshot_ref != first.snapshot_ref
    assert isinstance(stale_execution, WriteNotStarted)
    assert stale_execution.status is WriteNotStartedStatus.PLAN_CHANGED
    assert newer_overview.resting_hr_analysis is None
    assert changed_snapshot.status is AnalysisStatus.COMPLETED
    assert changed_snapshot.analysis_run_id != first.analysis_run_id
    assert changed_snapshot.provenance is not None
    assert changed_snapshot.provenance.snapshot_id != first.provenance.snapshot_id
    assert changed_snapshot.provenance.config_hash == first.provenance.config_hash
    assert (
        changed_snapshot.provenance.analysis_definition_id
        == first.provenance.analysis_definition_id
    )
    assert changed_snapshot.provenance.code_commit == first.provenance.code_commit
    assert changed_snapshot.provenance.code_dirty == first.provenance.code_dirty
    assert changed_snapshot.provenance.code_diff_hash == first.provenance.code_diff_hash
    assert (
        changed_snapshot.provenance.environment_lock_hash == first.provenance.environment_lock_hash
    )


def test_null_scenario_does_not_present_a_stable_association(tmp_path: Path) -> None:
    package = generate_export("null-v1", 42, tmp_path / "fixture")
    runtime = RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "store",
        real_store=tmp_path / "real-store",
    )

    with HealthLab.open(runtime) as health_lab:
        _execute_import(health_lab, package.export_path)
        receipt = _execute_analysis(
            health_lab, RunRestingHeartRateAnalysis(AnalysisDefinitionId("lag-signal-v1"))
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
            _execute_import(health_lab, package.export_path)
            receipts.append(
                _execute_analysis(
                    health_lab,
                    RunRestingHeartRateAnalysis(AnalysisDefinitionId("lag-signal-v1")),
                )
            )

    assert receipts[0].status is AnalysisStatus.INSUFFICIENT_DATA
    assert receipts[0].diagnostics == ("insufficient_complete_days",)
    assert receipts[0].provenance is not None
    assert receipts[0].provenance.snapshot_id == receipts[0].snapshot_ref
    assert receipts[0].provenance.result_id is None
    assert receipts[1].status is AnalysisStatus.UNSTABLE
    assert receipts[1].diagnostics == ("constant_outcome",)
    assert receipts[1].provenance is not None
    assert receipts[1].provenance.snapshot_id == receipts[1].snapshot_ref
    assert receipts[1].provenance.result_id is None
    assert receipts[2].status is AnalysisStatus.COMPLETED
    assert receipts[2].model_maturity is ModelMaturityStatus.EXPLORATORY
    assert "model_readiness_exploratory" in receipts[2].diagnostics
