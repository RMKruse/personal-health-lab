# Personal Health Analytics

Dieser Kontext beschreibt die fachliche Sprache für die persönliche Auswertung längsschnittlicher Gesundheits-, Aktivitäts- und Kontextdaten einer einzelnen Person.

Technische und betriebliche Begriffe stehen getrennt in [docs/TECHNICAL_GLOSSARY.md](./docs/TECHNICAL_GLOSSARY.md).

## Language

### Analysen und Outcomes

**Assoziation**:
Ein statistischer Zusammenhang zwischen zeitlich zugeordneten Messgrößen, der für sich allein keine Ursache-Wirkungs-Beziehung belegt. Der MVP macht Assoziationen zwischen Training, Ruhepuls und Körpergewicht sichtbar.
_Avoid_: Einfluss, Wirkung, Effekt

**Persönlicher kausaler Effekt**:
Die unter expliziten Annahmen geschätzte Veränderung eines gesundheitlichen Outcomes, die bei derselben Person auf eine Exposition zurückzuführen ist. Seine Schätzung ist ein langfristiges Ziel und nicht Bestandteil des ersten MVP.
_Avoid_: Korrelation, bloßer Zusammenhang, Gesundheits-Impact

**Aktivitätsmerkmal**:
Eine einzelne, getrennt analysierte Messgröße körperlicher Aktivität, beispielsweise Trainingsdauer oder aktive Energie. Mehrere Aktivitätsmerkmale dürfen dieselbe Aktivität teilweise abbilden und werden im MVP nicht zu einem Gesamtwert addiert.
_Avoid_: Trainingsbelastung, Aktivitätsscore, Gesundheits-Impact

**Gewichtstrend**:
Der geglättete längerfristige Verlauf der Körpermasse, bei dem kurzfristige Schwankungen einzelner Gewichtsmessungen nicht als relevantes Ergebnis interpretiert werden.
_Avoid_: Tagesgewicht, kurzfristige Gewichtsreaktion

**Outcome-Analyse**:
Eine Analyse mit genau einer primären Zielgröße. Ruhepuls und Gewichtstrend werden im MVP in getrennten Outcome-Analysen untersucht, auch wenn ihre Ergebnisse später gemeinsam betrachtet werden.
_Avoid_: Gesundheits-Impact-Analyse, Gesamtwirkung

**Anzeigezeitraum**:
Der in einer Ansicht sichtbare zeitliche Ausschnitt eines bereits berechneten Ergebnisses oder einer Datenprojektion. Seine Änderung startet keinen neuen Modelllauf.
_Avoid_: Analysezeitraum, automatische Neuberechnung

**Analysezeitraum**:
Der beim Start eines Modelllaufs ausdrücklich gewählte und mit ihm gespeicherte Zeitraum der geeigneten Eingabedaten. Er wird unabhängig vom Anzeigezeitraum auf Modellreife geprüft; standardmäßig umfasst er die gesamte geeignete Historie.
_Avoid_: Anzeigezeitraum, flüchtiger Diagrammausschnitt

**Gesamtübersicht**:
Eine gemeinsame oder eng gekoppelte Zeitansicht der getrennten Outcome-Analysen für Ruhepuls und Gewichtstrend. Sie ergänzt die Einzelanalysen um eine einfache statistische Beschreibung ihres Zusammenhangs, ohne eine Wirkrichtung zu behaupten.
_Avoid_: Kausalmodell, Gesamtwirkung

**Trendzusammenhang**:
Die statistisch beschriebene Gemeinsamkeit der langfristigen Verläufe zweier Outcomes. Ein Trendzusammenhang allein sagt nicht aus, ob kurzfristige Veränderungen gemeinsam auftreten oder ein Outcome das andere verursacht.
_Avoid_: kurzfristiger Zusammenhang, Wirkung

**Abweichungszusammenhang**:
Der statistisch beschriebene Zusammenhang kurzfristiger Abweichungen zweier Outcomes von ihren jeweiligen längerfristigen Verläufen. Er wird getrennt vom Trendzusammenhang ausgewiesen und belegt keine Wirkrichtung.
_Avoid_: Trendzusammenhang, Wirkung

### Zeit, Gewicht und Aktivität

**Gewichtsmessprotokoll**:
Die angestrebte morgendliche Gewichtsmessung an jedem Tag unter möglichst vergleichbaren Bedingungen. Mehrere Messungen pro Woche gelten als Mindestdichte; bei mehreren Messungen an einem Tag bestimmt jedoch die Regel für das bevorzugte Tagesgewicht den Analysewert. Nicht gemessene Tage sind fehlende Beobachtungen und keine Gewichtsfortschreibung.
_Avoid_: tägliche Vollständigkeit, automatisch geschätztes Tagesgewicht

**Bevorzugtes Tagesgewicht**:
Die nach Messzeitpunkt letzte wirksame Gewichtsmessung eines lokalen Kalendertags, unabhängig von Tageszeit oder ursprünglicher Apple-Health-Quelle. Gleichzeitige letzte Messungen mit demselben Wert liefern gemeinsam diesen Tageswert; bei unterschiedlichen Werten bleibt das Tagesgewicht bis zur Prüfung mehrdeutig. Frühere Messungen und alle beitragenden Messungsidentitäten bleiben erhalten.
_Avoid_: Tagesmittelwert, automatisch bevorzugte Morgenmessung, willkürlicher Quellen-Tiebreaker

**Messlokaler Kalendertag**:
Der Kalendertag in der Zeitzone, die am Messzeitpunkt für den ursprünglichen HealthKit-Zeitstempel galt. Tagesaggregationen verwenden diese Zeitzone und werden nicht nach der aktuellen Mac-Zeitzone rückdatiert.
_Avoid_: aktuelle Anzeigezeitzone als historische Tagesgrenze, UTC-Kalendertag

**Schlaftag**:
Der messlokale Kalendertag, an dem eine Schlafnacht endet und die Person aufwacht. Schlafintervalle über Mitternacht werden als Kontext diesem Aufwachtag zugeordnet.
_Avoid_: Einschlaftag, Aufteilung einer Nacht in zwei unabhängige Nächte

**Apple-Watch-Schlaf**:
Die aus eindeutig als Apple Watch klassifizierten Schlafintervallen abgeleitete Schlafnacht samt ihrer Merkmale. Schlafintervalle anderer oder unklarer Herkunft bleiben als kanonische Beobachtungen erhalten, tragen aber nicht zu Apple-Watch-Schlaf bei.
_Avoid_: Schlaf aus beliebiger Quelle, Zusammenführung unklarer Schlafquellen

**Schlafepisode**:
Eine zeitlich zusammenhängende Gruppe von Apple-Watch-Schlafintervallen, zwischen denen höchstens 90 Minuten liegen. Nicht durch Intervalle abgedeckte Lücken bleiben unbekannt und werden nicht als Schlaf fortgeschrieben.
_Avoid_: lückenlos angenommener Schlaf, Zusammenführung über lange Datenlücken

**Schlafnacht**:
Die eindeutig einzige Apple-Watch-Schlafepisode mit positiver und unter den Episoden desselben Schlaftags größter beobachteter Schlafdauer. Weitere Episoden bleiben als Nickerchen sichtbar; bei Gleichstand gibt es keine willkürlich gewählte Schlafnacht.
_Avoid_: Summe aller Schlafepisoden eines Tages, automatisch eingerechnetes Nickerchen

**Mehrdeutiges Schlafsegment**:
Ein Zeitabschnitt, für den sich gültige Apple-Watch-Schlafintervalle fachlich widersprechen. Widersprüchliche Schlafstadien belegen Schlaf ohne eindeutiges Stadium; ein Widerspruch zwischen Wach- und Schlafzustand belegt weder Schlaf- noch Wachdauer.
_Avoid_: willkürlich bevorzugtes Schlafstadium, doppelt gezählte Überlappung

**Schlafbeobachtungsstatus**:
Die Einordnung eines Schlaftags als `unbeobachtet`, `teilweise beobachtet` oder `beobachtet`. Unbeobachtete Merkmale bleiben fehlend; teilweise beobachtete Nächte weisen nur tatsächlich belegte Werte samt Qualitätsangaben aus und werden weder mit null noch durch Fortschreibung ergänzt.
_Avoid_: fehlender Schlaf als Nulldauer, imputierte Schlafnacht

**Schlafmerkmal**:
Ein unmittelbar aus einer Schlafnacht abgeleiteter Zeitpunkt oder eine beobachtete Dauer für Schlaf, Wachheit, Bettzeit oder ein einzelnes Schlafstadium. V0.3 bildet weder zusammengesetzte Schlafscores noch klinische Bewertungen.
_Avoid_: Schlafqualitätsscore, klinische Schlafbewertung, automatisch interpretierter Stufenanteil

**Schlafdatenqualität**:
Die nachvollziehbare Abdeckung, Herkunft und Mehrdeutigkeit der Beobachtungen, aus denen Schlafmerkmale abgeleitet sind. Sie bleibt als einzelne Evidenzangaben sichtbar und wird nicht zu einer Qualitätsnote verdichtet.
_Avoid_: kombinierte Schlafqualitätsnote, automatisch als gut oder schlecht bewerteter Schlaf

**Trainingseinheit**:
Ein als zusammenhängendes Workout aufgezeichneter Zeitraum mit originaler Trainingsart und optionalen Angaben zu Dauer, Distanz und aktiver Energie. Für Trainingsmerkmale gilt die gemeldete Dauer; nur wenn sie fehlt, wird die verstrichene Zeit zwischen Beginn und Ende verwendet. Eine unmögliche gemeldete Dauer oder negative gemeldete Summe wird zur Prüfung vorgelegt und weder stillschweigend ersetzt noch korrigiert. Die Angaben der Trainingseinheit bleiben von zeitlich überlappenden allgemeinen Aktivitätsmessungen getrennt: Sie tragen nur zu Trainingsartmerkmalen bei, während allgemeine Aktivitätsaggregate ausschließlich aus den entsprechenden Aktivitätsmessungen entstehen. Zwischen beiden Darstellungen findet weder eine automatische Zusammenführung noch eine Addition statt.
_Avoid_: Duplikat allgemeiner Aktivitätsmessungen, zusätzliche Quelle für Tagesaggregate

**Überlappende Trainingseinheiten**:
Zwei verschiedene wirksame Trainingseinheiten, deren Zeiträume sich für eine positive Dauer überschneiden. Beide bleiben unverändert erhalten und werden weder automatisch zusammengeführt noch zeitlich beschnitten. Die Überlappung öffnet einen Datenprüffall; bis zu einer Korrektur oder einem lokalen Ausschluss tragen beide Trainingseinheiten zu ihren Trainingsartmerkmalen bei und davon abhängige Ergebnisse bleiben vorläufig.
_Avoid_: Überlappende Aktivität, automatisch deduplizierte Trainingseinheit

**Aktivitätstag**:
Der messlokale Kalendertag, an dem eine allgemeine Aktivitätsmessung beginnt. Auch eine über Mitternacht reichende Messung trägt ihren vollständigen Wert zu diesem Starttag bei, weil Schritte, Distanz, Trainingszeit oder Energie nicht zuverlässig zeitanteilig aufgeteilt werden können. Nur ihr zeitliches Intervall für die tägliche Quellenabdeckung wird an messlokalen Tagesgrenzen geschnitten. Ein Nullwert ist eine beobachtete Null; ein negativer Wert bleibt als auffällige Beobachtung erhalten und öffnet einen Datenprüffall.
_Avoid_: zeitanteilig aufgeteilte Aktivitätsmessung, Endtag

**Überlappende Aktivitätsmessungen**:
Zwei verschiedene wirksame allgemeine Aktivitätsmessungen desselben Aktivitätsmerkmals und derselben Quellenklasse, deren Zeiträume sich für eine positive Dauer überschneiden. Beide bleiben erhalten und tragen bis zu einer Korrektur oder einem lokalen Ausschluss zum Tagesaggregat bei; die Überlappung öffnet einen Datenprüffall und davon abhängige Ergebnisse bleiben vorläufig. Überlappungen verschiedener Aktivitätsmerkmale sind zulässig, während Watch-/iPhone-Überlappungen durch die Aktivitätsquellenregel statt durch diesen Prüffall behandelt werden.
_Avoid_: Überlappende Aktivität, Überlappung verschiedener Aktivitätsmerkmale, automatisch deduplizierte Messung

**Trainingstag**:
Der messlokale Kalendertag, an dem eine Trainingseinheit beginnt. Eine über Mitternacht laufende Trainingseinheit wird als Ganzes diesem Starttag zugeordnet und nicht in zwei Trainingseinheiten geteilt.
_Avoid_: Endtag, geteilte Trainingseinheit

**Trainingsartmerkmal**:
Die pro Trainingstag und Trainingsart getrennt aggregierte Trainingsdauer oder Trainingsenergie. Das Modell hält unterschiedliche Trainingsarten als eigene Merkmale auseinander, statt sie ausschließlich durch eine gemeinsame Tagessumme zu ersetzen.
_Avoid_: undifferenzierte Trainingssumme, globaler Aktivitätsscore

**Analytische Trainingsart**:
Die für eine Modellversion verwendete Gruppierung originaler HealthKit-Trainingsarten. Ausreichend häufige Arten bleiben getrennt; seltene Arten werden vorläufig unter „Sonstige“ gebündelt, ohne ihre Originalkategorie zu verlieren.
_Avoid_: überschriebene HealthKit-Trainingsart, dauerhaft feste Gruppierung

**Apple-Ruhepuls**:
Der als HealthKit-Datentyp `restingHeartRate` bereitgestellte, von Apple geschätzte Ruhepuls. Der MVP übernimmt diesen Wert einschließlich seiner Quelle und möglicher nachträglicher Aktualisierungen, statt ihn selbst aus einzelnen Herzfrequenzmessungen abzuleiten.
_Avoid_: selbst berechneter Ruhepuls, Herzfrequenz im Sitzen

**Verzögerungsprofil**:
Die getrennte Beschreibung der Assoziation zwischen den Aktivitätsmerkmalen eines Tages und dem Apple-Ruhepuls an den folgenden Tagen eines versionierten Analysehorizonts. Es beschreibt zeitliche Zusammenhänge und keine Wirkungsdauer.
_Avoid_: Wirkungsprofil, Effektkurve

**Kurzfristiges Verzögerungsprofil**:
Ein eigenständiges Verzögerungsprofil für die folgenden Tage 1 bis 7. Es besitzt eine eigene Modellreife und bleibt vom langfristigen Verzögerungsprofil getrennt.
_Avoid_: Kurzansicht des langfristigen Verzögerungsprofils, frei gewähltes Lag-Fenster

**Langfristiges Verzögerungsprofil**:
Ein eigenständiges Verzögerungsprofil für die folgenden Tage 1 bis 30. Es besitzt eine eigene Regularisierung, Modellreife und Diagnostik und ist keine bloße Erweiterung des kurzfristigen Modelllaufs.
_Avoid_: frei gewähltes Lag-Fenster, Tag-30-Einzelanalyse

**Überlappende Aktivität**:
Die Situation, in der innerhalb des versionierten Verzögerungsfensters mehrere Aktivitätstage liegen und daher mehreren früheren Tagen derselbe spätere Ruhepuls gegenübersteht.
_Avoid_: additiver Effekt

**Gemeinsames Verzögerungsprofil**:
Ein Verzögerungsprofil, das die Aktivität an den einzelnen Vortagen seines versionierten Analysehorizonts gleichzeitig berücksichtigt. Es trennt die zeitlichen Assoziationen unter überlappender Aktivität, ohne sie als einzelne kausale Beiträge zu interpretieren.
_Avoid_: unabhängige Einzelanalysen je Lag-Tag, additiver Effekt

**Gemeinsames Aktivitätsmodell**:
Eine Outcome-Analyse, die mehrere getrennte Aktivitätsmerkmale wie Trainingsdauer und aktive Energie gleichzeitig berücksichtigt. Ihre geschätzten Parameter beschreiben bedingte Assoziationen und sind keine Gewichte eines Aktivitätsscores; Merkmalsauswahl und Modellform dürfen nach statistischer Prüfung geändert werden.
_Avoid_: Aktivitätsscore, feste Parametergewichtung

### Kontext, Krankheit und Medikamente

**Kontextmerkmal**:
Eine zeitlich zugeordnete Größe, die bei der Interpretation einer Assoziation helfen kann, ohne automatisch ein nachgewiesener Confounder zu sein. Im MVP relevante Kontextmerkmale sind Schlaf, Krankheit, Stress, Kalorienaufnahme und Medikamente; Alkohol und nichtmedizinischer Drogenkonsum liegen außerhalb des persönlichen Anwendungsfalls.
_Avoid_: gesicherter Confounder, Ursache

**Baselineannahme**:
Der ohne täglichen Check-in angenommene Normalzustand: keine Krankheit, durchschnittlicher Stress und keine dokumentierte Medikamentenabweichung. Er bleibt als Annahme gekennzeichnet; „keine Krankheit“ wird nicht aktiv bestätigt, eine durchschnittliche Stressstufe dagegen schon. Eine geplante Dosis ohne erfasste Abweichung gilt nur als planmäßig angenommen und niemals als bestätigte tatsächliche Einnahme.
_Avoid_: bestätigter Normalzustand, gemessener Wert

**Kontextabdeckungsbeginn**:
Das gemeinsame lokale Kalenderdatum, ab dem fehlende Krankheits- und Stressangaben als Baselineannahmen gelten. Frühere Lücken bleiben unbekannt, ausdrücklich erfasste frühere Angaben aber sichtbar.
_Avoid_: rückwirkend angenommene Vollständigkeit, getrennter Abdeckungsbeginn je Merkmal

**Kontextabweichung**:
Eine manuell erfasste Abweichung von der Baselineannahme, beispielsweise Krankheit, über- oder unterdurchschnittlicher Stress oder eine Medikamentenabweichung.
_Avoid_: täglicher Pflicht-Check-in

**Kontextzeitraum**:
Eine Kontextabweichung mit explizitem Beginn und einem expliziten Ende oder fachlich zulässigem offenem Ende, insbesondere für Krankheiten und Medikamentenänderungen. Der Zeitraum ordnet allen betroffenen Tagen denselben dokumentierten Kontext zu, ohne tägliche Wiederholungseingaben zu verlangen.
_Avoid_: Folge unabhängiger Tageseinträge

**Benutzerdefinierter Kontextzeitraum**:
Ein Kontextzeitraum mit wiederverwendbarer benutzerdefinierter Kontextbezeichnung, einschließlich gültigem lokalem Start- und Enddatum und optionaler Notiz, aber ohne Schweregrad oder frei definierbaren Wert. Unterschiedlich bezeichnete Zeiträume dürfen sich überlappen, gleich bezeichnete nicht.
_Avoid_: einmaliger Freitext, Messwert, Schweregrad, reiner Planungseintrag

**Benutzerdefinierte Kontextbezeichnung**:
Eine wiederverwendbare Bezeichnung mit stabiler Identität aus einem eigenen, zunächst leeren Katalog für benutzerdefinierte Kontextzeiträume. Umbenennungen gelten in neuen Datensatz-Snapshots auch für historische Referenzen; eine fachliche Aufteilung erzeugt neue Bezeichnungen und verlangt die ausdrückliche Neuzuordnung der betroffenen Zeiträume.
_Avoid_: einmaliger Freitext, Krankheitskategorie, physisches Löschen

**Manuelle Kontextrevision**:
Eine unveränderliche Fassung eines manuell erfassten Kontextdatensatzes, die in einem Datensatz-Snapshot gebunden ist. Korrektur, Rücknahme und Wiederherstellung erzeugen neue Revisionen und verändern frühere Snapshots nicht.
_Avoid_: Überschreiben, rückwirkende Änderung, Rücknahme als Enddatum, unveröffentlichter Entwurf

**Stressstufe**:
Die ordinale Einordnung „sehr niedrig“, „niedrig“, „durchschnittlich“, „hoch“ oder „sehr hoch“ für genau einen lokalen Kalendertag. Eine aktiv bestätigte durchschnittliche Stufe bleibt von der Baselineannahme unterscheidbar.
_Avoid_: klinische Stressdiagnose, kontinuierlich gemessener Stresswert, Stresszeitraum

**Krankheitskategorie**:
Eine wiederverwendbare, auswählbare Bezeichnung mit stabiler Identität aus einem eigenen Katalog für Krankheitszeiträume. Der vorgegebene Katalog kann dauerhaft um konkrete Kategorien erweitert werden; eine unspezifische Kategorie „Sonstige“ gibt es nicht.
_Avoid_: einmaliger Freitext, benutzerdefinierte Kontextbezeichnung, medizinischer Diagnosecode, physisches Löschen

**Krankheitszeitraum**:
Ein Kontextzeitraum mit Krankheitskategorie, Beginn und dem persönlichen Schweregrad „leicht“, „mittel“ oder „schwer“. Sein Ende darf fehlen, solange die Krankheit läuft; er dokumentiert persönlichen Kontext und keine ärztlich bestätigte Diagnose.
_Avoid_: Diagnose, unabhängige tägliche Krankheitseinträge

**Überlappende Krankheitszeiträume**:
Mehrere gleichzeitig aktive Krankheitszeiträume mit unterschiedlichen Kategorien und jeweils eigenem Schweregrad. Zeiträume derselben Kategorie dürfen sich nicht überlappen; ein Schweregradwechsel wird durch angrenzende Zeiträume abgebildet.
_Avoid_: kombinierte Gesamtdiagnose, genau eine Krankheit pro Tag, überlappende Zeiträume derselben Kategorie

**Krankheitsmerkmal**:
Die vereinfachte Repräsentation von Krankheitszeiträumen im ersten Modell: ob an einem Tag mindestens eine Krankheit aktiv ist und welcher höchste aktive Schweregrad vorliegt. Konkrete Kategorien bleiben gespeichert, werden aber erst bei ausreichender Modellreife einzeln analysiert.
_Avoid_: einzelne seltene Krankheit als unbedingtes Modellmerkmal, Diagnose

**Tägliche Kontextansicht**:
Die nach lokalem Kalenderdatum geordnete Sicht auf den manuellen Kontext eines Datensatz-Snapshots. Sie zeigt die Stressstufe samt Herkunft `unbekannt`, `Baselineannahme` oder `aktiv bestätigt`, alle aktiven Krankheiten mit Kategorie und Schweregrad, das Krankheitsmerkmal sowie alle aktiven benutzerdefinierten Kontextbezeichnungen. Sie bildet keinen kombinierten Kontextscore.
_Avoid_: Verlust überlappender Kontexte, kombinierter Kontextscore

**Medikamentenplan**:
Die einzige persönliche Zeitachse der ab einem festgelegten Zeitpunkt gültigen Baselines mit konkreten Medikamentennamen, Dosierungen und vorgesehenen Einnahmezeitpunkten. Der Einpersonen-Datenspeicher besitzt keine parallelen benannten Pläne, Profile oder Szenarien. Vor dem ersten dokumentierten Plan bleibt der Medikamentenstatus unbekannt; ein ausdrücklich leerer vollständiger Plan dokumentiert dagegen, dass ab seinem Beginn weder geplante Dosen noch Bedarfsmedikationen gelten. V0.3 speichert den validierten Namen unmittelbar im Planeintrag und besitzt keinen globalen Medikamentenkatalog sowie keine Wirkstoff- oder ATC-Normalisierung. Ein Medikamentenname wird getrimmt, enthält nach dem Zusammenfassen überflüssiger Leerzeichen 1 bis 120 Zeichen und keine Steuerzeichen; seine Groß-/Kleinschreibung bleibt für die Anzeige erhalten. Eine bewusste Planänderung erzeugt eine neue Baseline und gilt nicht als Einnahmeabweichung.
_Avoid_: Medikamentenabweichung, einmalige tatsächliche Einnahme

**Medikamentendosis**:
Eine positive Dezimalmenge mit einer validierten, unmittelbar gespeicherten Einheit. Die Einheit wird getrimmt, enthält nach dem Zusammenfassen überflüssiger Leerzeichen 1 bis 32 Zeichen und keine Steuerzeichen; ihre Groß-/Kleinschreibung bleibt für die Anzeige erhalten. Eine geplante Dosis legt eine exakte Menge fest; eine Bedarfsmedikation eine Referenzmenge, von der die tatsächliche Bedarfseinnahme abweichen darf. Tatsächliche Einnahmen verwenden die Einheit ihres referenzierten Planeintrags. V0.3 rechnet Einheiten nicht automatisch um und bildet keine Regeln wie Tageshöchstdosen ab.
_Avoid_: binäre Gleitkommazahl, unstrukturierter Dosierungsfreitext, automatische Einheitenumrechnung

**Manuelle Medikamentenrevision**:
Eine unveränderliche Fassung genau eines Medikamentenregimes, einer Einnahmeabweichung, einer Bedarfseinnahme oder einer Einnahmegrund-Kategorie, die in einem Datensatz-Snapshot gebunden ist. Jeder Medikamentenschreibvorgang ändert genau eines dieser Wurzelobjekte, verlangt einen aktiven Ausgangs-Snapshot und veröffentlicht bei Erfolg genau eine Revision und genau einen neuen aktiven Snapshot. Eine Regimerevision enthält dabei ihre vollständige Menge geplanter Dosen und Bedarfsmedikations-Einträge; V0.3 besitzt keine Entwürfe, Sammelbearbeitung oder atomare Änderung mehrerer Wurzelobjekte. Jedes Wurzelobjekt besitzt eine opake logische Identität und eine unverzweigte Folge eigener Revisionsidentitäten; Korrektur, Rücknahme und Wiederherstellung erzeugen neue Revisionen und verändern frühere Datensatz-Snapshots nicht. Geplante Dosen und Bedarfsmedikations-Einträge sind Bestandteile einer Regimerevision und besitzen keine getrennten Revisionsketten. Ihre opaken, innerhalb des Regimes eindeutigen Eintragsidentitäten bleiben in Revisionen derselben Regimeidentität erhalten; ein echter neuer Regimewechsel erzeugt neue Eintragsidentitäten. Bei einer Regimekorrektur werden alle referenzierenden Abweichungen und Bedarfseinnahmen im Ausgangs-Snapshot erneut validiert. Würde ein Bezug verschwinden oder ungültig, scheitert die Korrektur ohne automatische Neuzuordnung und ohne Teilwirkung. Eine Wiederherstellung wird gegen den aktuellen wirksamen Snapshot vollständig neu validiert. Regime, Planeinträge, Einnahmeabweichungen und Bedarfseinnahmen besitzen in V0.3 kein Freitextnotizfeld.
_Avoid_: Überschreiben, verzweigte Revision, separat revidierter Planeintrag

**Medikamentenstichtag**:
Der in der jeweiligen Schreibvorschau gebundene zeitzonenbewusste Zeitpunkt, bis zu dem ein Datensatz-Snapshot Medikamentenregime, geplante Dosisvorkommen, Einnahmeabweichungen und Bedarfseinnahmen abbildet. Jeder neu veröffentlichte Datensatz-Snapshot bindet unabhängig vom auslösenden Import-, Kontext- oder Medikamentenschreibvorgang seinen eigenen Medikamentenstichtag und den dann wirksamen Medikamentenstand. Dadurch wächst die Zeitachse planmäßig angenommener Dosisvorkommen reproduzierbar weiter, ohne einen künstlichen Medikamentenschreibvorgang zu verlangen. Der Stichtag bleibt bei der Ausführung unverändert und verhindert, dass das Fortschreiten der Uhr einen veröffentlichten Snapshot nachträglich verändert.
_Avoid_: bewegliches Jetzt, aktueller Anzeigezeitpunkt als historische Grenze

**Medikamentenregime**:
Die ab einem einschließlich gültigen, zeitzonenbewussten lokalen Zeitpunkt aktive, vollständige Version des Medikamentenplans. Ihr Beginn darf nicht nach dem im Datensatz-Snapshot gebundenen Medikamentenstichtag liegen; V0.3 speichert keine zukünftigen Regimewechsel. Ihr Ende wird ausschließlich durch den Beginn des nächsten Regimes bestimmt. Dadurch können an einem Änderungstag frühere geplante Dosen noch zum alten und spätere bereits zum neuen Regime gehören. Ein echter Regimewechsel darf mit einem eindeutigen Startzeitpunkt rückwirkend zwischen bestehende Regime eingefügt werden; nur danach veröffentlichte Snapshots zeigen die entsprechend neu abgegrenzte Zeitachse. Die Einfügung wird jedoch abgelehnt, wenn der übernommene Zeitraum wirksame Einnahmeabweichungen oder Bedarfseinnahmen enthält, die Planeinträge des dadurch verkürzten Regimes referenzieren. Diese Einträge müssen zuerst ausdrücklich zurückgenommen werden; V0.3 ordnet sie nicht automatisch neu zu. Jede tatsächliche Planänderung erzeugt ein neues Regime mit allen geänderten und unveränderten Planeinträgen; das vorherige Regime bleibt unveränderlich. Die Korrektur einer fehlerhaften historischen Dokumentation erzeugt dagegen eine neue manuelle Revision derselben Regimeidentität und gilt nicht als Regimewechsel. Die Rücknahme eines irrtümlich dokumentierten Regimes entfernt es aus späteren Snapshots und lässt das vorherige Regime bis zum nächsten weitergelten; ein tatsächliches Absetzen wird durch ein neues leeres Regime dokumentiert. Eine Rücknahme mit wirksamen referenzierenden Einnahmeabweichungen oder Bedarfseinnahmen wird ohne automatische Neuzuordnung oder Kaskade abgelehnt. Alle Änderungen wirken nur in danach veröffentlichten Datensatz-Snapshots. Einnahmeabweichungen und tatsächliche Bedarfsmedikationen erzeugen kein neues Regime. Ein Regimewechsel wird in Zeitachsen markiert und im Modell als möglicher Strukturbruch berücksichtigt, nicht nur als Ereignis am Änderungstag.
_Avoid_: einmalige Planänderung, Einnahmeabweichung

**Geplante Dosis**:
Ein Planeintrag innerhalb eines Medikamentenregimes mit konkretem Medikamentennamen, Dosis, lokaler Uhrzeit und einer nichtleeren Auswahl von Wochentagen. Die Uhrzeit gilt in der IANA-Zeitzone des Regimes und erzeugt pro ausgewähltem lokalem Kalendertag genau ein Dosisvorkommen: Eine durch Zeitumstellung übersprungene Uhrzeit verschiebt sich auf den ersten gültigen Zeitpunkt nach der Lücke, eine doppelte Uhrzeit verwendet das erste Vorkommen. Die aktuelle Anzeigezeitzone verändert historische Pläne nicht. Mehrere vorgesehene Einnahmen an einem Tag sind getrennte geplante Dosen; vollständig identische Planeinträge innerhalb eines Regimes sind unzulässig. V0.3 unterstützt keine freien Wiederholungsregeln, Intervalle wie „alle n Stunden“ oder Einnahmezeitfenster.
_Avoid_: tatsächliche Einnahme, Bedarfsmedikation, allgemeine Kalenderregel

**Geplantes Dosisvorkommen**:
Die einzelne Soll-Einnahme, die sich für einen konkreten lokalen Zeitpunkt aus einer geplanten Dosis und dem damals gültigen Medikamentenregime ergibt. Sie ist der eindeutige Bezugspunkt einer Einnahmeabweichung.
_Avoid_: wiederkehrender Planeintrag, bestätigte tatsächliche Einnahme

**Einnahmeabweichung**:
Eine einmalige Abweichung von genau einem geplanten Dosisvorkommen, unabhängig davon, ob sie versehentlich oder bewusst geschieht. Dieser Bezug ist unveränderlicher Teil der logischen Abweichungsidentität und darf in einer Korrektur nicht auf ein anderes Dosisvorkommen umgehängt werden. Pro Dosisvorkommen darf höchstens eine Abweichung wirksam sein; Korrekturen revidieren dieselbe Abweichungsidentität. Sie enthält null bis mehrere tatsächliche Einnahmen mit jeweils eigenem zeitzonenbewusstem Zeitpunkt und eigener Dosis: keine bedeutet ausgelassen, eine mit abweichender Zeit oder Dosis bedeutet eine zeitliche oder mengenmäßige Abweichung und mehrere bedeuten eine doppelte oder mehrfache Einnahme. Kein tatsächlicher Zeitpunkt darf nach dem im Datensatz-Snapshot gebundenen Medikamentenstichtag liegen. Eine tatsächliche Einnahme darf auch in einer anderen Zeitzone oder am folgenden lokalen Kalendertag liegen und bleibt dennoch dem ausdrücklich gewählten Dosisvorkommen zugeordnet. Zeit-, Dosis- und Mehrfachabweichung dürfen gemeinsam auftreten; V0.3 speichert dafür keine konkurrierende starre Abweichungskategorie und kein Absichtsmerkmal. Genau eine tatsächliche Einnahme mit demselben Zeitpunkt und derselben Dosis wie das Dosisvorkommen ist keine Abweichung und wird als inhaltsgleich abgelehnt. Die einmalige Abweichung verändert den Medikamentenplan nicht; eine dauerhafte bewusste Änderung erzeugt ein neues Regime. Ein falsch zugeordnetes Objekt wird zurückgenommen und mit neuer Identität angelegt.
_Avoid_: Planänderung, neue Baseline

**Bedarfsmedikation**:
Ein Planeintrag innerhalb eines Medikamentenregimes für ein Medikament, das ohne festen Sollzeitpunkt bei einem konkreten Bedarf eingenommen werden darf. Er darf eine geordnete Auswahl bevorzugter aktiver Einnahmegrund-Kategorien vorschlagen; diese schränkt den globalen Katalog nicht ein. Eine Änderung der Vorschlagsliste ist eine Änderung des vollständigen Regimes. Der Planeintrag ist von jeder tatsächlichen Einnahme getrennt.
_Avoid_: Bedarfseinnahme, vergessene Einnahme, Medikamentenplanänderung

**Bedarfseinnahme**:
Die tatsächliche Einnahme einer Bedarfsmedikation mit eigenem zeitzonenbewusstem Zeitpunkt, Dosis und höchstens einer optionalen aktiven Einnahmegrund-Kategorie. Der Bezug auf den Bedarfsmedikations-Eintrag ist unveränderlicher Teil der logischen Einnahmeidentität und darf in einer Korrektur nicht umgehängt werden; ein falsch zugeordnetes Objekt wird zurückgenommen und mit neuer Identität angelegt. Ihr Zeitpunkt darf nicht nach dem im Datensatz-Snapshot gebundenen Medikamentenstichtag liegen und muss in die Gültigkeit des referenzierten Bedarfsmedikations-Eintrags fallen. Die Zeitzone der Einnahme darf von der Regime-Zeitzone abweichen und wird nicht durch die aktuelle Anzeigezeitzone umgedeutet. Jede aktive globale Kategorie ist zulässig, auch wenn sie dort nicht bevorzugt vorgeschlagen wird. Die Einnahme verändert das Medikamentenregime nicht.
_Avoid_: Bedarfsmedikation als Planerlaubnis, Einnahmeabweichung, Medikamentenplanänderung

**Medikamentenmerkmal**:
Ein zeitlich variierendes Ereignis des Medikamentenkontexts, das im ersten Modell berücksichtigt werden kann: eine Planänderung, Einnahmeabweichung oder Bedarfseinnahme. Der vollständige gültige Medikamentenplan bleibt dokumentiert, wird aber nicht allein aufgrund seiner Existenz als variierendes Modellmerkmal behandelt.
_Avoid_: unveränderter Medikamentenplan als tägliches Ereignis

**Tägliche Medikamentenansicht**:
Die nach lokalem Kalenderdatum geordnete Sicht auf den Medikamentenkontext eines Datensatz-Snapshots bis zu dessen Medikamentenstichtag. Sie zeigt das gültige Regime und seine geplanten Dosisvorkommen jeweils als `planmäßig angenommen` oder mit der konkreten Einnahmeabweichung, alle tatsächlichen Bedarfseinnahmen sowie Regimewechsel als markierte Ereignisse. Eine Einnahmeabweichung gehört ausschließlich zum regimelokalen Kalendertag ihres geplanten Dosisvorkommens; ein tatsächlicher Einnahmezeitpunkt am Folgetag bleibt darin sichtbar und erzeugt keinen zweiten Tageseintrag. Eine Bedarfseinnahme gehört zu ihrem eigenen einnahmelokalen Kalendertag. Vor dem ersten Regime bleibt der Medikamentenstatus unbekannt. Normale Leseoperationen verwenden standardmäßig den aktiven Snapshot; Reproduktion verlangt einen ausdrücklichen früheren Snapshot. Eine getrennte Plan- und Katalogansicht zeigt die im gewählten Snapshot wirksamen Regime und Einnahmegrund-Kategorien, während eine Auditansicht für genau eine logische Identität alle Revisionen einschließlich Rücknahmen und Wiederherstellungen liefert. Zurückgezogene Objekte fehlen in normalen Ansichten standardmäßig. Die tägliche Ansicht bildet weder einen Adhärenzprozentsatz noch einen kombinierten Medikamentenscore und behandelt eine fehlende Abweichung nie als bestätigte Einnahme.
_Avoid_: bestätigte Einnahme aus Schweigen, Adhärenzscore, kombinierter Medikamentenscore

**Einnahmegrund**:
Eine optionale Angabe zum Anlass einer Bedarfsmedikation, beispielsweise Kopfschmerzen oder Übelkeit. Im MVP ist sie Zusatzinformation zur Einnahme und kein verpflichtend eigenständig erfasster Symptomverlauf.
_Avoid_: Diagnose, verpflichtendes Symptomtagebuch

**Einnahmegrund-Kategorie**:
Ein global wiederverwendbarer, auswählbarer Einnahmegrund aus einem anfangs leeren, vom Benutzer gepflegten Katalog. Namen werden getrimmt, enthalten 1 bis 80 Zeichen und keine Steuerzeichen; für die Eindeutigkeit werden Groß-/Kleinschreibung und überflüssige Leerzeichen ignoriert. Umbenennung, Rücknahme und Reaktivierung sind erlaubt, wobei auch zurückgezogene Einträge ihren Namen reservieren. Zurückgezogene Kategorien sind für neue Bedarfseinnahmen nicht auswählbar, bestehende Bedarfseinnahmen dürfen sie historisch weiter referenzieren. Eine Rücknahme wird jedoch abgelehnt, solange ein wirksamer Bedarfsmedikations-Eintrag die Kategorie bevorzugt vorschlägt. Eine Umbenennung ändert die historische Anzeige nur in danach veröffentlichten Snapshots. Medikamente können passende Kategorien bevorzugt vorschlagen, ohne jeweils getrennte Kataloge zu erzeugen. V0.3 besitzt weder eine Kategorie „Sonstige“ noch einen einmaligen Freitextgrund.
_Avoid_: einmaliger Freitext, Diagnosecode, medizinisch vorgegebener Startkatalog

### Schlaf und Ernährung

**HealthKit-Schlafsample**:
Ein aus Apple Health übernommenes Zeitintervall mit Schlafkategorie und Quelle. Der MVP bewahrt alle verfügbaren Samples einschließlich „im Bett“, „wach“, Core-, Tief-, REM- und nicht näher bestimmtem Schlaf; importierte Rohsamples sind noch keine bereinigten Modellmerkmale.
_Avoid_: Schlafscore, bereits bereinigte Schlafnacht

**Schlafmerkmal**:
Eine aus qualitätsgeprüften HealthKit-Schlafsamples für eine Schlafnacht abgeleitete Modellgröße. Das erste Modell verwendet insbesondere Gesamtschlafdauer, Wachzeit und Anteile verfügbarer Schlafstadien, nicht jedes einzelne Rohintervall.
_Avoid_: HealthKit-Rohsample, ungeprüftes Schlafintervall

**Schlafquelle**:
Die Apple Watch ist die einzige zulässige Quelle für Schlafsamples. Schlafdaten anderer Geräte oder Apps werden nicht zur Ergänzung oder Zusammenführung verwendet.
_Avoid_: iPhone-Schlafdaten, Drittanbieter-Schlafdaten, Quellenfusion

**Fehlender Schlafwert**:
Eine fehlende Beobachtung für eine Nacht ohne verwertbare Apple-Watch-Schlafdaten, insbesondere weil die Uhr nicht getragen wurde. Die Lücke bleibt erhalten; Ausschluss, gesonderte Missingness-Behandlung oder Interpolation sind explizite, modellspezifische Entscheidungen.
_Avoid_: schlaflose Nacht, geschätzte Schlafnacht

**Kanonische Ernährungsquelle**:
Apple Health beziehungsweise der HealthKit-Datenspeicher ist der einzige reguläre Eingang für Ernährungsdaten wie aufgenommene Energie, auch wenn eine andere App die Werte ursprünglich erzeugt hat. Die ursprüngliche App bleibt als Datenprovenienz erhalten.
_Avoid_: paralleler Yazio-Import, mehrere gleichrangige Ernährungsquellen

**HealthKit-Ernährungssample**:
Ein über die kanonische Ernährungsquelle importierter Messwert zu Energie, Makro- oder Mikronährstoffen mit Zeitpunkt und Provenienz. Der MVP bewahrt alle verfügbaren Ernährungssamples, unabhängig davon, ob sie bereits in einem Modell verwendet werden.
_Avoid_: bereinigte Tagesernährung, Modellmerkmal

**Ernährungstag**:
Der messlokale Kalendertag, an dem ein Ernährungssample beginnt. Auch ein über Mitternacht reichendes Sample wird diesem Starttag vollständig zugeordnet, weil seine konsumierte Menge nicht zeitproportional aufgeteilt werden kann.
_Avoid_: anteilig aufgeteiltes Ernährungssample, Endtag

**Ernährungsmerkmal**:
Eine aus qualitätsgeprüften HealthKit-Ernährungssamples abgeleitete Modellgröße. Das erste Gewichtsmodell verwendet Gesamtenergie sowie Protein, Kohlenhydrate und Fett; weitere importierte Nährstoffe bleiben zunächst außerhalb des Modells.
_Avoid_: einzelnes Ernährungssample, vollständiges Nährstoffmodell

**Fehlender Ernährungswert**:
Eine fehlende Beobachtung für ein Ernährungsmerkmal an einem Tag ohne wirksames Sample dieses Nährstofftyps. Sie wird weder als Nullaufnahme noch durch Fortschreibung oder Interpolation ersetzt; insbesondere wird nicht unterschieden, ob nichts konsumiert oder das Protokollieren vergessen wurde.
_Avoid_: Nullaufnahme, geschätzte Tagesernährung

**Ernährungstagsbestätigung**:
Die bewusste Nutzerangabe, dass alles an einem messlokalen Kalendertag Konsumierte protokolliert wurde. Mehrere ausdrücklich angezeigte Tage dürfen gemeinsam bestätigt werden, gelten fachlich aber als einzelne Tagesbestätigungen. Jede Bestätigung bindet die beitragenden Messungsidentitäten und -versionen, Werte, Korrekturen, Ausschlüsse und Tageszuordnungen; ändert sich davon etwas, verlangt ein danach aufgelöster Datenstand eine neue Bestätigung, auch wenn seine Tagessumme gleich bleibt. Ein erneut importiertes identisches Sample ändert die Bestätigung nicht, und frühere Datenstände bleiben unverändert.
_Avoid_: aus vorhandenen Samples abgeleitete Vollständigkeit, Genauigkeitsnachweis

**Ernährungsquellenabdeckung**:
Die je Ernährungstag und Ernährungsmerkmal sichtbare Herkunft der beitragenden ursprünglichen Apps, Quellsamples und Messungsversionen. Sie beschreibt Provenienz statt zeitlicher Tagesabdeckung; gemischte Quellen verhindern für sich allein keine vollständige Beobachtung und belegen sie auch nicht.
_Avoid_: 24-Stunden-Abdeckung, aus Quellen abgeleitete Vollständigkeit, gleichrangige Ernährungsquellen

**Ernährungsbeobachtungsstatus**:
Die nach einer benannten Vollständigkeitsregelversion vorgenommene Einordnung eines Ernährungstags als `missing`, `partial` oder `complete` samt konkreten Gründen wie fehlendem Merkmal, fehlender Bestätigung oder Änderung seit der Bestätigung. Die erste Regelversion verlangt Gesamtenergie, Protein, Kohlenhydrate, Fett und eine gültige Tagesbestätigung; eine spätere Änderung der Pflichtmerkmale erzeugt eine neue Regelversion und deutet frühere Eingänge oder Ergebnisse nicht um. Der Status weist Bestätigungs- und Quellenbelege aus, aber weder eine geschätzte Vollständigkeitswahrscheinlichkeit noch eine Qualitätsnote; statistische Modellunsicherheit bleibt davon getrennt und gilt bedingt auf die selbst berichtete Vollständigkeit. Auch der Datenqualitätsstatus bleibt unabhängig: Ein vollständig beobachteter Tag kann wegen eines offenen Datenprüffalls zugleich vorläufig sein.
_Avoid_: Vollständigkeitsscore, geschätzte Protokollierungswahrscheinlichkeit, statistisches Unsicherheitsintervall

**Vollständig beobachteter Ernährungstag**:
Ein Ernährungstag, an dem Gesamtenergie, Protein, Kohlenhydrate und Fett beobachtet sind und eine für den wirksamen Tagesinhalt gültige Ernährungstagsbestätigung vorliegt. Die Bestätigung ersetzt keinen fehlenden Ernährungswert und belegt nicht die Genauigkeit der protokollierten Mengen.
_Avoid_: automatisch aus vorhandenen Samples geschlossener Ernährungstag, garantiert genaue Tagesernährung

**Teilweise beobachteter Ernährungstag**:
Ein Ernährungstag mit mindestens einem beobachteten Ernährungsmerkmal, der nicht vollständig beobachtet ist, weil mindestens ein benötigtes Ernährungsmerkmal oder eine gültige Ernährungstagsbestätigung fehlt. Jedes Merkmal bleibt unabhängig beobachtet oder fehlend.
_Avoid_: vollständig beobachteter Ernährungstag, fehlendes Merkmal als Null

**Fehlender Ernährungstag**:
Ein Tag ohne beobachtetes Ernährungsmerkmal. Ob nichts konsumiert oder das Protokollieren vollständig vergessen wurde, bleibt unbekannt und wird nicht unterschieden.
_Avoid_: Fastentag, Nullaufnahme

### Gewichtstrends und Energie

**Gewichtstrendfenster**:
Eine von vier parallelen Perspektiven auf den Gewichtstrend über 1 Woche, 2 Wochen, 1 Monat oder 3 Monate. Längere Trendfenster liegen außerhalb des MVP.
_Avoid_: Tagesgewicht, unbegrenzter Langzeittrend

**Geglättetes Gewichtsniveau**:
Das innerhalb eines Gewichtstrendfensters geglättete Niveau der gemessenen Körpermasse. Es wird dargestellt und liefert Kontext, ist aber nicht das primäre Outcome des ersten Gewichtsmodells.
_Avoid_: einzelne Gewichtsmessung, Veränderungsrate

**Gewichtsveränderungsrate**:
Die über ein Gewichtstrendfenster geschätzte Richtung und Geschwindigkeit der Gewichtsveränderung. Sie ist das primäre Outcome des ersten Gewichtsmodells und wird getrennt vom geglätteten Gewichtsniveau ausgewiesen.
_Avoid_: Tagesdifferenz, geglättetes Gewichtsniveau

**Apple-Ruheenergie (Schätzung)**:
Die aus eindeutig als Apple Watch klassifizierten HealthKit-Samples des kumulativen Typs `basalEnergyBurned` abgeleitete Ruheenergie. Sie wird in kcal geführt und bleibt einschließlich Quelle, Gerät, Originaleinheit und beitragender Messungsidentitäten nachvollziehbar. Der Wert ist eine algorithmische Apple-Schätzung und keine direkte Kalorimetrie oder gemessene physiologische Wahrheit.
_Avoid_: gemessene Ruheenergie, selbst berechneter Grundumsatz, direkte Kalorimetrie

**Vollständig beobachteter Ruheenergietag**:
Ein messlokaler Kalendertag, dessen tatsächliche lokale Dauer von 23, 24 oder 25 Stunden durch zulässige wirksame Ruheenergieintervalle lückenlos und ohne ungelöste Überlappung, Quellen- oder Identitätsfrage abgedeckt ist. Tagesbeiträge werden anhand ihrer tatsächlichen UTC-Überlappungsdauer zeitanteilig aus den kumulativen Samples abgeleitet. Ein unvollständiger Tageswert bleibt für Gewichtsmodelle und Energiebilanz fehlend und wird weder mit null gefüllt noch hochgerechnet; vollständige Abdeckung belegt nicht die Genauigkeit der Schätzung. Ein vollständig beobachteter Tag kann wegen eines offenen Datenprüffalls zugleich vorläufig sein.
_Avoid_: gemessener Tagesenergieverbrauch, hochgerechnete Ruheenergie, vollständige Abdeckung als Genauigkeitsnachweis

**Überlappende Ruheenergiemessungen**:
Zwei verschiedene wirksame Ruheenergie-Samples mit positiver zeitlicher Überlappung. Sie werden weder automatisch priorisiert noch beschnitten oder addiert; die Überlappung öffnet einen Quellenkonflikt und macht alle betroffenen Ruheenergietage bis zu einer Korrektur oder einem lokalen Messungsausschluss unvollständig.
_Avoid_: automatische Quellenpriorität, doppelt gezählte Ruheenergie, stilles zeitliches Beschneiden

**Energiebilanz**:
Eine abgeleitete Anzeigegröße aus aufgenommener Energie abzüglich aktiver Energie und Ruheenergie für denselben Zeitraum. Sie wird berechnet und visualisiert, während Gewichtsmodelle die drei Ausgangsgrößen getrennt verwenden.
_Avoid_: eigenständige Messung, einziges Energiemerkmal des Modells

### Datenqualität und Ergebnisstatus

**Plausibilitätsauffälligkeit**:
Ein durch eine Regel markierter Messwert oder Zeitraum, der möglicherweise unvollständig oder fehlerhaft ist und eine Benutzerprüfung benötigt. Pro Quellmessungsversion und Plausibilitätsregelversion entsteht höchstens eine Auffälligkeit; sie hält alle verletzten Regelbestandteile, verwendeten Grenzen und den Historienstichtag als Begründungen fest. Eine veränderte Menge von Begründungen ist eine andere Art der Auffälligkeit. Eine Auffälligkeit ist kein Beweis für einen Datenfehler.
_Avoid_: automatisch erkannter Fehler, automatisch zu löschender Wert

**Datenprüffall**:
Eine aktuell entscheidungsbedürftige Frage zur lokalen analytischen Verwendung von Quelldaten. Er kann durch eine Plausibilitätsauffälligkeit, die erneute Prüfung einer Korrektur oder eines lokalen Messungsausschlusses, eine vermutete Quellenlöschung oder einen Quellmessungskonflikt entstehen; höchstens eine Auflösung ist wirksam, eine spätere löst die frühere ab und ihr Widerruf öffnet den Fall, ohne eine ältere Auflösung zu reaktivieren, und solange irgendein wirksamer Fall offen ist, bleibt der Datenstand vorläufig.
_Avoid_: Datenfehler, ausschließlich Plausibilitätsauffälligkeit

**Plausibilitätsregel**:
Eine pro kanonischem Datentyp unveränderlich versionierte vollständige Regelkonfiguration, die feste, vom Benutzer anpassbare Unter- oder Obergrenzen und/oder einen aus der persönlichen Datenhistorie abgeleiteten Referenzbereich verwendet. Der Benutzer kann feste Grenzen sowie die aktiven Regelbestandteile ändern; Grenzen sind endlich, stehen in der kanonischen Einheit und steigen bei beidseitiger Angabe von unten nach oben. Die Parameter und Sonderfälle der persönlichen Methodik bleiben versionierte eingebaute Methodik. Änderungen an Grenzen, aktivierten Regelbestandteilen oder Algorithmusparametern erzeugen eine neue Version. Der mit wachsender Historie neu berechnete persönliche Referenzbereich ist dagegen ein Auswertungsergebnis und keine neue Regelversion; jede erzeugte Auffälligkeit hält die tatsächlich verwendeten Grenzen und den Historienstichtag fest. Plausibilitätsregeln erzeugen Plausibilitätsauffälligkeiten, aber keine medizinischen Warnungen.
_Avoid_: Sicherheitsgrenze, Diagnosegrenze

**Persönlicher Referenzbereich**:
Ein aus der bisherigen persönlichen Datenhistorie abgeleiteter Bereich typischer Werte, der sich mit wachsender Datenbasis aktualisieren darf. Jeder Prüfzyklus hält dafür den bei seinem Start aktuell wirksamen Datensatz-Snapshot fest und verwendet je Messwert ausschließlich die darin enthaltene frühere Historie. Korrekturen und verspätete Importe können deshalb in späteren Prüfzyklen andere Grenzen ergeben; frühere Auffälligkeiten bleiben mit ihren damals verwendeten Grenzen reproduzierbar. Der persönliche Referenzbereich ergänzt feste Plausibilitätsgrenzen und ist kein populationsbezogener medizinischer Referenzbereich.
_Avoid_: medizinischer Normbereich, feste Grenze

**Regelgültigkeit**:
Der Zeitraum, in dem eine Version einer Plausibilitätsregel anhand des Messzeitpunkts angewendet wird. Die erste Regelversion eines zuvor bewusst ungeregelten Datentyps gilt ohne untere Zeitgrenze. Für spätere Änderungen ist der Gültigkeitsbeginn Montag 00:00 der bei der Änderung aktiven lokalen ISO-Kalenderwoche; Zeitzone und Offset werden mit der Regelversion festgehalten und bestimmen einen einzigen Zeitpunkt. Messungen werden anhand ihres tatsächlichen Zeitpunkts damit verglichen, sodass Reisen keine unterschiedlichen Gültigkeitsgrenzen erzeugen. Eine Regeländerung bewertet alle derzeit wirksamen Quellmessungsversionen seit diesem Wochenbeginn unter der neuen Version in einem eigenen Prüfzyklus neu. Frühere Auffälligkeiten bleiben im Audit, sind für den aktuellen Prüfstatus aber abgelöst; ältere abgeschlossene Wochen werden nicht neu bewertet. Eine verspätet importierte Quellmessungsversion wird mit der am Messzeitpunkt gültigen Regel geprüft und erzeugt eine mögliche Auffälligkeit im aktuellen Importprüfzyklus. Frühere Prüfzyklen und bereits vorhandene Messungen desselben Zeitraums bleiben unverändert.
_Avoid_: globale rückwirkende Neuberechnung

**Datenbestätigung**:
Die bewusste Benutzerangabe, dass ein angezeigter auffälliger Wert oder Zeitraum nach Prüfung für die Analyse verwendet werden darf. Sie gilt genau für die geprüfte Quellmessungsversion, die erzeugende Plausibilitätsregelversion und die Art der Auffälligkeit. Eine neue Quellmessungs- oder Regelversion kann daher einen neuen offenen Prüffall erzeugen; die frühere Bestätigung bleibt als Audit-Historie erhalten. Die Bestätigung dokumentiert die Prüfung, garantiert aber nicht die objektive Richtigkeit der Daten.
_Avoid_: automatische Freigabe, Wahrheitsnachweis

**Datenkorrektur**:
Eine nachvollziehbare, begründete Ersetzung eines importierten Werts mit oder ohne Plausibilitätsauffälligkeit, ohne den ursprünglichen Rohwert zu verändern; sie verweist auf die zugrunde liegende Quellmessungsversion und wird im MVP nicht nach HealthKit zurückgeschrieben. Sie schließt eine vorhandene zugehörige Auffälligkeit ohne zusätzliche Datenbestätigung und wirkt ab dem nächsten Datensatz-Snapshot; bei einer neuen Quellmessungsversion bleibt sie vorläufig wirksam, bis der neue Quellwert übernommen, korrigiert oder lokal ausgeschlossen wird.
_Avoid_: Überschreiben, Löschen des Rohwerts

**Lokaler Messungsausschluss**:
Die bewusste, begründete Entscheidung, eine vorhandene, als falsch erkannte Quellmessungsversion ohne bekannten Ersatzwert aus zukünftigen lokalen Analysen auszuschließen, während Rohwert und Audit-Historie erhalten bleiben. Sie schließt den zugehörigen Datenprüffall und wirkt ab dem nächsten Datensatz-Snapshot; bei einer neuen Quellmessungsversion bleibt sie vorläufig wirksam, bis der neue Quellwert übernommen, korrigiert oder erneut versionsgebunden ausgeschlossen wird, und behauptet anders als eine bestätigte Quellenlöschung nicht, dass die Messung in der Quelle gelöscht wurde.
_Avoid_: Datenkorrektur, Quellenlöschung, Löschen des Rohwerts

**Quellwertübernahme**:
Die bewusste Entscheidung, eine plausible neue Quellmessungsversion anstelle einer vorläufig weiterwirkenden älteren Korrektur oder eines lokalen Messungsausschlusses zu verwenden. Sie macht den neuen Quellwert ab dem nächsten Datensatz-Snapshot wirksam und beendet den offenen Datenprüffall, ohne die für frühere Versionen gültigen Entscheidungen zu widerrufen; ein weiterhin auffälliger neuer Wert benötigt stattdessen eine Datenbestätigung.
_Avoid_: Entscheidungswiderruf, Datenbestätigung eines unauffälligen Werts

**Bevorzugte Datenkorrektur**:
Die aktuelle, weder abgelöste noch widerrufene Datenkorrektur einer Quellmessungsversion. Frühere Korrekturen bleiben dauerhaft abgelöste Audit-Historie; wird die aktuelle Korrektur widerrufen, gilt wieder der Quellwert und ein weiterhin aktueller Datenprüffall öffnet sich erneut.
_Avoid_: Korrekturstapel, automatisch wiederbelebte Korrektur

**Entscheidungswiderruf**:
Die bewusste, verpflichtend begründete Aufhebung einer früheren Benutzerentscheidung durch eine neue unveränderliche Auditentscheidung. Sie wirkt ausschließlich auf danach aufgelöste Datensatz-Snapshots; frühere Snapshots und Analysen bleiben unverändert reproduzierbar.
_Avoid_: Löschen der Entscheidung, rückwirkende Änderung

**Wirksamer Analysewert**:
Der für eine konkrete Analyse gültige Wert: entweder der unveränderte importierte Wert oder dessen bevorzugte Datenkorrektur. Ein offener Datenprüffall schließt ihn nicht aus, sondern macht damit berechnete Ergebnisse vorläufig; nur eine ausdrückliche Ausschlussentscheidung entfernt die Messung, und die jeweils verwendete Version bleibt reproduzierbar.
_Avoid_: Rohwert, automatisch endgültiger Wert

**Veraltetes Analyseergebnis**:
Ein vorhandenes Ergebnis, dessen Snapshot-Referenz nicht mit dem aktuell aktiven Datensatz-Snapshot übereinstimmt. Jede Veröffentlichung eines abweichenden aktiven Snapshots macht alle an den vorherigen Snapshot gebundenen Ergebnisse veraltet, unabhängig davon, ob sich Daten innerhalb ihres Analysezeitraums oder ihrer Datentypen geändert haben. „Aktuell“ oder „veraltet“ ist die aus der aktiven Snapshot-Referenz abgeleitete Aktualitätsdimension und bleibt unabhängig von Datenstatus und Modellreife. Das Ergebnis bleibt unverändert an seinen damaligen Datensatz-Snapshot gebunden und darf nicht als aktuell präsentiert werden.
_Avoid_: aktuelles Ergebnis, gelöschter Modelllauf

**Aktuelles Analyseergebnis**:
Ein vorhandenes Ergebnis, dessen Snapshot-Referenz mit dem aktuell aktiven Datensatz-Snapshot übereinstimmt. Wird ein früherer unveränderlicher Snapshot bewusst wieder aktiviert, gelten seine daran gebundenen Ergebnisse erneut als aktuell; ihr gespeicherter Datenstatus und ihre Modellreife ändern sich dadurch nicht. Die Aktivierungshistorie der Snapshots hält die zwischenzeitliche Veraltung nachvollziehbar.
_Avoid_: neuester Modelllauf unabhängig vom Snapshot, nachträglich neu berechnetes Ergebnis

**Vorläufiges Analyseergebnis**:
Ein Ergebnis, dessen Datensatz-Snapshot einen beliebigen aktuell wirksamen offenen Datenprüffall enthält oder dessen ausgewählte Eingabedaten einen unvollständig beobachteten Aktivitätstag des verwendeten Datentyps und Zeitraums einschließen. Offene Datenprüffälle wirken unabhängig von Datentyp und Analysezeitraum global auf alle Ergebnisse des Snapshots; passive Abdeckungslücken wirken nur auf tatsächlich davon abhängige Ergebnisse. „Vorläufig“ oder „geprüft“ ist der Datenstatus und bleibt unabhängig von Aktualität und Modellreife. Das Ergebnis darf angezeigt werden, muss seinen vorläufigen Status und die Gründe sichtbar tragen und kann nach Datenprüfung oder späterer Modellanpassung durch eine Neuberechnung abgelöst werden. Ein späterer Snapshot ohne den Grund ändert den Datenstatus des alten Ergebnisses nicht; dieses bleibt vorläufig und wird zusätzlich veraltet.
_Avoid_: bestätigtes Ergebnis, fehlerfreies Ergebnis

**Geprüftes Analyseergebnis**:
Ein Ergebnis, dessen Datensatz-Snapshot keine aktuell wirksamen offenen Datenprüffälle enthält und dessen ausgewählte Eingabedaten keine für den verwendeten Datentyp und Zeitraum festgestellte passive Abdeckungslücke einschließen. Der Datenstatus behauptet weder objektive Fehlerfreiheit noch statistische Belastbarkeit.
_Avoid_: garantiert fehlerfreies Ergebnis, belastbares Analyseergebnis

**Modellreifeprüfung**:
Die modellspezifische Prüfung, ob Datenmenge, Vollständigkeit, Merkmalsabhängigkeiten, Schätzstabilität und Zeitreihendiagnostik eine belastbare Darstellung erlauben. Ihre verpflichtenden Kriterien und Schwellen gehören zur versionierten Analysedefinition und müssen für ein belastbares Ergebnis sämtlich bestanden sein. Eine spätere Analysedefinition klassifiziert frühere Ergebnisse nicht neu. Die Modellreifeprüfung ersetzt eine starre Mindestzahl von Kalendertagen und bleibt vom technischen Laufstatus getrennt.
_Avoid_: pauschale 90-Tage-Regel, erfolgreiche Programmausführung

**Exploratives Analyseergebnis**:
Ein formal berechnetes Ergebnis, das mindestens ein verpflichtendes Kriterium der Modellreifeprüfung nicht bestanden hat. Ein nicht berechenbarer oder numerisch gescheiterter Modelllauf erzeugt dagegen kein exploratives Analyseergebnis. „Explorativ“ oder „belastbar“ ist die Modellreife und bleibt unabhängig von Aktualität und Datenstatus. Ein exploratives Ergebnis darf zur Untersuchung angezeigt werden, muss aber sichtbar von einem belastbaren Ergebnis unterschieden sein.
_Avoid_: belastbares Ergebnis, fehlgeschlagener Modelllauf

**Belastbares Analyseergebnis**:
Ein Ergebnis, das sämtliche verpflichtenden Kriterien der mit seiner Analysedefinition versionierten Modellreifeprüfung bestanden hat. Seine Modellreife ist unabhängig von Aktualität und Datenstatus; ein belastbares Ergebnis kann deshalb beispielsweise aktuell und vorläufig oder veraltet und geprüft sein. Der Status bezeichnet statistische Stabilität im definierten Modell und ist weder ein Kausalitäts- noch ein medizinischer Gültigkeitsnachweis.
_Avoid_: kausaler Nachweis, medizinische Aussage

### Quellenabdeckung

**Aktivitätsquellenregel**:
Die Apple Watch ist die Primärquelle für Schritte, Distanz und weitere automatisch erfasste Aktivitätsdaten. Eine gemeinsame, versioniert abgeleitete Watch-Abdeckungszeitachse gilt für alle Aktivitätsmerkmale; sobald ein zulässiger Watch-Datensatz für einen Zeitraum Abdeckung belegt, werden dort sämtliche überlappenden iPhone-Aktivitätsdaten unterdrückt. Ein iPhone-Datensatz darf nur dann vollständig zu Aktivitätsaggregaten beitragen, wenn sein gesamtes Intervall innerhalb einer gemeinsamen Watch-Abdeckungslücke liegt. Ein über die Lückengrenze reichender Datensatz bleibt mit Begründung sichtbar, wird aber vollständig unterdrückt und weder zeitanteilig aufgeteilt noch geschätzt. Allgemeine Aktivitätsmessungen von Drittanbietern oder unklarer Herkunft bleiben erhalten und sichtbar, tragen aber weder zu Aktivitätsaggregaten noch zu Watch-Abdeckung oder iPhone-Fallback bei. Trainingseinheiten bleiben unabhängig von ihrer Quelle gültig; mögliche Duplikate werden über überlappende Trainingseinheiten geprüft.
_Avoid_: messgrößenspezifische Quellenaddition, gleichrangige Quellenaddition, iPhone als Primärquelle

**Watch-Abdeckungslücke**:
Ein zusammenhängender Zeitraum von standardmäßig vier Stunden ohne verwertbare, von der Apple Watch stammende Daten über die verfügbaren Watch-Messgrößen hinweg. Die Schwelle ist ein unveränderlicher Parameter der jeweiligen Ableitungsversion, als ganze Minutenzahl von 1 bis 1440 konfigurierbar und standardmäßig 240 Minuten; eine Änderung berechnet die Quellenabdeckung neu, ohne Quellmessungen zu verändern. Abdeckung dürfen eindeutig als Watch-Daten klassifizierte Aktivitätsmessungen, Trainingseinheiten und Apple-Watch-Schlafintervalle belegen; Körpergewicht, Ernährung und einzelne Apple-Ruhepulswerte belegen keine kontinuierliche Watch-Abdeckung. Alle zulässigen Watch-Intervalle werden auf einer fortlaufenden Zeitachse vereinigt. Nur eine beidseitig von Watch-Abdeckung mit gleichem UTC-Offset begrenzte Datenlücke unterhalb der konfigurierten Schwelle gilt ebenfalls als abgedeckt; eine Lücke ab der Schwelle oder zwischen unterschiedlichen UTC-Offsets gilt vollständig als Watch-Abdeckungslücke. Zeit vor dem ersten und nach dem letzten Watch-Beleg wird niemals als Watch-Abdeckung abgeleitet.
_Avoid_: bewegungslose Minute, einzelner fehlender Aktivitätswert

**Tägliche Quellenabdeckung**:
Die sichtbare zeitliche Aufteilung einer Tagesaggregation in Apple-Watch-Abdeckung, iPhone-Fallback und unbeobachtete Zeiträume. Sie wird für jeden messlokalen Kalendertag vom frühesten bis zum spätesten zulässigen Watch- oder iPhone-Aktivitätsmessungstag ausgewiesen, auch wenn einzelne Aktivitätsmerkmale an einem Tag fehlen. Innerhalb einer Watch-Abdeckungslücke werden zulässige iPhone-Intervalle vereinigt; nur eine beidseitig von iPhone-Evidenz mit gleichem UTC-Offset begrenzte Lücke unterhalb derselben konfigurierten Schwelle gilt ebenfalls als iPhone-Fallback. Zeit außerhalb dieser belegten oder kurz überbrückten Watch- und iPhone-Intervalle bleibt unbeobachtet. Ein unbeobachtetes Intervall zwischen unterschiedlichen UTC-Offsets wird bis zum nächsten Beleg nach dem Offset des vorherigen Belegs in messlokale Tage geschnitten; nur vor dem ersten Beleg gilt dafür der Offset des folgenden Belegs. Die Aufteilung bleibt neben dem aggregierten Aktivitätswert erhalten und macht gemischte Quellen transparent.
_Avoid_: quellenloser Tagesgesamtwert, vollständige Tagesabdeckung

**Quellenneutrale Aktivitätsanalyse**:
Die vorläufige MVP-Regel, nach der Aktivitätswerte aus Apple-Watch-Abdeckung und zulässigem iPhone-Fallback im Modell gleichwertig behandelt werden. Die gespeicherte Quellenabdeckung ermöglicht eine spätere Neubewertung dieser Annahme.
_Avoid_: qualitätsgewichtete Quelle, endgültige Quellenäquivalenz

**Unvollständig beobachteter Aktivitätstag**:
Ein Tag mit einer beliebigen positiven unbeobachteten Dauer nach Anwendung der Watch- und iPhone-Überbrückungsregeln. Ein vollständig durch zulässigen iPhone-Fallback abgedeckter Tag ist vollständig beobachtet, bleibt aber sichtbar als iPhone-ergänzt gekennzeichnet. Vorhandene Aktivitätswerte eines unvollständig beobachteten Tags werden im MVP mitgerechnet; fehlende Aktivitätsmerkmale bleiben fehlend, und die Lücke sowie davon betroffene Analyseergebnisse werden sichtbar gekennzeichnet.
_Avoid_: inaktiver Tag, vollständiger Tageswert
