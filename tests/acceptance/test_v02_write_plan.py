from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from personal_health_lab.application import (
    CapacityCheck,
    CapacityReason,
    CapacityStatus,
    DataMode,
    FileVaultCheck,
    FileVaultReason,
    FileVaultStatus,
    HealthLab,
    HealthLabError,
    ImportHealthExport,
    ImportReceipt,
    ImportStatus,
    OverviewSelection,
    PersonBindingStatus,
    PlanFingerprint,
    RuntimeConfig,
    WriteApprovalStatus,
    WriteNotStarted,
    WriteNotStartedStatus,
)
from personal_health_lab.storage import (
    LocalStore,
    StoreError,
    full_snapshot_import_estimate,
    probe_capacity,
)

MATRIX_PATH = Path(__file__).with_name("v02_matrix.toml")
REGISTERED_CASES = {
    case_id: "test_v02_import_write_contract"
    for case_id in (
        "V02-A-API-004",
        "V02-A-API-005",
        "V02-A-API-006",
        "V02-A-API-007",
        "V02-A-API-010",
        "V02-A-API-011",
        "V02-A-API-012",
        "V02-A-API-013",
        "V02-A-API-014",
    )
}
REGISTERED_CASES.update(
    {
        "V02-A-RHR-001": (
            "test_analysis_preview_is_read_only_and_execution_rechecks_the_request"
        ),
        "V02-A-RHR-002": "test_signal_scenario_runs_as_a_pinned_deterministic_lag_analysis",
        "V02-A-RHR-003": "test_analysis_rechecks_the_snapshot_under_the_writer_lock",
        "V02-A-RHR-004": "test_analysis_store_busy_is_write_not_started",
        "V02-A-RHR-005": (
            "test_analysis_reports_insufficient_and_unstable_inputs_with_stable_diagnostics"
        ),
        "V02-A-RHR-006": "test_cli_runs_and_exposes_the_built_in_lag_analysis",
        "V02-A-RHR-007": "test_cli_runs_and_exposes_the_built_in_lag_analysis",
        "V02-A-RHR-008": "test_streamlit_shows_imported_daily_series",
        "V02-A-RHR-009": "test_streamlit_shows_the_same_empty_overview",
        "V02-A-RHR-010": "test_streamlit_maps_unstable_analysis",
        "V02-A-RHR-011": "test_streamlit_maps_store_busy_analysis",
        "V02-A-RHR-012": "test_streamlit_maps_plan_changed_analysis",
        "V02-A-RHR-013": "test_json_cli_analysis_reports_plan_changed",
        "V02-A-PRE-001": "test_new_store_has_stable_identity_and_immutable_mode",
        "V02-A-PRE-002": "test_complete_store_copy_retains_the_same_identity",
        "V02-A-PRE-003": (
            "test_legacy_real_store_gets_identity_only_after_confirmed_execution"
        ),
        "V02-A-PRE-004": "test_new_store_has_stable_identity_and_immutable_mode",
        "V02-A-PRE-005": "test_real_import_uses_one_typed_confirmation_plan",
        "V02-A-PRE-006": "test_real_store_binds_only_after_a_decided_personal_import",
        "V02-A-PRE-007": "test_quarantined_real_import_binds_the_store",
        "V02-A-PRE-008": "test_pending_person_binding_is_visible_in_workspace_status",
        "V02-A-PRE-009": "test_real_import_uses_one_typed_confirmation_plan",
        "V02-A-PRE-010": "test_real_import_uses_one_typed_confirmation_plan",
        "V02-A-PRE-011": "test_synthetic_import_skips_filevault_probe",
        "V02-A-PRE-012": "test_hardware_encryption_without_filevault_is_unprotected",
        "V02-A-PRE-013": "test_real_import_uses_one_typed_confirmation_plan",
        "V02-A-PRE-014": "test_filevault_is_rechecked_after_the_writer_lock",
        "V02-A-PRE-015": "test_filevault_improvement_to_protected_may_continue",
    }
)
REGISTERED_CASES.update(
    {
        "V02-A-MIG-001": "test_current_store_migration_is_a_no_op_without_backup",
        "V02-A-MIG-002": "test_registered_store_migration_chain_executes_as_one_operation",
        "V02-A-MIG-003": "test_registered_store_migration_chain_executes_as_one_operation",
        "V02-A-MIG-004": "test_unregistered_jump_or_downgrade_is_blocked",
        "V02-A-MIG-005": "test_migration_required_session_only_allows_diagnosis_and_migration",
        "V02-A-MIG-006": "test_abandoning_migration_plan_changes_nothing",
        "V02-A-MIG-007": "test_registered_store_migration_chain_executes_as_one_operation",
        "V02-A-MIG-008": "test_migration_uses_one_backup_and_one_writer_lock",
        "V02-A-MIG-009": "test_failed_backup_or_capacity_preflight_prevents_migration_start",
        "V02-A-MIG-010": "test_populated_store_is_migrated_copy_on_write",
        "V02-A-MIG-011": "test_migration_makes_existing_analysis_stale",
        "V02-A-MIG-012": "test_populated_store_is_migrated_copy_on_write",
        "V02-A-MIG-013": "test_migration_validation_failure_prevents_activation",
        "V02-A-MIG-014": "test_missing_cow_migration_bound_blocks_plan",
        "V02-A-MIG-015": "test_migration_fault_keeps_old_snapshot_and_retry_starts_fresh",
        "V02-A-MIG-016": "test_migration_fault_keeps_old_snapshot_and_retry_starts_fresh",
        "V02-A-MIG-017": "test_migration_fault_keeps_old_snapshot_and_retry_starts_fresh",
        "V02-A-MIG-018": "test_direct_migration_rollback_restores_backup_and_old_snapshot",
        "V02-A-MIG-019": "test_later_successful_state_change_blocks_migration_rollback",
        "V02-A-MIG-020": "test_direct_migration_rollback_restores_backup_and_old_snapshot",
        "V02-A-ADP-010": "test_streamlit_focuses_migration_in_restricted_session",
        "V02-A-PRE-025": "test_cow_migration_v1_bounds_normal_and_stress_fixtures",
    }
)
REGISTERED_CASES.update(
    {
        "V02-A-BAK-001": "test_metadata_backup_is_one_redacted_portable_sqlite_file",
        "V02-A-BAK-002": "test_metadata_backup_is_one_redacted_portable_sqlite_file",
        "V02-A-BAK-003": "test_metadata_backup_is_one_redacted_portable_sqlite_file",
        "V02-A-BAK-004": (
            "test_metadata_backup_hash_survives_vacuum_and_same_target_is_no_op"
        ),
        "V02-A-BAK-005": "test_metadata_backup_is_one_redacted_portable_sqlite_file",
        "V02-A-BAK-006": (
            "test_metadata_backup_hash_survives_vacuum_and_same_target_is_no_op"
        ),
        "V02-A-BAK-007": "test_metadata_backup_conflict_blocks_without_overwriting",
        "V02-A-BAK-008": "test_metadata_backup_is_real_only",
        "V02-A-BAK-009": "test_metadata_backup_capacity_is_rechecked_under_the_writer_lock",
        "V02-A-BAK-010": "test_cli_maps_metadata_backup_without_exposing_its_path",
        "V02-A-PRE-021": "test_metadata_backup_v1_measures_populated_writer_phases",
    }
)
REGISTERED_CASES.update(
    {
        "V02-A-RST-001": "test_valid_backup_begins_a_read_only_restore_pending_session",
        "V02-A-RST-002": "test_restore_rejects_synthetic_populated_and_conflicting_targets",
        "V02-A-RST-003": "test_restore_rejects_synthetic_populated_and_conflicting_targets",
        "V02-A-RST-004": "test_valid_backup_begins_a_read_only_restore_pending_session",
        "V02-A-RST-005": "test_valid_backup_begins_a_read_only_restore_pending_session",
        "V02-A-RST-006": "test_valid_backup_begins_a_read_only_restore_pending_session",
        "V02-A-RST-007": "test_restore_sources_match_exactly_and_activate_overlay_once",
        "V02-A-RST-008": "test_restore_sources_match_exactly_and_activate_overlay_once",
        "V02-A-RST-009": "test_restore_sources_match_exactly_and_activate_overlay_once",
        "V02-A-RST-010": "test_supported_backup_schema_is_migrated_only_in_staging",
        "V02-A-RST-011": "test_supported_backup_schema_is_migrated_only_in_staging",
        "V02-A-RST-012": "test_unknown_or_unregistered_backup_schema_is_blocked",
        "V02-A-RST-013": "test_restore_sources_match_exactly_and_activate_overlay_once",
        "V02-A-RST-014": (
            "test_restore_activation_fault_rolls_back_overlay_and_retries"
        ),
        "V02-A-RST-015": "test_restore_sources_match_exactly_and_activate_overlay_once",
        "V02-A-RST-016": (
            "test_abort_discards_pending_store_and_activated_backup_is_idempotent"
        ),
        "V02-A-RST-017": (
            "test_abort_discards_pending_store_and_activated_backup_is_idempotent"
        ),
        "V02-A-RST-018": (
            "test_restore_keeps_superseded_conflict_resolution_tombstoned"
        ),
        "V02-A-PRE-022": "test_restore_start_v1_bounds_normal_and_stress_fixtures",
        "V02-A-PRE-023": "test_restore_sources_match_exactly_and_activate_overlay_once",
        "V02-A-PRE-024": (
            "test_restore_activate_v1_bounds_snapshot_and_overlay_allocation"
        ),
        "V02-A-ADP-009": (
            "test_streamlit_completes_restore_through_the_shared_import_controls"
        ),
    }
)
REGISTERED_CASES.update(
    {
        "V02-A-RES-001": "test_analysis_results_keep_frozen_status_facts_and_separate_history",
        "V02-A-RES-002": "test_incomplete_analysis_creates_no_result_or_maturity",
        "V02-A-RES-003": "test_incomplete_analysis_creates_no_result_or_maturity",
        "V02-A-RES-004": "test_analysis_results_keep_frozen_status_facts_and_separate_history",
        "V02-A-RES-005": "test_reactivating_an_immutable_snapshot_rederives_current_freshness",
        "V02-A-RES-006": "test_analysis_results_keep_frozen_status_facts_and_separate_history",
        "V02-A-RES-007": "test_passive_coverage_gaps_only_mark_results_that_use_them",
        "V02-A-RES-008": "test_analysis_results_keep_frozen_status_facts_and_separate_history",
        "V02-A-RES-009": "test_analysis_results_keep_frozen_status_facts_and_separate_history",
        "V02-A-RES-010": "test_analysis_results_keep_frozen_status_facts_and_separate_history",
        "V02-A-RES-011": "test_analysis_results_keep_frozen_status_facts_and_separate_history",
        "V02-A-RES-012": "test_analysis_results_keep_frozen_status_facts_and_separate_history",
        "V02-A-RES-013": "test_analysis_results_keep_frozen_status_facts_and_separate_history",
        "V02-A-RES-014": "test_reuse_requires_every_reproduction_identity_to_match",
        "V02-A-RES-015": "test_analysis_results_keep_frozen_status_facts_and_separate_history",
        "V02-A-STO-013": "test_analysis_results_keep_frozen_status_facts_and_separate_history",
        "V02-A-STO-014": "test_incomplete_analysis_creates_no_result_or_maturity",
        "V02-A-ADP-008": "test_cli_and_streamlit_project_analysis_status_axes",
    }
)
REGISTERED_CASES.update(
    {
        "V02-A-RULE-001": "test_fixed_rules_are_inclusive_and_keep_flagged_source_values",
        "V02-A-RULE-002": "test_fixed_rules_are_inclusive_and_keep_flagged_source_values",
        "V02-A-RULE-003": (
            "test_personal_range_uses_previous_effective_days_and_keeps_earlier_findings"
        ),
        "V02-A-RULE-004": "test_personal_range_is_inclusive_and_skips_unready_inputs",
        "V02-A-RULE-005": "test_personal_range_is_inclusive_and_skips_unready_inputs",
        "V02-A-RULE-006": "test_personal_range_is_inclusive_and_skips_unready_inputs",
        "V02-A-RULE-007": (
            "test_personal_range_uses_previous_effective_days_and_keeps_earlier_findings"
        ),
        "V02-A-RULE-008": "test_personal_range_uses_corrections_instead_of_source_values",
        "V02-A-RULE-009": (
            "test_personal_range_uses_previous_effective_days_and_keeps_earlier_findings"
        ),
        **{
            f"V02-A-RULE-{number:03d}": (
                "test_plausibility_rule_versions_are_typed_immutable_and_time_bound"
            )
            for number in range(10, 16)
        },
        "V02-A-RULE-016": (
            "test_imports_use_the_stored_rule_timeline_without_backfilling_inactive_weeks"
        ),
        "V02-A-RULE-017": (
            "test_rule_change_reevaluates_only_measurements_since_its_local_week_boundary"
        ),
        "V02-A-RULE-018": (
            "test_deactivation_and_reactivation_leave_a_time_bound_gap"
        ),
        "V02-A-RULE-019": (
            "test_imports_use_the_stored_rule_timeline_without_backfilling_inactive_weeks"
        ),
        "V02-A-RULE-020": (
            "test_historical_review_pins_its_basis_and_reuses_only_identical_confirmations"
        ),
        "V02-A-RULE-021": "test_late_measurement_uses_the_rule_for_its_measurement_time",
        "V02-A-RULE-022": "test_fixed_rules_are_inclusive_and_keep_flagged_source_values",
        "V02-A-RULE-023": (
            "test_historical_review_pins_its_basis_and_reuses_only_identical_confirmations"
        ),
        "V02-A-RULE-024": (
            "test_historical_review_pins_its_basis_and_reuses_only_identical_confirmations"
        ),
        "V02-A-RULE-025": "test_fixed_rules_are_inclusive_and_keep_flagged_source_values",
        "V02-A-RULE-026": "test_fixed_rules_are_inclusive_and_keep_flagged_source_values",
        "V02-A-RULE-027": "test_unknown_source_type_is_cataloged_and_requests_a_rule_once",
        "V02-A-RULE-028": "test_unknown_source_type_is_cataloged_and_requests_a_rule_once",
        "V02-A-REV-001": "test_clean_import_cycle_closes_immediately",
        "V02-A-REV-002": (
            "test_import_rule_change_and_historical_cycles_close_independently"
        ),
        "V02-A-REV-003": "test_clean_import_cycle_closes_immediately",
        "V02-A-REV-004": "test_fixed_rules_are_inclusive_and_keep_flagged_source_values",
        "V02-A-REV-005": "test_fixed_rules_are_inclusive_and_keep_flagged_source_values",
        "V02-A-REV-006": "test_fixed_rules_are_inclusive_and_keep_flagged_source_values",
        "V02-A-REV-007": (
            "test_new_flagged_source_version_requires_confirmation_after_local_exclusion"
        ),
        "V02-A-REV-008": "test_correction_without_a_case_can_be_revoked_to_the_clean_source",
        "V02-A-REV-009": (
            "test_new_flagged_source_version_requires_confirmation_after_local_exclusion"
        ),
        "V02-A-REV-010": "test_corrections_supersede_forward_and_revocation_uses_source",
        "V02-A-REV-011": "test_corrections_supersede_forward_and_revocation_uses_source",
        "V02-A-REV-012": "test_deletion_decisions_and_reappearance_are_forward_only",
        "V02-A-REV-013": (
            "test_new_clean_source_version_can_replace_a_continued_correction"
        ),
        "V02-A-REV-014": (
            "test_new_clean_source_version_can_replace_a_continued_correction"
        ),
        "V02-A-REV-015": (
            "test_new_flagged_source_version_requires_confirmation_after_local_exclusion"
        ),
        "V02-A-REV-016": (
            "test_new_flagged_source_version_requires_confirmation_after_local_exclusion"
        ),
        "V02-A-REV-017": (
            "test_batch_confirmation_materializes_every_value_and_revokes_only_effective_members"
        ),
        "V02-A-REV-018": (
            "test_large_batch_plan_remains_complete"
        ),
        "V02-A-REV-019": (
            "test_batch_confirmation_fails_atomically_when_the_match_set_changes"
        ),
        "V02-A-REV-020": (
            "test_batch_confirmation_materializes_every_value_and_revokes_only_effective_members"
        ),
        "V02-A-REV-021": (
            "test_batch_confirmation_materializes_every_value_and_revokes_only_effective_members"
        ),
        "V02-A-ADP-004": "test_json_cli_keeps_the_complete_batch_and_reports_plan_changed",
        "V02-A-ADP-011": "test_json_cli_keeps_the_complete_batch_and_reports_plan_changed",
        "V02-A-REV-022": "test_review_decision_requests_enforce_mandatory_reasons",
        "V02-A-REV-023": (
            "test_new_clean_source_version_can_replace_a_continued_correction"
        ),
        "V02-A-REV-024": (
            "test_confirmation_stales_but_never_rewrites_an_existing_analysis"
        ),
        "V02-A-REV-025": (
            "test_confirmation_stales_but_never_rewrites_an_existing_analysis"
        ),
        "V02-A-REV-026": (
            "test_confirmation_stales_but_never_rewrites_an_existing_analysis"
        ),
    }
)
REGISTERED_CASES.update(
    {
        **{
            f"V02-A-SRC-{number:03d}": (
                "test_package_occurrences_and_measurement_versions_remain_separate"
            )
            for number in range(1, 4)
        },
        **{
            f"V02-A-SRC-{number:03d}": (
                "test_only_the_newest_ordered_export_governs_source_versions"
            )
            for number in range(4, 7)
        },
        "V02-A-SRC-007": (
            "test_strong_and_natural_identity_create_only_payload_versions"
        ),
        "V02-A-SRC-008": (
            "test_strong_and_natural_identity_create_only_payload_versions"
        ),
        "V02-A-SRC-009": (
            "test_identity_collision_is_visible_in_the_public_data_review_projection"
        ),
        "V02-A-SRC-010": (
            "test_strong_and_natural_identity_create_only_payload_versions"
        ),
        **{
            f"V02-A-SRC-{number:03d}": (
                "test_comparable_exports_open_one_deletion_case_per_absence_phase"
            )
            for number in range(11, 14)
        },
        **{
            f"V02-A-SRC-{number:03d}": ("test_deletion_decisions_and_reappearance_are_forward_only")
            for number in range(14, 17)
        },
        **{
            f"V02-A-SRC-{number:03d}": "test_conflicts_can_be_preferred_split_and_revoked"
            for number in range(17, 20)
        },
        "V02-A-SRC-020": "test_a_newer_unambiguous_version_supersedes_conflict_preference",
        "V02-A-SRC-021": "test_deletion_decisions_and_reappearance_are_forward_only",
        "V02-A-STO-001": "test_import_publishes_one_validated_four_file_snapshot",
        "V02-A-STO-002": "test_sqlite_catalog_and_audit_constraints_are_hard",
        "V02-A-STO-003": "test_import_publishes_one_validated_four_file_snapshot",
        "V02-A-STO-004": "test_sqlite_catalog_and_audit_constraints_are_hard",
        "V02-A-STO-005": "test_sqlite_catalog_and_audit_constraints_are_hard",
        "V02-A-STO-006": "test_sqlite_catalog_and_audit_constraints_are_hard",
        "V02-A-STO-007": "test_import_publishes_one_validated_four_file_snapshot",
        "V02-A-STO-008": (
            "test_snapshot_validation_rejects_every_closed_contract_violation"
        ),
        "V02-A-STO-009": (
            "test_resolved_measurement_disposition_xor_accepts_closed_variants"
        ),
        "V02-A-STO-010": "test_import_publishes_one_validated_four_file_snapshot",
        "V02-A-STO-011": "test_manifest_rejects_nested_non_schema_fields",
        "V02-A-STO-012": "test_import_publishes_one_validated_four_file_snapshot",
        "V02-A-STO-015": "test_import_publishes_one_validated_four_file_snapshot",
        "V02-A-STO-016": (
            "test_publication_fault_keeps_old_snapshot_active_and_quarantines_remainder"
        ),
        "V02-A-STO-017": "test_import_publishes_one_validated_four_file_snapshot",
        "V02-A-STO-018": (
            "test_publication_fault_keeps_old_snapshot_active_and_quarantines_remainder"
        ),
        "V02-A-SRC-022": (
            "test_negative_exports_v1_are_rejected_without_changing_the_snapshot"
        ),
        "V02-A-SRC-023": "test_recognized_package_validation_failure_is_quarantined",
        "V02-A-SRC-024": "test_import_publishes_one_validated_four_file_snapshot",
    }
)
REGISTERED_CASES.update(
    {
        **{
            f"V02-A-PRE-{number:03d}": "test_import_capacity_public_plan_states"
            for number in range(16, 20)
        },
        "V02-A-PRE-020": "test_full_snapshot_import_v1_formula_contract",
        "V02-A-PRE-026": "test_full_snapshot_import_requires_writer_and_scratch_bounds",
        "V02-A-PRE-027": "test_full_snapshot_import_requires_writer_and_scratch_bounds",
        "V02-A-PRE-028": "test_full_snapshot_import_v1_formula_contract",
        "V02-A-PRE-029": "test_full_snapshot_import_v1_formula_contract",
        "V02-A-PRE-030": "test_full_snapshot_import_v1_measures_real_writer_phases",
        "V02-A-PRE-031": "test_import_capacity_uses_the_hard_exact_boundary",
        "V02-A-PRE-032": "test_enospc_after_positive_preflight_keeps_the_active_snapshot",
        "V02-A-PRE-033": "test_unwritable_or_locked_import_target_is_blocked",
        "V02-A-PRE-034": "test_competing_import_writer_keeps_readers_available",
    }
)


def _matrix() -> dict[str, object]:
    with MATRIX_PATH.open("rb") as source:
        return tomllib.load(source)


def _cases_for(runner: str) -> list[dict[str, object]]:
    return [case for case in _matrix()["case"] if case["runner"] == runner]


def _validate_matrix(matrix: dict[str, object]) -> None:
    assert matrix["schema_version"] == 1
    assert matrix["release"] == "v0.2"
    sections = {
        name: matrix.get(name, []) for name in ("contract", "fixture", "fault_point", "case")
    }
    all_ids: list[str] = []
    for rows in sections.values():
        assert isinstance(rows, list)
        all_ids.extend(row["id"] for row in rows)
    assert len(all_ids) == len(set(all_ids)), "duplicate V0.2 matrix ID"

    contract_ids = {row["id"] for row in sections["contract"]}
    fixture_ids = {row["id"] for row in sections["fixture"]}
    fault_point_ids = {row["id"] for row in sections["fault_point"]}
    registered_cases = {row["id"]: row["runner"] for row in sections["case"]}
    assert all(
        len(fixture["sha256"]) == 64 and set(fixture["sha256"]) <= set("0123456789abcdef")
        for fixture in sections["fixture"]
    )
    inline_fixture_prefixes = (
        "V02-F-SYS-CAPACITY",
        "V02-F-SYS-ALLOC",
        "V02-F-SYS-ENOSPC",
        "V02-F-SYS-UNWRITABLE",
    )
    for fixture in sections["fixture"]:
        if fixture["id"].startswith(inline_fixture_prefixes):
            assert hashlib.sha256(fixture["parameters"].encode()).hexdigest() == fixture["sha256"]
    assert registered_cases == REGISTERED_CASES, "unregistered or unclaimed V0.2 case"
    for case in sections["case"]:
        assert set(case["contracts"]) <= contract_ids, f"invalid contract reference in {case['id']}"
        assert set(case["fixtures"]) <= fixture_ids, f"invalid fixture reference in {case['id']}"
        assert set(case.get("fault_points", ())) <= fault_point_ids, (
            f"invalid fault-point reference in {case['id']}"
        )


def _package(
    path: Path, value: int = 60, *, record_count: int = 1, input_bytes: int | None = None
) -> Path:
    if record_count == 1 and input_bytes is None:
        records = [
            '<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
            'sourceName="Test Watch" sourceVersion="1" device="Test Device" '
            'unit="count/min" creationDate="2024-01-01 07:01:00 +0100" '
            'startDate="2024-01-01 07:00:00 +0100" '
            f'endDate="2024-01-01 07:01:00 +0100" value="{value}"/>'
        ]
    else:
        records = []
    start = datetime(2024, 1, 1, 7, tzinfo=UTC)
    for index in range(len(records), record_count):
        record_start = start + timedelta(minutes=index)
        record_end = record_start + timedelta(minutes=1)
        records.append(
            '<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
            'sourceName="Test Watch" sourceVersion="1" device="Test Device" '
            'unit="count/min" '
            f'creationDate="{record_end:%Y-%m-%d %H:%M:%S} +0000" '
            f'startDate="{record_start:%Y-%m-%d %H:%M:%S} +0000" '
            f'endDate="{record_end:%Y-%m-%d %H:%M:%S} +0000" '
            f'value="{value + index % 10}"/>'
        )
    xml = f'<?xml version="1.0"?><HealthData>{"".join(records)}</HealthData>'
    if input_bytes is not None:
        padding = input_bytes - len(xml.encode())
        if padding < 7:
            raise ValueError("input_bytes is too small for the generated fixture")
        payload = hashlib.shake_256(str(path).encode()).hexdigest((padding + 1) // 2)[
            : padding - 7
        ]
        xml = xml.replace("</HealthData>", f"<!--{payload}--></HealthData>")
        assert len(xml.encode()) == input_bytes
    export = ZipInfo("apple_health_export/export.xml", date_time=(1980, 1, 1, 0, 0, 0))
    export.compress_type = ZIP_DEFLATED
    with ZipFile(path, "w") as archive:
        archive.writestr(export, xml)
    return path


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        mode=DataMode.SYNTHETIC,
        synthetic_store=tmp_path / "synthetic-store",
        real_store=tmp_path / "real-store",
    )


def _store_files(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_v02_matrix_is_well_formed_and_fully_registered() -> None:
    _validate_matrix(_matrix())


def test_import_plan_includes_full_snapshot_capacity(tmp_path: Path) -> None:
    config = _config(tmp_path)
    request = ImportHealthExport(_package(tmp_path / "health.zip"))

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)

    assert plan.preflight.capacity.method_id == "full-snapshot-import/v1"
    assert plan.preflight.capacity.estimate_bytes > 0
    assert plan.preflight.capacity.required_bytes > plan.preflight.capacity.estimate_bytes
    assert plan.preflight.capacity.target_volume.startswith("volume-")


def test_import_capacity_uses_the_hard_exact_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    estimate = full_snapshot_import_estimate(776_061, 294_403, 4096)
    assert estimate is not None
    required = estimate + max((estimate + 3) // 4, 256 * 1024**2) + 1024**3
    available = required
    monkeypatch.setattr(os, "access", lambda path, mode: True)
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda path: SimpleNamespace(
            f_bavail=available,
            f_frsize=1,
            f_flag=0,
        ),
    )

    exact = probe_capacity(tmp_path, estimate)
    available -= 1
    short = probe_capacity(tmp_path, estimate)
    unknown = probe_capacity(tmp_path, None)

    assert exact.status is CapacityStatus.READY
    assert exact.required_bytes == required
    assert short.status is CapacityStatus.INSUFFICIENT
    assert unknown.status is CapacityStatus.UNKNOWN
    assert unknown.reason is CapacityReason.ESTIMATE_UNKNOWN


def test_import_capacity_public_plan_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    request = ImportHealthExport(_package(tmp_path / "health.zip"))
    available = 0

    def controlled_statvfs(path: Path) -> SimpleNamespace:
        return SimpleNamespace(f_bavail=available, f_frsize=1, f_flag=0)

    with HealthLab.open(config) as health_lab:
        initial = health_lab.preview_write(request)
        estimate = full_snapshot_import_estimate(
            initial.details.input_bytes,
            0,
            1,
            record_count=initial.details.record_count,
        )
        assert estimate is not None
        required = estimate + max((estimate + 3) // 4, 256 * 1024**2) + 1024**3
        available = required
        monkeypatch.setattr(os, "access", lambda path, mode: True)
        monkeypatch.setattr(os, "statvfs", controlled_statvfs)

        exact = health_lab.preview_write(request)
        available -= 1
        short = health_lab.preview_write(request)
        monkeypatch.setattr(os, "statvfs", lambda path: (_ for _ in ()).throw(OSError()))
        unknown_space = health_lab.preview_write(request)
        monkeypatch.setattr(os, "statvfs", controlled_statvfs)
        monkeypatch.setattr(
            "personal_health_lab.storage._store.full_snapshot_import_estimate",
            lambda *args, **kwargs: None,
        )
        unknown_estimate = health_lab.preview_write(request)

    assert exact.approval.status is WriteApprovalStatus.READY
    assert short.approval.status is WriteApprovalStatus.BLOCKED
    assert unknown_space.approval.status is WriteApprovalStatus.BLOCKED
    assert unknown_space.preflight.capacity.reason is CapacityReason.SPACE_UNKNOWN
    assert unknown_estimate.approval.status is WriteApprovalStatus.BLOCKED
    assert unknown_estimate.preflight.capacity.reason is CapacityReason.ESTIMATE_UNKNOWN


def test_full_snapshot_import_v1_formula_contract(tmp_path: Path) -> None:
    normal = full_snapshot_import_estimate(776_061, 294_403, 4096, record_count=730)
    stress = full_snapshot_import_estimate(
        776_061 * 8, 294_403 * 8, 4096, record_count=730 * 8
    )

    assert normal == 6_934_528
    assert stress == 54_968_320
    assert full_snapshot_import_estimate(1, 0, 4096, writer_bound=False) is None
    assert full_snapshot_import_estimate(1, 0, 4096, scratch_bound=False) is None

    config = _config(tmp_path)
    request = ImportHealthExport(_package(tmp_path / "method.zip"))
    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
    assert plan.preflight.capacity.method_id == "full-snapshot-import/v1"


def test_full_snapshot_import_requires_writer_and_scratch_bounds(
    tmp_path: Path,
) -> None:
    store = LocalStore.open_writer(tmp_path / "store", DataMode.SYNTHETIC)
    try:
        temporary = store._query.execute(
            "SELECT current_setting('temp_directory')"
        ).fetchone()
        bound = store.preflight_full_snapshot_import(1024, 1)
        store._scratch_bound = False
        unbound_scratch = store.preflight_full_snapshot_import(1024, 1)
        unbound_writer = store.preflight_full_snapshot_import(1024, 1, writer_bound=False)
    finally:
        store.close()

    assert bound.method_id == "full-snapshot-import/v1"
    assert temporary == (str(tmp_path / "store" / ".duckdb-temp"),)
    assert bound.estimate_bytes is not None
    assert unbound_scratch.status is CapacityStatus.UNKNOWN
    assert unbound_scratch.reason is CapacityReason.ESTIMATE_UNKNOWN
    assert unbound_writer.status is CapacityStatus.UNKNOWN
    assert unbound_writer.reason is CapacityReason.ESTIMATE_UNKNOWN


def _allocated_tree(root: Path) -> int:
    return sum(
        path.stat().st_blocks * 512
        for path in (root, *root.rglob("*"))
        if path.exists()
    )


@pytest.mark.parametrize("factor", (1, 8), ids=("normal", "stress"))
def test_full_snapshot_import_v1_measures_real_writer_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, factor: int
) -> None:
    config = _config(tmp_path)
    base = _package(
        tmp_path / "base.zip",
        record_count=128 * factor,
        input_bytes=294_403 * factor,
    )
    candidate = _package(
        tmp_path / "candidate.zip",
        70,
        record_count=256 * factor,
        input_bytes=776_061 * factor,
    )

    with HealthLab.open(config) as health_lab:
        base_plan = health_lab.preview_write(ImportHealthExport(base))
        base_receipt = health_lab.execute_write(
            ImportHealthExport(base), expected_plan=base_plan.fingerprint
        )
        assert isinstance(base_receipt.result, ImportReceipt)

        request = ImportHealthExport(candidate)
        plan = health_lab.preview_write(request)
        baseline = _allocated_tree(config.active_store)
        phases: dict[str, int] = {}

        def measure(root: Path, phase: str) -> None:
            phases[phase] = max(0, _allocated_tree(root) - baseline)

        monkeypatch.setattr(
            "personal_health_lab.storage._store._allocation_checkpoint", measure
        )
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(receipt.result, ImportReceipt)
    assert receipt.result.status is ImportStatus.COMMITTED
    assert set(phases) == {"combined", "staged", "activated"}
    assert max(phases.values()) <= plan.preflight.capacity.estimate_bytes


def test_import_capacity_is_rechecked_under_the_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = 0

    def preflight(
        store: LocalStore, input_bytes: int, record_count: int = 0
    ) -> CapacityCheck:
        nonlocal checks
        checks += 1
        ready = checks < 3
        return CapacityCheck(
            CapacityStatus.READY if ready else CapacityStatus.INSUFFICIENT,
            "volume-test",
            "full-snapshot-import/v1",
            1,
            256 * 1024**2,
            1024**3,
            1024**3 + 256 * 1024**2 + (1 if ready else 0),
            1024**3 + 256 * 1024**2 + 1,
            4096,
        )

    monkeypatch.setattr(LocalStore, "preflight_full_snapshot_import", preflight)
    config = _config(tmp_path)
    request = ImportHealthExport(_package(tmp_path / "health.zip"))

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

        assert isinstance(receipt.result, WriteNotStarted)
        assert receipt.result.status is WriteNotStartedStatus.BLOCKED
        assert receipt.final_preflight.capacity is not None
        assert receipt.final_preflight.capacity.status is CapacityStatus.INSUFFICIENT
        assert health_lab.load_overview(OverviewSelection()).snapshot_count == 0


def test_enospc_after_positive_preflight_keeps_the_active_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)

    with HealthLab.open(config) as health_lab:
        first_request = ImportHealthExport(_package(tmp_path / "first.zip"))
        first_plan = health_lab.preview_write(first_request)
        first = health_lab.execute_write(first_request, expected_plan=first_plan.fingerprint)
        assert isinstance(first.result, ImportReceipt)
        assert first.result.snapshot_ref is not None

        request = ImportHealthExport(_package(tmp_path / "second.zip", 61))
        plan = health_lab.preview_write(request)

        replace = Path.replace
        phases: list[str] = []

        monkeypatch.setattr(
            "personal_health_lab.storage._store._allocation_checkpoint",
            lambda root, phase: phases.append(phase),
        )

        def fail_activation(source: Path, target: Path) -> Path:
            if source.parent.name == "staging":
                raise OSError(errno.ENOSPC, "injected full volume")
            return replace(source, target)

        monkeypatch.setattr(Path, "replace", fail_activation)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

        assert isinstance(receipt.result, ImportReceipt)
        assert receipt.result.status is ImportStatus.QUARANTINED
        assert phases == ["combined", "staged"]
        overview = health_lab.load_overview(OverviewSelection())
        assert overview.snapshot_count == 1
        assert overview.measurement_version_count == 1
        quarantine = (
            config.active_store / "quarantine" / "imports" / str(receipt.result.import_id)
        )
        assert (quarantine / "staging" / "measurement_versions.parquet").exists()
        assert json.loads((quarantine / "diagnostic.json").read_text()) == {
            "diagnostic": "capacity_exhausted"
        }


@pytest.mark.parametrize(
    ("failure_errno", "diagnostic"),
    ((errno.EROFS, "target_read_only"), (errno.EACCES, "target_unwritable")),
)
def test_target_becoming_unwritable_during_publish_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_errno: int,
    diagnostic: str,
) -> None:
    config = _config(tmp_path)
    with HealthLab.open(config) as health_lab:
        first = ImportHealthExport(_package(tmp_path / "first.zip"))
        first_plan = health_lab.preview_write(first)
        committed = health_lab.execute_write(first, expected_plan=first_plan.fingerprint)
        assert isinstance(committed.result, ImportReceipt)

        request = ImportHealthExport(_package(tmp_path / "second.zip", 61))
        plan = health_lab.preview_write(request)
        replace = Path.replace

        def fail_activation(source: Path, target: Path) -> Path:
            if source.parent.name == "staging":
                raise OSError(failure_errno, "injected target protection")
            return replace(source, target)

        monkeypatch.setattr(Path, "replace", fail_activation)
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
        overview = health_lab.load_overview(OverviewSelection())

    assert isinstance(receipt.result, ImportReceipt)
    assert receipt.result.status is ImportStatus.QUARANTINED
    assert receipt.result.diagnostics == (diagnostic,)
    assert overview.snapshot_count == 1
    assert overview.measurement_version_count == 1


def test_recovery_quarantines_snapshot_from_crash_before_catalog_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    request = ImportHealthExport(_package(tmp_path / "health.zip"))

    def crash_before_commit(root: Path, phase: str) -> None:
        if phase == "activated":
            raise RuntimeError("injected crash before catalog commit")

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        monkeypatch.setattr(
            "personal_health_lab.storage._store._allocation_checkpoint",
            crash_before_commit,
        )
        with pytest.raises(RuntimeError, match="injected crash"):
            health_lab.execute_write(request, expected_plan=plan.fingerprint)

    monkeypatch.undo()
    with HealthLab.open(config) as health_lab:
        overview = health_lab.load_overview(OverviewSelection())

    assert overview.snapshot_count == 0
    assert overview.quarantined_import_count == 1
    quarantined = list((config.active_store / "quarantine" / "imports").iterdir())
    assert len(quarantined) == 1
    assert (quarantined[0] / "snapshot" / "measurement_versions.parquet").exists()


def test_recovery_reconciles_personal_quarantine_before_binding_commit(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(DataMode.REAL, tmp_path / "synthetic", tmp_path / "real")
    with HealthLab.open(config):
        pass
    quarantine = config.active_store / "quarantine" / "imports" / ("a" * 32)
    staging = quarantine / "staging"
    staging.mkdir(parents=True)
    (staging / "manifest.json").write_text(
        json.dumps(
            {
                "operation_id": "b" * 32,
                "import_id": "a" * 32,
                "snapshot_id": "c" * 32,
                "status": "published",
            }
        ),
        encoding="utf-8",
    )
    (quarantine / "diagnostic.json").write_text(
        json.dumps({"diagnostic": "target_read_only"}), encoding="utf-8"
    )

    with HealthLab.open(config) as health_lab:
        workspace = health_lab.load_workspace_status()
        overview = health_lab.load_overview(OverviewSelection())

    assert workspace.person_binding is PersonBindingStatus.BOUND
    assert overview.quarantined_import_count == 1


def test_unwritable_or_locked_import_target_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    request = ImportHealthExport(_package(tmp_path / "health.zip"))
    with HealthLab.open(config) as health_lab:
        monkeypatch.setattr(os, "access", lambda path, mode: False)
        unwritable_plan = health_lab.preview_write(request)

    assert unwritable_plan.approval.status is WriteApprovalStatus.BLOCKED
    assert unwritable_plan.preflight.capacity.reason is CapacityReason.TARGET_UNWRITABLE

    monkeypatch.setattr(os, "access", lambda path, mode: True)
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda path: SimpleNamespace(
            f_bavail=10 * 1024**3,
            f_frsize=4096,
            f_flag=getattr(os, "ST_RDONLY", 1),
        ),
    )
    read_only = probe_capacity(tmp_path, 1)
    assert read_only.status is CapacityStatus.UNWRITABLE
    assert read_only.reason is CapacityReason.TARGET_READ_ONLY

    monkeypatch.undo()
    monkeypatch.setattr(
        "personal_health_lab.application._application.probe_filevault",
        lambda path: FileVaultCheck(
            FileVaultStatus.UNKNOWN,
            "redacted-volume",
            FileVaultReason.VOLUME_LOCKED,
        ),
    )
    config = RuntimeConfig(
        DataMode.REAL,
        tmp_path / "synthetic",
        tmp_path / "real",
    )
    request = ImportHealthExport(_package(tmp_path / "health.zip"))

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)

    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert "target_locked" in plan.diagnostics


def test_writer_open_failure_is_typed_blocked_when_target_became_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    request = ImportHealthExport(_package(tmp_path / "health.zip"))

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)

        def fail_open(*args: object, **kwargs: object) -> LocalStore:
            monkeypatch.setattr(os, "access", lambda path, mode: False)
            raise StoreError("read-only")

        monkeypatch.setattr(
            LocalStore,
            "open_writer",
            fail_open,
        )
        receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)

    assert isinstance(receipt.result, WriteNotStarted)
    assert receipt.result.status is WriteNotStartedStatus.BLOCKED
    assert receipt.final_preflight.capacity.reason is CapacityReason.TARGET_UNWRITABLE


def test_existing_unwritable_store_can_open_read_only_to_report_blocked_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    request = ImportHealthExport(_package(tmp_path / "health.zip"))
    with HealthLab.open(config):
        pass

    monkeypatch.setattr(
        LocalStore,
        "_try_writer_lock",
        lambda root: (_ for _ in ()).throw(StoreError("read-only")),
    )
    monkeypatch.setattr(os, "access", lambda path, mode: False)
    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)

    assert plan.approval.status is WriteApprovalStatus.BLOCKED
    assert plan.preflight.capacity.reason is CapacityReason.TARGET_UNWRITABLE


def test_competing_import_writer_keeps_readers_available(tmp_path: Path) -> None:
    config = _config(tmp_path)
    request = ImportHealthExport(_package(tmp_path / "health.zip"))

    with HealthLab.open(config) as health_lab:
        plan = health_lab.preview_write(request)
        with (config.active_store / ".writer.lock").open("a+b") as writer_lock:
            fcntl.flock(writer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
            overview = health_lab.load_overview(OverviewSelection())

    assert isinstance(receipt.result, WriteNotStarted)
    assert receipt.result.status is WriteNotStartedStatus.STORE_BUSY
    assert overview.snapshot_count == 0


@pytest.mark.parametrize(
    "case",
    _cases_for("test_v02_import_write_contract"),
    ids=lambda case: case["id"],
)
def test_v02_import_write_contract(
    case: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id = case["id"]
    config = _config(tmp_path)
    package = _package(tmp_path / "health.zip")
    import_fixture = next(
        fixture
        for fixture in _matrix()["fixture"]
        if fixture["id"] == "V02-F-DOM-IMPORT-001"
    )
    assert hashlib.sha256(package.read_bytes()).hexdigest() == import_fixture["sha256"]
    request = ImportHealthExport(package)

    with HealthLab.open(config) as health_lab:
        before = _store_files(config.active_store)
        plan = health_lab.preview_write(request)

        if case_id in {"V02-A-API-004", "V02-A-API-006"}:
            assert plan.approval.status is WriteApprovalStatus.READY
            assert _store_files(config.active_store) == before
        elif case_id == "V02-A-API-005":
            receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
            assert isinstance(receipt.result, ImportReceipt)
            assert receipt.result.status is ImportStatus.COMMITTED
            assert health_lab.load_overview(OverviewSelection()).snapshot_count == 1
        elif case_id == "V02-A-API-007":
            changed = health_lab.preview_write(
                ImportHealthExport(_package(tmp_path / "changed.zip", 61))
            )
            assert changed.fingerprint != plan.fingerprint
            assert _store_files(config.active_store) == before
        elif case_id == "V02-A-API-010":
            receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
            assert receipt.plan_fingerprint == plan.fingerprint
            fingerprint = str(plan.fingerprint).encode()
            assert all(
                fingerprint not in content for content in _store_files(config.active_store).values()
            )
        elif case_id == "V02-A-API-011":
            blocked = health_lab.preview_write(ImportHealthExport(tmp_path / "missing.zip"))
            assert {status.value for status in WriteApprovalStatus} == {
                "ready",
                "confirmation_required",
                "blocked",
            }
            assert plan.approval.status is WriteApprovalStatus.READY
            assert blocked.approval.status is WriteApprovalStatus.BLOCKED
        elif case_id == "V02-A-API-012":
            blocked_request = ImportHealthExport(tmp_path / "missing.zip")
            blocked_plan = health_lab.preview_write(blocked_request)
            blocked = health_lab.execute_write(
                blocked_request,
                expected_plan=blocked_plan.fingerprint,
            )
            assert isinstance(blocked.result, WriteNotStarted)
            assert blocked.result.status is WriteNotStartedStatus.BLOCKED

            changed = _package(tmp_path / "health.zip", 61)
            stale = health_lab.execute_write(request, expected_plan=plan.fingerprint)
            assert changed == request.package_path
            assert isinstance(stale.result, WriteNotStarted)
            assert stale.result.status is WriteNotStartedStatus.PLAN_CHANGED

            with (config.active_store / ".writer.lock").open("a+b") as writer_lock:
                fcntl.flock(writer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                busy_plan = health_lab.preview_write(request)
                busy = health_lab.execute_write(request, expected_plan=busy_plan.fingerprint)
            assert isinstance(busy.result, WriteNotStarted)
            assert busy.result.status is WriteNotStartedStatus.STORE_BUSY
        elif case_id == "V02-A-API-013":
            receipt = health_lab.execute_write(request, expected_plan=plan.fingerprint)
            assert isinstance(receipt.result, ImportReceipt)
            assert {status.value for status in ImportStatus} == {
                "committed",
                "duplicate",
                "rejected",
                "quarantined",
                "restore_pending",
                "store_busy",
            }
        elif case_id == "V02-A-API-014":

            def fail_publish(*args: object, **kwargs: object) -> None:
                raise StoreError("injected technical defect")

            assert _store_files(config.active_store)
            monkeypatch.setattr(LocalStore, "publish_import", fail_publish)
            with pytest.raises(HealthLabError):
                health_lab.execute_write(request, expected_plan=plan.fingerprint)
        else:
            raise AssertionError(f"unhandled V0.2 case: {case_id}")

    assert isinstance(plan.fingerprint, PlanFingerprint)
