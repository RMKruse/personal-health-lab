import fcntl
from datetime import date
from pathlib import Path
from typing import get_args
from zipfile import ZipFile

import pytest

from personal_health_lab.application import (
    AnalysisDefinitionId,
    ConfigurationError,
    DataMode,
    HealthLab,
    ImportHealthExport,
    ImportReceipt,
    OverviewSelection,
    RunAnalysis,
    RunAnalysisPlan,
    RunRestingHeartRateAnalysis,
    RuntimeConfig,
    WriteApprovalStatus,
    WriteNotStarted,
    WriteNotStartedStatus,
    WriteRequest,
)


def _package(path: Path, *, day: int, value: int = 1) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "apple_health_export/export.xml",
            f'''<HealthData><ExportDate value="2024-01-{day:02d} 12:00:00 +0100"/>
            <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="{value}"
            sourceName="Apple Watch" sourceVersion="1" device="Apple Watch"
            creationDate="2024-01-{day:02d} 12:00:00 +0100"
            startDate="2024-01-{day:02d} 12:00:00 +0100"
            endDate="2024-01-{day:02d} 12:01:00 +0100"/></HealthData>''',
        )
    return path


def _import(health_lab: HealthLab, package: Path) -> None:
    request = ImportHealthExport(package)
    receipt = health_lab.execute_write(
        request, expected_plan=health_lab.preview_write(request).fingerprint
    )
    assert isinstance(receipt.result, ImportReceipt)


def _request(*, start_date: date | None = None, end_date: date | None = None) -> RunAnalysis:
    return RunAnalysis(
        AnalysisDefinitionId("rhr-activity-lag-1-7-v1"),
        start_date=start_date,
        end_date=end_date,
    )


def test_run_analysis_is_the_only_canonical_analysis_request_and_validates_inputs() -> None:
    requests = set(get_args(WriteRequest))

    assert RunAnalysis in requests
    assert RunRestingHeartRateAnalysis not in requests
    with pytest.raises(ConfigurationError):
        RunAnalysis(AnalysisDefinitionId("unknown-definition"))
    with pytest.raises(ConfigurationError):
        RunAnalysis(AnalysisDefinitionId("rhr-activity-lag-1-7-v1"), schema_version="2.0")
    with pytest.raises(ConfigurationError):
        _request(start_date=date(2024, 1, 2), end_date=date(2024, 1, 1))
    with pytest.raises(ConfigurationError):
        RunAnalysis(
            AnalysisDefinitionId("rhr-activity-lag-1-7-v1"),
            start_date="2024-01-01",  # type: ignore[arg-type]
        )


def test_run_analysis_preview_binds_definition_snapshot_and_inclusive_eligible_range(
    tmp_path: Path,
) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    request = _request(start_date=date(2024, 1, 1), end_date=date(2024, 1, 3))

    with HealthLab.open(runtime) as health_lab:
        _import(health_lab, _package(tmp_path / "snapshot.zip", day=2))
        plan = health_lab.preview_write(request)

    assert isinstance(plan.details, RunAnalysisPlan)
    assert plan.details.analysis_definition.analysis_definition_id == request.analysis_definition_id
    assert plan.details.analysis_definition.definition_version == 1
    assert plan.details.base_snapshot_ref is not None
    assert plan.details.requested_start_date == date(2024, 1, 1)
    assert plan.details.requested_end_date == date(2024, 1, 3)
    assert plan.details.eligible_start_date == date(2024, 1, 2)
    assert plan.details.eligible_end_date == date(2024, 1, 2)
    assert plan.details.schema_version == "1.0"
    assert plan.details.reuse_candidate is None
    assert plan.approval.status is WriteApprovalStatus.READY


def test_run_analysis_nonstart_paths_never_create_a_model_run(tmp_path: Path) -> None:
    runtime = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    request = _request()

    with HealthLab.open(runtime) as health_lab:
        blocked_plan = health_lab.preview_write(request)
        blocked = health_lab.execute_write(request, expected_plan=blocked_plan.fingerprint).result
        _import(health_lab, _package(tmp_path / "first.zip", day=2))
        ready_plan = health_lab.preview_write(request)
        with (runtime.active_store / ".writer.lock").open("a+b") as writer_lock:
            fcntl.flock(writer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = health_lab.execute_write(request, expected_plan=ready_plan.fingerprint).result
        _import(health_lab, _package(tmp_path / "second.zip", day=3, value=2))
        changed = health_lab.execute_write(request, expected_plan=ready_plan.fingerprint).result
        current_plan = health_lab.preview_write(request)
        pending = health_lab.execute_write(request, expected_plan=current_plan.fingerprint).result
        overview = health_lab.load_overview(OverviewSelection())

    assert isinstance(blocked, WriteNotStarted)
    assert blocked.status is WriteNotStartedStatus.BLOCKED
    assert blocked.diagnostics == ("no_snapshot",)
    assert isinstance(busy, WriteNotStarted)
    assert busy.status is WriteNotStartedStatus.STORE_BUSY
    assert isinstance(changed, WriteNotStarted)
    assert changed.status is WriteNotStartedStatus.PLAN_CHANGED
    assert isinstance(pending, WriteNotStarted)
    assert pending.status is WriteNotStartedStatus.BLOCKED
    assert pending.diagnostics == ("analysis_start_not_available",)
    assert overview.analysis_history == ()
