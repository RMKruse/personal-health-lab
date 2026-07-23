import json
import sqlite3
from pathlib import Path

import duckdb

from personal_health_lab import DataMode
from personal_health_lab.application import (
    AnalysisDefinitionId,
    AnalysisFreshness,
    AnalysisReceipt,
    HealthLab,
    ImportHealthExport,
    OverviewSelection,
    ReproducibilityStatus,
    RunRestingHeartRateAnalysis,
    RuntimeConfig,
)
from personal_health_lab.synthetic_export import generate_export


def _execute(health_lab: HealthLab, request):
    plan = health_lab.preview_write(request)
    return health_lab.execute_write(request, expected_plan=plan.fingerprint).result


def test_reactivating_an_immutable_snapshot_rederives_current_freshness(tmp_path: Path) -> None:
    first = generate_export("lag-signal-v1", 42, tmp_path / "first")
    second = generate_export("lag-signal-v1", 43, tmp_path / "second")
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    request = RunRestingHeartRateAnalysis(AnalysisDefinitionId("lag-signal-v2"))

    with HealthLab.open(config) as health_lab:
        _execute(health_lab, ImportHealthExport(first.export_path))
        receipt = _execute(health_lab, request)
        assert isinstance(receipt, AnalysisReceipt)
        original = health_lab.load_overview(OverviewSelection()).resting_hr_analysis
        assert original is not None
        _execute(health_lab, ImportHealthExport(second.export_path))

    # Internal activation setup: the public rollback operation is delivered by its own slice.
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        metadata.execute(
            "UPDATE active_snapshot SET snapshot_id = ? WHERE singleton = 1",
            (str(original.snapshot_id),),
        )

    with HealthLab.open(config) as health_lab:
        reactivated = health_lab.load_overview(OverviewSelection()).resting_hr_analysis

    assert reactivated is not None
    assert reactivated.freshness is AnalysisFreshness.CURRENT
    assert reactivated.data_status_reasons == original.data_status_reasons


def test_legacy_analysis_artifacts_remain_readable(tmp_path: Path) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")

    with HealthLab.open(config) as health_lab:
        _execute(health_lab, ImportHealthExport(fixture.export_path))
        _execute(
            health_lab,
            RunRestingHeartRateAnalysis(AnalysisDefinitionId("lag-signal-v2")),
        )
        original = health_lab.load_overview(OverviewSelection()).resting_hr_analysis

    assert original is not None
    assert original.provenance is not None
    artifact = (
        config.active_store
        / "parquet"
        / "analyses"
        / str(original.provenance.result_id)
        / "result.parquet"
    )
    replacement = artifact.with_suffix(".legacy.parquet")
    with duckdb.connect() as query:
        query.execute("CREATE TABLE legacy AS SELECT * FROM read_parquet(?)", [str(artifact)])
        diagnostics, methodology = query.execute(
            "SELECT diagnostics, methodology FROM legacy LIMIT 1"
        ).fetchone()
        legacy_diagnostics = json.loads(diagnostics)
        legacy_methodology = json.loads(methodology)
        for key in (
            "input_completeness",
            "outcome_standard_deviation",
            "maximum_time_series_gap_days",
        ):
            legacy_diagnostics.pop(key)
        for key in (
            "minimum_input_completeness",
            "max_feature_dependency",
            "minimum_bootstrap_success_rate",
            "minimum_outcome_standard_deviation",
            "maximum_time_series_gap_days",
        ):
            legacy_methodology.pop(key)
        query.execute(
            "UPDATE legacy SET diagnostics = ?, methodology = ?",
            [json.dumps(legacy_diagnostics), json.dumps(legacy_methodology)],
        )
        query.execute("COPY legacy TO ? (FORMAT PARQUET)", [str(replacement)])
    replacement.replace(artifact)
    with sqlite3.connect(config.active_store / "metadata.sqlite3") as metadata:
        metadata.execute(
            "UPDATE analysis_runs SET code_dirty = 1, reproducibility = 'not_recorded', "
            "data_status = 'reviewed', maturity_criteria = '[]'"
        )

    with HealthLab.open(config) as health_lab:
        loaded = health_lab.load_overview(OverviewSelection()).resting_hr_analysis

    assert loaded is not None
    assert loaded.provenance is not None and loaded.provenance.code_dirty
    assert loaded.reproducibility is ReproducibilityStatus.LOCAL_DEVELOPMENT
    assert not loaded.status_facts_recorded
    assert loaded.diagnostics.input_completeness is None
    assert loaded.methodology.minimum_input_completeness is None
