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
Die nach Messzeitpunkt letzte Gewichtsmessung eines lokalen Kalendertags, unabhängig von Tageszeit oder ursprünglicher Apple-Health-Quelle. Sie wird als Tageswert für Trendanalysen verwendet; frühere Messungen desselben Tages bleiben als Rohdaten erhalten.
_Avoid_: Tagesmittelwert, automatisch bevorzugte Morgenmessung

**Messlokaler Kalendertag**:
Der Kalendertag in der Zeitzone, die am Messzeitpunkt für den ursprünglichen HealthKit-Zeitstempel galt. Tagesaggregationen verwenden diese Zeitzone und werden nicht nach der aktuellen Mac-Zeitzone rückdatiert.
_Avoid_: aktuelle Anzeigezeitzone als historische Tagesgrenze, UTC-Kalendertag

**Schlaftag**:
Der messlokale Kalendertag, an dem eine Schlafnacht endet und die Person aufwacht. Schlafintervalle über Mitternacht werden als Kontext diesem Aufwachtag zugeordnet.
_Avoid_: Einschlaftag, Aufteilung einer Nacht in zwei unabhängige Nächte

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
Die getrennte Beschreibung der Assoziation zwischen den Aktivitätsmerkmalen eines Tages und dem Apple-Ruhepuls an jedem der folgenden sieben Tage. Es beschreibt zeitliche Zusammenhänge und keine Wirkungsdauer.
_Avoid_: Wirkungsprofil, Effektkurve

**Überlappende Aktivität**:
Die Situation, in der innerhalb des siebentägigen Verzögerungsfensters mehrere Aktivitätstage liegen und daher mehreren früheren Tagen derselbe spätere Ruhepuls gegenübersteht.
_Avoid_: additiver Effekt

**Gemeinsames Verzögerungsprofil**:
Ein Verzögerungsprofil, das die Aktivität an den einzelnen Vortagen 1 bis 7 gleichzeitig berücksichtigt. Es trennt die zeitlichen Assoziationen unter überlappender Aktivität, ohne sie als einzelne kausale Beiträge zu interpretieren.
_Avoid_: sieben unabhängige Analysen, additiver Effekt

**Gemeinsames Aktivitätsmodell**:
Eine Outcome-Analyse, die mehrere getrennte Aktivitätsmerkmale wie Trainingsdauer und aktive Energie gleichzeitig berücksichtigt. Ihre geschätzten Parameter beschreiben bedingte Assoziationen und sind keine Gewichte eines Aktivitätsscores; Merkmalsauswahl und Modellform dürfen nach statistischer Prüfung geändert werden.
_Avoid_: Aktivitätsscore, feste Parametergewichtung

### Kontext, Krankheit und Medikamente

**Kontextmerkmal**:
Eine zeitlich zugeordnete Größe, die bei der Interpretation einer Assoziation helfen kann, ohne automatisch ein nachgewiesener Confounder zu sein. Im MVP relevante Kontextmerkmale sind Schlaf, Krankheit, Stress, Kalorienaufnahme und Medikamente; Alkohol und nichtmedizinischer Drogenkonsum liegen außerhalb des persönlichen Anwendungsfalls.
_Avoid_: gesicherter Confounder, Ursache

**Baselineannahme**:
Der ohne täglichen Check-in angenommene Normalzustand: keine Krankheit, durchschnittlicher Stress und keine Medikamentenabweichung. Eine Baselineannahme bleibt als Annahme gekennzeichnet und ist nicht gleichbedeutend mit einer aktiv bestätigten Beobachtung.
_Avoid_: bestätigter Normalzustand, gemessener Wert

**Kontextabweichung**:
Eine manuell erfasste Abweichung von der Baselineannahme, beispielsweise Krankheit, über- oder unterdurchschnittlicher Stress oder eine Medikamentenabweichung.
_Avoid_: täglicher Pflicht-Check-in

**Kontextzeitraum**:
Eine Kontextabweichung mit explizitem Beginn und Ende, insbesondere für Krankheiten und Medikamentenänderungen. Der Zeitraum ordnet allen betroffenen Tagen denselben dokumentierten Kontext zu, ohne tägliche Wiederholungseingaben zu verlangen.
_Avoid_: Folge unabhängiger Tageseinträge

**Stressstufe**:
Die ordinale tägliche Einordnung „sehr niedrig“, „niedrig“, „durchschnittlich“, „hoch“ oder „sehr hoch“. Ohne bewusste Angabe gilt „durchschnittlich“ als Baselineannahme; eine aktiv bestätigte durchschnittliche Stufe bleibt davon unterscheidbar.
_Avoid_: klinische Stressdiagnose, kontinuierlich gemessener Stresswert

**Krankheitskategorie**:
Eine wiederverwendbare, auswählbare Bezeichnung für einen Krankheitszeitraum, beispielsweise „Erkältung“. Der vorgegebene Katalog kann vom Benutzer dauerhaft um neue Kategorien erweitert werden.
_Avoid_: einmaliger Freitext, medizinischer Diagnosecode

**Krankheitszeitraum**:
Ein Kontextzeitraum mit Krankheitskategorie, Beginn, Ende und dem persönlichen Schweregrad „leicht“, „mittel“ oder „schwer“. Er dokumentiert den persönlichen Kontext und ist keine ärztlich bestätigte Diagnose.
_Avoid_: Diagnose, unabhängige tägliche Krankheitseinträge

**Überlappende Krankheitszeiträume**:
Mehrere gleichzeitig aktive Krankheitszeiträume mit jeweils eigener Kategorie und eigenem Schweregrad. Sie bleiben getrennt erhalten; ein Tag ist nicht auf eine einzige Krankheitskategorie beschränkt.
_Avoid_: kombinierte Gesamtdiagnose, genau eine Krankheit pro Tag

**Krankheitsmerkmal**:
Die vereinfachte Repräsentation von Krankheitszeiträumen im ersten Modell: ob an einem Tag mindestens eine Krankheit aktiv ist und welcher höchste aktive Schweregrad vorliegt. Konkrete Kategorien bleiben gespeichert, werden aber erst bei ausreichender Modellreife einzeln analysiert.
_Avoid_: einzelne seltene Krankheit als unbedingtes Modellmerkmal, Diagnose

**Medikamentenplan**:
Die ab einem festgelegten Zeitpunkt gültige Baseline mit konkreten Medikamentennamen, Dosierungen und vorgesehenen Einnahmezeitpunkten. Eine bewusste Planänderung erzeugt eine neue Baseline und gilt nicht als Einnahmeabweichung.
_Avoid_: Medikamentenabweichung, einmalige tatsächliche Einnahme

**Medikamentenregime**:
Die während eines Gültigkeitszeitraums aktive Version des Medikamentenplans. Ein Regimewechsel wird in Zeitachsen markiert und im Modell als möglicher Strukturbruch berücksichtigt, nicht nur als Ereignis am Änderungstag.
_Avoid_: einmalige Planänderung, Einnahmeabweichung

**Einnahmeabweichung**:
Eine unbeabsichtigte Abweichung vom gültigen Medikamentenplan, insbesondere eine vergessene, verspätete oder doppelte Einnahme. Sie verändert den Medikamentenplan nicht.
_Avoid_: Planänderung, neue Baseline

**Bedarfsmedikation**:
Ein Medikament ohne festen Sollzeitpunkt, das bei einem konkreten Bedarf eingenommen werden darf. Eine Einnahme wird mit tatsächlichem Zeitpunkt, Dosis und optionalem Grund dokumentiert und gilt nicht als Einnahmeabweichung.
_Avoid_: vergessene Einnahme, Medikamentenplanänderung

**Medikamentenmerkmal**:
Ein zeitlich variierendes Ereignis des Medikamentenkontexts, das im ersten Modell berücksichtigt werden kann: eine Planänderung, Einnahmeabweichung oder tatsächliche Bedarfsmedikation. Der vollständige gültige Medikamentenplan bleibt dokumentiert, wird aber nicht allein aufgrund seiner Existenz als variierendes Modellmerkmal behandelt.
_Avoid_: unveränderter Medikamentenplan als tägliches Ereignis

**Einnahmegrund**:
Eine optionale Angabe zum Anlass einer Bedarfsmedikation, beispielsweise Kopfschmerzen oder Übelkeit. Im MVP ist sie Zusatzinformation zur Einnahme und kein verpflichtend eigenständig erfasster Symptomverlauf.
_Avoid_: Diagnose, verpflichtendes Symptomtagebuch

**Einnahmegrund-Kategorie**:
Ein global wiederverwendbarer, auswählbarer Einnahmegrund aus einem vorgegebenen oder vom Benutzer ergänzten Katalog. Medikamente können passende Kategorien bevorzugt vorschlagen, ohne jeweils getrennte Kataloge zu erzeugen.
_Avoid_: einmaliger Freitext, Diagnosecode

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

**Ernährungsmerkmal**:
Eine aus qualitätsgeprüften HealthKit-Ernährungssamples abgeleitete Modellgröße. Das erste Gewichtsmodell verwendet Gesamtenergie sowie Protein, Kohlenhydrate und Fett; weitere importierte Nährstoffe bleiben zunächst außerhalb des Modells.
_Avoid_: einzelnes Ernährungssample, vollständiges Nährstoffmodell

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
Ein vorhandenes Ergebnis, dessen zugrunde liegender Daten- oder Prüfstatus sich durch eine spätere Bestätigung, Korrektur oder andere wirksame Benutzerentscheidung geändert hat. Es bleibt unverändert an seinen damaligen Datensatz-Snapshot gebunden und darf weder als aktuell präsentiert noch nachträglich hochgestuft werden; nur ein neuer Modelllauf kann den neuen Zustand abbilden.
_Avoid_: aktuelles Ergebnis, gelöschter Modelllauf

**Vorläufiges Analyseergebnis**:
Ein Ergebnis, dessen Datensatz-Snapshot offene Datenprüffälle oder unvollständig beobachtete Aktivitätstage enthält. Es darf angezeigt werden, muss seinen vorläufigen Status und die Gründe sichtbar tragen und kann nach Datenprüfung oder späterer Modellanpassung durch eine Neuberechnung abgelöst werden.
_Avoid_: bestätigtes Ergebnis, fehlerfreies Ergebnis

**Modellreifeprüfung**:
Die modellspezifische Prüfung, ob Datenmenge, Vollständigkeit, Merkmalsabhängigkeiten, Schätzstabilität und Zeitreihendiagnostik eine belastbare Darstellung erlauben. Sie ersetzt eine starre Mindestzahl von Kalendertagen.
_Avoid_: pauschale 90-Tage-Regel, erfolgreiche Programmausführung

**Exploratives Analyseergebnis**:
Ein formal berechnetes Ergebnis, dessen Modellreifeprüfung nicht vollständig bestanden wurde. Es darf zur Untersuchung angezeigt werden, muss aber sichtbar von einem belastbaren Ergebnis unterschieden sein.
_Avoid_: belastbares Ergebnis, fehlgeschlagener Modelllauf

**Belastbares Analyseergebnis**:
Ein Ergebnis, dessen festgelegte Modellreifeprüfung bestanden wurde. Der Status bezeichnet statistische Stabilität im definierten Modell und ist weder ein Kausalitäts- noch ein medizinischer Gültigkeitsnachweis.
_Avoid_: kausaler Nachweis, medizinische Aussage

### Quellenabdeckung

**Aktivitätsquellenregel**:
Die Apple Watch ist die Primärquelle für Schritte, Distanz und weitere automatisch erfasste Aktivitätsdaten. iPhone-Daten dürfen ausschließlich fehlende Apple-Watch-Abdeckung ergänzen und nicht überlappende Watch-Werte ersetzen oder verdoppeln.
_Avoid_: gleichrangige Quellenaddition, iPhone als Primärquelle

**Watch-Abdeckungslücke**:
Ein zusammenhängender Zeitraum von standardmäßig mindestens vier Stunden ohne verwertbare, von der Apple Watch stammende Daten über die verfügbaren Watch-Messgrößen hinweg. Die Schwelle ist benutzerkonfigurierbar; das bloße Fehlen von Schritten oder Bewegung in einem kurzen Intervall gilt nicht als Abdeckungslücke.
_Avoid_: bewegungslose Minute, einzelner fehlender Aktivitätswert

**Tägliche Quellenabdeckung**:
Die sichtbare zeitliche Aufteilung einer Tagesaggregation in Apple-Watch-Abdeckung, iPhone-Fallback und unbeobachtete Zeiträume. Sie bleibt neben dem aggregierten Aktivitätswert erhalten und macht gemischte Quellen transparent.
_Avoid_: quellenloser Tagesgesamtwert, vollständige Tagesabdeckung

**Quellenneutrale Aktivitätsanalyse**:
Die vorläufige MVP-Regel, nach der Aktivitätswerte aus Apple-Watch-Abdeckung und zulässigem iPhone-Fallback im Modell gleichwertig behandelt werden. Die gespeicherte Quellenabdeckung ermöglicht eine spätere Neubewertung dieser Annahme.
_Avoid_: qualitätsgewichtete Quelle, endgültige Quellenäquivalenz

**Unvollständig beobachteter Aktivitätstag**:
Ein Tag mit einem oder mehreren Zeiträumen ohne zulässige Watch- oder iPhone-Abdeckung. Seine vorhandenen Aktivitätswerte werden im MVP mitgerechnet, aber die Lücke und davon betroffene Analyseergebnisse werden sichtbar gekennzeichnet.
_Avoid_: inaktiver Tag, vollständiger Tageswert
