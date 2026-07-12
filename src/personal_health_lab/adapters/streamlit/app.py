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
