# Apple-Health-Ruheenergievertrag für V0.4

## Entscheidung

V0.4 übernimmt ausschließlich HealthKit-`basalEnergyBurned` als **Apple-Ruheenergieschätzung**.
Der kanonische Typ heißt `apple_resting_energy`, die kanonische Einheit ist `kcal`. Ein Tageswert
ist die Summe der zeitanteilig auf den messlokalen Kalendertag entfallenden, wirksamen
kumulativen Intervalle. Er ist nur dann analysetauglich, wenn der tatsächliche lokale Tag
(23, 24 oder 25 Stunden) ohne Lücke abgedeckt ist und keine ungelöste Identitäts-, Quellen-
oder Überlappungsfrage besteht. Fehlende oder unvollständige Tage bleiben fehlend;
sie werden weder zu null gesetzt noch hochgerechnet.

Die Anzeige nennt den Wert stets **„Apple-Ruheenergie (Schätzung)“**. Apple beschreibt
`basalEnergyBurned` als kumulative Energie für grundlegende Körperfunktionen
([Apple: `basalEnergyBurned`](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/basalenergyburned)).
Apple Watch berechnet Kalorien unter anderem aus persönlichen Angaben; Apple veröffentlicht aber
weder eine Basalgleichung noch Unsicherheit oder einen basalenergiespezifischen Messstatus
([Apple: Messgenauigkeit](https://support.apple.com/en-us/105002),
[Apple: Kalibrierung](https://support.apple.com/en-us/105048)). Der Wert ist daher keine direkte
Kalorimetrie und keine gemessene physiologische Wahrheit.

## Evidenzgrenze

Apple dokumentiert für den manuellen Export nur, dass alle Gesundheits- und Fitnessdaten als XML
exportiert werden. Ein öffentliches, versioniertes XSD/DTD oder ein Vertrag für `Record`-Attribute,
Quellenpriorität, Duplikate oder basalenergiespezifische Exportformen ist nicht veröffentlicht
([Apple: Health-Daten exportieren](https://support.apple.com/guide/iphone/share-your-health-data-iph5ede58c3d/ios)).
HealthKit-API-Semantik darf deshalb nicht als XML-Garantie ausgegeben werden.

Auch das Repository enthält keine reale oder sanitärisierte Ruheenergie-Exportprobe und kein
`BasalEnergyBurned`-Fixture. Der bestehende V0.3-Vertrag verschiebt die Prüfung realer
XML-Schreibweisen ausdrücklich in den V0.5-Pilot
([V0.3-Exportvertrag](apple-health-v03-export-contract.md)). Der aktuelle Import kennt den Typ
nicht in seiner Positivliste und katalogisiert ihn deshalb als unbekannt; `CanonicalHealthType`
besitzt noch keinen Ruheenergietyp
([`health_import`](../../src/personal_health_lab/health_import/__init__.py),
[`health_data`](../../src/personal_health_lab/health_data/__init__.py)). Die folgende XML-Form ist
somit ein **eigener, synthetisch zu testender V0.4-Vertrag**, kein behauptetes Apple-Schema.

## Kanonischer Mengen- und Einheitenvertrag

| Feld | V0.4-Vertrag |
|---|---|
| HealthKit-Quelle | `HKQuantityTypeIdentifierBasalEnergyBurned` (Swift: `.basalEnergyBurned`) |
| kanonischer Typ | `apple_resting_energy` |
| Wertsemantik | algorithmisch geschätzte, über das Sampleintervall kumulierte Ruheenergie |
| kanonische Einheit | `kcal` |
| zunächst akzeptierte XML-Einheiten | `kcal` und `kJ` |
| Umrechnung | `kcal → kcal`; `kJ → kcal` durch Division durch `4.184` |
| Originalprovenienz | Originalwert, Originaleinheit, Start, Ende, Erzeugungszeitpunkt, Quelle, Quellversion, Gerät und erkannte Metadaten bleiben erhalten |

HealthKit klassifiziert Ruheenergie als kumulative Energiedimension; kumulative Größen sind über
einen Zeitraum summierbar
([Apple: kumulative Aggregation](https://developer.apple.com/documentation/healthkit/hkquantityaggregationstyle/cumulative)).
Ein Quantity-Sample mit späterem Ende ist ein Intervall und sein Wert keine momentane
`kcal/h`-Rate
([Apple: Samples](https://developer.apple.com/documentation/healthkit/samples)). HealthKit kann
ältere first-party Workout-Samples einschließlich Basalenergie verdichten und aufeinanderfolgende
kumulative Mengen gleicher Rate zu einem längeren, summengleichen Sample zusammenfassen. Die
Rohgranularität ist folglich nicht stabil
([Apple: condensed workout samples](https://developer.apple.com/documentation/healthkit/accessing-condensed-workout-samples)).

Apple HealthKit unterstützt kompatible Energieeinheiten und Umrechnung; `1 Cal = 1 kcal = 4184 J`
([Apple: `HKUnit`](https://developer.apple.com/documentation/healthkit/hkunit/init(from:)-9qont),
[Apple: large calorie](https://developer.apple.com/documentation/healthkit/hkunit/largecalorie())).
V0.4 akzeptiert im XML dennoch nur `kcal` und `kJ`, weil dies die bereits geschlossene
Energie-Konvention des Repositorys fortsetzt. Andere kompatible API-Einheiten wie `J` oder `Cal`
werden im Exportadapter nicht geraten, sondern als nicht unterstützte `(type, unit)`-Kombination
katalogisiert; eine belegte reale Exportform kann die versionierte Mappingliste später erweitern.
Nur endliche Werte werden importiert. Negative Werte und eine beobachtete Null bleiben als
Originaldaten erhalten, öffnen aber einen Plausibilitätsfall; Abwesenheit ist niemals Null.

## Quelle, Identität und Überlappung

`sourceRevision` bezeichnet die App oder das direkt schreibende Gerät samt Version; `device`
bezeichnet das Gerät, das Daten erzeugt hat. Schreiber und Erzeuger sind daher getrennte
Provenienzdimensionen
([Apple: `sourceRevision`](https://developer.apple.com/documentation/healthkit/hkobject/sourcerevision),
[Apple: `device`](https://developer.apple.com/documentation/healthkit/hkobject/device)). Die
Quellversion ist keine Messungsversion.

V0.4 verwendet folgende Reihenfolge:

1. Die vorhandene logische Identitäts- und Versionsauflösung entfernt wiederholte
   Exportvorkommen und wählt genau eine wirksame Version je logischer Messung. Sync-Identifier ist
   nur ein starker Kandidat, keine universelle Identität
   ([Apple: Sync-Identifier](https://developer.apple.com/documentation/healthkit/hkmetadatakeysyncidentifier),
   [Repository-Recherche](apple-health-export-identity-version-deletion.md)).
2. Für den Analyseeingang sind nur durch eine **versionierte, exakte Quellenregel** als
   first-party Apple Watch klassifizierte Samples zulässig. V0.4 übernimmt dafür zunächst die
   vorhandene exakte Klasse `sourceName == "Apple Watch" && device == "Apple Watch"`.
   Andere oder unklare Quellen bleiben kanonisch gespeichert, tragen aber nicht zum Tageswert bei
   und erzeugen `ineligible_source` beziehungsweise `unknown_source`.
3. Verschiedene wirksame Samples desselben Typs mit positiver zeitlicher Überlappung werden nie
   automatisch priorisiert, beschnitten oder addiert. Sie öffnen einen Quellenkonflikt; alle
   betroffenen Tage bleiben bis zu Korrektur oder lokalem Ausschluss nicht analysetauglich.
   Angrenzende Intervalle (`end == start`) überlappen nicht.
4. Nicht überlappende zulässige Intervalle, auch bei einem nachvollziehbaren Gerätewechsel,
   dürfen gemeinsam einen Tag abdecken.

HealthKit-Statistiken führen Quellen standardmäßig zusammen, und die Health-App besitzt eine vom
Benutzer veränderbare Quellenpriorität
([Apple: `HKStatistics`](https://developer.apple.com/documentation/healthkit/hkstatistics),
[Apple: Health-Quellen priorisieren](https://support.apple.com/en-us/108779)). Der XML-Export
bewahrt diese Reihenfolge und Apples intervallgenauen Merge-Algorithmus nicht als dokumentierten
Vertrag. Eine vermeintliche Nachbildung wäre daher weder prüfbar noch reproduzierbar. Die obige
konservative Regel verwendet stattdessen die vorhandene Korrektur-/Ausschluss-Seam des Projekts.

## Messlokaler Tag, Zeitzone und DST

Der fachliche Tag ist das halboffene Intervall `[00:00, nächstes 00:00)` in der **am Sampleort
geltenden benannten Zeitzone**. Tagesgrenzen werden kalenderbasiert erzeugt, nicht durch Addition
von 24 Stunden. Damit dauern DST-Tage tatsächlich 23 oder 25 Stunden. Apple selbst bildet
Statistikintervalle aus Ankerdatum und `DateComponents`; `HKMetadataKeyTimeZone` kann die
Benutzerzeitzone bei Erzeugung eines Objekts festhalten
([Apple: Statistics Collection](https://developer.apple.com/documentation/healthkit/executing-statistics-collection-queries),
[Apple: Zeitzonenmetadatum](https://developer.apple.com/documentation/healthkit/hkmetadatakeytimezone)).
Apple definiert jedoch keinen kanonischen Ruheenergietag und keine Reise- oder DST-Regel.

Für jedes positive Intervall wird der kanonische Wert anhand der tatsächlichen UTC-Dauer auf die
geschnittenen messlokalen Tagesintervalle verteilt:

```text
Tagesbeitrag = Sample-kcal × UTC-Sekunden(Sample ∩ lokaler Tag)
                            / UTC-Sekunden(Sample)
```

Diese proportionale Zuordnung ist die versionierte Projektregel
`resting-energy-day-allocation/v1`. Sie erhält die Gesamtsumme und verträgt verdichtete Samples;
Apple dokumentiert jedoch keine allgemeine Aufteilungsregel für ein kumulatives Sample an einer
Tagesgrenze. Deshalb bleiben Originalsample und Ableitungsversion verlinkt. Ein positiver
Punktsample (`start == end`) kann nicht zeitanteilig zugeordnet werden, trägt nicht zur
Tagesabdeckung bei und öffnet `missing_energy_interval`.

Die Import-Seam speichert zusätzlich das optionale benannte Zeitzonenmetadatum als typisiertes
Provenienzfeld. Fehlt es, bleibt der bereits aus dem Offset des Original-Startzeitpunkts abgeleitete
`measurement_local_day` erhalten. Für einen vollständig innerhalb dieses Datums liegenden Sample
ist die Zuordnung weiterhin eindeutig. Ein offset-only Sample über eine lokale Tagesgrenze oder
ein Tag mit Offsetwechsel hat jedoch keine belegte benannte Kalenderzone: die betroffenen Tage
erhalten `timezone_basis_missing` und sind nicht vollständig. Die aktuelle Mac-Zeitzone darf
historische Tage niemals rückdatieren.

## Abdeckung und Vollständigkeit

Apple veröffentlicht für Ruheenergie weder Erzeugungstakt noch erwartete Samplezahl, Wear-Time,
Lückentoleranz, Backfill-Frist oder Vollständigkeitsflag. Außerdem kann fehlende HealthKit-
Leseberechtigung wie Abwesenheit wirken
([Apple: HealthKit autorisieren](https://developer.apple.com/documentation/healthkit/authorizing-access-to-health-data),
[Apple: HealthKit lesen](https://developer.apple.com/documentation/healthkit/reading-data-from-healthkit)).
Vollständigkeit ist daher eine V0.4-Beobachtungsregel, keine Apple-Garantie.

Ein Ruheenergietag ist genau dann `complete`, wenn:

- eine eindeutige benannte lokale Tagesgrenze vorliegt;
- die Vereinigung aller zulässigen, wirksamen positiven Intervalle das gesamte tatsächliche
  lokale Tagesintervall ohne Lücke abdeckt; V0.4 überbrückt keine unbeobachtete Lücke;
- kein positiver Punktsample und keine ungelöste Überlappung, Identitätsmehrdeutigkeit oder
  Quellenfrage den Tag betrifft; und
- alle beitragenden Werte und Einheiten erfolgreich kanonisiert wurden.

Der abgeleitete Tagesvertrag enthält mindestens `value: float | None`, `unit: kcal`,
`is_complete`, geschlossene `incomplete_reasons`, Abdeckungssegmente, beitragende logische und
Version-IDs, Quellenklasse/-version, Zeitzonenbasis, Ableitungsversion und Qualitätsstatus.
Teilwerte dürfen zur Prüfung angezeigt werden, aber das Gewichtsmodell erhält `value=None`, sobald
`is_complete == false`. `complete` bedeutet ausschließlich lückenlos und konfliktfrei beobachtet;
es bedeutet weder genau, physiologisch korrekt noch gemessen. Beobachtungsvollständigkeit und
Datenqualität bleiben getrennt: Ein `complete`-Tag mit einem offenen Plausibilitätsfall behält
seinen wirksamen Analysewert, trägt `quality_status = provisional` und macht davon abhängige
Ergebnisse nach dem bestehenden Projektvertrag vorläufig.

## Datenqualität und Schätzsemantik

HealthKit stellt generische Metadaten für Benutzereingabe, Zeitzone, Algorithmusversion und den
frühesten für eine Schätzung verwendeten Zeitpunkt bereit, verlangt sie aber nicht für
Basalenergie
([Apple: Metadata Keys](https://developer.apple.com/documentation/healthkit/metadata-keys),
[Apple: user-entered](https://developer.apple.com/documentation/healthkit/hkmetadatakeywasuserentered),
[Apple: algorithm version](https://developer.apple.com/documentation/healthkit/hkmetadatakeyalgorithmversion),
[Apple: earliest estimate data](https://developer.apple.com/documentation/healthkit/hkmetadatakeydateofearliestdatausedforestimate)).
V0.4 bewahrt vorhandene Werte typisiert; ihr Fehlen wird als fehlende Provenienz und nicht als
Importfehler behandelt. Manuell eingegebene Ruheenergie ist für die Apple-Schätzung nicht
zulässig. Quelle, Gerät oder digitale Signatur belegen Herkunft, nicht Richtigkeit.

Für Analyse und Oberfläche gelten deshalb:

- `estimate_semantics = "apple_algorithmic_estimate"` ist Teil des versionierten Tagesvertrags;
- keine medizinische oder kalorimetrische Genauigkeitsbehauptung;
- Quellen-/Geräte-/Algorithmuswechsel bleiben sichtbar und können als Sensitivität oder
  Qualitätsgrund verwendet werden;
- offene Prüffälle und unvollständige Tage werden nicht durch Imputation verborgen;
- Energiebilanz bleibt eine abgeleitete Anzeige, während Ernährung, aktive Energie und
  Apple-Ruheenergie im Gewichtsmodell getrennte Eingänge bleiben.

## Kompatibilität mit der geschlossenen typisierten Import-Seam

Die kleinste tragfähige Erweiterung bleibt innerhalb der bestehenden Architekturentscheidungen
([ADR 0006](../adr/0006-use-typed-canonical-health-records.md),
[ADR 0023](../adr/0023-isolate-healthkit-behind-import-mapping.md)):

1. `CanonicalHealthType.APPLE_RESTING_ENERGY = "apple_resting_energy"` ergänzen und fest an
   `CanonicalUnit.KILOCALORIE` binden.
2. Ausschließlich in `health_import` das XML-Quellmapping
   `HKQuantityTypeIdentifierBasalEnergyBurned → APPLE_RESTING_ENERGY` mit `kcal`/`kJ`
   ergänzen; Originalwert und -einheit bleiben in `HealthProvenance`.
3. `CanonicalHealthRecord`, vorhandene logische Identität, Messungsversionen,
   Exportvorkommen, Quellauflösung, Korrekturen und Ausschlüsse unverändert wiederverwenden.
4. Nur das optionale benannte Zeitzonen- und Schätzmetadatum als konkrete typisierte
   Provenienzfelder ergänzen; kein freies Metadata-Dictionary und kein neuer generischer
   Observation-Typ.
5. Tagesableitung, Quellenklassifikation und Vollständigkeit als eigene versionierte Projektion
   hinter der Application-Seam halten. HealthKit-Strings gelangen weder in Speicherungsausgaben
   noch in Analysen oder Adapter.

Nicht erforderlich sind eine zweite Importpipeline, eine dynamische Typregistrierung, die
Nachbildung von HealthKits unbekannter Quellenpriorität oder eine Stoffwechselgleichung. Der
V0.5-Pilot muss lediglich die reale XML-Schreibweise, Quellenstrings, Einheiten und vorhandenen
Metadaten gegen diesen konservativen Vertrag prüfen; Abweichungen erweitern den Importadapter,
nicht rückwirkend die behauptete Apple-Semantik.

## Offene Apple-Primärdokumentationslücken

Apple dokumentiert derzeit nicht:

- ein stabiles XML-Schema oder basalenergiespezifische Exportattribute;
- Erzeugungsalgorithmus, Eingaben, Fehler/Unsicherheit, Takt oder Backfill der Ruheenergie;
- eine verpflichtende Quellen-, Geräte-, Zeitzonen- oder Algorithmusmetadatenbelegung;
- die intervallgenaue Mehrquellen-/Duplikatauflösung von `HKStatistics`;
- die Zuordnung eines kumulativen, tagesübergreifenden Samples zu lokalen Tagen; oder
- eine fachliche Vollständigkeits- beziehungsweise Abdeckungsregel.

Diese Punkte sind deshalb oben ausdrücklich als versionierte V0.4-Projektregeln und nicht als
Apple-Eigenschaften formuliert.
