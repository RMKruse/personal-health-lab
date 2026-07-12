from __future__ import annotations

import streamlit as st

from personal_health_lab.adapters._config import load_runtime_config
from personal_health_lab.application import (
    ConfigurationError,
    HealthLab,
    HealthLabError,
    OverviewSelection,
)

try:
    config = load_runtime_config()
    with HealthLab.open(config) as health_lab:
        overview = health_lab.load_overview(OverviewSelection())
except (KeyError, ValueError, ConfigurationError, HealthLabError):
    st.error("HealthLab-Konfiguration oder lokaler Datenspeicher ist ungültig.")
    st.stop()

st.title("HealthLab Übersicht")
st.caption(f"Datenmodus: {config.mode.value}")
st.write(f"Status: {overview.status.value}")
st.info(overview.message)
st.caption(
    f"Importe: {overview.import_count} · Pakete: {overview.package_count} · "
    f"Messungen: {overview.logical_measurement_count} · "
    f"Quellversionen: {overview.measurement_version_count} · "
    f"Snapshots: {overview.snapshot_count}"
)
titles = {
    "active_energy": "Aktive Energie",
    "apple_resting_heart_rate": "Apple-Ruhepuls",
}
for series in overview.daily_series:
    st.subheader(f"{titles[series.data_type.value]} ({series.unit.value})")
    st.line_chart(
        {
            "Datum": [value.day for value in series.values],
            "Wert": [value.value for value in series.values],
        },
        x="Datum",
        y="Wert",
    )

if overview.resting_hr_analysis is not None:
    result = overview.resting_hr_analysis
    st.subheader("Verzögerungsprofil")
    st.caption(
        "Assoziation zwischen aktiver Energie und Apple-Ruhepuls; "
        "keine kausale oder medizinische Aussage."
    )
    st.caption(f"Modellreife: {result.model_maturity}")
    st.dataframe(
        [
            {
                "Folgetag": item.lag_days,
                "Richtung": item.direction.value,
                "bpm je 100 kcal": item.estimate_per_100_kcal,
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
                "bpm je persönliche SD": item.estimate_per_personal_standard_deviation,
            }
            for item in result.lag_associations
        ]
    )
    st.metric(
        "Kumulativer Zusammenhang je 100 kcal",
        f"{result.cumulative_association.estimate_per_100_kcal:.2f} bpm",
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
