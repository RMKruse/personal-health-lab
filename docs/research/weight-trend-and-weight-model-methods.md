# Methoden für Gewichtstrends und Gewichtsmodelle

## Fragestellung und Grenze der Aussage

V0.4 soll für die vier **Gewichtstrendfenster** 1 Woche, 2 Wochen, 1 Monat und
3 Monate das **geglättete Gewichtsniveau** und die **Gewichtsveränderungsrate**
darstellen. Zusätzlich soll es beschreiben, wie Energieaufnahme, Protein,
Kohlenhydrate, Fett, aktive Energie und Ruheenergie gemeinsam mit der
Gewichtsveränderungsrate zusammenhängen.

Das ist eine deskriptive und prognostische Einpersonen-Zeitreihenanalyse. Sie schätzt
weder einen persönlichen kausalen Kalorieneffekt noch eine medizinische Wirkung. Dynamische
Körpergewichtsmodelle zeigen, dass eine Energieabweichung wegen Anpassungen von Verbrauch und
Körperzusammensetzung nicht als konstante lineare Umrechnung in Körpergewicht interpretiert
werden darf ([Hall et al. 2011](https://doi.org/10.1016/S0140-6736(11)60812-X)).

## Empfehlung

Die kleinste wissenschaftlich vertretbare V0.4-Familie besteht aus:

1. einer gemeinsamen, vorab festgelegten **lokal-linearen Gewichtstrenddefinition** je
   Gewichtstrendfenster;
2. zwei verschachtelten, horizon-spezifischen **Ridge-Assoziationsmodellen** für die
   Gewichtsveränderungsrate;
3. einem Residual-Moving-Block-Bootstrap mit vollständigem Refit;
4. einer diagnostischen Modellreifeprüfung statt einer Mindestzahl von Kalendertagen;
5. einer nur ergänzenden **Energiebilanz**, die nie zusammen mit ihren Bestandteilen als
   Modellmerkmal verwendet wird.

Es gibt keine freie Formelwahl, automatische Variablenselektion oder nach Ergebnis gewählte
Fenster. Alle vier Gewichtstrendfenster und beide Modellformen sind eingebaute, typisierte und
versionierte Analysedefinitionen.

## Geglättetes Gewichtsniveau und Gewichtsveränderungsrate

Die bereits für
[Trend- und Abweichungszusammenhänge](./outcome-trend-deviation-association-methods.md)
entschiedene lokal-lineare Zerlegung wird wiederverwendet, nicht ein zweiter Gewichtstrend
erfunden. Für Tag `t` und Gewichtstrendfenster `h` wird aus den wirksamen beobachteten
**bevorzugten Tagesgewichten** lokal gewichtet gefittet:

```text
W[i] = a_h(t) + b_h(t) * (day[i] - t) + error[i]

geglättetes Gewichtsniveau_h(t) = a_h(t)                 [kg]
Gewichtsveränderungsrate_h(t) = 7 * b_h(t)               [kg/Woche]
```

Lokal-lineare Regression liefert Niveau und erste Ableitung aus demselben kleinen Fit, kann
unregelmäßige Beobachtungsabstände direkt verwenden und passt sich besser an Ränder an als ein
lokal-konstanter Glätter ([Cleveland 1979](https://doi.org/10.1080/01621459.1979.10481038),
[Fan 1992](https://doi.org/10.1080/01621459.1992.10476255)). Kernel, Bandbreite,
Randregel und die Umrechnung der vier fachlichen Fenster bleiben vorab festgelegt. Sie werden
durch
[Trend- und Abweichungszusammenhänge synthetisch kalibrieren](https://github.com/RMKruse/personal-health-lab/issues/103)
eingefroren.

Fehlende Gewichtstage erhalten keinen künstlichen Wert. Ein geglättetes Niveau an einem Tag
ohne Gewichtsmessung ist ein abgeleitetes Modellergebnis, keine imputierte Messung. Der Fit gibt
nur dann Niveau und Rate aus, wenn lokale Gewichtssumme, Zeitspanne, Verteilung der Messungen,
maximale Lücke und 2×2-Numerik die versionierten Unterstützungskriterien erfüllen. Einseitig
gestützte Randwerte bleiben sichtbar markiert und müssen eine eigene Stabilitätsprüfung bestehen.

## Zwei verschachtelte Gewichtsmodelle je Fenster

Für jedes Gewichtstrendfenster werden zwei vorab benannte Modelle auf derselben Menge geeigneter
Modellanker gefittet:

```text
Energiekomponenten:
rate_h(t) ~ Energieaufnahme + aktive Energie + Ruheenergie

Energie + Makronährstoffe:
rate_h(t) ~ Energieaufnahme + Protein + Kohlenhydrate + Fett
            + aktive Energie + Ruheenergie
```

`rate_h(t)` ist die lokal geschätzte Gewichtsveränderungsrate. Die Tagesmerkmale werden mit
demselben Kalender- und Kernel-Support wie der zugehörige lokale Gewichtstrend zu
fensterspezifischen Mittelwerten verdichtet. Dafür gelten nur Tage, an denen alle Merkmale des
jeweiligen Modells fachlich vollständig beobachtet sind; alle Merkmale eines Modellankers
verwenden dieselben geeigneten Tage. Es erfolgt weder eine Hochrechnung auf das vollständige
Fenster noch eine Nullsetzung fehlender Tage.

Alle Prädiktoren werden innerhalb des Analyselaufs zentriert und skaliert. Interzept und ein
vorab festgelegter linearer Kalenderterm bleiben unpenalisiert; die Merkmalskoeffizienten
erhalten eine gemeinsame horizon-spezifische Ridge-Strafe. Ridge wurde gerade für
nichtorthogonale Prädiktoren entwickelt
([Hoerl & Kennard 1970](https://doi.org/10.1080/00401706.1970.10488634)). Das ist hier nötig,
weil Energieaufnahme und Makronährstoffe physikalisch und durch das Protokollieren stark
zusammenhängen. Die erweiterte Modellform beantwortet, ob die Makronährstoffe über die drei
Energiekomponenten hinaus stabile zusätzliche Vorhersageinformation liefern; sie erlaubt keine
isolierte Nährstoffwirkung.

Die Modelle berichten:

- Koeffizienten je persönlicher Standardabweichung sowie zurücktransformiert je 100 kcal
  beziehungsweise 10 g;
- gemeinsame Vorhersage, Residuen und zeitblockierte Vorhersagegüte gegenüber einem
  Interzept-/Kalender-Basismodell;
- Änderung der Vorhersagegüte vom Energiekomponentenmodell zum erweiterten Modell;
- Rang, Kondition, Merkmalsunterstützung, Ridge-Stabilität und Blockeinfluss.

Eine Energieadjustierung ist für Nährstoffmodelle etabliert, ihre konkrete Koeffizientenbedeutung
hängt aber von der gewählten Modellform ab
([Willett, Howe & Kushi 1997](https://doi.org/10.1093/ajcn/65.4.1220S)). Deshalb werden die
Makronährstoffkoeffizienten ausschließlich als bedingte Assoziationen der festgelegten
Ridge-Definition beschrieben. Ein isokalorisches Substitutions-, Kompositions- oder
mechanistisches Stoffwechselmodell wäre eine andere Fragestellung und bleibt außerhalb V0.4.

## Fehlende und teilweise beobachtete Tage

### Primärregel

Ein fehlender Ernährungswert ist gemäß [`CONTEXT.md`](../../CONTEXT.md#zeit-gewicht-und-aktivität)
weder Nullaufnahme noch geschätzte Tagesernährung. Entsprechend gilt:

- fehlende Gewichte werden nicht fortgeschrieben oder interpoliert;
- fehlende Ernährung, aktive Energie oder Ruheenergie werden nicht mit null gefüllt;
- unvollständige Aktivitätstage werden nicht auf 24 Stunden hochgerechnet;
- ein Modellanker wird nur aus gemeinsam geeigneten Tagen aller benötigten Merkmale gebildet;
- Beobachtungsanteil, längste Lücke, Randlücken und zeitliche Verteilung werden je Merkmal und
  Fenster gespeichert und angezeigt.

Die vollständigen Fälle definieren zunächst eine bedingte Aussage über ausreichend beobachtete
Zeiträume. Das Ignorieren des Fehlprozesses ist nur unter zusätzlichen Missing-at-random- und
Parametertrennungsannahmen gerechtfertigt ([Rubin 1976](https://doi.org/10.1093/biomet/63.3.581)).
Diese Annahmen sind hier nicht aus den Daten prüfbar: Wiegen, Ernährungserfassung, Watch-Tragezeit,
Krankheit und Reisen können zugleich Beobachtbarkeit und Gewicht beeinflussen.

V0.4 verwendet deshalb keine Mehrfachimputation. Als vorab festgelegte Sensitivitäten vergleicht
es dichte gegen dünne Beobachtungsperioden, lässt zusammenhängende Blöcke weg und prüft im
synthetischen Gate zufällige, zeitabhängige und outcomeabhängige Lücken. Widersprüche verhindern
`robust`; sie werden nicht durch einen aufgefüllten Datensatz verdeckt.

### Belegte Datenvertragslücken

Der heutige Stand kann die Primärregel noch nicht vollständig ausführen:

- `DailyNutritionFeature` unterscheidet `value=None` von mindestens einem vorhandenen Sample,
  besitzt aber keinen fachlichen Vollständigkeitsstatus. Ein teilweise protokollierter Tag sieht
  daher wie ein vollständig protokollierter Tag aus.
- `ActivityDay` besitzt bereits `is_complete`, Abdeckungssegmente und Gründe für unvollständige
  Aktivität; dieses Muster kann fachlich wiederverwendet werden.
- `CanonicalHealthType` enthält keine Ruheenergie. Apple stellt dafür
  `basalEnergyBurned` als kumulativen HealthKit-Energietyp bereit
  ([Apple HealthKit](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/basalenergyburned)),
  aber Quellenauswahl, Überlappungsauflösung, Tagesgrenzen und Vollständigkeit sind im Projekt noch
  nicht entschieden.

Ob ein Ernährungstag vollständig ist, kann nicht allein aus den vorhandenen Samples geraten
werden; es braucht eine explizite fachliche Regel beziehungsweise Nutzerevidenz. Ebenso darf
HealthKits Ruheenergieschätzung nicht still als beobachtete Wahrheit behandelt werden.

## Unsicherheit

IID-Standardfehler sind ungeeignet, weil benachbarte lokale Trends und Merkmalsfenster stark
überlappen. Primär wird ein **Residual-Moving-Block-Bootstrap** mit mindestens 2.000 erfolgreichen
vollständigen Refits je versionierter Analysedefinition verwendet:

1. Lokaler Gewichtstrend und Ridge-Modell werden auf dem beobachteten Kalenderraster gefittet.
2. Zusammenhängende Blöcke der Gewichtsresiduen werden resampelt. Die beobachteten Merkmale
   sowie ihre Gewichts-, Fehl- und Abdeckungsmasken bleiben fest; die Inferenz ist damit bewusst
   auf den beobachteten Support bedingt.
3. Aus Trend und resampelten Residuen entstehen Pseudogewichte. Gewichtstrend,
   Fenstermerkmale, Standardisierung, Ridge-Fit und alle Diagnosen werden je Replik vollständig
   neu berechnet.
4. Gespeichert werden Seed, Blocklänge, erfolgreiche Repliken, Ausfallgründe,
   Quantil-Monte-Carlo-Fehler und Stabilität bei Nachbarblocklängen.

Moving-Block-Bootstrap erhält lokale Abhängigkeit schwach stationärer Residuen
([Künsch 1989](https://doi.org/10.1214/aos/1176347265)). Die Primärdarstellung zeigt
punktweise Intervalle und ein studentisiertes Maximalabweichungsband über alle angezeigten
Koeffizienten der vier Fenster innerhalb einer Modellfamilie. Für die vier Gewichtstrendkurven
gilt ein getrenntes gemeinsames Band über alle dargestellten Tage und Fenster. Die genaue
Blocklängenregel und Familienabgrenzung werden synthetisch kalibriert, nicht nach dem günstigsten
realen Ergebnis gewählt.

Die Intervalle sind bedingt auf Modellfamilie, beobachtete Quellen und Fehlannahmen. Sie erfassen
keinen unbekannten systematischen Bias. Selbstberichtete Energieaufnahme kann erheblich und
aufnahmeabhängig untererfasst sein
([Schoeller 1990](https://doi.org/10.1111/j.1753-4887.1990.tb02882.x)); auch die
Energieverbrauchsschätzung von Wearables zeigte in einer Primärstudie deutlich höhere Fehler als
die Herzfrequenzmessung
([Shcherbina et al. 2017](https://doi.org/10.3390/jpm7020003)). Quellen- und Gerätewechsel sowie
plausible Fehlerstärken gehören deshalb in Diagnose und synthetische Sensitivität, nicht in ein
scheinbar präzises Messfehlerkorrekturverfahren ohne identifizierte Reliabilität.

## Modellreife

`robust` bleibt eine UND-Verknüpfung versionierter Kriterien. Ein präzises Nullergebnis darf
robust sein; das Ausschließen von null ist kein Reifekriterium. Ein nicht berechenbarer oder
numerisch gescheiterter Lauf liefert kein exploratives Ergebnis.

| Bereich | Pflichtdiagnosen |
|---|---|
| Gewichtstrend | lokale Messungszahl und Zeitspanne, gewichtete Unterstützung, links-/rechtsseitige Unterstützung, maximale Lücke, 2×2-Numerik, Randanteil, Bandbreitennachbarschaft |
| Gemeinsame Merkmalsabdeckung | vollständige gemeinsame Tage, Zeitspanne, Verteilung, längste interne und Randlücke, Anteil geeigneter Modellanker je Fenster |
| Effektive Information | Zahl unabhängiger Kalenderblöcke, Residuen-ACF, effektive Information nach Überlappung |
| Prädiktorunterstützung | persönliche Variation, unterschiedliche Werte, Korrelationsmatrix, Rang, Konditionszahl, kleinste Singulärwerte |
| Fit und Vorhersage | Residuen-ACF und -trend, zeitblockierte Vorhersagegüte gegen Basismodell, praktisch kalibrierte Fehlergrenze |
| Einfluss und Stabilität | Blockweglassen, Nachbar-Ridge-Strafen, Bandbreiten- und Blocklängennachbarschaft, Richtung und Größenordnung der Koeffizienten |
| Bootstrap | Erfolgsquote, Bandbreite, Quantilstabilität, Reproduzierbarkeit und numerische Ausfälle |
| Daten- und Messqualität | offene Datenprüffälle, Quellen-/Gerätewechsel, Ernährungs-Vollständigkeitsstatus, Aktivitäts- und Ruheenergieabdeckung |

Schwellen werden nicht aus einer pauschalen Zahl wie 90 oder 180 Kalendertagen abgeleitet. Ein
synthetischer Methodenprototyp variiert wahre Gewichtstrends, Energie-/Makronährstoffkorrelation,
Messfehler, Autokorrelation, Beobachtungsdichte, zusammenhängende Lücken, teilweise Ernährung,
Aktivitäts- und Ruheenergieabdeckung sowie Null- und Nichtnullzusammenhänge. Er kalibriert Bias,
RMSE, zeitblockierte Vorhersage, Intervall- und Bandabdeckung, Null-Fehlalarm, Bootstrap-Ausfälle,
Rechenzeit und Reifeschwellen.

## Energiebilanz bleibt eine ergänzende Anzeige

Für einen Tag oder ein Gewichtstrendfenster ist die **Energiebilanz**:

```text
Energieaufnahme - aktive Energie - Ruheenergie
```

Sie wird nur für gemeinsam fachlich vollständige Tage berechnet. Fensterwerte sind Summe und
Tagesmittel über diese Tage, zusammen mit Zähler, Solltagen und Lücken; sie werden nie auf fehlende
Tage hochgerechnet. Fehlt eine Komponente oder ist der Tag teilweise beobachtet, bleibt auch die
Energiebilanz fehlend.

Die Energiebilanz ist eine abgeleitete Anzeige, keine eigenständige Messung. Sie erhält ohne
identifizierte Messfehlerverteilungen kein künstliches Konfidenzintervall. Im Ridge-Modell werden
stattdessen Energieaufnahme, aktive Energie und Ruheenergie getrennt verwendet. Würde zusätzlich
ihre lineare Differenz aufgenommen, entstünde deterministische Kollinearität ohne neue Information.
Eine kumulierte Energiebilanz darf nicht als direkte Gewichtsprognose oder „Kaloriendefizit“ mit
garantierter Wirkung beschriftet werden.

## Projektabhängigkeiten und Umsetzungsschnitt

Der aktuelle [`pyproject.toml`](../../pyproject.toml) deklariert nur DuckDB, jsonschema und
Streamlit direkt. NumPy 2.5.1 und pandas sind im [`uv.lock`](../../uv.lock) transitiv vorhanden;
SciPy, statsmodels und scikit-learn fehlen. Die abgeschlossene Methodenentscheidung für die
kurz- und langfristigen Aktivitätsmodelle verlangt NumPy bereits als direkte analytische
V0.4-Abhängigkeit. Dieselbe Abhängigkeit genügt für lokale 2×2-Fits, Standardisierung,
Singulärwertdiagnostik und Ridge-Lösungen der Gewichtsmodelle. Eine weitere Statistikabhängigkeit
ist nicht begründet, solange der synthetische Prototyp kein konkretes numerisches oder
Abdeckungsdefizit zeigt.

Vorhanden und wiederverwendbar sind:

- bevorzugtes Tagesgewicht samt `missing`-/`ambiguous`-Status;
- getrennte tägliche Energie- und Makronährstoffmerkmale mit Provenienz;
- aktive Energie, Abdeckungssegmente und `ActivityDay.is_complete`;
- Snapshot-Pinning, Analyse-Läufe, Reuse-Key, Code-/Umgebungshash und Ergebnisgeschichte;
- deterministischer Moving-Block-Bootstrap, Quantile und Modellreifefakten als Muster aus
  `resting_hr_analysis`.

Nicht vorhanden sind ein kanonischer Ruheenergietyp, Ernährungsvollständigkeit, ein gemeinsames
V0.4-Analyseeingangsbündel und eine Ergebnisfamilie für Gewichtstrends/-modelle. Wegen
[ADR-0016](../adr/0016-resting-heart-rate-analysis-is-one-deep-module.md) werden private
Ruhepulsanalysefunktionen nicht importiert; wiederverwendet werden nur Methode und öffentliche
Verträge. Neue kanonische Daten bleiben nach
[ADR-0006](../adr/0006-use-typed-canonical-health-records.md) typisiert und HealthKit-spezifische
Namen nach [ADR-0023](../adr/0023-isolate-healthkit-behind-import-mapping.md) hinter der
Import-Mapping-Seam.

## Verworfene Alternativen

- **Gleitender Mittelwert plus Tagesdifferenzen:** liefert keine gemeinsame saubere
  Niveau-/Ratenableitung, behandelt Ränder schlecht und verstärkt Messrauschen in der Rate.
- **Fehlende Tage interpolieren oder unvollständige Energie hochrechnen:** erzeugt unbelegte
  Pseudobeobachtungen und zu enge Unsicherheit.
- **Energiebilanz als einziges Modellmerkmal:** versteckt verschiedene Messfehler und macht die
  Beiträge von Aufnahme, Aktivität und Ruheenergie unprüfbar.
- **Energiebilanz zusätzlich zu ihren drei Bestandteilen fitten:** ist exakt kollinear.
- **Gewöhnliche kleinste Quadrate für alle Energie- und Makromerkmale:** ist bei der erwarteten
  Nichtorthogonalität instabil; Ridge ist die kleinere robuste Modellfamilie.
- **Lasso oder schrittweise Variablenauswahl:** beantwortet eine nicht gestellte Auswahlfrage und
  macht Ergebnisse bei korrelierten Merkmalen unnötig sprunghaft.
- **Zustandsraum-, Bayes- oder mechanistisches Stoffwechselmodell:** benötigt zusätzliche
  Identifizierbarkeitsannahmen und Eingaben, die V0.4 nicht besitzt.
- **Modellreife über einen positiven Befund oder eine starre Tageszahl:** verwechselt Signal,
  Datenmenge und Stabilität.

## Offene, jetzt präzise Entscheidungen

Vor dem V0.4-Analysevertrag sind drei Schritte nötig:

1. die kanonische Ruheenergiequelle, Tagesableitung und Abdeckung festlegen;
2. festlegen, wodurch ein Ernährungstag als vollständig beobachtet gilt;
3. anschließend die Gewichtstrend-/Ridge-Familie synthetisch kalibrieren und erst bei einem
   belegten Defizit eine weitere Statistikabhängigkeit erwägen.
