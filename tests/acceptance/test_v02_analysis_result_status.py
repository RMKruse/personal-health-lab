from datetime import date
from pathlib import Path

from personal_health_lab import DataMode
from personal_health_lab.application import (
    AnalysisDefinitionId,
    AnalysisFreshness,
    AnalysisReceipt,
    AnalysisStatus,
    ConfirmDataReviewBatch,
    DataQualityStatus,
    DataReviewSelection,
    HealthLab,
    ImportHealthExport,
    ModelMaturityStatus,
    OverviewSelection,
    ReproducibilityStatus,
    RunRestingHeartRateAnalysis,
    RuntimeConfig,
)
from personal_health_lab.synthetic_export import GenerationOptions, generate_export


def _execute(health_lab: HealthLab, request):
    plan = health_lab.preview_write(request)
    return health_lab.execute_write(request, expected_plan=plan.fingerprint).result


def test_analysis_results_keep_frozen_status_facts_and_separate_history(tmp_path: Path) -> None:
    first_export = generate_export("lag-signal-v1", 42, tmp_path / "first")
    second_export = generate_export("lag-signal-v1", 43, tmp_path / "second")
    config = RuntimeConfig(
        DataMode.SYNTHETIC,
        tmp_path / "store",
        tmp_path / "real",
    )
    request = RunRestingHeartRateAnalysis(AnalysisDefinitionId("lag-signal-v2"))
    short_request = RunRestingHeartRateAnalysis(
        AnalysisDefinitionId("lag-signal-v2"), end_date=date(2024, 6, 1)
    )

    with HealthLab.open(config) as health_lab:
        _execute(health_lab, ImportHealthExport(first_export.export_path))
        assert health_lab.load_data_review(DataReviewSelection()).cases
        receipt = _execute(health_lab, request)
        assert isinstance(receipt, AnalysisReceipt)
        assert receipt.status is AnalysisStatus.COMPLETED
        assert receipt.model_maturity is ModelMaturityStatus.ROBUST
        current = health_lab.load_overview(OverviewSelection()).resting_hr_analysis
        assert current is not None
        assert current.freshness is AnalysisFreshness.CURRENT
        assert current.data_status is DataQualityStatus.PROVISIONAL
        assert current.data_status_reasons
        assert current.maturity_criteria
        assert next(
            criterion
            for criterion in current.maturity_criteria
            if criterion.code.value == "time_series_continuity"
        ).passed
        expected_reproducibility = (
            ReproducibilityStatus.LOCAL_DEVELOPMENT
            if current.provenance.code_dirty
            else ReproducibilityStatus.REPRODUCIBLE
        )
        assert current.reproducibility is expected_reproducibility
        frozen_status = (
            current.data_status,
            current.data_status_reasons,
            current.model_maturity,
            current.maturity_criteria,
        )
        provisional_exploratory = _execute(health_lab, short_request)
        assert isinstance(provisional_exploratory, AnalysisReceipt)
        projected = health_lab.load_overview(
            OverviewSelection(end_date=short_request.end_date)
        ).resting_hr_analysis
        assert projected is not None
        assert projected.data_status is DataQualityStatus.PROVISIONAL
        assert projected.model_maturity is ModelMaturityStatus.EXPLORATORY

        _execute(health_lab, ConfirmDataReviewBatch(DataReviewSelection(), "geprüft"))
        reviewed_exploratory = _execute(health_lab, short_request)
        assert isinstance(reviewed_exploratory, AnalysisReceipt)
        projected = health_lab.load_overview(
            OverviewSelection(end_date=short_request.end_date)
        ).resting_hr_analysis
        assert projected is not None
        assert projected.data_status is DataQualityStatus.REVIEWED
        assert projected.model_maturity is ModelMaturityStatus.EXPLORATORY
        reviewed_receipt = _execute(health_lab, request)
        assert isinstance(reviewed_receipt, AnalysisReceipt)
        reviewed = health_lab.load_overview(OverviewSelection()).resting_hr_analysis
        assert reviewed is not None
        assert reviewed.data_status is DataQualityStatus.REVIEWED
        assert reviewed.model_maturity is ModelMaturityStatus.ROBUST

        _execute(health_lab, ImportHealthExport(second_export.export_path))
        stale_overview = health_lab.load_overview(OverviewSelection())

    assert stale_overview.resting_hr_analysis is None
    assert stale_overview.analysis_history
    assert stale_overview.last_reviewed_analysis == stale_overview.analysis_history[0]
    historical = next(
        item for item in stale_overview.analysis_history if item.provenance == current.provenance
    )
    assert historical.freshness is AnalysisFreshness.STALE
    assert (
        historical.data_status,
        historical.data_status_reasons,
        historical.model_maturity,
        historical.maturity_criteria,
    ) == frozen_status
    assert historical.provenance == current.provenance
    assert historical.completed_at is not None


def test_incomplete_analysis_creates_no_result_or_maturity(tmp_path: Path) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")

    with HealthLab.open(config) as health_lab:
        receipt = _execute(
            health_lab,
            RunRestingHeartRateAnalysis(AnalysisDefinitionId("lag-signal-v2")),
        )
        overview = health_lab.load_overview(OverviewSelection())

    assert isinstance(receipt, AnalysisReceipt)
    assert receipt.status is AnalysisStatus.INSUFFICIENT_DATA
    assert receipt.result_ref is None
    assert receipt.model_maturity is None
    assert overview.resting_hr_analysis is None
    assert not overview.analysis_history


def test_passive_coverage_gaps_only_mark_results_that_use_them(tmp_path: Path) -> None:
    fixture = generate_export(
        "lag-signal-v1",
        42,
        tmp_path / "fixture",
        options=GenerationOptions(missing_active_energy_probability=0.05),
    )
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")

    with HealthLab.open(config) as health_lab:
        _execute(health_lab, ImportHealthExport(fixture.export_path))
        receipt = _execute(
            health_lab,
            RunRestingHeartRateAnalysis(AnalysisDefinitionId("lag-signal-v2")),
        )
        result = health_lab.load_overview(OverviewSelection()).resting_hr_analysis

    assert isinstance(receipt, AnalysisReceipt)
    assert receipt.status is AnalysisStatus.COMPLETED
    assert result is not None
    coverage = next(
        reason
        for reason in result.data_status_reasons
        if reason.code.value == "passive_coverage_gap"
    )
    assert coverage.evidence_ids
    assert all(evidence.startswith("active_energy:") for evidence in coverage.evidence_ids)
