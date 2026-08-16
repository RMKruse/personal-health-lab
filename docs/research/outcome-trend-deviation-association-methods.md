# Methoden für Trend- und Abweichungszusammenhänge der Outcomes

## Fragestellung und fachliche Grenzen

Gesucht ist eine wissenschaftlich vertretbare Beschreibung des Zusammenhangs zwischen
Apple-Ruhepuls und Körpergewicht in der **Gesamtübersicht**. Sie muss die im
[`CONTEXT.md`](../../CONTEXT.md#analysen-und-outcomes) bereits getrennt definierten Größen
**Trendzusammenhang** und **Abweichungszusammenhang** liefern. Sie bleibt eine Assoziation
einer Person: weder eine Wirkrichtung noch ein persönlicher kausaler Effekt oder eine
medizinische Aussage wird behauptet.

Apple-Ruhepuls ist ein diskreter, aus über den Tag verteilten Ruhephasen geschätzter
Tageswert. Apple kann eigene Schätzungen für den aktuellen und vorherigen Tag durch bessere
Schätzungen ersetzen. Die Analyse muss deshalb den im Datensatz-Snapshot wirksamen
Tageswert verwenden und darf ihn nicht als kontinuierliche Rohpulsreihe behandeln
([Apple HealthKit](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/restingheartrate)).
Beim Körpergewicht bleibt das **bevorzugte Tagesgewicht** die wirksame Beobachtung;
nicht gemessene Tage bleiben gemäß [`CONTEXT.md`](../../CONTEXT.md#zeit-gewicht-und-aktivität)
fehlend und werden weder als Null noch als fortgeschriebene Messung behandelt.

## Empfehlung

Die kleinste vertretbare Lösung ist eine vorab festgelegte, fensterspezifische
**lokal-lineare Zerlegung** beider Reihen. Sie wird getrennt für die vier vorhandenen
**Gewichtstrendfenster** 1 Woche, 2 Wochen, 1 Monat und 3 Monate berechnet. Es gibt keinen
über diese Fenster gemittelten Gesamtscore.

Für Fenster `h` und den messlokalen Kalendertag `t` seien:

```text
R[t] = T_R,h[t] + D_R,h[t]     Apple-Ruhepuls
W[t] = T_W,h[t] + D_W,h[t]     bevorzugtes Tagesgewicht
```

`T` ist der separat lokal-linear geglättete Verlauf, `D` die beobachtete Abweichung davon.
Die Zeitdistanz bestimmt die Gewichte; Kernel, Fensterbreite und Randregel sind Teil der
versionierten Analysedefinition und werden nicht nach dem größten gefundenen Zusammenhang
gewählt. Lokal-lineare Regression ist für unregelmäßige Beobachtungsdichten und Randpunkte
besser geeignet als übliche lokal-konstante Kernglätter
([Fan 1992](https://doi.org/10.1080/01621459.1992.10476255)). Das Modell verwendet nur
wirksame Analysewerte. Eine geglättete Vorhersage an einem Tag ohne Gewichtsmessung ist ein
abgeleitetes Modellergebnis und keine imputierte oder fortgeschriebene Gewichtsmessung.

### Trendzusammenhang

Primäre Größe ist je Fenster die Pearson-Korrelation der beiden lokal geschätzten
Steigungsreihen:

```text
r_trend,h = corr(slope(T_R,h)[t], slope(T_W,h)[t])
```

Sie beschreibt, ob die längerfristigen Verläufe im selben Zeitraum eher gemeinsam steigen
oder fallen. Die Gewichtssteigung entspricht dabei der **Gewichtsveränderungsrate**; für
den Ruhepuls wird die analoge Rate in bpm pro Woche gebildet. Ausgewertet werden nur Tage,
an denen beide lokalen Fits die versionierte Mindestunterstützung besitzen.

Eine Korrelation der geglätteten **Niveaus** darf ergänzend visualisiert, aber nicht als
primäre inferentielle Größe verwendet werden: persistente oder gemeinsam monotone, dennoch
unabhängige Zeitreihen können hohe Scheinkorrelationen erzeugen
([Yule 1926](https://doi.org/10.2307/2341482),
[Granger & Newbold 1974](https://doi.org/10.1016/0304-4076(74)90034-7)). Die
Steigungskorrelation beseitigt nicht jede gemeinsame zeitabhängige Störgröße; sie vermeidet
lediglich, dass ein gemeinsamer Niveauverlauf selbst das Zielmaß ist.

### Abweichungszusammenhang

Primäre Größe ist je Fenster die zeitgleiche Pearson-Korrelation der gepaarten Abweichungen
an Tagen, an denen **beide** Outcomes tatsächlich beobachtet wurden:

```text
r_abweichung,h = corr(D_R,h[t], D_W,h[t])
```

Das entspricht dem klassischen Herausrechnen vorab definierter Zeittrends; die algebraische
Grundlage der partiellen Zeitregression wurde von Frisch und Waugh beschrieben
([Frisch & Waugh 1933](https://doi.org/10.2307/1907330)). Der Wert beschreibt, ob
kurzfristige Abweichungen von den jeweiligen längerfristigen Verläufen gemeinsam auftreten.
Er ist keine Aussage darüber, welches Outcome zeitlich oder kausal vorausgeht.

V0.4 benötigt dafür weder frei durchsuchte Lags noch nichtlineare Abhängigkeitsmaße. Falls
später eine eigene Frage zur zeitlichen Ordnung entsteht, erhält sie eine getrennte,
vorab festgelegte Lag-Analysedefinition. Als Sensitivität kann zusätzlich Spearman-Rang-
korrelation gezeigt werden; ein Widerspruch zu Pearson ist ein Diagnosehinweis auf
Ausreißer oder Nichtlinearität, kein drittes Primärergebnis.

### Warum die Zerlegung selbst geprüft werden muss

Trend und Abweichung sind nicht unabhängig von der gewählten Zeitskala. In einer
Primärstudie lieferten dieselben Daten je nach Glättungsparameter sowohl positive als auch
negative Residualzusammenhänge
([You, Lin & Young 2018](https://doi.org/10.1371/journal.pone.0195360)). Deshalb gelten:

- jedes Gewichtstrendfenster ist ein eigenes, benanntes Ergebnis;
- die Fensterwahl erfolgt fachlich vor dem Fit und niemals ergebnisgetrieben;
- pro Fenster wird eine kleine, ebenfalls vorab festgelegte Nachbarschaft der Bandbreite
  als Sensitivität geprüft;
- ein Ergebnis darf nicht als belastbar gelten, wenn Richtung oder praktisch relevante
  Größe nur bei exakt einer Glättung bestehen;
- `robust` darf auch ein präzise mit null vereinbarer Zusammenhang sein. Modellreife ist
  keine Behauptung, dass eine Assoziation vorhanden sei.

## Unsicherheit

IID-Standardfehler und der gewöhnliche Korrelations-p-Wert sind für diese überlappenden,
autokorrelierten Glättungen ungeeignet. Die Hauptunsicherheit wird mit einem **gepaarten
Residual-Moving-Block-Bootstrap** geschätzt:

1. Beide Trends werden gefittet und die beiden Abweichungsreihen samt Fehlindikatoren auf
   dem vollständigen Kalenderraster gehalten.
2. Zusammenhängende Blöcke des gepaarten Residual- und Fehlstatusvektors werden gemeinsam
   resampelt. Die beiden Outcomes werden niemals unabhängig voneinander resampelt.
3. Pseudoreihen werden aus den ursprünglichen Trendfits und den resampelten Residuen
   zusammengesetzt; anschließend werden **Zerlegung und beide Zusammenhangsmaße vollständig
   neu geschätzt**.
4. Intervall, erfolgreiche Repliken, Seed, Blocklänge und Quantilstabilität werden
   gespeichert. Die Primärdarstellung verwendet ein gemeinsames Maximalabweichungsband über
   Trend- und Abweichungszusammenhang aller vier Fenster, damit nicht nachträglich das
   günstigste Fenster herausgegriffen wird.

Block-Bootstrap erhält die lokale Abhängigkeit stationärer Beobachtungsfolgen
([Künsch 1989](https://doi.org/10.1214/aos/1176347265)). Daher werden hier nicht die
trendbehafteten Rohniveaus blockweise umgeordnet, sondern die nach der Zerlegung erwarteten
stationären Residuen. Blocklänge, Zahl der Repliken und Toleranz der Monte-Carlo-Stabilität
gehören zur Analysedefinition; eine datenabhängige Blocklängenwahl ist möglich, muss aber
selbst versioniert und durch Nachbarlängen geprüft werden
([Politis & White 2004](https://doi.org/10.1081/ETC-120028836)).

Die Intervalle sind bedingt auf die gewählte Modellfamilie und die beobachteten Daten. Sie
decken weder unbekannten systematischen Gerätebias noch einen nicht ignorierbaren
Fehlprozess ab. Die Oberfläche berichtet Schätzwert, Intervall, Datenqualität,
Modellreife und Methodik statt „signifikant“/„nicht signifikant“, wie es die
[`HEALTH_ANALYTICS_ARCHITECTURE.md`](../../HEALTH_ANALYTICS_ARCHITECTURE.md#analytische-ebenen)
bereits verlangt.

## Modellreife statt Mindestzahl von Kalendertagen

Eine Kalendertagszahl misst weder gemeinsame Beobachtung, Variation noch zeitlich
unabhängige Information. Die vorhandene **Modellreifeprüfung** bleibt deshalb eine
UND-Verknüpfung versionierter Kriterien: nur wenn jedes Pflichtkriterium besteht, ist das
formal berechnete Ergebnis **belastbar**; sonst ist es **explorativ**. Ein nicht
berechenbarer oder numerisch gescheiterter Lauf erzeugt weiterhin kein exploratives
Ergebnis. Datenstatus und technischer Laufstatus bleiben davon getrennt
([`CONTEXT.md`](../../CONTEXT.md#datenqualität-und-ergebnisstatus)).

Der aktuelle Ruhepuls-Fit verwendet noch `minimum_observations` als Berechenbarkeitstor
und `robust_observations` als eines mehrerer Reifekriterien
([`resting_hr_analysis._fit`](../../src/personal_health_lab/resting_hr_analysis/__init__.py)).
Diese bestehende Trennung von technischem Ausgang und Modellreife wird übernommen; die
beiden festen Beobachtungsschwellen werden für Outcome-Zusammenhänge nicht kopiert.

### Modellübergreifende Pflichtdiagnosen

| Bereich | Beobachteter Diagnosewert | Reifefrage |
|---|---|---|
| Gemeinsame Unterstützung | geeignete Zeitspanne, Tage mit beiden wirksamen Outcomes, Verteilung dieser Tage, längste interne und Randlücke | Tragen die Daten beide Zielgrößen über den beanspruchten Zeitraum statt nur in einem kurzen Cluster? |
| Effektive Information | Zahl vollständiger Kalenderblöcke je geprüfter Blocklänge, Residuen-ACF und daraus abgeleitete effektive Information | Bleibt nach zeitlicher Abhängigkeit genügend Information für das Intervall? |
| Variation | Streuung beider lokalen Steigungen und beider Abweichungen, Zahl unterschiedlicher Werte | Ist das jeweilige Korrelationsmaß definiert und praktisch identifizierbar? |
| Fehlbeobachtung | Fehlanteil und Lückenfolge je Outcome, Messdichte nach Zeit, Quelle und Wochentag, Anteil gepaarter Tage | Hängt das Ergebnis nicht ausschließlich an einer schmalen beobachteten Teilmenge? |
| Datenqualität | offene Datenprüffälle, verwendete Messungs- und Korrektur-IDs, Quellen- und Gerätewechsel | Sind Unsicherheiten der Quelldaten sichtbar und ist der getrennte Datenstatus korrekt eingefroren? |
| Zeitreihen-Fit | Residuen-ACF, vorab festgelegte Ljung-Box-Lags, verbleibender Residuentrend | Ist nach der Zerlegung keine starke unmodellierte Zeitstruktur übrig? |
| Einfluss | Änderung beider Zusammenhangsmaße beim Weglassen zusammenhängender Blöcke | Wird das Ergebnis nicht von einem einzelnen Zeitraum getragen? |
| Glättungssensitivität | Schätzwerte und Intervalle in der festgelegten Bandbreitennachbarschaft | Bleiben Richtung oder praktische Äquivalenz im kalibrierten Toleranzbereich? |
| Bootstrap | Erfolgsrate, Intervallbreite, Quantil-Monte-Carlo-Fehler und Nachbar-Blocklängen | Ist die berichtete Unsicherheit numerisch und gegenüber plausiblen Blöcken stabil? |
| Ergebnispräzision | Intervallbreite relativ zu vorab festgelegten praktisch relevanten Korrelationsbereichen | Trennt das Ergebnis relevante positive, negative oder praktisch äquivalente Bereiche ausreichend? |

Der Ljung-Box-Test ist ein Mehr-Lag-Test auf verbleibende Modellfehlanpassung
([Ljung & Box 1978](https://doi.org/10.1093/biomet/65.2.297)). Er wird zusammen mit der
Residual-ACF und nicht als einzelne p-Wert-Wahrheitsschwelle verwendet. Das blockweise
Weglassen überträgt die Idee der Einflussdiagnostik auf abhängige Tage; klassische
Einflussdiagnostik verbindet Residuum und Hebelwirkung einer Beobachtung
([Cook 1977](https://doi.org/10.1080/00401706.1977.10489493)).

### Modellspezifische Pflichtdiagnosen der lokal-linearen Zerlegung

- **Lokale Unterstützung:** Anzahl und Summe der Zeitgewichte, links-/rechtsseitige
  Unterstützung und maximale Entfernung der tatsächlich beitragenden Messungen je
  Auswertungstag. Extrapolation über lange Gewichtslücken besteht das Kriterium nicht.
- **Lokale Numerik:** skalierte Determinante beziehungsweise kleinster Pivot jeder lokalen
  2×2-Normalgleichung; konstante Zeitkoordinaten oder numerisch singuläre Fits sind nicht
  berechenbar.
- **Randanteil:** Anteil der ausgegebenen Steigungen, der nur einseitig gestützt ist, und
  gesonderte Stabilität beim Entfernen der Randbereiche.
- **Skalentrennung:** verbleibende niedrige Frequenz in `D_R,h` und `D_W,h`, Korrelation der
  Residuen mit Kalenderzeit sowie Stabilität in der Bandbreitennachbarschaft. Misslingt die
  Trennung, ist der Abweichungszusammenhang explorativ.
- **Korrelationsform:** Streudiagramm, standardisierte Residuen und Pearson-/Spearman-
  Sensitivität; eine einzelne extreme Abweichung darf nicht die Richtung bestimmen.
- **Bootstrap-Reproduzierbarkeit:** der vollständige lokale Fit muss in der kalibrierten
  Mindestquote der Repliken berechenbar sein.

Diese Werte und ihre Schwellen werden wie die vorhandenen
`ModelMaturityCriterion`-Fakten mit Analysedefinition, beobachtetem Wert und Schwelle
gespeichert. Die Schwellen stammen nicht aus einer neuen 90-/180-Tage-Faustregel, sondern
aus einem synthetischen Release-Gate. Simulationen können bei bekannter Wahrheit Bias,
RMSE, Intervallabdeckung und Fehlalarm direkt messen; ein sauberer Plan benennt dafür
Ziele, Datengenerierung, Zielgrößen, Methoden und Performancemaße
([Morris, White & Crowther 2019](https://doi.org/10.1002/sim.8086)).

Das Release-Gate variiert mindestens Beobachtungsdichte, zusammenhängende Lücken,
Autokorrelation, Trendkrümmung, getrennte und gemeinsame Strukturbrüche, Null-, positive
und negative Zusammenhänge, Gerätewechsel sowie zufällige Messfehlerstärke. Es kalibriert
je Gewichtstrendfenster die erforderliche lokale Unterstützung, Intervallbreite,
Blockanzahl, Bootstrap-Stabilität und Sensitivitätstoleranz. Die beobachtete Zahl geeigneter
Tage bleibt sichtbar, ist aber allein weder notwendiges noch hinreichendes Reifekriterium.

## Messfehler und Fehlbeobachtung

### Messfehler

Zufälliger Messfehler schwächt eine beobachtete Korrelation typischerweise ab; die
klassische Korrektur setzt bekannte Reliabilitäten voraus
([Spearman 1904](https://doi.org/10.2307/1412159)). Bei diesen Daten können außerdem
gemeinsame Tagesbedingungen, Geräte- oder Quellenwechsel korrelierten Fehler erzeugen, der
den Zusammenhang in beide Richtungen verzerren kann. Deshalb:

- Quellen-, Geräte-, Messzeit- und Versionswechsel werden als Diagnosemarken erhalten;
- mehrere Messungen eines Tages und externe Referenzmessungen dürfen eine plausible
  Messfehlervarianz für Sensitivitäten liefern, werden aber nicht automatisch zu einer
  „wahren“ Messung gemittelt;
- ohne belastbare Reliabilität bleibt der Primärwert unkorrigiert und wird unter mehreren
  vorab festgelegten Messfehlervarianzen erneut simuliert;
- SIMEX ist erst begründet, wenn eine Messfehlervarianz identifiziert ist; das Verfahren
  wurde für parametrische Messfehlermodelle entwickelt
  ([Cook & Stefanski 1994](https://doi.org/10.1080/01621459.1994.10476871)).

Das Residual-Bootstrap erfasst Streuung, die in den beobachteten Residuen steckt, aber
keinen unbekannten konstanten Bias und keine falsche Zuordnung eines Tages. Offene
Datenprüffälle machen das Ergebnis gemäß bestehendem Vertrag **vorläufig**, unabhängig
davon, ob die Modellreifeprüfung besteht.

### Fehlende Tage

Die Residualkorrelation verwendet vollständige Paare und beschreibt daher zunächst den
Abweichungszusammenhang **an gemeinsam gemessenen Tagen**. Das Ignorieren eines
Fehlprozesses ist nur unter bestimmten Missing-at-random- und Parametertrennungsannahmen
gerechtfertigt
([Rubin 1976](https://doi.org/10.1093/biomet/63.3.581)). Diese Annahme ist hier nicht aus
den Daten beweisbar: Wiegen, Watch-Tragezeit, Krankheit oder Reisen können sowohl
Beobachtbarkeit als auch Outcome beeinflussen.

Die Primäranalyse imputiert deshalb keine Tagesmessungen. Sie zeigt Fehlmuster und
Messdichte, prüft Sensitivitäten nach dichten versus dünnen Messperioden und formuliert die
Interpretation bedingt auf die beobachteten Tage. Ein explizites Modell für nicht
ignorierbares Fehlen wäre eine spätere eigene Analysedefinition mit zusätzlichen Annahmen,
nicht eine versteckte Erweiterung dieses Zusammenhangsmodells.

## Projektabhängigkeiten und Umsetzungsschnitt

Der aktuelle Stand deklariert ausschließlich DuckDB, jsonschema und Streamlit als direkte
Laufzeitabhängigkeiten ([`pyproject.toml`](../../pyproject.toml)). NumPy und pandas stehen
nur transitiv im [`uv.lock`](../../uv.lock) und dürfen nicht stillschweigend als öffentliche
Projektabhängigkeit verwendet werden.

Für die empfohlene erste Analysedefinition ist keine neue Abhängigkeit nötig:

- ein lokal-linearer Fit löst pro Tag nur ein gewichtetes 2×2-System;
- Pearson-Korrelation und Grundstatistiken sind in der Python-Standardbibliothek vorhanden
  ([Python `statistics`](https://docs.python.org/3/library/statistics.html#statistics.correlation));
- der bestehende [`resting_hr_analysis`](../../src/personal_health_lab/resting_hr_analysis/__init__.py)
  implementiert bereits deterministischen Moving-Block-Bootstrap, Quantile und eine kleine
  lineare Lösung ohne Statistikframework.

Die privaten Funktionen des Ruhepulsanalysemoduls werden wegen der tiefen Modulgrenze aus
[ADR-0016](../adr/0016-resting-heart-rate-analysis-is-one-deep-module.md) nicht aus einem
neuen Modul importiert. Wiederverwendet werden Methodenmuster und bestehende kanonische
Typen, nicht private Implementierungsseams. Die Ergebnisprojektion folgt dem
präsentationsneutralen, fokussierten Lesevertrag aus
[ADR-0027](../adr/0027-v02-application-uses-typed-write-requests-and-focused-read-projections.md).

Ein bivariates Zustandsraum-Messfehlermodell oder eine frei geschätzte penalized-spline-
Familie ist derzeit nicht nötig. Beide würden numerische Optimierung, belastbare
Matrixdiagnostik und zusätzliche Identifizierbarkeitsannahmen einführen. Sie werden erst
neu bewertet, wenn der synthetische Kalibrierungsprototyp zeigt, dass die vorab
festgelegte lokal-lineare Zerlegung die Abdeckungs- oder Stabilitätsziele nicht erreicht.

## Verworfene Abkürzungen

- **Korrelation der beiden Roh- oder Trendniveaus mit iid-p-Wert:** Scheinkorrelation durch
  Persistenz und gemeinsame Kalenderzeit.
- **Ein einziges automatisch gewähltes Glättungsfenster:** ergebnisabhängige Definition von
  Trend und Abweichung; die vier fachlichen Gewichtstrendfenster bleiben getrennt.
- **Tagesdifferenzen als Primärmethode:** einfach, aber sie ändern die Zielgröße und
  verstärken kurzfristigen Messfehler; nur Sensitivität bei misslungener Zerlegung.
- **Fehlende Gewichtstage fortschreiben oder linear interpolieren:** erzeugt scheinbare
  Beobachtungen und zu enge Unsicherheit.
- **Modellreife durch Ausschluss von null:** verwechselt Stabilität mit einem positiven
  Befund; ein präzises Nullergebnis kann belastbar sein.
- **Neue Statistikabhängigkeit vor Kalibrierung:** die empfohlene 2×2-Zerlegung und der
  Block-Bootstrap sind mit Standardbibliothek und vorhandenen Projektmustern ausführbar.

## Offener Kalibrierungsschritt

Vor einem produktiven Vertrag ist genau ein kleiner Methodenprototyp nötig. Er friert
Kernel, Randregel, Blocklängenregel und Reifeschwellen je Gewichtstrendfenster anhand des
beschriebenen synthetischen Release-Gates ein. Erst wenn dieser Prototyp die Zielabdeckung
oder numerische Stabilität verfehlt, besteht Evidenz für eine komplexere Modellfamilie oder
eine neue direkte numerische Abhängigkeit.
