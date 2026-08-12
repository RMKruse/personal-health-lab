# Apple-Health-Exportvertrag für V0.3

## Ergebnis

Apple dokumentiert für „Alle Gesundheitsdaten exportieren“ nur eine XML-Ausgabe, aber kein
versioniertes `export.xml`-Schema und keine Zusage, dass HealthKit-UUIDs exportiert werden
([Apple-Benutzerhandbuch](https://support.apple.com/guide/iphone/share-health-and-fitness-data-iph5ede58c3d/ios)).
HealthKit-API-Semantik und beobachtete XML-Form dürfen deshalb nicht gleichgesetzt werden. V0.3
soll den folgenden **eigenen, geschlossenen Importvertrag** mit synthetischen Fixtures festschreiben;
die Bestätigung gegen einen persönlichen Export bleibt entsprechend dem Wayfinder-Ziel ein
optionaler V0.5-Befund. Das setzt die bereits für V0.2 dokumentierte konservative Identität fort
([Recherche](apple-health-export-identity-version-deletion.md),
[ADR 0009](../adr/0009-version-source-measurements-across-exports.md)).

## XML-Hülle und Attribute

Die öffentliche Apple-Dokumentation belegt die HealthKit-Objekte und Eigenschaften, nicht deren
XML-Schreibweise. Die folgende Attributmenge ist daher der für V0.3 zu testende Parservertrag;
`Record` entspricht zusätzlich dem bereits implementierten Vertrag
([`health_import`](../../src/personal_health_lab/health_import/__init__.py)).

| Element | V0.3-Attribute | Behandlung |
|---|---|---|
| `Record` | erforderlich: `type`, `sourceName`, `startDate`, `endDate`; typabhängig erforderlich: `value`, `unit`; optional: `sourceVersion`, `device`, `creationDate`; Kinder: `MetadataEntry(key,value)` | Unterstützte numerische und Schlaf-Typen kanonisch importieren. Unbekannte Typen/Einheiten/Werte katalogisieren; Sync-Identifier und -Version erkennen, übrige Metadaten verlustfrei bewahren. |
| `Workout` | erforderlich: `workoutActivityType`, `sourceName`, `startDate`, `endDate`; optional: `duration`, `durationUnit`, `totalDistance`, `totalDistanceUnit`, `totalEnergyBurned`, `totalEnergyBurnedUnit`, `sourceVersion`, `device`, `creationDate`; `MetadataEntry` bewahren | Als eigener kanonischer Workout-Typ importieren. HealthKit bestätigt Aktivitätsart, Dauer, Distanz und aktive Energie als Workout-Eigenschaften, aber nicht diese XML-Namen ([`HKWorkout`](https://developer.apple.com/documentation/healthkit/hkworkout)). |
| `ActivitySummary` | optional: `dateComponents`, `activeEnergyBurned`, `activeEnergyBurnedGoal`, `activeEnergyBurnedUnit`, `appleExerciseTime`, `appleExerciseTimeGoal`, `appleStandHours`, `appleStandHoursGoal` | **Nur katalogisieren.** `HKActivitySummary` ist ein Tagesobjekt ([Apple](https://developer.apple.com/documentation/healthkit/hkactivitysummary)); es ersetzt keine zeitlich und nach Quelle aufgelösten Aktivitäts-Records. |
| `WorkoutStatistics`, `WorkoutEvent`, `WorkoutRoute` | Elementname, Attributnamen und Vorkommenszahl | **Nur katalogisieren.** Keine zweite kanonische Distanz-/Energiequelle und keine Route in V0.3; detaillierte Workout-Statistik ist in HealthKit separat typisiert ([Apple](https://developer.apple.com/documentation/healthkit/hkworkout/allstatistics)). |

Zeitstempel verwenden V0.3-exakt `YYYY-MM-DD HH:MM:SS ±HHMM`; Offset, Originaltext und daraus
abgeleiteter messlokaler Tag bleiben erhalten. Start und Ende sind Sample-Eigenschaften
([`HKSample`](https://developer.apple.com/documentation/healthkit/hksample)); `creationDate` ist
kein von Apple dokumentierter Revisionszähler. Fehlende optionale Attribute sind leere/fehlende
Provenienz, kein Importfehler. Unbekannte Attribute werden ignoriert, ihre Namen aber pro
Exportformat-Version diagnostisch gezählt.

## Unterstützte Quelltypen und Einheiten

Apple führt die Quantity-Identifier zentral auf
([`HKQuantityTypeIdentifier`](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier)).
Apple legt dabei eine Einheitendimension fest, nicht einen einzigen XML-Einheitenstring; HealthKit
unterstützt kompatible Einheiten und Umrechnung ([`HKUnit`](https://developer.apple.com/documentation/healthkit/hkunit)).
V0.3 normalisiert genau einmal und bewahrt Originalwert/-einheit, wie das Architekturmodell verlangt
([Architektur](../../HEALTH_ANALYTICS_ARCHITECTURE.md#quellen-und-einheiten)).

| Fachwert | XML-`type` | zulässige Quellstrings → kanonisch |
|---|---|---|
| Körpergewicht | `HKQuantityTypeIdentifierBodyMass` | `kg`, `g`, `lb` → `kg`; Body Mass ist eine diskrete Massengröße ([Apple](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/bodymass)). |
| Trainingszeit | `HKQuantityTypeIdentifierAppleExerciseTime` | `min`, `s` → `min`; kumulative Zeitgröße ([Apple](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/appleexercisetime)). |
| Schritte | `HKQuantityTypeIdentifierStepCount` | `count` → `count` ([Apple](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/stepcount)). |
| Gehen/Laufen-Distanz | `HKQuantityTypeIdentifierDistanceWalkingRunning` | `m`, `km`, `mi` → `km` ([Apple](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/distancewalkingrunning)). |
| Workout | `Workout` | `durationUnit`: `min`, `s` → `min`; `totalDistanceUnit`: `m`, `km`, `mi` → `km`; `totalEnergyBurnedUnit`: `kcal`, `kJ` → `kcal`; alle Summen optional | 

Für Ernährung sind **alle 39 derzeit von Apple gelisteten** `HKQuantityTypeIdentifierDietary…`
Quelltypen unterstützt. Sie sind kumulative Mengen; Energie verwendet Energie-, Wasser Volumen-
und alle übrigen Typen Masseneinheiten
([Energie](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/dietaryenergyconsumed),
[Wasser](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/dietarywater),
[Beispiel Massengröße](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/dietarybiotin)).

- `kcal`, `kJ` → `kcal`: `DietaryEnergyConsumed`.
- `mL`, `L` → `mL`: `DietaryWater`.
- `mcg`, `mg`, `g` → `g`: `DietaryBiotin`, `DietaryCaffeine`, `DietaryCalcium`,
  `DietaryCarbohydrates`, `DietaryChloride`, `DietaryCholesterol`, `DietaryChromium`,
  `DietaryCopper`, `DietaryFatMonounsaturated`, `DietaryFatPolyunsaturated`,
  `DietaryFatSaturated`, `DietaryFatTotal`, `DietaryFiber`, `DietaryFolate`, `DietaryIodine`,
  `DietaryIron`, `DietaryMagnesium`, `DietaryManganese`, `DietaryMolybdenum`, `DietaryNiacin`,
  `DietaryPantothenicAcid`, `DietaryPhosphorus`, `DietaryPotassium`, `DietaryProtein`,
  `DietaryRiboflavin`, `DietarySelenium`, `DietarySodium`, `DietarySugar`, `DietaryThiamin`,
  `DietaryVitaminA`, `DietaryVitaminB12`, `DietaryVitaminB6`, `DietaryVitaminC`,
  `DietaryVitaminD`, `DietaryVitaminE`, `DietaryVitaminK`, `DietaryZinc`.

Der Parser akzeptiert nur endliche Zahlen und die je Typ geschlossene, dimensionskompatible Liste.
Andere Einheiten werden als nicht unterstützte `(type, unit)`-Kombination katalogisiert statt
stillschweigend umgedeutet. Mikronährstoffwerte bleiben Rohdaten; V0.3 leitet nur Energie sowie
Kohlenhydrate, Fett und Protein als Tagesmerkmale ab.

## Schlaf und Workout-Kategorien

Schlaf ist `Record type="HKCategoryTypeIdentifierSleepAnalysis"` ohne Einheit; `value` muss exakt
einer der Exportstrings `HKCategoryValueSleepAnalysisInBed`,
`HKCategoryValueSleepAnalysisAwake`, `HKCategoryValueSleepAnalysisAsleepUnspecified`,
`HKCategoryValueSleepAnalysisAsleepCore`, `HKCategoryValueSleepAnalysisAsleepDeep` oder
`HKCategoryValueSleepAnalysisAsleepREM` entsprechen. Das veraltete
`HKCategoryValueSleepAnalysisAsleep` wird verlustfrei als `asleep_unspecified` übernommen und im
Original bewahrt. Apple dokumentiert Überlappung von `inBed` mit den detaillierten Stadien und
Nicht-Überlappung der Detailstadien untereinander; Watch-Daten können am Rand eines In-Bed-
Intervalls Detailstücke auslassen
([`HKCategoryValueSleepAnalysis`](https://developer.apple.com/documentation/healthkit/hkcategoryvaluesleepanalysis)).
Der Import darf Überlappungen daher nicht deduplizieren.

`Workout.workoutActivityType` wird als originaler HealthKit-String verlustfrei bewahrt; die
kanonische Entität bleibt ein Workout, nicht je Trainingsart ein neuer Gesundheitstyp. Apple hält
die Menge in `HKWorkoutActivityType` und erweitert sie über OS-Versionen
([Apple](https://developer.apple.com/documentation/healthkit/hkworkoutactivitytype)). Unbekannte
Aktivitätsarten bleiben gültige Workouts und werden erst in einer versionierten analytischen
Gruppierung zu „Sonstige“ zusammengefasst, wie `CONTEXT.md` festlegt
([Analytische Trainingsart](../../CONTEXT.md#zeit-gewicht-und-aktivität)).

## Quelle, Gerät und Identität

`sourceName` ist die anzeigbare Quelle; `sourceVersion` ist die Version der speichernden App, des
OS oder Geräts und **keine Messungsversion**
([`HKSourceRevision`](https://developer.apple.com/documentation/healthkit/hksourcerevision/version)).
`device` bleibt ein opaker Originalstring. HealthKit-Gerätefelder wie Name, Hersteller, Modell und
lokale Kennung sind optional; die lokale Kennung gilt nur auf der erzeugenden Hardware
([`HKDevice`](https://developer.apple.com/documentation/healthkit/hkdevice)). Daher sind
`sourceName`-Substring und der exportierte `device`-Text keine stabile Apple-Watch-ID.
Downstream-Regeln dürfen sie nur als versionierte, prüfbare Watch/iPhone-Klassifikation verwenden;
unklare Quellen ergeben unbekannte Abdeckung, nicht Apple-Watch-Abdeckung.

Die V0.3-Identität bleibt bewusst heuristisch:

1. `MetadataEntry key="HKMetadataKeySyncIdentifier"` liefert zusammen mit `sourceName` einen
   gehashten starken **Kandidaten**, wenn vorhanden; Apple beschreibt den Sync-Identifier als von
   der schreibenden App gesetztes Metadatum, nicht als universelle Objekt-ID
   ([Apple](https://developer.apple.com/documentation/healthkit/hkmetadatakeysyncidentifier)). Eine
   vorhandene `HKMetadataKeySyncVersion` wird als Provenienz bewahrt, nicht als Identität verwendet.
2. Sonst lautet der natürliche Kandidat `Elementart + kanonischer Quelltyp + startDate + endDate +
   sourceName + device`: für numerische und Schlaf-Records ist der Quelltyp das XML-`type`, für
   Workouts die Elementart `Workout`.
3. Wert, Einheit, Schlafwert, `workoutActivityType` und alle übrigen fachlichen Attribute bilden
   den Payload-/Versionshash; `creationDate`, `sourceVersion` und Sync-Version bleiben Provenienz
   und erzeugen allein keine neue Version.
4. Kollisionen natürlicher Kandidaten werden als `ambiguous` sichtbar und nie automatisch
   verschmolzen. Eine HealthKit-UUID wäre stärker, wird im dokumentierten Exportvertrag aber nicht
   zugesichert ([`HKObject.uuid`](https://developer.apple.com/documentation/healthkit/hkobject/uuid)).

## Mindestvertrag für die Folgetickets

- **Gewicht/Ernährung:** obige 40 `Record`-Typen, typgebundene Einheiten, immutable Originalwerte,
  messlokale Tage und Identitätskandidaten; bevorzugtes Tagesgewicht und Tagesaggregate sind
  Ableitungen, keine Importidentität.
- **Aktivität:** `AppleExerciseTime`, `StepCount`, `DistanceWalkingRunning` als Interval-Records
  sowie `Workout` mit originaler Trainingsart; `ActivitySummary` und `WorkoutStatistics` nur
  katalogisieren. Watch-Lücken und iPhone-Fallback rechnen ausschließlich aus zeitlich aufgelösten,
  klassifizierbaren Records.
- **Schlaf:** alle sechs aktuellen und der veraltete Schlafwert als kategorische Intervalle,
  Originalkategorie und Überlappung erhalten; Apple-Watch-Eignung ist eine separate, versionierte
  Quellenentscheidung und bei unklarer Quelle `unknown`.
- **Alles andere:** unbekannte Top-Level-Elemente, `Record.type`, Einheiten, Schlafwerte und
  Workout-Kinder/Arten mit Anzahl und Exportbezug katalogisieren; niemals daraus dynamisch einen
  kanonischen Gesundheitstyp erzeugen
  ([ADR 0006](../adr/0006-use-typed-canonical-health-records.md),
  [ADR 0023](../adr/0023-isolate-healthkit-behind-import-mapping.md)).

Die einzige offene Evidenzfrage ist die reale XML-Schreibweise aktueller iOS-/watchOS-Versionen.
Sie ändert den V0.3-Fachvertrag nicht: eine Abweichung erweitert später nur den versionierten
Importadapter und seine Fixtures; sie darf keine Apple-API-Eigenschaft nachträglich als
Exportgarantie umdeuten.
