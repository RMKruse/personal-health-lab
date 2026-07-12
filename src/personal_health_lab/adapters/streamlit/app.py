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
