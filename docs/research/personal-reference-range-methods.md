# Persönliche Referenzbereiche in V0.2

## Fragestellung und Geltungsbereich

V0.2 benötigt eine einfache, robuste und erklärbare Methode für persönliche
Referenzbereiche. Der aktuelle kanonische Typkatalog enthält genau zwei
numerische Typen: `apple_resting_heart_rate` und `active_energy`. Die Empfehlung
gilt nur für diese Typen und erzeugt Datenqualitäts-Hinweise, keine medizinischen
Warnungen oder Diagnosen.

## Entscheidung

### Apple-Ruhepuls: nachlaufender Median/MAD-Bereich

Für `apple_resting_heart_rate` wird je neuem wirksamen Tageswert ein ausschließlich
aus früheren Werten berechneter persönlicher Referenzbereich verwendet:

1. Referenzfenster: die 42 unmittelbar vorausgehenden messlokalen Kalendertage;
   der zu prüfende Wert und spätere Werte sind ausgeschlossen.
2. Referenzbereitschaft: mindestens 28 gültige Tageswerte im Fenster.
3. Zentrum: Median `m`.
4. Streuung: `MAD = median(abs(x - m))`.
5. Bereich: `m ± 3.5 * MAD / 0.6745`.
6. Auffälligkeit: nur wenn der aktuelle Wert strikt außerhalb dieses Bereichs
   liegt.

Das ist der von NIST beschriebene modifizierte Z-Wert mit der konservativen
Kennzeichnungsschwelle `abs(M) > 3.5`. NIST beschreibt MAD als robuste
Alternative zur Standardabweichung und trennt die Kennzeichnung eines möglichen
Ausreißers ausdrücklich von der Feststellung eines Fehlers oder seiner Löschung:
[Detection of Outliers](https://www.itl.nist.gov/div898/handbook/eda/section3/eda35h.htm),
[Median Absolute Deviation](https://www.itl.nist.gov/div898/software/dataplot/refman2/auxillar/mad.htm).
Die Schwelle ist eine Heuristik zur Kennzeichnung und garantiert ohne ein
Verteilungsmodell keine bestimmte Fehlalarmrate.

Die 42/28-Schwellen sind eine versionierte, konservative V0.2-Produktvorgabe,
keine medizinisch oder populationsstatistisch validierte Grenze. Das
Kalenderfenster lässt eine anhaltende Niveauverschiebung in die Referenz
einwandern, während die Mindestbelegung nach längeren Datenlücken die Prüfung
aussetzt. Diese Vorgaben werden erst in V0.5 an realen Verteilungen kalibriert.

Apple beschreibt `restingHeartRate` als diskreten, über den Tagesverlauf
geschätzten Wert und weist darauf hin, dass Apple Watch Schätzungen des aktuellen
oder vorherigen Tages ersetzen kann. Deshalb verwendet das Fenster höchstens den
jeweils wirksamen Wert pro messlokalem Tag, nicht alle Quellversionen:
[Apple: restingHeartRate](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/restingheartrate).

### Aktive Energie: in V0.2 kein persönlicher Referenzbereich

Für kanonische `active_energy`-Quellsamples darf V0.2 keine persönliche
Referenzauffälligkeit erzeugen. Apple beschreibt diese Samples als kumulative
Werte, die HealthKit verdichten oder zusammenführen kann. Ihr Betrag hängt daher
von Sampledauer und Segmentierung ab; einzelne Samples sind untereinander keine
stabile persönliche Vergleichsgröße:
[Apple: activeEnergyBurned](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/activeenergyburned).

Ein persönlicher Bereich wird erst für einen späteren, quellenabdeckungsbewussten
Tagesaggregate-Typ erwogen. Vor der in V0.3 geplanten Quellenabdeckung kann ein
niedriger Tageswert zudem nicht zuverlässig von einem unbeobachteten Zeitraum
unterschieden werden. Feste typgebundene Importgrenzen bleiben davon unberührt.

## Gemeinsame Auswertungsregeln

- Fehlende Werte werden ausgelassen, nie mit null ersetzt und nie interpoliert.
  Ein fehlender Wert erzeugt durch diese Regel keine Plausibilitätsauffälligkeit.
- Bei weniger als 28 Referenzwerten lautet der Zustand `nicht bereit`; es wird
  kein künstlich breiter oder schmaler Bereich geschätzt.
- Bei `MAD == 0` ist die persönliche Regel `nicht trennscharf` und erzeugt keine
  Auffälligkeit. V0.2 führt dafür weder Epsilon noch eine zweite
  Streuungsmethode ein; feste Grenzen prüfen den Wert weiterhin.
- Die Referenz verwendet frühere wirksame Werte, die endlich sind und feste
  typgebundene Grenzen bestehen. Eine persönliche Auffälligkeit allein schließt
  einen früheren Wert nicht aus: Sie ist kein Fehlerbeweis, der Median/MAD ist
  gegen einzelne Extremwerte robust, und ein anhaltender realer Niveauwechsel
  muss die nachlaufende Referenz verschieben können. Eine Korrektur ersetzt den
  Quellwert über den wirksamen Analysewert.
- Der persönliche Bereich ergänzt feste Plausibilitätsgrenzen. Er darf niemals
  einen Wert automatisch korrigieren, löschen, aus Analysen ausschließen oder
  als medizinisch gefährlich bezeichnen.

## Verworfene Alternativen

- **Mittelwert und Standardabweichung:** Beide werden gerade durch die Werte
  verschoben, die erkannt werden sollen; klassische Z-Werte sind laut NIST bei
  kleinen Stichproben zusätzlich irreführend.
- **Empirische 2,5-/97,5-Prozentile:** Bei der kleinen V0.2-Referenz beruhen die
  Ränder praktisch auf wenigen Extremwerten und sind weder stabil noch
  ausreißerrobust.
- **IQR/Tukey-Fences:** Ebenfalls robust, aber ohne Vorteil gegenüber dem direkt
  als modifizierten Z-Wert erklärbaren MAD-Verfahren.
- **Qn, Change-Point-, saisonale oder lernende Modelle:** Für zwei aktuelle
  Typen unnötige zusätzliche Methodik und Parameter. Sie werden erst erwogen,
  wenn V0.5-Daten ein konkretes Versagen von Median/MAD zeigen.
- **Persönlicher Bereich für aktive Quellsamples:** Vergleicht inkommensurable
  kumulative Fragmente. Ein Tagesbereich vor der Quellenabdeckung würde fehlende
  Beobachtung mit geringer Aktivität verwechseln.

## Synthetische Akzeptanzfälle

1. 27 frühere Ruhepulswerte: kein persönlicher Bereich, keine persönliche
   Auffälligkeit.
2. 28 frühere Werte in 42 Tagen mit `MAD > 0`: Bereich und Entscheidung sind aus
   genau diesen früheren Werten reproduzierbar.
3. Ein einzelner extremer früherer, aber festerseits gültiger Wert verschiebt
   Median und MAD nicht wesentlich.
4. Fehlende Tage senken nur die Anzahl; sie erscheinen nicht als Nullwerte. Fällt
   die Anzahl unter 28, wird die persönliche Prüfung ausgesetzt.
5. Eine anhaltende Niveauverschiebung wandert mit dem Fenster in die Referenz;
   es gibt keine Verwendung zukünftiger Werte.
6. Identische frühere Werte mit `MAD == 0`: keine persönliche Auffälligkeit.
7. Kein `active_energy`-Quellsample erzeugt eine persönliche Auffälligkeit,
   unabhängig von Wert oder Dauer.
8. Feste Plausibilitätsgrenzen funktionieren unabhängig von der Bereitschaft der
   persönlichen Regel.

## Bezug zum aktuellen Projektstand

Der aktuelle Typkatalog und die typgebundenen Einheiten stehen in
[`health_data`](../../src/personal_health_lab/health_data/__init__.py). Die
Zielarchitektur verlangt eine geschlossene, versionierte Typmenge und plant die
Kalibrierung realer Verteilungen ausdrücklich erst für V0.5. V0.2 sollte daher
nur diese eine persönliche Methode und die explizite Nichtanwendbarkeit für
aktive Quellenergie spezifizieren; zusätzliche Typen erhalten ihre Regel erst
mit ihrer Aufnahme in den kanonischen Katalog.
