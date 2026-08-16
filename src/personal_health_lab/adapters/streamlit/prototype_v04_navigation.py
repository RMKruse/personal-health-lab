"""PROTOTYPE — three V0.4 UI variants on one disposable page via ``?variant=``.

Run this file with ``uv run streamlit run <path-to-this-file>``.
"""

from __future__ import annotations

from datetime import date

import streamlit as st

_AREAS = (
    "Gesamtübersicht",
    "Import & Datenprüfung",
    "Aktivität & Ruhepuls",
    "Gewicht & Ernährung",
    "Kontext & Medikamente",
    "Methodik, Einstellungen & Backup",
)
_VARIANTS = {"A": "Bereich zuerst", "B": "Analyse-Cockpit", "C": "Tagesjournal"}
_DAYS = (
    ("So, 10. Aug", "62 bpm", "78,4 kg", "geprüft"),
    ("Mo, 11. Aug", "60 bpm", "78,2 kg", "geprüft"),
    ("Di, 12. Aug", "64 bpm", "78,3 kg", "vorläufig"),
    ("Mi, 13. Aug", "61 bpm", "78,1 kg", "geprüft"),
    ("Do, 14. Aug", "59 bpm", "78,0 kg", "geprüft"),
    ("Fr, 15. Aug", "63 bpm", "77,9 kg", "geprüft"),
    ("Sa, 16. Aug", "61 bpm", "77,8 kg", "vorläufig"),
)


def _style() -> None:
    st.html(
        """
        <style>
        .block-container {max-width: 1180px; padding-bottom: 7rem;}
        [data-testid="stMetric"] {border: 1px solid #d9e1e8; border-radius: .75rem; padding: .7rem;}
        .st-key-prototype_switcher {
          position: fixed; z-index: 9999; left: 50%; bottom: 1.1rem; transform: translateX(-50%);
          width: 23rem; padding: .45rem .7rem; border-radius: 999px;
          background: #14212b; box-shadow: 0 8px 30px #0005;
        }
        .st-key-prototype_switcher p {color: white; text-align: center; margin-top: .35rem;}
        .st-key-prototype_switcher button {border-radius: 999px; min-height: 2.2rem;}
        .status-strip {display:flex; gap:.45rem; flex-wrap:wrap; margin:.3rem 0 1rem;}
        .status-strip span {border:1px solid #b7c6d3; border-radius:999px; padding:.25rem .65rem;}
        .eyebrow {font-size:.75rem; letter-spacing:.08em; text-transform:uppercase; color:#607484;}
        .sparkline {border:1px solid #d9e1e8; border-radius:.75rem; padding:.7rem; margin:.5rem 0;}
        .sparkline strong {display:block; margin-bottom:.25rem;}
        </style>
        """
    )


def _sparkline(label: str, values: tuple[float, ...]) -> None:
    low, high = min(values), max(values)
    span = high - low or 1
    points = " ".join(
        f"{10 + index * 700 / (len(values) - 1):.1f},{140 - (value - low) * 120 / span:.1f}"
        for index, value in enumerate(values)
    )
    st.html(
        f"""
        <div class="sparkline"><strong>{label}</strong>
        <svg viewBox="0 0 720 150" role="img" aria-label="{label}">
          <line x1="10" y1="140" x2="710" y2="140" stroke="#d9e1e8" />
          <polyline points="{points}" fill="none" stroke="#296a8a" stroke-width="4"
            stroke-linecap="round" stroke-linejoin="round" />
        </svg></div>
        """
    )


def _set_query_param(key: str, value: str) -> None:
    st.query_params[key] = value
    st.rerun()


def _switcher(variant: str) -> None:
    keys = tuple(_VARIANTS)
    index = keys.index(variant)
    with st.container(key="prototype_switcher"):
        previous, label, following = st.columns((1, 5, 1), vertical_alignment="center")
        if previous.button("←", key="variant-previous", width="stretch"):
            _set_query_param("variant", keys[(index - 1) % len(keys)])
        label.markdown(f"**{variant} — {_VARIANTS[variant]}**")
        if following.button("→", key="variant-next", width="stretch"):
            _set_query_param("variant", keys[(index + 1) % len(keys)])


def _status() -> None:
    st.html(
        '<div class="status-strip"><span>Aktuell</span><span>Daten: vorläufig · 2 Gründe</span>'
        '<span>Modellreife: belastbar</span></div>'
    )


def _display_period(key: str) -> str:
    period = st.segmented_control(
        "Anzeigezeitraum",
        ("1 Woche", "2 Wochen", "1 Monat", "3 Monate"),
        default="1 Monat",
        key=key,
    )
    st.caption("Ändert nur die sichtbare Projektion — kein neuer Modelllauf.")
    return period or "1 Monat"


def _analysis_launcher(key: str) -> None:
    with st.form(f"analysis-{key}"):
        st.markdown("#### Neuen Modelllauf starten")
        start, end = st.columns(2)
        start.date_input("Analysezeitraum von", date(2025, 8, 16), key=f"start-{key}")
        end.date_input("Analysezeitraum bis", date(2026, 8, 16), key=f"end-{key}")
        definition = st.selectbox(
            "Eingebaute Analysedefinition",
            (
                "Kurzfristiges Verzögerungsprofil · Tag 1-7",
                "Langfristiges Verzögerungsprofil · Tag 1-30",
            ),
            key=f"definition-{key}",
        )
        submitted = st.form_submit_button("Analyse bewusst starten", type="primary")
    if submitted:
        st.session_state[f"run-{key}"] = definition
    if run := st.session_state.get(f"run-{key}"):
        st.success(f"Modelllauf abgeschlossen: {run}")
        st.caption("Run v04-demo-018 · Snapshot synthetic-042 · 16.08.2026, 14:32")


def _lag_profiles(key: str) -> None:
    short, long = st.tabs(("Kurzfristig · Tag 1-7", "Langfristig · Tag 1-30"))
    with short:
        st.caption("Eigenständiger Modelllauf · belastbar · 327 vollständige Tage")
        _sparkline("bpm je 100 kcal", (-0.28, -0.21, -0.12, -0.05, 0.01, 0.03, 0.02))
    with long:
        st.caption("Eigenständiger Modelllauf · explorativ · Stabilitätskriterium nicht bestanden")
        values = tuple(round(-0.18 * (0.88**day) + (day % 5 - 2) * 0.008, 3) for day in range(30))
        _sparkline("bpm je 100 kcal", values)
    with st.expander("Methodik und Diagnostik"):
        st.write(
            "Gemeinsames Distributed-Lag-Modell mit Glättung und Ridge; Unsicherheit per "
            "zeitabhängigem Bootstrap. Assoziationen sind keine Wirkungen."
        )
        st.code("Definition: activity-lag-short-v04\nSnapshot: synthetic-042\nBootstrap: 1.000")


def _weight_analysis(key: str) -> None:
    window = st.segmented_control(
        "Gewichtstrendfenster",
        ("1 Woche", "2 Wochen", "1 Monat", "3 Monate"),
        default="1 Monat",
        key=f"weight-window-{key}",
    )
    _sparkline("Gewicht (kg)", (78.8, 78.7, 78.5, 78.6, 78.3, 78.1, 77.8))
    left, middle, right = st.columns(3)
    left.metric("Veränderungsrate", "-0,42 kg/Woche")
    middle.metric("Geglättetes Niveau", "77,9 kg")
    right.metric("Energiebilanz", "-210 kcal/Tag")
    st.caption(
        f"Perspektive: {window}. Energiebilanz ist Anzeigegröße, nicht einziges Modellmerkmal."
    )


def _day_drilldown(key: str) -> None:
    st.markdown("#### Tages-Drill-down")
    columns = st.columns(7)
    for index, day in enumerate(_DAYS):
        if columns[index].button(day[0].split(", ")[1], key=f"day-{key}-{index}"):
            st.session_state[f"day-{key}"] = index
    selected = _DAYS[st.session_state.get(f"day-{key}", 6)]
    st.info(
        f"{selected[0]} · Apple-Ruhepuls {selected[1]} · "
        f"Tagesgewicht {selected[2]} · {selected[3]}"
    )
    source, context, quality = st.columns(3)
    source.caption("Quellen")
    source.write("Apple Watch · Waage")
    context.caption("Kontext")
    context.write("7 h 42 min Schlaf · Stress durchschnittlich")
    quality.caption("Datenqualität")
    quality.write("Aktivität 19 min Abdeckungslücke")


def _support_area(area: str, key: str) -> None:
    st.subheader(area)
    if area == "Import & Datenprüfung":
        st.metric("Offene Datenprüffälle", "2")
        st.dataframe(
            {"Tag": ("12.08.2026", "16.08.2026"), "Grund": ("Quellenkonflikt", "Abdeckungslücke")},
            hide_index=True,
            width="stretch",
        )
    elif area == "Kontext & Medikamente":
        st.write(
            "Krankheit, Stress und Medikamente bleiben sichtbarer Kontext — "
            "keine Ursachenbehauptung."
        )
        st.dataframe(
            {
                "Kontext": ("Stress", "Schlaf", "Medikament"),
                "Heute": ("durchschnittlich", "7 h 42 min", "planmäßig angenommen"),
            },
            hide_index=True,
            width="stretch",
        )
    elif area == "Methodik, Einstellungen & Backup":
        st.write("Versionierte Methodik, Datenmodus und Sicherung an einem betrieblichen Ort.")
        st.code("Datenmodus: synthetisch\nDatenspeicher: synthetic-042\nDefinitionen: eingebaut")
        st.button("Metadatensicherung vorbereiten", key=f"backup-{key}")


def render_variant_a() -> None:
    st.caption("VARIANTE A · BEREICH ZUERST")
    area = st.sidebar.radio("Bereich", _AREAS, key="area-a")
    st.title(area)
    _status()
    _display_period("display-a")
    if area == "Gesamtübersicht":
        left, right = st.columns((2, 1))
        with left:
            st.subheader("Outcomes im selben Zeitraum")
            _sparkline("Apple-Ruhepuls", (64, 62, 63, 60, 61, 59, 61))
            _sparkline("Gewichtstrend", (78.6, 78.5, 78.4, 78.2, 78.1, 78.0, 77.9))
        with right:
            st.metric("Trendzusammenhang", "r = 0,34")
            st.metric("Abweichungszusammenhang", "r = -0,08")
        _day_drilldown("a")
    elif area == "Aktivität & Ruhepuls":
        _lag_profiles("a")
        _analysis_launcher("a")
    elif area == "Gewicht & Ernährung":
        _weight_analysis("a")
        _analysis_launcher("a-weight")
    else:
        _support_area(area, "a")


def render_variant_b() -> None:
    st.caption("VARIANTE B · ANALYSE-COCKPIT")
    st.title("HealthLab Cockpit")
    area = st.pills("Arbeitsbereich", _AREAS, default=_AREAS[0], key="area-b") or _AREAS[0]
    _status()
    main, rail = st.columns((3, 1), gap="large")
    with rail:
        st.markdown("### Nächste Aktion")
        st.warning("2 Datenprüffälle offen")
        with st.popover("Analyse starten", width="stretch"):
            _analysis_launcher("b")
        st.markdown("### Ergebnis")
        st.metric("Letzter Modelllauf", "14:32")
        st.caption("Anzeigeänderungen lassen ihn unverändert.")
    with main:
        st.subheader(area)
        _display_period("display-b")
        if area == "Gesamtübersicht":
            outcomes, patterns = st.tabs(("Outcomes", "Zusammenhänge"))
            with outcomes:
                _sparkline("Ruhepuls", (64, 62, 63, 60, 61, 59, 61))
                _sparkline("Gewicht", (78.6, 78.5, 78.4, 78.2, 78.1, 78.0, 77.9))
            with patterns:
                st.metric("Trendzusammenhang", "r = 0,34")
                st.metric("Abweichungszusammenhang", "r = -0,08")
            _day_drilldown("b")
        elif area == "Aktivität & Ruhepuls":
            _lag_profiles("b")
        elif area == "Gewicht & Ernährung":
            _weight_analysis("b")
        else:
            _support_area(area, "b")


def render_variant_c() -> None:
    st.caption("VARIANTE C · TAGESJOURNAL")
    st.title("Heute verstehen, Muster getrennt prüfen")
    today, patterns, data, tools = st.tabs(("Heute", "Muster", "Daten", "Werkzeuge"))
    with today:
        _display_period("display-c")
        _day_drilldown("c")
        st.subheader("Verläufe rund um den gewählten Tag")
        _sparkline("Apple-Ruhepuls", (64, 62, 63, 60, 61, 59, 61))
        _sparkline("Gewichtstrend", (78.6, 78.5, 78.4, 78.2, 78.1, 78.0, 77.9))
    with patterns:
        lens = st.segmented_control(
            "Analyse",
            ("Aktivität & Ruhepuls", "Gewicht & Ernährung", "Outcome-Zusammenhänge"),
            default="Aktivität & Ruhepuls",
            key="lens-c",
        )
        _status()
        if lens == "Gewicht & Ernährung":
            _weight_analysis("c")
        else:
            _lag_profiles("c")
        _analysis_launcher("c")
    with data:
        left, right = st.columns(2)
        with left:
            _support_area("Import & Datenprüfung", "c")
        with right:
            _support_area("Kontext & Medikamente", "c")
    with tools:
        _support_area("Methodik, Einstellungen & Backup", "c")
        st.markdown("#### Die sechs V0.4-Bereiche")
        st.write(" · ".join(_AREAS))


def render_prototype() -> None:
    """Render the disposable V0.4 navigation prototype with placeholder data."""

    st.set_page_config(page_title="PROTOTYPE · HealthLab V0.4", layout="wide")
    _style()
    variant = st.query_params.get("variant", "A").upper()
    if variant not in _VARIANTS:
        variant = "A"
    st.warning(
        "Wegwerfprototyp mit Platzhalterdaten — keine echten Gesundheitsdaten oder Modellläufe."
    )
    {"A": render_variant_a, "B": render_variant_b, "C": render_variant_c}[variant]()
    _switcher(variant)


if __name__ == "__main__":
    render_prototype()
