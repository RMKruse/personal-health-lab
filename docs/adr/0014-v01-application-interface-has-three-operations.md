---
status: superseded by ADR-0027
---

# Das V0.1-Anwendungs-Interface hat drei Operationen

Die externe V0.1-Seam bietet ausschließlich `import_health_export`, `run_resting_hr_analysis` und `load_overview`. `HealthLab` wird als Context Manager geöffnet und deterministisch geschlossen; ein globaler Singleton ist ausgeschlossen. Stabile Receipts, kleine versionierte Konfigurationen und ein präsentationsneutrales `Overview` verbergen Parser-, Staging-, Speicher-, Aggregations- und Modelldetails, sodass CLI, Streamlit und End-to-End-Tests denselben kleinen Test- und Nutzungsumfang verwenden. Externe Analysekonfigurationen enthalten nur benutzerrelevante Angaben; statistische Details gehören zu einer versionierten internen Analysedefinition. Öffentliche Status und Ausnahmen gehören `application`; interne Fehler werden dort einmalig übersetzt und dürfen nicht zu Adaptern durchdringen. `ImportReceipt` unterscheidet geschlossen `committed`, `duplicate`, `rejected`, `quarantined` und `store_busy`. CLI und Streamlit formatieren `Overview`, ergänzen aber keine fachliche Logik.
