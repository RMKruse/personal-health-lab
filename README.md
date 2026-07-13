# personal-health-lab
A privacy-first platform for exploring, modeling, and visualizing longitudinal personal health data.

## V0.1-Grundgerüst starten

Voraussetzung ist Python 3.12 oder neuer sowie [`uv`](https://docs.astral.sh/uv/). Ein frischer
Checkout wird mitsamt Entwicklungswerkzeugen so installiert:

```bash
uv sync --locked --all-groups
```

Die leere Übersicht lässt sich im synthetischen Modus über das Production-CLI laden. Synthetische
und reale Daten erhalten bewusst zwei verschiedene Speicherorte:

```bash
uv run healthlab \
  --mode synthetic \
  --synthetic-store "$HOME/.local/share/healthlab/synthetic" \
  --real-store "$HOME/.local/share/healthlab/real" \
  overview
```

Für Automatisierung steht dasselbe Ergebnis als versioniertes JSON bereit:

```bash
uv run healthlab \
  --mode synthetic \
  --synthetic-store "$HOME/.local/share/healthlab/synthetic" \
  --real-store "$HOME/.local/share/healthlab/real" \
  overview --json
```

Die drei Produktionsbefehle sind `import <export.zip>`, `analyze` und `overview`; jeder
akzeptiert `--json`. Ihre JSON-Ausgaben folgen dem mitinstallierten Schema
`personal_health_lab/adapters/cli/schemas/output-1.0.schema.json`. Effektive Speicherpfade
erscheinen darin ausschließlich als `<redacted>`.

Exit-Code `0` bedeutet Erfolg oder No-op (`duplicate`, `reused`), `3` ein erwartbar
unvollständiges Ergebnis (`rejected`, `quarantined`, `store_busy`, `insufficient_data`,
`unstable`), `2` einen Verwendungs- oder Konfigurationsfehler und `1` einen technischen
Fehler.

Streamlit verwendet dieselbe `HealthLab`-Schnittstelle und dieselben Speicherorte:

```bash
HEALTHLAB_MODE=synthetic \
HEALTHLAB_SYNTHETIC_STORE="$HOME/.local/share/healthlab/synthetic" \
HEALTHLAB_REAL_STORE="$HOME/.local/share/healthlab/real" \
uv run streamlit run src/personal_health_lab/adapters/streamlit/app.py
```

## Reproduzierbare synthetische Health-Exporte

Der getrennte Development-Adapter erzeugt die versionierten Szenarien `lag-signal-v1` und
`null-v1`. Das erste enthält eine dokumentierte negative Lag-1-Assoziation zwischen aktiver
Energie und dem Apple-Ruhepuls des folgenden messlokalen Tages; das zweite enthält kein
eingebautes Verzögerungssignal. Beide Standardexporte decken 365 messlokale Tage ab und enthalten
Fixtures für die Europe/Berlin-Zeitumstellungen sowie Reisen nach America/New_York und Asia/Tokyo.

```bash
uv run healthlab-dev generate \
  --scenario lag-signal-v1 \
  --seed 42 \
  --destination .scratch/lag-signal-v1 \
  --json

uv run healthlab-dev generate \
  --scenario null-v1 \
  --seed 42 \
  --destination .scratch/null-v1 \
  --json
```

Jedes Ziel enthält `apple-health-export.zip`, `scenario-metadata.json` und `checksums.sha256`.
Gleiche Szenarioversion, gleicher Seed und gleiche Optionen erzeugen byte-identische Dateien.
Rauschen und Missingness lassen sich bei Bedarf explizit setzen:

```bash
uv run healthlab-dev generate \
  --scenario lag-signal-v1 \
  --seed 73 \
  --destination .scratch/lag-with-missingness \
  --active-energy-noise-standard-deviation 40 \
  --resting-heart-rate-noise-standard-deviation 0.5 \
  --missing-active-energy-probability 0.05 \
  --missing-resting-heart-rate-probability 0.02
```

Missingness-Wahrscheinlichkeiten müssen kleiner als `1` sein. Der Generator wählt daraus pro
Datentyp und Seed deterministisch `floor(Wahrscheinlichkeit × 365)` fehlende Tage; die tatsächlich
realisierten Anzahlen stehen zusätzlich in den Szenario-Metadaten.

`healthlab-dev` besitzt keine Produktions- oder Real-Store-Konfiguration und verweigert Ziele
innerhalb eines bestehenden HealthLab-Datenspeichers.

Die Qualitätsprüfungen laufen lokal mit:

```bash
uv run ruff check .
uv run mypy
uv run pytest
```

## Sicherheitsgrenzen für Health-Importe

`RuntimeConfig` begrenzt jedes nicht vertrauenswürdige ZIP vor dem XML-Import. Die versionierten
Standardwerte sind `512 MiB` für Paket-, Eintrags- und gesamte entpackte Größe, `8` Einträge sowie
ein maximales Kompressionsverhältnis von `200`. Sie lassen sich pro Lauf über
`max_import_package_bytes`, `max_import_entries`, `max_import_entry_bytes`,
`max_import_uncompressed_bytes` und `max_import_compression_ratio` verkleinern oder vergrößern.
Größen und Anzahlen müssen positive Ganzzahlen sein; das Verhältnis muss endlich und mindestens
`1` sein. Überschreitungen sowie Traversal, Symlinks, unerwartete Dateien, NUL-/UTF-16-XML, DTDs
und XML-Entitäten werden einheitlich mit `invalid_health_export` abgewiesen.

## Dokumentation

- [Architektur und versionierte Roadmap](./HEALTH_ANALYTICS_ARCHITECTURE.md)
- [Fachglossar](./CONTEXT.md)
- [Technisches und betriebliches Glossar](./docs/TECHNICAL_GLOSSARY.md)
- [Architekturentscheidungen](./docs/adr/)

## V0.1-Modulabhängigkeiten

```mermaid
flowchart LR
    CLI["CLI-Adapter"] --> APP["application"]
    UI["Streamlit-Adapter"] --> APP
    APP --> IMP["health_import"]
    APP --> ANA["resting_hr_analysis"]
    APP --> OVR["overview"]
    IMP --> STORE["storage"]
    ANA --> STORE
    OVR --> STORE
    IMP --> DATA["health_data"]
    ANA --> DATA
    STORE --> DATA
    DCLI["Development-CLI-Adapter"] -.-> SYN["synthetic_export"]
```

Verbindliche Regeln:

- Adapter hängen vom Anwendungsmodul ab, niemals umgekehrt.
- `health_data` hängt von keinem anderen Projektmodul ab.
- `storage` kennt kanonische Typen, aber keine Analyse- oder UI-Logik.
- Der Development-CLI-Adapter darf `synthetic_export` verwenden; das reale Anwendungsmodul und der reale Datenmodus nicht.
- Neue Abhängigkeiten dürfen keinen Zyklus erzeugen.

## V0.1-Interface

```python
with HealthLab.open(runtime_config) as app:
    app.import_health_export(package_path)
    app.run_resting_hr_analysis(config)
    app.load_overview(selection)
```

CLI, Streamlit und End-to-End-Tests verwenden dasselbe Interface. Import-, Speicher-, Qualitäts- und Analyselogik bleibt in den tiefen internen Modulen.
