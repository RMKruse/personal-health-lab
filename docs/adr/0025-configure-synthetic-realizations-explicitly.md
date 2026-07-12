# Synthetische Realisierungen explizit konfigurieren

Eine versionierte synthetische Szenariodefinition besitzt Signalform, Erzeugungsalgorithmen und
Standardwerte; Änderungen daran erzeugen eine neue Szenario-ID. Für reproduzierbare
Robustheits-Fixtures darf der Development-Adapter zusätzlich unveränderliche `GenerationOptions`
für Rauschstärken und Missingness übergeben. Die vollständige Identität einer Realisierung besteht
aus Szenario-ID, Seed und effektiven Optionen, die gemeinsam in den Szenario-Metadaten gespeichert
werden. Damit konkretisiert diese Entscheidung die in ADR 0004 verborgene Szenariodefinition und
erweitert das Generator-Interface, ohne den Generator für Produktionsadapter oder den realen
Datenmodus erreichbar zu machen.
