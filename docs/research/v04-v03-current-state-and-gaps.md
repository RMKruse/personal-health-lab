# V0.4-Iststand und Restlücken im abgeschlossenen V0.3-Stand

Stand: 2026-08-16

## Belegter Ausgangspunkt

Der abgeschlossene V0.3-Release-Stand ist Commit
[`4714878`](https://github.com/RMKruse/personal-health-lab/commit/47148780b75cc2f3eb502dc4eeb6f8cca90e2246).
Der zugehörige
[`Quality`-Lauf](https://github.com/RMKruse/personal-health-lab/actions/runs/31620275358)
hat Linux-Lint, Typprüfung, das unveränderte V0.2-Gate, die V0.3-Delta-Matrix, die vollständige
Testsuite, die macOS-Probe-Conformance und die erzeugte Acceptance-Evidence erfolgreich
abgeschlossen.

Die Untersuchung erfolgte auf `dev` bei Commit `2e13ce2`. Zwischen dem V0.3-Release-Commit und
diesem Stand gibt es keine Änderung unter `src/`; spätere Änderungen betreffen Methodendokumente,
Akzeptanztests, Matrizen und den Workflow. Damit beschreibt der untersuchte Produktionscode den
abgeschlossenen V0.3-Fähigkeitsstand. Die spätere Test- und Workflow-Fassung besitzt jedoch noch
keinen eigenen entfernten `Quality`-Nachweis.

## Kurzfazit

V0.3 hat die wiederverwendbaren technischen Seams für V0.4 bereits: ein geschlossenes typisiertes
Application-Interface, atomare Snapshot-Veröffentlichung, versionierte abgeleitete
Parquet-Familien, reproduzierbare Analyse-Läufe, zwei Adapter auf derselben Application-Seam und
eine selbstprüfende synthetische Abnahmematrix.

Nicht vorhanden ist der V0.4-Fachvertrag auf diesen Seams. Die einzige produktive Analyse ist ein
kurzfristiges Aktivenergie-Ruhepuls-Modell. V0.3-Datenfamilien sind zwar persistiert und als
Application-Projektionen sichtbar, werden aber noch nicht als gemeinsamer, versionierter
Analyseeingang verwendet. Die Oberfläche hat vier statt sechs Navigationsbereiche, und die
synthetische Ground Truth deckt nur das alte Aktivenergie-Ruhepuls-Signal beziehungsweise ein
Nullszenario ab.

## 1. Analyse

### Bereits vorhanden

- Das tiefe Modul
  [`resting_hr_analysis`](https://github.com/RMKruse/personal-health-lab/blob/47148780b75cc2f3eb502dc4eeb6f8cca90e2246/src/personal_health_lab/resting_hr_analysis/__init__.py#L59-L130)
  besitzt eine eingebaute, typisierte Analysedefinition statt frei übergebener Modellparameter.
- `lag-signal-v2` schätzt aktive Energie und Apple-Ruhepuls gemeinsam für Tag 1 bis 7 mit
  Ridge-Regularisierung, Moving-Block-Bootstrap, punktweisen Intervallen und einem simultanen Band
  ([Fit](https://github.com/RMKruse/personal-health-lab/blob/47148780b75cc2f3eb502dc4eeb6f8cca90e2246/src/personal_health_lab/resting_hr_analysis/__init__.py#L285-L437)).
- Modelllauf, Datensatz-Snapshot, Konfigurationshash, Code- und Umgebungsidentität, Dirty-Diff,
  Wiederverwendung, Datenstatus, Modellreife und historische Ergebnisse werden gemeinsam geführt
  ([Laufvertrag](https://github.com/RMKruse/personal-health-lab/blob/47148780b75cc2f3eb502dc4eeb6f8cca90e2246/src/personal_health_lab/resting_hr_analysis/__init__.py#L546-L730)).
- Unvollständige oder numerisch instabile Eingaben erzeugen keinen scheinbar erfolgreichen
  Ergebnisdatensatz. Offene Datenprüffälle und Abdeckungslücken frieren nachvollziehbare
  Statusgründe ein.

### Belegte Restlücken

- Der Fit liest ausschließlich `active_energy` und `apple_resting_heart_rate`; Workouts,
  Aktivitätsmerkmale, Schlaf, Krankheit, Stress, Medikamente, Gewicht und Ernährung erreichen das
  Modell nicht.
- Es gibt genau ein gemeinsames 1–7-Tage-Profil. Das getrennte kurzfristige 1–7- und langfristige
  1–30-Tage-Profil mit jeweils eigener Definition, Regularisierung, Diagnostik und Modellreife fehlt.
- Trainingsdauer und -energie werden nicht nach analytischer Trainingsart gruppiert; seltene Arten
  werden analytisch noch nicht unter „Sonstige“ gebündelt.
- Gewichtsniveau, Gewichtsveränderungsrate, Energiebilanz, Gewichtsmodelle sowie Trend- und
  Abweichungszusammenhang der Outcomes fehlen vollständig.
- Die aktuelle Reifeprüfung enthält unter anderem eine feste Schwelle von 180 vollständigen Tagen,
  sechs weitere einfache Schwellen und 250 Bootstrap-Resamples. Sie erfüllt damit noch nicht die
  für V0.4 entschiedene kalibrierte diagnostische Modellreife und nicht den beschlossenen
  vollständigen Bootstrap-Refit mit mindestens 2.000 Replikaten.
- Der lineare Solver ist eine kleine eigene Gauß-Jordan-Implementierung. Die V0.4-Methodenentscheidung
  sieht NumPy für die größeren penalisierten Modellmatrizen vor; der bestehende Solver ist kein zu
  erhaltender V0.4-Seam.

Konsequenz: Lebenszyklus, Provenienz und Statusmodell bleiben; Modellform, Eingangsbündel,
Diagnostik und Ergebnisstruktur werden durch
[V0.4-Analyseverträge und Ergebnisstruktur festlegen](https://github.com/RMKruse/personal-health-lab/issues/96)
neu festgelegt, gestützt durch die beiden synthetischen Kalibrierungsprototypen.

## 2. Application

### Bereits vorhanden

- `HealthLab` ist die einzige öffentliche Produktions-Seam und wird deterministisch als Context
  Manager geöffnet.
- Alle Schreibvorgänge laufen über dieselbe typisierte `preview_write`-/`execute_write`-Seam mit
  Plan-Fingerprint, Snapshot-Pinning, Bestätigung, Preflight, Writer-Lock und geschlossenem
  `WriteRequest`-Verbund
  ([WriteRequest](https://github.com/RMKruse/personal-health-lab/blob/47148780b75cc2f3eb502dc4eeb6f8cca90e2246/src/personal_health_lab/application/_application.py#L2020-L2044)).
- V0.3 veröffentlicht unveränderliche, präsentationsneutrale Auswahl- und Ergebniswerte für
  Gewicht und Ernährung, Schlaf, Aktivität und Abdeckung, Workouts, täglichen und auditierten
  Kontext sowie Medikamente. Snapshot- und Anzeigezeitraumauswahl sind bereits typisiert.
- Der Analyseauftrag wird wie andere Schreibvorgänge geplant, bindet den aktiven Snapshot und
  prüft ihn unter dem Writer-Lock erneut.

### Belegte Restlücken

- Es existiert nur `RunRestingHeartRateAnalysis`; ein geschlossener V0.4-Verbund der getrennten
  Analyseoperationen und ihrer eingebauten Definitionen fehlt.
- Das bestehende `Overview` enthält nur tägliche aktive Energie, Apple-Ruhepuls, ein aktuelles
  Ruhepulsergebnis und dessen Historie
  ([Overview](https://github.com/RMKruse/personal-health-lab/blob/47148780b75cc2f3eb502dc4eeb6f8cca90e2246/src/personal_health_lab/overview/_overview.py#L44-L59)).
  Es trägt weder die Gesamtübersicht der zwei Outcomes noch die V0.4-Detailergebnisse.
- Anzeigezeitraum und Analysezeitraum sind als getrennte Requests technisch möglich, aber ihre
  V0.4-Verträge, Standardwerte und Run-Auswahl sind nicht entschieden.
- Mehrere V0.3-Projektionen leiten Tageswerte erneut im Application-Modul aus Messungen ab. Es gibt
  noch keine gemeinsame Application-Operation, die exakt die persistierten, versionierten
  V0.3-Ableitungsfamilien als Analyseeingang bindet.

Konsequenz: Die vorhandene schmale Seam wird erweitert, nicht durch einen generischen Command- oder
Query-Bus ersetzt. Die genaue Operationsteilung gehört zu
[V0.4-Anwendungsoperationen und Adapterparität festlegen](https://github.com/RMKruse/personal-health-lab/issues/100).

## 3. Persistenz

### Bereits vorhanden

- SQLite besitzt veränderliche Metadaten, Audit, aktive Snapshot-Bindung und Analyse-Laufmetadaten;
  Parquet besitzt unveränderliche kanonische und abgeleitete Snapshot-Artefakte; DuckDB bleibt
  Abfrage- und Materialisierungsschicht.
- Snapshot-Schema 7 führt bereits getrennte hart typisierte Familien für
  `weight_nutrition_days`, `sleep_episodes`, `sleep_nights`, `activity_days`,
  `activity_coverage_segments`, `workout_features`, `daily_context`, `medication_context` und
  `derivation_lineage`
  ([Schemas](https://github.com/RMKruse/personal-health-lab/blob/47148780b75cc2f3eb502dc4eeb6f8cca90e2246/src/personal_health_lab/storage/_store.py#L125-L324)).
- Snapshot-Erzeugung, Copy-on-write-Migration, Kapazitäts-Preflight, Writer-Lock, Validierung und
  Aktivierung sind atomar. Frühere Snapshots und referenzierte Analyseartefakte bleiben erhalten.
- `analysis_runs` trägt bereits definition-, snapshot-, config-, code- und umgebungsgebundene
  Reproduzierbarkeit. Das bestehende Ruhepulsergebnis wird zuerst in Staging-Parquet geschrieben
  und danach veröffentlicht
  ([Persistenz](https://github.com/RMKruse/personal-health-lab/blob/47148780b75cc2f3eb502dc4eeb6f8cca90e2246/src/personal_health_lab/storage/_store.py#L11638-L11827)).

### Belegte Restlücken

- `load_analysis_input` liefert nur `DailyHealthSeries` aus dem aktiven Snapshot
  ([Eingang](https://github.com/RMKruse/personal-health-lab/blob/47148780b75cc2f3eb502dc4eeb6f8cca90e2246/src/personal_health_lab/storage/_store.py#L11532-L11541)).
  Die vorhandenen V0.3-Ableitungsfamilien sind noch nicht als ein reproduzierbares V0.4-Eingangsbündel
  abrufbar.
- Persistieren, Wiederverwenden und Laden sind auf `RestingHeartRateAnalysisResult` und dessen
  feste Spalten wie `estimate_per_100_kcal` zugeschnitten. Ergebnisfamilien für zwei
  Verzögerungsprofile, Gewichtstrends, Gewichtsmodelle und Outcome-Zusammenhänge fehlen.
- Es ist noch nicht entschieden, welche deterministischen Ansichten versionierte
  Leseprojektionen bleiben und welche statistischen Ergebnisse eigenständige Analyse-Läufe sind.
- Die Bindung von Analysedefinition, Eingangsfamilien, Ableitungsvertrags-IDs und Run-Ergebnissen
  muss für V0.4 geschlossen und migrationsfähig beschrieben werden.

Konsequenz: Die bestehenden Snapshot- und Analyse-Laufmechanismen sind der Wiederverwendungs-Seam;
der neue Vertrag gehört zu
[V0.4-Persistenz- und Reproduzierbarkeitsvertrag festlegen](https://github.com/RMKruse/personal-health-lab/issues/101).

## 4. Adapter

### Bereits vorhanden

- Human-CLI, versioniertes JSON 3.0 und Streamlit importieren Produktionsverträge ausschließlich
  aus `application`.
- Die CLI hat Lesewege für alle V0.3-Projektionen und einen manuellen `analyze`-Befehl. Streamlit
  plant und startet denselben Analyseauftrag über `HealthLab`.
- V0.3-Release- und Adaptertests prüfen die geschlossene öffentliche Surface sowie CLI-/Streamlit-
  Parität. Die Delta-Matrix referenziert die konkreten Adapter-Runner.
- Streamlit zeigt bereits Zeitreihen, Verzögerungsprofil, punktweise und simultane Unsicherheit,
  Methodik, Provenienz, Datenstatus und Modellreife.

### Belegte Restlücken

- Streamlit besitzt vier Bereiche: „Übersicht“, „Datenprüfung“, „Kerndaten“ sowie „Kontext &
  Medikamente“
  ([Navigation](https://github.com/RMKruse/personal-health-lab/blob/47148780b75cc2f3eb502dc4eeb6f8cca90e2246/src/personal_health_lab/adapters/streamlit/app.py#L1280-L1310)).
  Die sechs beschlossenen V0.4-Navigationsbereiche und ihre Interaktionen fehlen.
- „Kerndaten“ bündelt Gewicht, Ernährung, Schlaf, Aktivität, Workouts und Importdetails; die
  fachlichen Analyseflüsse und die Trennung von Anzeige- und Analysezeitraum sind nicht erprobt.
- CLI `analyze` und JSON-Ausgaben kennen nur das alte Ruhepulsprofil. Es gibt keine semantischen
  V0.4-Verträge oder Paritätsfälle für die neuen Operationen und Ergebnisfamilien.
- Die Gesamtübersicht mit synchronisierten Ruhepuls- und Gewichtspanels, Tagesdetails,
  Qualitätsmarkierungen sowie Trend- und Abweichungszusammenhang fehlt.

Konsequenz: Der UI-Prototyp klärt zuerst Informationsarchitektur und Interaktion; danach werden
Application-Operationen und Adapterparität festgelegt. Zuständig sind
[Sechs V0.4-Navigationsbereiche und Analyseflüsse erproben](https://github.com/RMKruse/personal-health-lab/issues/97),
[V0.4-Informationsarchitektur und Interaktionsvertrag festlegen](https://github.com/RMKruse/personal-health-lab/issues/93)
und das anschließende Paritätsticket.

## 5. Synthetische Fixtures

### Bereits vorhanden

- `healthlab-dev` erzeugt reproduzierbare Apple-Health-ähnliche ZIP-Pakete, die denselben
  untrusted Importpfad wie reale Exporte durchlaufen.
- Der Generator besitzt versionierte Szenario-ID, Seed und explizite Rausch- und
  Missingness-Optionen. `lag-signal-v1` enthält eine bekannte negative Tag-1-Assoziation,
  `null-v1` kein eingebautes Signal
  ([Szenarien](https://github.com/RMKruse/personal-health-lab/blob/47148780b75cc2f3eb502dc4eeb6f8cca90e2246/src/personal_health_lab/synthetic_export/_generator.py#L83-L97)).
- Die V0.3-Abnahme katalogisiert sechs Fixture-Familien: Core Journey, Aktivitätsabdeckung,
  Kontextworkflow, Medikamentenworkflow, Persistenzlebenszyklus und Sicherheitsgrenzen.
- Die V0.3-Delta-Matrix enthält 38 Verträge, 102 Fälle und 27 gehashte Fixtures; der Validator
  lehnt offene Referenzen und Hash-Drift ab.

### Belegte Restlücken

- Der produktive Generator erzeugt nur aktive Energie und Apple-Ruhepuls. V0.3-Kerndaten werden in
  statischen XMLs, Test-Generatoren und Rezepten ergänzt, nicht in einer kumulativen statistischen
  Szenariodefinition.
- Es fehlen bekannte Ground Truth und kontrollierte Gegenbeispiele für mehrere korrelierte
  Aktivitätsmerkmale, kurz- und langfristige Profile, analytische Trainingsarten,
  Kontextadjustierung, Gewichtstrends, Energie- und Makronährstoffmodelle sowie Trend- und
  Abweichungszusammenhänge.
- Es fehlen gezielte Szenarien für V0.4-Reifekriterien: Fehlbeobachtung, Rang/Kondition,
  Reststruktur, Blockeinfluss, Glättungs- und Blocklängenstabilität, Bootstrap-Präzision und
  Grenzunterstützung.
- Ein kumulatives V0.4-Release-Fixture, das die unveränderten V0.1–V0.3-Gates mit allen neuen
  Analysefamilien verbindet, existiert nicht.

Konsequenz: Die beiden Methodenprototypen kalibrieren zuerst die statistischen Fixtures. Der
endgültige kumulative Vertrag gehört zu
[V0.4-synthetische Abnahme und Release-Gate festlegen](https://github.com/RMKruse/personal-health-lab/issues/95).

## 6. Release-Evidenz

### Bereits vorhanden

- Die V0.3-Matrix ist selbstvalidierend und bindet Verträge, Fälle, Fixture-Hashes, Plattformen und
  Runner. Die Evidence-Datei bindet zusätzlich Git-Commit, Python, `uv.lock`, V0.2-Matrix und
  V0.2-Evidence, Kapazitätsmethoden sowie Linux- und macOS-Gates
  ([Evidence-Builder](https://github.com/RMKruse/personal-health-lab/blob/47148780b75cc2f3eb502dc4eeb6f8cca90e2246/scripts/v03_acceptance_evidence.py#L71-L127)).
- Der V0.3-Release-Commit hat einen erfolgreichen entfernten Lauf mit hochgeladenem
  `acceptance-evidence`-Artefakt. Damit ist nicht nur lokales Bestehen, sondern die konkrete
  Plattformausführung belegt.
- Das Release-Gate prüft die geschlossene Application-Surface, die vier Revision-Intents,
  unbekannte Varianten und den einzigen V0.3-JSON-Vertrag.

### Belegte Restlücken

- Matrix und Evidence-Schema kennen nur V0.3. Keine V0.4-Analysedefinition, Ergebnisfamilie,
  Navigation, semantische Adapterparität oder kumulative statistische Ground Truth ist registriert.
- Der Evidence-Builder übernimmt Gate-Status aus CI-Umgebungsvariablen, verwendet lokal jedoch den
  Default `success`. Ein lokal erzeugtes JSON allein belegt deshalb keinen entfernten Gate-Lauf;
  V0.4 muss weiterhin das CI-Artefakt als maßgeblichen Nachweis verwenden.
- Der letzte entfernte `Quality`-Nachweis pinnt den V0.3-Release-Commit. Die nachfolgenden
  Planungs-, Testmatrix- und Workflowänderungen bis zum untersuchten Stand besitzen keinen eigenen
  entfernten Lauf. Der Produktionscode ist unverändert, die Release-Evidence des aktuellen
  Repository-Stands ist damit aber nicht vollständig commit-gepinnt.

Konsequenz: V0.4 erweitert die bestehende kumulative Matrix und Evidence-Kette; ein zweites
unabhängiges Release-System ist nicht nötig.

## Wiederverwendung ohne vorgezogene Implementierung

V0.4 sollte diese vorhandenen Seams ausdrücklich weiterverwenden:

1. `HealthLab.preview_write` / `HealthLab.execute_write` und fokussierte typisierte
   Leseprojektionen.
2. Eingebaute versionierte Analysedefinitionen statt freier Formeln oder benutzerdefinierten Codes.
3. Unveränderliche Datensatz-Snapshots, Ableitungsvertrags-IDs und Parquet-Lineage.
4. Analyse-Run-/Result-IDs, Snapshot-Pinning, Reuse-Key, Code-/Umgebungshash und historische
   Ergebnisse.
5. Gemeinsame Application-Verträge für CLI, JSON und Streamlit.
6. Versionierte synthetische Szenarioidentität, realer Importpfad, Delta-Matrix und kumulative
   Acceptance-Evidence.

Nicht vorwegzunehmen sind ein generisches Analyseframework, ein allgemeiner Command-/Query-Bus,
Pluginformeln, freie Variablenkombinationen oder ein neues Release-System. Keine dieser
Abstraktionen wird durch die belegten V0.4-Restlücken verlangt.

## Abdeckung im bestehenden Wayfinder-Graphen

Alle belegten Restlücken sind bereits durch bestehende Tickets abgedeckt: Methoden und
Kalibrierungsprototypen führen zu Analyseverträgen; darauf folgen Informationsarchitektur,
Application-/Adapterparität, Persistenz/Reproduzierbarkeit, Modulgrenzen und das synthetische
Release-Gate. Aus dieser Bestandsaufnahme entsteht deshalb kein zusätzliches Ticket und kein neuer
Fog-of-war-Punkt.
