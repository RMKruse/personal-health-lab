from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote

import streamlit as st

from personal_health_lab.adapters._config import load_runtime_config
from personal_health_lab.application import (
    AbortMetadataRestore,
    AnalysisDefinitionId,
    AnalysisStatus,
    BatchDecisionTarget,
    BeginMetadataRestore,
    CanonicalHealthType,
    CanonicalUnit,
    ConfigurationError,
    ConfirmDataReviewBatch,
    CreateMetadataBackup,
    CreatePlausibilityRuleVersion,
    DataConfirmation,
    DataCorrection,
    DataReviewBatchActionId,
    DataReviewBatchPlan,
    DataReviewCaseKind,
    DataReviewDecisionId,
    DataReviewSelection,
    HealthLab,
    HealthLabError,
    ImportHealthExport,
    ImportReceipt,
    ImportStatus,
    LocalMeasurementExclusion,
    MeasurementVersionId,
    MetadataBackupReceipt,
    MetadataRestorePlan,
    MetadataRestoreReceipt,
    MigrateStore,
    OverviewSelection,
    OverviewStatus,
    PlausibilityRuleSpecification,
    ResolveDataReviewCase,
    RevokeDataReviewDecision,
    RollbackMigration,
    RollbackMigrationPlan,
    RunHistoricalReview,
    RunRestingHeartRateAnalysis,
    RuntimeConfig,
    SingleDecisionTarget,
    SourceConflictResolution,
    SourceConflictStrategy,
    SourceDeletionResolution,
    SourceDeletionVerdict,
    SourceValueAcceptance,
    StoreMigrationPlan,
    WorkspaceState,
    WriteApprovalStatus,
    WriteNotStarted,
)


def _chart_data(values: object) -> str:
    return f"data:application/json,{quote(json.dumps(values, separators=(',', ':')))}"


def _render_health_import(config: RuntimeConfig) -> None:
    import_plan = st.session_state.get("import_plan")
    uploaded = st.file_uploader(
        "Apple-Health-Export",
        type="zip",
        disabled=import_plan is not None,
    )
    if st.button("Health-Export prüfen", disabled=uploaded is None or import_plan is not None):
        request = None
        try:
            assert uploaded is not None
            uploaded.seek(0)
            with NamedTemporaryFile(suffix=".zip", delete=False) as package:
                shutil.copyfileobj(uploaded, package)
                package.flush()
                request = ImportHealthExport(Path(package.name))
            with HealthLab.open(config) as health_lab:
                plan = health_lab.preview_write(request)
            st.session_state["import_request"] = request
            st.session_state["import_plan"] = plan
            st.rerun()
        except (OSError, HealthLabError):
            if request is not None:
                request.package_path.unlink(missing_ok=True)
            st.error("Health-Export konnte nicht importiert werden.")

    import_plan = st.session_state.get("import_plan")
    if import_plan is None:
        return
    import_request = st.session_state["import_request"]
    st.subheader("Schreibvorschau")
    st.write(f"Freigabe: {import_plan.approval.status.value}")
    st.code(str(import_plan.fingerprint))
    st.caption(
        f"Paket: {import_plan.details.package_size} Bytes · "
        f"SHA-256 {import_plan.details.package_hash or '-'}"
    )
    st.caption(
        "Bestätigungen: "
        + (", ".join(confirmation.value for confirmation in import_plan.confirmations) or "-")
    )
    filevault = import_plan.preflight.filevault
    st.caption(
        "FileVault: "
        + (
            f"{filevault.status.value} · Ziel {filevault.target_volume}"
            + (f" · Grund {filevault.reason.value}" if filevault.reason else "")
            if filevault
            else "-"
        )
    )
    capacity = import_plan.preflight.capacity
    st.caption(
        "Kapazität: "
        + (
            f"{capacity.status.value} · Ziel {capacity.target_volume} · "
            f"Methode {capacity.method_id} · Schätzung {capacity.estimate_bytes} · "
            f"Marge {capacity.safety_margin_bytes} · Mindestrest "
            f"{capacity.minimum_remaining_bytes} · Verfügbar {capacity.available_bytes}"
            if capacity
            else "-"
        )
    )
    st.caption("Diagnosen: " + (", ".join(import_plan.diagnostics) or "-"))
    execute_label = (
        "Bestätigen und ausführen"
        if import_plan.approval.status is WriteApprovalStatus.CONFIRMATION_REQUIRED
        else "Vorschau ausführen"
    )
    if st.button(
        execute_label,
        disabled=import_plan.approval.status is WriteApprovalStatus.BLOCKED,
    ):
        import_failed = False
        try:
            with HealthLab.open(config) as health_lab:
                write_receipt = health_lab.execute_write(
                    import_request,
                    expected_plan=import_plan.fingerprint,
                )
            import_result = write_receipt.result
            st.session_state["last_import_status"] = import_result.status
            st.session_state["last_import_snapshot"] = (
                str(import_result.snapshot_ref or "-")
                if isinstance(import_result, ImportReceipt)
                else "-"
            )
            st.session_state["last_import_diagnostics"] = (
                ", ".join(import_result.diagnostics) or "-"
            )
        except (OSError, HealthLabError):
            import_failed = True
            st.error("Health-Export konnte nicht importiert werden.")
        finally:
            import_request.package_path.unlink(missing_ok=True)
            st.session_state.pop("import_request", None)
            st.session_state.pop("import_plan", None)
        if not import_failed:
            st.rerun()
    if st.button("Vorschau verwerfen und bearbeiten"):
        import_request.package_path.unlink(missing_ok=True)
        st.session_state.pop("import_request", None)
        st.session_state.pop("import_plan", None)
        st.rerun()


try:
    config = load_runtime_config()
except (KeyError, ValueError, ConfigurationError):
    st.error("HealthLab-Konfiguration oder lokaler Datenspeicher ist ungültig.")
    st.stop()

st.title("HealthLab Übersicht")
try:
    with HealthLab.open(config) as health_lab:
        workspace_status = health_lab.load_workspace_status()
except (ConfigurationError, HealthLabError):
    st.error("HealthLab-Konfiguration oder lokaler Datenspeicher ist ungültig.")
    st.stop()
st.caption(
    f"Datenmodus: {workspace_status.mode.value} · Datenspeicher-ID: "
    f"{workspace_status.store_id or '-'} · Bindung: {workspace_status.person_binding.value}"
)
st.caption("Zulässige Schreibaktionen: " + ", ".join(workspace_status.allowed_writes))

if workspace_status.state is WorkspaceState.MIGRATION_REQUIRED:
    try:
        with HealthLab.open(config) as health_lab:
            migration_diagnostics = health_lab.load_migration_diagnostics()
            migration_request = MigrateStore()
            migration_plan = health_lab.preview_write(migration_request)
            assert isinstance(migration_plan.details, StoreMigrationPlan)
    except HealthLabError:
        st.error("Migrationsdiagnose konnte nicht geladen werden.")
        st.stop()
    st.subheader("Datenspeichermigration")
    st.caption(
        f"Schema: {migration_diagnostics.source_version} → "
        f"{migration_diagnostics.target_version}"
    )
    st.caption(
        "Schritte: "
        + (
            ", ".join(f"{source} → {target}" for source, target in migration_plan.details.steps)
            or "-"
        )
    )
    st.caption(f"Migrationssicherung: {migration_plan.details.backup_file or '-'}")
    st.caption(
        "Betroffene Snapshots: "
        + (", ".join(map(str, migration_plan.details.affected_snapshot_refs)) or "-")
    )
    st.caption(
        "Bestehende Analysen werden stale: "
        + str(migration_plan.details.existing_analyses_become_stale).lower()
    )
    migration_capacity = migration_plan.preflight.capacity
    st.caption(
        "Kapazität: "
        + (
            f"{migration_capacity.status.value} · Methode {migration_capacity.method_id} · "
            f"Schätzung {migration_capacity.estimate_bytes} · "
            f"Marge {migration_capacity.safety_margin_bytes} · "
            f"Mindestrest {migration_capacity.minimum_remaining_bytes}"
            if migration_capacity is not None
            else "-"
        )
    )
    st.caption("Diagnosen: " + (", ".join(migration_plan.diagnostics) or "-"))
    st.code(str(migration_plan.fingerprint))
    if st.button(
        "Datenspeichermigration ausführen",
        disabled=migration_plan.approval.status is WriteApprovalStatus.BLOCKED,
    ):
        try:
            with HealthLab.open(config) as health_lab:
                migration_result = health_lab.execute_write(
                    migration_request, expected_plan=migration_plan.fingerprint
                ).result
            if isinstance(migration_result, WriteNotStarted):
                st.warning(f"Datenspeichermigration: {migration_result.status.value}")
            else:
                st.success(f"Datenspeichermigration: {migration_result.status.value}")
                st.rerun()
        except HealthLabError:
            st.error("Datenspeichermigration konnte nicht ausgeführt werden.")
    if st.button("Migration abbrechen"):
        st.info("Datenspeichermigration nicht ausgeführt.")
    st.stop()

if workspace_status.state is WorkspaceState.RESTORE_PENDING:
    try:
        with HealthLab.open(config) as health_lab:
            recovery_status = health_lab.load_recovery_status()
            abort_restore_request = AbortMetadataRestore()
            abort_restore_plan = health_lab.preview_write(abort_restore_request)
    except HealthLabError:
        st.error("Wiederherstellungsstatus konnte nicht geladen werden.")
        st.stop()
    st.subheader("Metadatenwiederherstellung")
    st.caption(f"Status: {recovery_status.status.value}")
    st.caption(f"Wiederherstellungs-ID: {recovery_status.restore_id}")
    st.caption(f"Sicherungs-ID: {recovery_status.backup_id}")
    st.caption(
        f"Sicherungsschema: {recovery_status.source_schema_version} → "
        f"{recovery_status.target_schema_version}"
    )
    st.caption(
        "Migrationsschritte: "
        + (
            ", ".join(
                f"{source} → {target}"
                for source, target in recovery_status.migration_steps
            )
            or "-"
        )
    )
    _render_health_import(config)
    if st.button(
        "Wiederherstellung abbrechen",
        disabled=abort_restore_plan.approval.status is WriteApprovalStatus.BLOCKED,
    ):
        try:
            with HealthLab.open(config) as health_lab:
                abort_result = health_lab.execute_write(
                    abort_restore_request,
                    expected_plan=abort_restore_plan.fingerprint,
                ).result
            st.success(f"Wiederherstellung: {abort_result.status.value}")
        except HealthLabError:
            st.error("Wiederherstellung konnte nicht abgebrochen werden.")
    st.stop()

try:
    with HealthLab.open(config) as health_lab:
        rollback_request = RollbackMigration()
        rollback_plan = health_lab.preview_write(rollback_request)
        assert isinstance(rollback_plan.details, RollbackMigrationPlan)
except HealthLabError:
    st.error("Migrationsrollback konnte nicht geplant werden.")
    st.stop()

if rollback_plan.details.migration_operation_id is not None:
    rollback = rollback_plan.details
    st.subheader("Migrationsrollback")
    st.caption(f"Migration: {rollback.migration_operation_id}")
    st.caption(f"Schema: {rollback.source_version} → {rollback.target_version}")
    st.caption(f"Migrationssicherung: {rollback.backup_file or '-'}")
    st.caption(f"Wiederhergestellter Snapshot: {rollback.restored_snapshot_ref or '-'}")
    st.caption(f"Unreferenzierter Snapshot: {rollback.current_snapshot_ref or '-'}")
    st.caption("Diagnosen: " + (", ".join(rollback_plan.diagnostics) or "-"))
    st.code(str(rollback_plan.fingerprint))
    if st.button(
        "Letzte Migration zurückrollen",
        disabled=rollback_plan.approval.status is WriteApprovalStatus.BLOCKED,
    ):
        try:
            with HealthLab.open(config) as health_lab:
                rollback_result = health_lab.execute_write(
                    rollback_request, expected_plan=rollback_plan.fingerprint
                ).result
            if isinstance(rollback_result, WriteNotStarted):
                st.warning(f"Migrationsrollback: {rollback_result.status.value}")
            else:
                st.success(f"Migrationsrollback: {rollback_result.status.value}")
                st.rerun()
        except HealthLabError:
            st.error("Migrationsrollback konnte nicht ausgeführt werden.")

_render_health_import(config)

with st.expander("Sicherung & Wiederherstellung"):
    backup_plan = st.session_state.get("backup_plan")
    backup_target = st.text_input(
        "Zieldatei für Metadatensicherung",
        disabled=backup_plan is not None,
    )
    if st.button(
        "Metadatensicherung prüfen",
        disabled=not backup_target or backup_plan is not None,
    ):
        try:
            backup_request = CreateMetadataBackup(Path(backup_target))
            with HealthLab.open(config) as health_lab:
                st.session_state["backup_plan"] = health_lab.preview_write(backup_request)
            st.session_state["backup_request"] = backup_request
            st.rerun()
        except (OSError, ConfigurationError, HealthLabError):
            st.error("Metadatensicherung konnte nicht geplant werden.")
    backup_plan = st.session_state.get("backup_plan")
    if backup_plan is not None:
        st.code(str(backup_plan.fingerprint))
        st.caption(
            f"Audit-Höchststand: {backup_plan.details.audit_max_position} · "
            f"Sicherungs-ID: {backup_plan.details.backup_id} · "
            f"Zieldatei: {backup_plan.details.target_file}"
        )
        st.caption(
            "Bestätigungen: "
            + (", ".join(item.value for item in backup_plan.confirmations) or "-")
        )
        if st.button(
            "Metadatensicherung ausführen",
            disabled=backup_plan.approval.status is WriteApprovalStatus.BLOCKED,
        ):
            try:
                with HealthLab.open(config) as health_lab:
                    backup_result = health_lab.execute_write(
                        st.session_state["backup_request"],
                        expected_plan=backup_plan.fingerprint,
                    ).result
                if isinstance(backup_result, MetadataBackupReceipt):
                    st.session_state["last_backup"] = (
                        backup_result.status.value,
                        backup_result.target_file,
                    )
                elif isinstance(backup_result, WriteNotStarted):
                    st.session_state["last_backup"] = (
                        backup_result.status.value,
                        "-",
                    )
                st.session_state.pop("backup_plan", None)
                st.session_state.pop("backup_request", None)
                st.rerun()
            except (OSError, HealthLabError):
                st.error("Metadatensicherung konnte nicht geschrieben werden.")
        if st.button("Sicherungsvorschau verwerfen und bearbeiten"):
            st.session_state.pop("backup_plan", None)
            st.session_state.pop("backup_request", None)
            st.rerun()
    last_backup = st.session_state.pop("last_backup", None)
    if last_backup is not None:
        message = f"Metadatensicherung: {last_backup[0]} · Datei {last_backup[1]}"
        if last_backup[0] in {"completed", "no_op"}:
            st.success(message)
        else:
            st.warning(message)

    restore_plan = st.session_state.get("restore_plan")
    restore_backup = st.text_input(
        "Metadatensicherung wiederherstellen",
        disabled=restore_plan is not None,
    )
    if st.button(
        "Wiederherstellung prüfen",
        disabled=not restore_backup or restore_plan is not None,
    ):
        try:
            restore_request = BeginMetadataRestore(Path(restore_backup))
            with HealthLab.open(config) as health_lab:
                st.session_state["restore_plan"] = health_lab.preview_write(restore_request)
            st.session_state["restore_request"] = restore_request
            st.rerun()
        except (OSError, ConfigurationError, HealthLabError):
            st.error("Wiederherstellung konnte nicht geplant werden.")
    restore_plan = st.session_state.get("restore_plan")
    if restore_plan is not None:
        assert isinstance(restore_plan.details, MetadataRestorePlan)
        st.code(str(restore_plan.fingerprint))
        st.caption(
            f"Sicherungs-ID: {restore_plan.details.backup_id or '-'} · "
            f"Audit-Höchststand: {restore_plan.details.audit_max_position or 0}"
        )
        st.caption(
            f"Sicherungsschema: {restore_plan.details.source_schema_version or '-'} → "
            f"{restore_plan.details.target_schema_version}"
        )
        st.caption(
            "Bestätigungen: "
            + (", ".join(item.value for item in restore_plan.confirmations) or "-")
        )
        if st.button(
            "Wiederherstellung beginnen",
            disabled=restore_plan.approval.status is WriteApprovalStatus.BLOCKED,
        ):
            try:
                with HealthLab.open(config) as health_lab:
                    restore_result = health_lab.execute_write(
                        st.session_state["restore_request"],
                        expected_plan=restore_plan.fingerprint,
                    ).result
                if isinstance(restore_result, MetadataRestoreReceipt):
                    st.session_state["last_restore"] = restore_result.status.value
                st.session_state.pop("restore_plan", None)
                st.session_state.pop("restore_request", None)
                st.rerun()
            except (OSError, HealthLabError):
                st.error("Wiederherstellung konnte nicht begonnen werden.")
        if st.button("Wiederherstellungsvorschau verwerfen und bearbeiten"):
            st.session_state.pop("restore_plan", None)
            st.session_state.pop("restore_request", None)
            st.rerun()

analysis_plan = st.session_state.get("analysis_plan")
start_date = st.date_input("Von", value=None, disabled=analysis_plan is not None)
end_date = st.date_input("Bis", value=None, disabled=analysis_plan is not None)
try:
    selection = OverviewSelection(start_date=start_date, end_date=end_date)
except ValueError as error:
    st.error(str(error))
    st.stop()

if st.button("Ruhepulsanalyse prüfen", disabled=analysis_plan is not None):
    try:
        analysis_request = RunRestingHeartRateAnalysis(
            AnalysisDefinitionId("lag-signal-v2"),
            start_date=selection.start_date,
            end_date=selection.end_date,
        )
        with HealthLab.open(config) as health_lab:
            st.session_state["analysis_plan"] = health_lab.preview_write(analysis_request)
        st.session_state["analysis_request"] = analysis_request
        st.rerun()
    except (ConfigurationError, HealthLabError):
        st.error("Ruhepulsanalyse konnte nicht abgeschlossen werden.")

analysis_plan = st.session_state.get("analysis_plan")
if analysis_plan is not None:
    st.subheader("Analysevorschau")
    st.code(str(analysis_plan.fingerprint))
    st.caption(f"Gepinnter Snapshot: {analysis_plan.details.base_snapshot_ref or '-'}")
    st.caption("Diagnosen: " + (", ".join(analysis_plan.diagnostics) or "-"))
    if st.button(
        "Ruhepulsanalyse ausführen",
        disabled=analysis_plan.approval.status is WriteApprovalStatus.BLOCKED,
    ):
        try:
            with HealthLab.open(config) as health_lab:
                analysis_result = health_lab.execute_write(
                    st.session_state["analysis_request"],
                    expected_plan=analysis_plan.fingerprint,
                ).result
            st.session_state["last_analysis_status"] = analysis_result.status
            st.session_state["last_analysis_diagnostics"] = (
                ", ".join(analysis_result.diagnostics) or "-"
            )
            st.session_state.pop("analysis_plan", None)
            st.session_state.pop("analysis_request", None)
            st.rerun()
        except (ConfigurationError, HealthLabError):
            st.error("Ruhepulsanalyse konnte nicht abgeschlossen werden.")
    if st.button("Analysevorschau verwerfen und bearbeiten"):
        st.session_state.pop("analysis_plan", None)
        st.session_state.pop("analysis_request", None)
        st.rerun()

try:
    with HealthLab.open(config) as health_lab:
        overview = health_lab.load_overview(selection)
        data_review = health_lab.load_data_review(DataReviewSelection())
        data_review_details = tuple(
            health_lab.load_data_review_case(case.case_id) for case in data_review.cases
        )
        plausibility_rules = health_lab.load_plausibility_rules()
except (ConfigurationError, HealthLabError):
    st.error("HealthLab-Konfiguration oder lokaler Datenspeicher ist ungültig.")
    st.stop()

import_status = st.session_state.pop("last_import_status", None)
if import_status is not None:
    import_message = (
        f"Importstatus: {import_status} · Snapshot "
        f"{st.session_state.pop('last_import_snapshot')} · "
        f"Diagnosen: {st.session_state.pop('last_import_diagnostics')}"
    )
    if import_status in {ImportStatus.COMMITTED, ImportStatus.DUPLICATE}:
        st.success(import_message)
    else:
        st.warning(import_message)

analysis_status = st.session_state.pop("last_analysis_status", None)
if analysis_status is not None:
    analysis_message = (
        f"Analysestatus: {analysis_status} · "
        f"Diagnosen: {st.session_state.pop('last_analysis_diagnostics')}"
    )
    if analysis_status in {AnalysisStatus.COMPLETED, AnalysisStatus.REUSED}:
        st.success(analysis_message)
    else:
        st.warning(analysis_message)

st.write(f"Status: {overview.status.value}")
st.info(overview.message)
st.caption(
    f"Importe: {overview.import_count} · Pakete: {overview.package_count} · "
    f"Messungen: {overview.logical_measurement_count} · "
    f"Quellversionen: {overview.measurement_version_count} · "
    f"Snapshots: {overview.snapshot_count} · "
    f"Quarantänisierte Importe: {overview.quarantined_import_count}"
)
if data_review.cases:
    st.subheader("Datenprüfung")
    st.caption(f"Datenstatus: {data_review.status.value}")
    batch_kind = st.selectbox(
        "Filter für Sammelbestätigung",
        tuple(DataReviewCaseKind),
        format_func=lambda item: item.value,
    )
    batch_note = st.text_input("Optionale Sammelnotiz")
    if st.button("Sammelbestätigung prüfen"):
        assert batch_kind is not None
        batch_request = ConfirmDataReviewBatch(
            DataReviewSelection(batch_kind), batch_note or None
        )
        with HealthLab.open(config) as health_lab:
            st.session_state["review_plan"] = health_lab.preview_write(batch_request)
        st.session_state["review_request"] = batch_request
        st.rerun()
    for detail in data_review_details:
        case = detail.case
        reasons = (
            ", ".join(
                f"{reason.code.value} [{reason.lower_bound}, {reason.upper_bound}] {reason.unit}"
                for reason in detail.reasons
            )
            or "-"
        )
        st.warning(
            f"Prüffall {case.case_id}: {case.kind.value} · "
            f"Messung {case.logical_measurement_id or case.measurement_version_id or '-'} · "
            f"Regel {case.rule_version_id or '-'} · Evidenz {case.evidence_fingerprint} · "
            f"Aktionen {', '.join(case.allowed_actions) or '-'} · "
            f"Kandidaten {', '.join(map(str, case.candidate_version_ids)) or '-'}"
        )
        st.caption(
            "Details: "
            f"Typ {detail.source_type or '-'} · Zeitpunkt {detail.measured_at or '-'} · "
            f"Wert {detail.effective_value} · "
            "Quelle "
            f"{detail.effective_value_source.value if detail.effective_value_source else '-'} · "
            f"Begründungen {reasons}"
        )
        decision_note = st.text_input(
            "Optionale Entscheidungsnotiz", key=f"decision-note-{case.case_id}"
        )
        if case.kind.value == "suspected_source_deletion":
            verdict = st.selectbox(
                "Löschungsvermutung",
                tuple(SourceDeletionVerdict),
                key=f"verdict-{case.case_id}",
                format_func=lambda item: item.value,
            )
            if st.button("Auflösung prüfen", key=f"resolve-{case.case_id}"):
                assert verdict is not None
                review_request_local = ResolveDataReviewCase(
                    case.case_id, SourceDeletionResolution(verdict, decision_note or None)
                )
                with HealthLab.open(config) as health_lab:
                    st.session_state["review_plan"] = health_lab.preview_write(review_request_local)
                st.session_state["review_request"] = review_request_local
                st.rerun()
        elif case.kind.value == "plausibility":
            if st.button("Auffälligkeit bestätigen", key=f"confirm-{case.case_id}"):
                review_request_local = ResolveDataReviewCase(
                    case.case_id, DataConfirmation(decision_note or None)
                )
                with HealthLab.open(config) as health_lab:
                    st.session_state["review_plan"] = health_lab.preview_write(review_request_local)
                st.session_state["review_request"] = review_request_local
                st.rerun()
        elif case.kind.value == "source_conflict":
            strategy = st.selectbox(
                "Konfliktauflösung",
                tuple(SourceConflictStrategy),
                key=f"strategy-{case.case_id}",
                format_func=lambda item: item.value,
            )
            preferred = st.selectbox(
                "Bevorzugte Quellversion",
                case.candidate_version_ids,
                key=f"preferred-{case.case_id}",
                format_func=str,
            )
            if st.button("Auflösung prüfen", key=f"resolve-{case.case_id}"):
                assert strategy is not None
                review_request_local = ResolveDataReviewCase(
                    case.case_id,
                    SourceConflictResolution(
                        strategy,
                        preferred if strategy is SourceConflictStrategy.PREFER else None,
                        decision_note or None,
                    ),
                )
                with HealthLab.open(config) as health_lab:
                    st.session_state["review_plan"] = health_lab.preview_write(review_request_local)
                st.session_state["review_request"] = review_request_local
                st.rerun()
        elif case.kind.value == "continued_override":
            if "confirm" in case.allowed_actions:
                if st.button("Neue Quellversion bestätigen", key=f"confirm-{case.case_id}"):
                    review_request_local = ResolveDataReviewCase(
                        case.case_id, DataConfirmation(decision_note or None)
                    )
                    with HealthLab.open(config) as health_lab:
                        st.session_state["review_plan"] = health_lab.preview_write(
                            review_request_local
                        )
                    st.session_state["review_request"] = review_request_local
                    st.rerun()
            elif st.button("Neue Quellversion übernehmen", key=f"accept-{case.case_id}"):
                assert case.measurement_version_id is not None
                review_request_local = ResolveDataReviewCase(
                    case.case_id,
                    SourceValueAcceptance(
                        case.measurement_version_id, decision_note or None
                    ),
                )
                with HealthLab.open(config) as health_lab:
                    st.session_state["review_plan"] = health_lab.preview_write(
                        review_request_local
                    )
                st.session_state["review_request"] = review_request_local
                st.rerun()
        if case.measurement_version_id is not None and case.kind.value in {
            "plausibility",
            "continued_override",
        }:
            correction_value = st.number_input(
                "Korrekturwert",
                value=float(detail.effective_value or 0),
                key=f"correction-value-{case.case_id}",
            )
            correction_reason = st.text_input(
                "Korrekturgrund", key=f"correction-reason-{case.case_id}"
            )
            assert detail.canonical_unit is not None
            if st.button("Korrektur prüfen", key=f"correct-{case.case_id}"):
                try:
                    review_request_local = ResolveDataReviewCase(
                        case.case_id,
                        DataCorrection(
                            case.measurement_version_id,
                            correction_value,
                            detail.canonical_unit,
                            correction_reason,
                            decision_note or None,
                        ),
                    )
                    with HealthLab.open(config) as health_lab:
                        st.session_state["review_plan"] = health_lab.preview_write(
                            review_request_local
                        )
                    st.session_state["review_request"] = review_request_local
                    st.rerun()
                except ConfigurationError:
                    st.error("Korrekturgrund fehlt.")
            exclusion_reason = st.text_input(
                "Ausschlussgrund", key=f"exclusion-reason-{case.case_id}"
            )
            if st.button("Lokal ausschließen", key=f"exclude-{case.case_id}"):
                try:
                    review_request_local = ResolveDataReviewCase(
                        case.case_id,
                        LocalMeasurementExclusion(
                            case.measurement_version_id,
                            exclusion_reason,
                            decision_note or None,
                        ),
                    )
                    with HealthLab.open(config) as health_lab:
                        st.session_state["review_plan"] = health_lab.preview_write(
                            review_request_local
                        )
                    st.session_state["review_request"] = review_request_local
                    st.rerun()
                except ConfigurationError:
                    st.error("Ausschlussgrund fehlt.")
with st.expander("Datenprüfentscheidung widerrufen"):
    decision_id = st.text_input("Entscheidungs-ID")
    batch_action_id = st.text_input("Sammelaktions-ID")
    revoke_reason = st.text_input("Widerrufsgrund")
    if st.button("Widerruf prüfen"):
        try:
            revoke_request = RevokeDataReviewDecision(
                (
                    BatchDecisionTarget(DataReviewBatchActionId(batch_action_id))
                    if batch_action_id
                    else SingleDecisionTarget(DataReviewDecisionId(decision_id))
                ),
                revoke_reason,
            )
            with HealthLab.open(config) as health_lab:
                st.session_state["review_plan"] = health_lab.preview_write(revoke_request)
            st.session_state["review_request"] = revoke_request
            st.rerun()
        except (ValueError, ConfigurationError):
            st.error("Widerruf ist ungültig.")

with st.expander("Quellmessung direkt korrigieren"):
    direct_version = st.text_input("Quellversions-ID")
    direct_value = st.number_input("Direkter Korrekturwert", value=0.0)
    direct_unit = st.selectbox(
        "Kanonische Einheit", tuple(CanonicalUnit), format_func=lambda item: item.value
    )
    direct_reason = st.text_input("Direkter Korrekturgrund")
    direct_note = st.text_input("Optionale direkte Korrekturnotiz")
    if st.button("Direkte Korrektur prüfen"):
        try:
            assert direct_unit is not None
            direct_request = ResolveDataReviewCase(
                None,
                DataCorrection(
                    MeasurementVersionId(direct_version),
                    direct_value,
                    direct_unit,
                    direct_reason,
                    direct_note or None,
                ),
            )
            with HealthLab.open(config) as health_lab:
                st.session_state["review_plan"] = health_lab.preview_write(direct_request)
            st.session_state["review_request"] = direct_request
            st.rerun()
        except (ValueError, ConfigurationError):
            st.error("Direkte Korrektur ist ungültig.")

review_plan = st.session_state.get("review_plan")
if review_plan is not None:
    st.code(str(review_plan.fingerprint))
    if isinstance(review_plan.details, DataReviewBatchPlan):
        st.dataframe(
            [
                {
                    "case_id": str(item.case_id),
                    "value": item.effective_value,
                    "unit": None if item.canonical_unit is None else item.canonical_unit.value,
                }
                for item in review_plan.details.matches
            ],
            use_container_width=True,
        )
    if st.button("Datenprüfentscheidung ausführen"):
        with HealthLab.open(config) as health_lab:
            decision_result = health_lab.execute_write(
                st.session_state["review_request"],
                expected_plan=review_plan.fingerprint,
            ).result
        st.success(f"Datenprüfentscheidung: {decision_result.status.value}")
        st.session_state.pop("review_plan", None)
        st.session_state.pop("review_request", None)
        st.rerun()
if overview.quarantined_import_count:
    st.warning("Mindestens ein unterbrochener Import wurde sicher quarantänisiert.")

with st.expander("Plausibilitätsregeln"):
    for rule in plausibility_rules.rules:
        active = rule.active_version
        st.caption(
            f"{rule.data_type.value}: {active.version_id} · "
            f"ab {active.effective_from or '-'} · aktiv {active.specification.active}"
        )
    selected_type = st.selectbox(
        "Datentyp", tuple(CanonicalHealthType), format_func=lambda item: item.value
    )
    assert selected_type is not None
    selected_rule = next(
        rule for rule in plausibility_rules.rules if rule.data_type is selected_type
    )
    expected_unit = selected_rule.recommendation.specification.unit
    lower_text = st.text_input("Untere Grenze", value="")
    upper_text = st.text_input("Obere Grenze", value="")
    personal = st.checkbox("Persönlichen Bereich aktivieren")
    adopt_recommendation = st.checkbox("Ausgelieferte Empfehlung übernehmen")
    effective_text = st.text_input("Gültig ab lokalem ISO-Montag", value="")
    if st.button("Regelversion prüfen"):
        try:
            specification = (
                selected_rule.recommendation.specification
                if adopt_recommendation
                else PlausibilityRuleSpecification(
                    expected_unit,
                    None if not lower_text else float(lower_text),
                    None if not upper_text else float(upper_text),
                    personal,
                )
            )
            rule_request = CreatePlausibilityRuleVersion(
                selected_type,
                specification,
                datetime.fromisoformat(effective_text),
                (selected_rule.recommendation.recommendation_id if adopt_recommendation else None),
            )
            with HealthLab.open(config) as health_lab:
                st.session_state["rule_plan"] = health_lab.preview_write(rule_request)
            st.session_state["rule_request"] = rule_request
            st.rerun()
        except (ValueError, ConfigurationError, HealthLabError):
            st.error("Plausibilitätsregel ist ungültig.")
    rule_plan = st.session_state.get("rule_plan")
    if rule_plan is not None:
        st.code(str(rule_plan.fingerprint))
        if st.button(
            "Regelversion ausführen",
            disabled=rule_plan.approval.status is WriteApprovalStatus.BLOCKED,
        ):
            try:
                with HealthLab.open(config) as health_lab:
                    rule_result = health_lab.execute_write(
                        st.session_state["rule_request"],
                        expected_plan=rule_plan.fingerprint,
                    ).result
                st.success(f"Regelversion: {rule_result.status.value}")
                st.session_state.pop("rule_plan", None)
                st.session_state.pop("rule_request", None)
                st.rerun()
            except HealthLabError:
                st.error("Plausibilitätsregel konnte nicht gespeichert werden.")

    st.divider()
    historical_start = st.date_input("Historische Prüfung von", value=None)
    historical_end = st.date_input("Historische Prüfung bis", value=None)
    historical_version = st.selectbox(
        "Historische Regelversion",
        selected_rule.versions,
        format_func=lambda item: item.version_id,
    )
    if st.button("Historische Prüfung planen"):
        if historical_start is None or historical_end is None:
            st.error("Historischer Prüfzeitraum fehlt.")
        else:
            try:
                historical_request = RunHistoricalReview(
                    selected_type,
                    historical_start,
                    historical_end,
                    historical_version.version_id,
                )
                with HealthLab.open(config) as health_lab:
                    st.session_state["historical_plan"] = health_lab.preview_write(
                        historical_request
                    )
                st.session_state["historical_request"] = historical_request
                st.rerun()
            except (ConfigurationError, HealthLabError):
                st.error("Historische Datenprüfung ist ungültig.")
    historical_plan = st.session_state.get("historical_plan")
    if historical_plan is not None:
        details = historical_plan.details
        st.code(str(historical_plan.fingerprint))
        st.caption(
            f"Gepinnte Basis: {details.base_snapshot_ref or '-'} · "
            f"Zeitraum {details.start_date} bis {details.end_date} · "
            f"Regel {details.rule_version_id}"
        )
        if st.button(
            "Historische Prüfung ausführen",
            disabled=historical_plan.approval.status is WriteApprovalStatus.BLOCKED,
        ):
            try:
                with HealthLab.open(config) as health_lab:
                    historical_result = health_lab.execute_write(
                        st.session_state["historical_request"],
                        expected_plan=historical_plan.fingerprint,
                    ).result
                st.success(f"Historische Datenprüfung: {historical_result.status.value}")
                st.session_state.pop("historical_plan", None)
                st.session_state.pop("historical_request", None)
                st.rerun()
            except HealthLabError:
                st.error("Historische Datenprüfung konnte nicht ausgeführt werden.")

titles = {
    "active_energy": "Aktive Energie",
    "apple_resting_heart_rate": "Apple-Ruhepuls",
}
for series in overview.daily_series:
    st.subheader(f"{titles[series.data_type.value]} ({series.unit.value})")
    st.vega_lite_chart(
        spec={
            "data": {
                "url": _chart_data(
                    [
                        {"Datum": value.day.isoformat(), "Wert": value.value}
                        for value in series.values
                    ]
                )
            },
            "mark": {"type": "line", "tooltip": True},
            "encoding": {
                "x": {"field": "Datum", "type": "temporal"},
                "y": {"field": "Wert", "type": "quantitative"},
            },
        },
        use_container_width=True,
    )

if overview.status is OverviewStatus.PROVISIONAL:
    previous = overview.last_ready_analysis_provenance
    if previous is None:
        st.warning("Dieses Analyseergebnis ist explorativ; ein robustes Ergebnis fehlt.")
    else:
        st.warning(
            f"Dieses Analyseergebnis ist explorativ. Letztes robustes Ergebnis: "
            f"Run {previous.analysis_run_id} · Snapshot {previous.snapshot_id}."
        )

if overview.resting_hr_analysis is None:
    st.info("Kein aktuelles Analyseergebnis.")
if overview.analysis_history:
    st.subheader("Analysehistorie")
    for historical in overview.analysis_history:
        provenance = historical.provenance
        st.caption(
            f"{historical.freshness.value} · Datenstatus {historical.data_status.value} · "
            f"Modellreife {historical.model_maturity.value} · "
            f"Run {provenance.analysis_run_id if provenance else '-'} · "
            f"Snapshot {historical.snapshot_id} · "
            f"Ausgeführt {historical.completed_at.isoformat() if historical.completed_at else '-'}"
        )

if overview.resting_hr_analysis is not None:
    result = overview.resting_hr_analysis
    provenance = result.provenance
    st.subheader("Verzögerungsprofil")
    st.caption(
        "Assoziation zwischen aktiver Energie und Apple-Ruhepuls; "
        "keine kausale oder medizinische Aussage."
    )
    st.caption(
        f"Aktualität: {result.freshness.value} · Datenstatus: {result.data_status.value} · "
        f"Modellreife: {result.model_maturity.value}"
    )
    st.caption(f"Reproduzierbarkeit: {result.reproducibility.value}")
    if provenance is not None:
        st.caption(
            f"Run {provenance.analysis_run_id} · Snapshot {provenance.snapshot_id} · "
            f"Ergebnis {provenance.result_id} · Definition {provenance.analysis_definition_id}"
        )
        st.caption(
            f"Konfiguration {provenance.config_hash} (Schema "
            f"{provenance.config_schema_version}) · Environment "
            f"{provenance.environment_lock_hash}"
        )
        st.caption(
            f"Commit {provenance.code_commit} · Dirty {str(provenance.code_dirty).lower()} · "
            f"Diff {provenance.code_diff_hash or '-'}"
        )
    lag_rows = [
        {
            "Folgetag": item.lag_days,
            "Schätzung": item.estimate_per_100_kcal,
            "Punktweise Untergrenze": item.pointwise_interval.lower_per_100_kcal,
            "Punktweise Obergrenze": item.pointwise_interval.upper_per_100_kcal,
            "Simultane Untergrenze": (
                item.simultaneous_band.lower_per_100_kcal
                if item.simultaneous_band is not None
                else None
            ),
            "Simultane Obergrenze": (
                item.simultaneous_band.upper_per_100_kcal
                if item.simultaneous_band is not None
                else None
            ),
            "Richtung": item.direction.value,
            "bpm je persönliche SD": item.estimate_per_personal_standard_deviation,
        }
        for item in result.lag_associations
    ]
    st.vega_lite_chart(
        spec={
            "data": {"url": _chart_data(lag_rows)},
            "layer": [
                {
                    "mark": {"type": "area", "opacity": 0.15, "color": "#7c8db5"},
                    "encoding": {
                        "x": {"field": "Folgetag", "type": "ordinal"},
                        "y": {"field": "Simultane Untergrenze", "type": "quantitative"},
                        "y2": {"field": "Simultane Obergrenze"},
                    },
                },
                {
                    "mark": {"type": "area", "opacity": 0.3, "color": "#4c78a8"},
                    "encoding": {
                        "x": {"field": "Folgetag", "type": "ordinal"},
                        "y": {"field": "Punktweise Untergrenze", "type": "quantitative"},
                        "y2": {"field": "Punktweise Obergrenze"},
                    },
                },
                {
                    "mark": {"type": "line", "point": True, "color": "#1f3b5d"},
                    "encoding": {
                        "x": {"field": "Folgetag", "type": "ordinal", "title": "Folgetag"},
                        "y": {
                            "field": "Schätzung",
                            "type": "quantitative",
                            "title": "bpm je 100 kcal",
                        },
                        "tooltip": [
                            {"field": "Folgetag", "type": "ordinal"},
                            {"field": "Schätzung", "type": "quantitative"},
                            {
                                "field": "Punktweise Untergrenze",
                                "type": "quantitative",
                            },
                            {
                                "field": "Punktweise Obergrenze",
                                "type": "quantitative",
                            },
                            {
                                "field": "Simultane Untergrenze",
                                "type": "quantitative",
                            },
                            {
                                "field": "Simultane Obergrenze",
                                "type": "quantitative",
                            },
                        ],
                    },
                },
            ],
        },
        use_container_width=True,
    )
    st.caption(
        "Dunkelblau: punktweises Intervall · Hellblau: simultanes Band · "
        "Detailwerte erscheinen im Tooltip."
    )
    cumulative = result.cumulative_association
    st.metric(
        "Kumulativer Zusammenhang je 100 kcal",
        f"{cumulative.estimate_per_100_kcal:.2f} bpm",
    )
    st.caption(
        f"Punktweises Intervall des kumulativen Zusammenhangs: "
        f"{cumulative.pointwise_interval.lower_per_100_kcal:.2f} bis "
        f"{cumulative.pointwise_interval.upper_per_100_kcal:.2f} bpm je 100 kcal."
    )
    st.caption(
        f"Diagnosen: {result.diagnostics.complete_days} vollständige Tage · "
        f"Merkmalsabhängigkeit {result.diagnostics.feature_dependency} · "
        f"Bootstrap {result.diagnostics.bootstrap_successes}/"
        f"{result.diagnostics.bootstrap_resamples} · "
        f"Guardrail {result.diagnostics.association_guardrail}"
    )
    st.caption(
        f"Moving-Block-Bootstrap: {result.methodology.resample_count} Resamples, "
        f"Blocklänge {result.methodology.block_length_days} Tage, "
        f"Intervallniveau {result.methodology.interval_level:.0%}."
    )
