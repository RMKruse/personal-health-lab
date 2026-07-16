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
    AnalysisDefinitionId,
    AnalysisStatus,
    BatchDecisionTarget,
    CanonicalHealthType,
    CanonicalUnit,
    ConfigurationError,
    ConfirmDataReviewBatch,
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
    OverviewSelection,
    OverviewStatus,
    PlausibilityRuleSpecification,
    ResolveDataReviewCase,
    RestingHeartRateAnalysisConfig,
    RevokeDataReviewDecision,
    RunHistoricalReview,
    SingleDecisionTarget,
    SourceConflictResolution,
    SourceConflictStrategy,
    SourceDeletionResolution,
    SourceDeletionVerdict,
    SourceValueAcceptance,
    WriteApprovalStatus,
)


def _chart_data(values: object) -> str:
    return f"data:application/json,{quote(json.dumps(values, separators=(',', ':')))}"


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
if import_plan is not None:
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
    execute_disabled = import_plan.approval.status is WriteApprovalStatus.BLOCKED
    execute_label = (
        "Bestätigen und ausführen"
        if import_plan.approval.status is WriteApprovalStatus.CONFIRMATION_REQUIRED
        else "Vorschau ausführen"
    )
    if st.button(execute_label, disabled=execute_disabled):
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

start_date = st.date_input("Von", value=None)
end_date = st.date_input("Bis", value=None)
try:
    selection = OverviewSelection(start_date=start_date, end_date=end_date)
except ValueError as error:
    st.error(str(error))
    st.stop()

if st.button("Zeitraum analysieren"):
    try:
        with HealthLab.open(config) as health_lab:
            analysis_receipt = health_lab.run_resting_hr_analysis(
                RestingHeartRateAnalysisConfig(
                    AnalysisDefinitionId("lag-signal-v1"),
                    start_date=selection.start_date,
                    end_date=selection.end_date,
                )
            )
        st.session_state["last_analysis_status"] = analysis_receipt.status
        st.session_state["last_analysis_diagnostics"] = (
            ", ".join(analysis_receipt.diagnostics) or "-"
        )
        st.rerun()
    except (ConfigurationError, HealthLabError):
        st.error("Ruhepulsanalyse konnte nicht abgeschlossen werden.")

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
        st.warning("Dieses Analyseergebnis ist vorläufig; ein belastbares Ergebnis fehlt.")
    else:
        st.warning(
            f"Dieses Analyseergebnis ist vorläufig. Letztes belastbares Ergebnis: "
            f"Run {previous.analysis_run_id} · Snapshot {previous.snapshot_id} · "
            f"Ergebnis {previous.result_id}."
        )

if overview.resting_hr_analysis is not None:
    result = overview.resting_hr_analysis
    provenance = result.provenance
    st.subheader("Verzögerungsprofil")
    st.caption(
        "Assoziation zwischen aktiver Energie und Apple-Ruhepuls; "
        "keine kausale oder medizinische Aussage."
    )
    st.caption(f"Modellreife: {result.model_maturity}")
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
