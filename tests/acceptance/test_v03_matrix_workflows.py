import fcntl
from datetime import date, datetime
from pathlib import Path

import pytest
import test_v03_context as context

from personal_health_lab.application import (
    ContextCoverageStartCreate,
    DataMode,
    HealthLab,
    ImportHealthExport,
    IntakeReasonCategoryCreate,
    MedicationRegimeCreate,
    ReviseContextCoverageStart,
    ReviseIntakeReasonCategory,
    ReviseMedicationRegime,
    RuntimeConfig,
    WriteApprovalStatus,
    WriteNotStarted,
    WriteNotStartedStatus,
)


def _assert_manual_write_plan_changed_and_busy(tmp_path: Path, *, context_write: bool) -> None:
    config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / "store", tmp_path / "real")
    with HealthLab.open(config) as health_lab:
        imported = ImportHealthExport(context._package(tmp_path / "export.zip"))
        health_lab.execute_write(
            imported, expected_plan=health_lab.preview_write(imported).fingerprint
        )
        request = (
            ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 1)))
            if context_write
            else ReviseMedicationRegime(
                MedicationRegimeCreate(
                    datetime.fromisoformat("2024-01-01T00:00:00+01:00"),
                    "Europe/Berlin",
                    (),
                    (),
                )
            )
        )
        stale_plan = health_lab.preview_write(request)
        competing = (
            ReviseIntakeReasonCategory(IntakeReasonCategoryCreate("Schmerz"))
            if context_write
            else ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 1)))
        )
        health_lab.execute_write(
            competing, expected_plan=health_lab.preview_write(competing).fingerprint
        )
        changed = health_lab.execute_write(request, expected_plan=stale_plan.fingerprint)
        assert isinstance(changed.result, WriteNotStarted)
        assert changed.result.status is WriteNotStartedStatus.PLAN_CHANGED
        current_plan = health_lab.preview_write(request)
        with (config.active_store / ".writer.lock").open("a+b") as writer_lock:
            fcntl.flock(writer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = health_lab.execute_write(request, expected_plan=current_plan.fingerprint)
        assert isinstance(busy.result, WriteNotStarted)
        assert busy.result.status is WriteNotStartedStatus.STORE_BUSY


@pytest.mark.parametrize("case", ("NO-SNAPSHOT", "ATOMIC-FAULT"))
def test_context_matrix_contract(case: str, tmp_path: Path) -> None:
    if case == "NO-SNAPSHOT":
        config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / case, tmp_path / "real")
        with HealthLab.open(config) as health_lab:
            plan = health_lab.preview_write(
                ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 1)))
            )
        assert plan.approval.status is WriteApprovalStatus.BLOCKED
    else:
        _assert_manual_write_plan_changed_and_busy(
            tmp_path / f"{case}-concurrency", context_write=True
        )


@pytest.mark.parametrize("case", ("NO-SNAPSHOT", "ATOMIC-FAULT"))
def test_medication_matrix_contract(case: str, tmp_path: Path) -> None:
    if case == "NO-SNAPSHOT":
        config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / case, tmp_path / "real")
        request = ReviseMedicationRegime(
            MedicationRegimeCreate(
                datetime.fromisoformat("2024-01-01T00:00:00+01:00"),
                "Europe/Berlin",
                (),
                (),
            )
        )
        with HealthLab.open(config) as health_lab:
            assert health_lab.preview_write(request).approval.status is WriteApprovalStatus.BLOCKED
    else:
        _assert_manual_write_plan_changed_and_busy(
            tmp_path / f"{case}-concurrency", context_write=False
        )
