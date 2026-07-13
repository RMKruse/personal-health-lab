from __future__ import annotations

import json
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote

import streamlit as st

from personal_health_lab.adapters._config import load_runtime_config
from personal_health_lab.application import (
    AnalysisDefinitionId,
    ConfigurationError,
    HealthLab,
    HealthLabError,
    OverviewSelection,
    RestingHeartRateAnalysisConfig,
)


def _chart_data(values: object) -> str:
    return f"data:application/json,{quote(json.dumps(values, separators=(',', ':')))}"


try:
    config = load_runtime_config()
except (KeyError, ValueError, ConfigurationError):
    st.error("HealthLab-Konfiguration oder lokaler Datenspeicher ist ungültig.")
    st.stop()

st.title("HealthLab Übersicht")
st.caption(f"Datenmodus: {config.mode.value}")

uploaded = st.file_uploader("Apple-Health-Export", type="zip")
if st.button("Health-Export importieren", disabled=uploaded is None):
    try:
        assert uploaded is not None
        uploaded.seek(0)
        with NamedTemporaryFile(suffix=".zip") as package:
            shutil.copyfileobj(uploaded, package)
            package.flush()
            with HealthLab.open(config) as health_lab:
                import_receipt = health_lab.import_health_export(Path(package.name))
        st.session_state["last_import_status"] = import_receipt.status.value
        st.session_state["last_import_snapshot"] = str(import_receipt.snapshot_ref or "-")
        st.session_state["last_import_diagnostics"] = ", ".join(import_receipt.diagnostics) or "-"
        st.rerun()
    except (OSError, HealthLabError):
        st.error("Health-Export konnte nicht importiert werden.")

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
        st.session_state["last_analysis_status"] = analysis_receipt.status.value
        st.session_state["last_analysis_diagnostics"] = (
            ", ".join(analysis_receipt.diagnostics) or "-"
        )
        st.rerun()
    except (ConfigurationError, HealthLabError):
        st.error("Ruhepulsanalyse konnte nicht abgeschlossen werden.")

try:
    with HealthLab.open(config) as health_lab:
        overview = health_lab.load_overview(selection)
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
    if import_status in {"committed", "duplicate"}:
        st.success(import_message)
    else:
        st.warning(import_message)

analysis_status = st.session_state.pop("last_analysis_status", None)
if analysis_status is not None:
    analysis_message = (
        f"Analysestatus: {analysis_status} · "
        f"Diagnosen: {st.session_state.pop('last_analysis_diagnostics')}"
    )
    if analysis_status in {"completed", "reused"}:
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
if overview.quarantined_import_count:
    st.warning("Mindestens ein unterbrochener Import wurde sicher quarantänisiert.")

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

if overview.status.value == "provisional":
    previous = overview.last_ready_analysis_provenance
    if previous is None:
        st.warning("Dieses Ergebnis ist provisorisch; ein früheres robustes Ergebnis fehlt.")
    else:
        st.warning(
            f"Dieses Ergebnis ist provisorisch. Letztes robustes Ergebnis: "
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
                    },
                },
            ],
        },
        use_container_width=True,
    )
    st.caption(
        "Das Diagramm zeigt Schätzung, punktweises Intervall und simultanes Band; "
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
