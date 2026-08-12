import fcntl
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import pytest
import test_v03_context as context
import test_v03_medication as medication

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

_coverage = context.test_context_coverage_start_publishes_an_immutable_snapshot_and_baseline
_stress = context.test_daily_stress_and_custom_contexts_share_the_revision_snapshot_contract
_illness = context.test_illness_periods_project_active_categories_and_reject_same_category_overlap
_catalog_lifecycle = context.test_context_catalog_revision_withdrawal_and_restore_are_audited
_catalog_split = context.test_illness_and_custom_context_catalogs_are_explicitly_split
_open_period = context.test_open_context_period_is_bound_to_its_snapshot
_regime = medication.test_medication_regime_projects_dst_and_keeps_old_snapshot_stable
_deviation = medication.test_medication_deviation_binds_one_occurrence_and_keeps_old_snapshot_stable
_as_needed = medication.test_as_needed_intakes_and_reason_categories_are_snapshot_bound
_entry_ids = medication.test_as_needed_plan_entry_ids_must_be_explicitly_valid_and_unique
_binding = medication.test_import_carries_every_effective_manual_revision_binding_forward
_fault = medication.test_manual_snapshot_fault_keeps_revision_audit_and_activation_atomic
_empty_plan = medication.test_medication_days_distinguish_unknown_from_an_explicit_empty_plan
_all_deviations = (
    medication.test_medication_deviations_cover_omitted_time_amount_and_multiple_intakes
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

_CONTEXT_CASES: dict[str, Callable[..., None]] = {
    "NO-SNAPSHOT": _coverage,
    "COVERAGE-BASELINE": _coverage,
    "OBSERVED-STRESS": _stress,
    "ILLNESS-COLLISION": _illness,
    "OPEN-PERIOD": _open_period,
    "CATALOG-RENAME": _catalog_split,
    "WITHDRAW-RESTORE": _catalog_lifecycle,
    "CUSTOM-OVERLAP": _stress,
    "ATOMIC-FAULT": _fault,
}

_MEDICATION_CASES: dict[str, Callable[..., None]] = {
    "NO-SNAPSHOT": _regime,
    "EMPTY-PLAN": _empty_plan,
    "REGIME-REVISION": _regime,
    "DST": _regime,
    "DEVIATION": _all_deviations,
    "TIMEZONE": _deviation,
    "AS-NEEDED": _as_needed,
    "CATEGORY": _as_needed,
    "REFERENCES": _entry_ids,
    "WITHDRAWALS": _as_needed,
    "AS-OF": _binding,
    "ATOMIC-FAULT": _fault,
}


@pytest.mark.parametrize("case", _CONTEXT_CASES, ids=_CONTEXT_CASES)
def test_context_matrix_contract(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    test = _CONTEXT_CASES[case]
    if case == "NO-SNAPSHOT":
        config = RuntimeConfig(DataMode.SYNTHETIC, tmp_path / case, tmp_path / "real")
        with HealthLab.open(config) as health_lab:
            plan = health_lab.preview_write(
                ReviseContextCoverageStart(ContextCoverageStartCreate(date(2024, 1, 1)))
            )
        assert plan.approval.status is WriteApprovalStatus.BLOCKED
    elif test is _fault:
        _assert_manual_write_plan_changed_and_busy(
            tmp_path / f"{case}-concurrency", context_write=True
        )
        test(tmp_path / case, monkeypatch)
    else:
        test(tmp_path / case)


@pytest.mark.parametrize("case", _MEDICATION_CASES, ids=_MEDICATION_CASES)
def test_medication_matrix_contract(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    test = _MEDICATION_CASES[case]
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
    elif test is _fault:
        _assert_manual_write_plan_changed_and_busy(
            tmp_path / f"{case}-concurrency", context_write=False
        )
        test(tmp_path / case, monkeypatch)
    elif test is _entry_ids:
        test()
    else:
        test(tmp_path / case)
