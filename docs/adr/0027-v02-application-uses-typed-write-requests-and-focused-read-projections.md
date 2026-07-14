---
status: accepted
---

# V0.2 verwendet typisierte Schreibaufträge und fokussierte Leseprojektionen

V0.2 ersetzt die benannten schreibenden `HealthLab`-Methoden durch genau eine gemeinsame `preview_write`-/`execute_write`-Seam mit einer geschlossenen Union unveränderlicher Schreibaufträge, einem gemeinsamen Plan und einem gemeinsamen Receipt. Für Lesezugriffe bleibt `application` die einzige öffentliche Seam, veröffentlicht aber statt eines anwachsenden Gesamt-`Overview` mehrere benannte präsentationsneutrale Projektionen; dadurch teilen CLI und Streamlit Zulässigkeit und Orchestrierung, ohne interne Speicherformen oder einen generischen Command-/Query-Bus offenzulegen. Diese Entscheidung ersetzt für V0.2 die öffentlichen Grenzen aus ADR-0014 und ADR-0022.
