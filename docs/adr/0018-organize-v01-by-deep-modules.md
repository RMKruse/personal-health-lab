# V0.1 wird nach tiefen Modulen organisiert

Die Python-Struktur folgt den Modulen `application`, `health_data`, `health_import`, `resting_hr_analysis`, `storage`, `overview`, `synthetic_export` und `adapters` statt horizontaler Packages wie `ingestion`, `validation`, `analytics` und `visualization`. Verhalten bleibt lokal hinter dem Interface des verantwortlichen Moduls; `health_data` besitzt ausschließlich die von Import, Speicherung und Analyse tatsächlich geteilten kanonischen Werttypen und Invarianten.
