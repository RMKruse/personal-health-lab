# Methoden für kurz- und langfristige Aktivitäts-Verzögerungsprofile

## Fragestellung und Geltungsbereich

Gesucht ist eine wissenschaftlich vertretbare V0.4-Modellfamilie für zwei **getrennte**
gemeinsame Verzögerungsprofile des Apple-Ruhepulses: Tag 1–7 und Tag 1–30. Beide Modelle
sollen mehrere korrelierte allgemeine Aktivitätsmerkmale und analytische Trainingsarten
gleichzeitig berücksichtigen, überlappende Aktivitätstage korrekt abbilden und Schlaf-,
Krankheits-, Stress- und Medikamentenkontext sparsam einordnen. Es geht um bedingte
Assoziationen einer Person, nicht um kausale oder medizinische Aussagen.

Apple beschreibt `restingHeartRate` als diskreten Tageswert, der aus über den Tag
verteilten Ruhephasen geschätzt und für den aktuellen oder vorherigen Tag ersetzt werden
kann. Das Modell muss deshalb den jeweils wirksamen Tageswert verwenden und darf ihn
nicht als rohe kontinuierliche Herzfrequenz behandeln
([Apple HealthKit](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/restingheartrate)).

## Empfehlung

V0.4 soll zwei versionierte, unabhängig geschätzte **lineare penalized Distributed-Lag-
Modelle** erhalten, eines mit `L = 7`, eines mit `L = 30`:

```text
RHR[t] = Interzept + Kalendertrend[t] + Wochentag[t] + Kontext[t]
         + Summe über Merkmale j und Lags l=1..L von X[j,t-l] * beta[j,l]
         + Fehler[t]
```

Alle zulässigen Aktivitätsmerkmale stehen gemeinsam in derselben Designmatrix; dadurch
werden mehrere frühere Aktivitätstage demselben späteren Ruhepuls gleichzeitig
gegenübergestellt. Genau dafür wurden Distributed-Lag-Modelle entwickelt. Eine Basis im
Lag-Raum erlaubt außerdem, die verzögerte Struktur als zusammenhängendes Profil statt als
Folge unabhängiger Einzeltests zu schätzen
([Gasparrini, Armstrong & Kenward 2010](https://doi.org/10.1002/sim.3940)).

Die Dosisbeziehung bleibt in V0.4 je Merkmal linear; nur die Form über die Lags wird
geglättet. Das ist ein absichtlicher, datenparsamer Spezialfall der penalisierten DLNM-
Familie. Penalized DLNMs erlauben getrennte Strafterme für die Lag-Struktur und eine
zusätzliche Schrumpfung zum Nullprofil; Simulationen im Methodenpapier zeigten bessere
inferentielle Eigenschaften als unpenalisierte Formen
([Gasparrini et al. 2017](https://academic.oup.com/biometrics/article/73/3/938/7537723),
[Korrektur 2022](https://academic.oup.com/biometrics/article/78/2/812/7460013)).

Die beiden Horizonte verwenden dieselbe Modellfamilie, aber jeweils eigene Lag-Basis,
Strafparameter, Bootstrap-Regel, Diagnostik und Modellreife. Das 1–7-Profil wird **nicht**
aus dem 1–30-Lauf ausgeschnitten: Die Wahl des maximalen Lags beeinflusst Schätzung und
Glättung, und ein langer Lauf könnte Information und Regularisierung aus Tag 8–30 in die
kurze Kurve tragen. Da die Läufe teilweise dieselben Daten verwenden, werden ihre
Ergebnisse zugleich nicht als statistisch unabhängig verglichen. Diese Trennung ist eine
projektbezogene methodische Ableitung, keine Behauptung aus den zitierten Studien.

## Modellform und Regularisierung

1. Alle kontinuierlichen Aktivitätsmerkmale werden innerhalb des Analysezeitraums
   zentriert und skaliert. Angezeigt werden das Profil in natürlicher Einheit und pro
   persönlicher Standardabweichung; die Merkmale werden weder addiert noch zu einem
   Aktivitätsscore verdichtet.
2. Je Merkmalsprofil bestraft ein quadratischer Differenzterm unnötige Krümmung über
   benachbarte Lags. Ein zusätzlicher schwacher Ridge-Term schrumpft das ganze Profil und
   stabilisiert korrelierte Merkmale. Ridge wurde gerade für nichtorthogonale
   Regressionsprädiktoren als Bias-Varianz-Kompromiss eingeführt
   ([Hoerl & Kennard 1970](https://doi.org/10.1080/00401706.1970.10488634));
   Differenzstrafen sind der Kern von P-Splines
   ([Eilers & Marx 1996](https://doi.org/10.1214/ss/1038425655)).
3. Strafparameter und Basisdimension sind horizon-spezifische, deterministische Regeln
   der Analysedefinition. Sie werden im Kalibrierungsprototyp auf einem vorab
   festgelegten Gitter gewählt und dann eingefroren. Keine Laufzeit-AIC-Auswahl und kein
   schrittweises Entfernen einzelner Lags: nicht berücksichtigte Modellselektion kann die
   Intervallabdeckung verschlechtern
   ([Gasparrini et al. 2017](https://academic.oup.com/biometrics/article/73/3/938/7537723)).
4. Eine L1-/Lasso-Auswahl ist nicht die Primärmethode. Bei stark korrelierten Prädiktoren
   kann sie einzelne Stellvertreter auswählen; Elastic Net gruppiert solche Prädiktoren
   eher, bildet aber die Ordnung der Lags ohne weitere Struktur nicht ab
   ([Zou & Hastie 2005](https://doi.org/10.1111/j.1467-9868.2005.00503.x)).
5. Das Modell weist je Merkmal tägliche Lag-Schätzungen und vorab definierte kumulative
   Kontraste aus: 1–7 im Kurzmodell sowie 1–7, 8–30 und 1–30 im Langmodell. Es summiert
   niemals Koeffizienten verschiedener Merkmale oder Trainingsarten.

Mehrere Expositionen vervielfachen die Parameterzahl; veröffentlichte Multiple-Exposure-
DLMs reduzieren sie deshalb mit glatten Basen und ganzer Profilselektion. Das belegt den
Bedarf an Dimensionsreduktion, nicht die Schätzbarkeit von Interaktionen in einer einzigen
persönlichen Zeitreihe
([Antonelli, Wilson & Coull 2022](https://pmc.ncbi.nlm.nih.gov/articles/PMC10724118/)).
V0.4 enthält daher weder Aktivität×Aktivität-Interaktionen noch nichtlineare Dosisflächen.

### Merkmale und analytische Trainingsarten

- Allgemeine Tagesmerkmale wie aktive Energie, Trainingszeit, Schritte und Distanz dürfen
  gemeinsam eingehen. Hohe Korrelation wird diagnostiziert und regularisiert, aber nicht
  als identifizierte physiologische Trennung ausgegeben.
- Analytische Trainingsarten werden als getrennte tägliche Dauer- und Energiemerkmale
  modelliert; mehrere Arten dürfen am selben Tag positiv sein. Seltene Arten werden durch
  die versionierte Gruppierungsregel zu „Sonstige“ gebündelt.
- Eine Gesamtdauer, die exakt die Summe aller Typdauern ist, darf nicht zusätzlich neben
  sämtlichen Teilen stehen. Gleiches gilt für jede andere deterministische Doppelcodierung.
- Überlappende Rohmessungen werden nicht statistisch „repariert“. Das Modell verwendet die
  qualitätsgeprüften Tagesaggregate; offene Überlappungsfälle machen das Ergebnis gemäß
  [`CONTEXT.md`](../../CONTEXT.md#überlappende-aktivitätsmessungen) vorläufig.

Glatte Einschränkungen können bei stark korrelierten Lag-Prädiktoren selbst scheinbare
Gegenbewegungen erzeugen. Darum sind bekannte Null-, monotone, verzögerte und
Vorzeichenwechsel-Szenarien sowie Sensitivität gegen Basis und Strafterm verpflichtend
([Basagaña et al. 2022](https://doi.org/10.1093/ije/dyab179)).

### Kontext

Der Primärlauf enthält nur eine vorab festgelegte, niedrigdimensionale
Outcome-Tages-Adjustierung:

- glatter Kalendertrend und Wochentag;
- Gesamtschlafdauer der zugeordneten Nacht plus Beobachtungsindikator, nicht gleichzeitig
  alle sich zu einer Summe ergänzenden Schlafstadien;
- höchste Krankheitsstufe als eine Codierung mit Baseline-Referenz erst ab dem
  Kontextabdeckungsbeginn, nicht zusätzlich ein deterministisch redundantes
  Krankheits-Ja/Nein; frühere unbekannte Tage werden nicht als gesund umgedeutet;
- Stressabweichung von der Baseline und Herkunft `unbekannt`/`angenommen`/`bestätigt`;
- Medikamenten-Regimewechsel als Segmentgrenze sowie sparsame Ereignisindikatoren für
  Abweichung und Bedarfseinnahme, keine substanzspezifischen Lag-Profile.

Ein zweiter, klar als Sensitivität bezeichneter Lauf ohne diese Kontextterme zeigt, wie
stark die Aktivitätsprofile von der Adjustierung abhängen. Schlaf, Krankheit, Stress und
Medikamente können zeitlich sowohl vor als auch nach Aktivität liegen. Konventionelle
Adjustierung zeitabhängiger Größen, die selbst von früherer Exposition beeinflusst wurden,
identifiziert keinen kausalen Effekt
([Robins, Hernán & Brumback 2000](https://pubmed.ncbi.nlm.nih.gov/10955408/)).
Deshalb bleiben beide Varianten des V0.4-Modells deskriptive bedingte Assoziationen.

## Unsicherheit und Zeitreihenabhängigkeit

Die Primärunsicherheit bleibt ein Moving-Block-Bootstrap, weil zusammenhängende Blöcke die
Abhängigkeit stationärer Beobachtungsfolgen erhalten
([Künsch 1989](https://doi.org/10.1214/aos/1176347265)). Dabei gelten folgende Regeln:

- Aktivität, Kontext und Ruhepuls werden stets als ausgerichteter Tagesvektor behandelt;
  niemals werden Merkmale getrennt oder einzelne Tage iid resampelt.
- Die Blocklänge wird pro Horizont aus einer versionierten Regel bestimmt und gegen eine
  kleine vorab festgelegte Nachbarschaft geprüft. Verfahren zur datenabhängigen Wahl einer
  Blocklänge sind publiziert
  ([Politis & White 2004](https://doi.org/10.1081/ETC-120028836)); eine einzelne feste
  Sieben-Tage-Regel genügt nicht automatisch für beide Horizonte.
- Jede Replik schätzt den vollständigen Fit erneut. Der Seed, die Blocklängenregel und die
  Zahl erfolgreicher Repliken werden gespeichert. 250 Repliken sind für eine 2,5-%-Flanke
  nur etwa sechs Rangpositionen tief; V0.4 verwendet mindestens 2.000 erfolgreiche
  Repliken und berichtet Monte-Carlo-Stabilität.
- Je Schätzung gibt es ein punktweises Intervall; primär ist ein studentisiertes
  Maximalabweichungsband über **alle angezeigten Lag-Koeffizienten des Modelllaufs**.
  Kumulative Kontraste werden in jeder Replik neu berechnet. Einzelne „signifikante Tage“
  werden nicht herausgegriffen. Bei penalisierten Glättungen hängt Intervallabdeckung auch
  von Glättungsbias und nicht berücksichtigter Strafparameterunsicherheit ab
  ([Marra & Wood 2012](https://doi.org/10.1111/j.1467-9469.2011.00760.x)); deshalb bleibt
  empirische Abdeckung im Release-Gate zwingend.

Starke verbleibende Residualautokorrelation ist kein Grund, iid-Standardfehler zu zeigen,
sondern ein nicht bestandenes Reifekriterium. Der Ljung-Box-Portmanteau-Test ist ein
etablierter Mehr-Lag-Test auf verbleibende Zeitreihenstruktur
([Ljung & Box 1978](https://doi.org/10.1093/biomet/65.2.297)); V0.4 kombiniert ihn mit
Residual-ACF und Blocklängensensitivität, statt allein einen p-Wert als Wahrheitsschwelle
zu verwenden.

## Modellreife

Es gibt keine wissenschaftlich universelle Mindestzahl von Kalendertagen für diese
konkrete Kombination aus Horizont, Korrelation, Missingness und Strafterm. Die Schwellen
werden je Analysedefinition mit Simulationen festgelegt. Simulationen können Bias,
Intervallabdeckung und Fehler unter bekannter Wahrheit direkt messen; ihre Planung soll
Aims, data-generating mechanisms, estimands, methods und performance measures explizit
festlegen
([Morris, White & Crowther 2019](https://pmc.ncbi.nlm.nih.gov/articles/PMC6492164/)).

Ein berechenbarer Lauf ist nur dann `robust`, wenn **alle** versionierten Kriterien seines
Horizonts bestehen:

1. **Datenmenge und Vollständigkeit:** genügend vollständige Outcome-Tage nach Abzug des
   Lag-Vorlaufs, ausreichende Aktivitäts- und Kontextabdeckung und genügend effektiv
   unabhängige Blöcke; Grenzwerte stammen aus der Kalibrierung, nicht aus einer 90-/180-
   Tage-Faustregel.
2. **Merkmalsunterstützung:** jedes ausgewiesene Merkmal hat genügend Variation und jede
   Trainingsgruppe genügend positive Tage; keine konstante oder deterministisch redundante
   Spalte.
3. **Abhängigkeit und Numerik:** SVD-Rang, kleinster Singulärwert und Konditionszahl der
   standardisierten unpenalisierten und augmentierten Designmatrix liegen im kalibrierten
   Bereich. Eine maximale paarweise Korrelation allein reicht für Mehrfachabhängigkeiten
   nicht aus; Konditionsdiagnostik wurde gerade zur Lokalisierung kollinearer Beziehungen
   entwickelt
   ([Belsley, Kuh & Welsch 1980](https://doi.org/10.1002/0471725153)).
4. **Fit und Residuen:** endliche Schätzung, Strafparameter nicht am Suchgitterrand,
   ausreichende Residualfreiheitsgrade und keine nach der versionierten ACF/Ljung-Box-Regel
   unmodellierte starke Zeitstruktur.
5. **Bootstrap:** kalibrierte Mindesterfolgsrate, stabile Quantile und simultane Bänder bei
   benachbarten Blocklängen.
6. **Schätzstabilität:** vorab festgelegte Toleranzen für Richtung und praktisch relevante
   Größe der kumulativen Kontraste bei Basis-/Strafterm-Nachbarschaft, Kontext-Sensitivität
   und Weglassen zusammenhängender Zeitabschnitte.
7. **Synthetisches Release-Gate:** getrennt für 1–7 und 1–30 werden bekannte Null-,
   verzögerte, glatte, scharf begrenzte und Vorzeichenwechsel-Profile unter abgestuften
   Korrelationen, Lücken, Kontextlagen und Rauschstärken geprüft. Akzeptanzmaße sind Bias,
   RMSE, Abdeckung der punktweisen und simultanen Intervalle, Null-Fehlalarm und
   Bootstrap-Ausfallrate.

Ein formal berechenbarer Lauf mit mindestens einem Fehlkriterium bleibt `exploratory`;
Singularität, nicht endliche Werte oder zu wenige Fit-Zeilen erzeugen dagegen kein
Analyseergebnis. `robust` bedeutet statistische Stabilität innerhalb der eingefrorenen
Definition, weder Kausalität noch medizinische Gültigkeit.

## Projektabhängigkeiten und minimaler Technikentscheid

Der aktuelle Stand deklariert nur DuckDB, jsonschema und Streamlit
([`pyproject.toml`](../../pyproject.toml)); NumPy erscheint lediglich transitiv über
Streamlit ([`uv.lock`](../../uv.lock)). DuckDB stellt univariate `regr_*`-Aggregate bereit,
aber keinen strukturiert penalisierten Mehrfach-Lag-Fit
([DuckDB-Dokumentation](https://duckdb.org/docs/stable/sql/functions/aggregates.html)).
Der vorhandene Fit bildet Normalgleichungen und löst sie selbst mit Gauß-Jordan
([`resting_hr_analysis`](../../src/personal_health_lab/resting_hr_analysis/__init__.py));
das ist für eine korrelierte 30-Tage-Mehrfachmatrix kein angemessener numerischer Kern.

**Einzige jetzt begründete neue Direktabhängigkeit: `numpy`.** `numpy.linalg.lstsq` löst
auch unter- oder überbestimmte Least-Squares-Systeme mit Rang- und Singulärwertdiagnostik,
und `numpy.linalg.cond` liefert die SVD-basierte Konditionszahl
([NumPy `lstsq`](https://numpy.org/doc/stable/reference/generated/numpy.linalg.lstsq.html),
[NumPy `cond`](https://numpy.org/doc/stable/reference/generated/numpy.linalg.cond.html)).
Damit lassen sich Differenz- und Ridge-Strafen als zusätzliche Zeilen einer augmentierten
Designmatrix lösen und alle Bootstrap-Repliken ohne weiteres Statistik-Framework fitten.
Eine transitive Abhängigkeit darf dafür nicht stillschweigend als öffentliche
Projektabhängigkeit verwendet werden.

`scipy`, `statsmodels`, scikit-learn oder ein probabilistisches Framework werden in V0.4
nicht aufgenommen. `statsmodels.GLMGam.select_penweight` ist aktuell ausdrücklich
experimentell und warnt vor lokalen Optima
([statsmodels-Dokumentation](https://www.statsmodels.org/stable/generated/statsmodels.gam.generalized_additive_model.GLMGam.select_penweight.html)).
Eine solche Abhängigkeit wird erst begründet, wenn der Kalibrierungsprototyp zeigt, dass
augmentiertes NumPy-Least-Squares die Reifeziele nicht erreicht oder eine spätere
nichtlineare Dosisfläche beziehungsweise explizite AR-Fehlerstruktur benötigt wird.

## Notwendiger Kalibrierungsprototyp

Vor Implementierung der produktiven Verträge ist ein kleiner, gemeinsam ausgewerteter
Methodenprototyp erforderlich. Er vergleicht für beide Horizonte nur wenige vorab
definierte Basis-/Strafgitter und Blocklängenregeln auf den oben genannten synthetischen
Szenarien und liefert die numerischen Reifeschwellen sowie den minimalen Bootstrap-Umfang.
Er ist kein UI-Prototyp und kein frei konfigurierbares Modell. Erst dieses Experiment kann
entscheiden, ob NumPy genügt; die Literatur allein kann keine projektspezifischen Schwellen
für eine 365-Tage-Einpersonen-Zeitreihe liefern.

## Verworfene Alternativen

- **Unabhängige Regression je Lag und Merkmal:** ignoriert überlappende Aktivität und
  vervielfacht Auswahlmöglichkeiten.
- **Ein 1–30-Lauf mit kurzer 1–7-Ansicht:** verletzt die geforderte getrennte
  Regularisierung und Modellreife.
- **Unpenalisierte 30×Merkmal-Koeffizienten:** zu empfindlich gegenüber benachbarten und
  merkmalsübergreifenden Korrelationen.
- **Lasso-/schrittweise Merkmalsauswahl:** instabile Stellvertreterwahl und zusätzliche
  Selektionsunsicherheit.
- **Nichtlineare Dosis-Lag-Flächen, Interaktionen oder substanzspezifische Medikamente:**
  Parameteraufwand ohne derzeit belegte persönliche Dosisunterstützung.
- **Bayesianisches Hierarchiemodell als V0.4-Standard:** methodisch vertretbar, aber mit
  neuer Laufzeit, Priorkalibrierung und Konvergenzdiagnostik; erst erwägen, wenn der
  minimale frequentistische Prototyp die Reifekriterien verfehlt.
