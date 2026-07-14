# Technisches und betriebliches Glossar

Dieses Glossar enthält technische, betriebliche und lieferumfangsbezogene Begriffe. Die fachliche Sprache der persönlichen Gesundheitsanalyse steht in [CONTEXT.md](../CONTEXT.md).

## Produkt- und Laufzeitgrenzen

**Modul**:
Ein Teil des Systems mit genau einem Interface und einer verborgenen Implementierung. Ein tiefes Modul bündelt viel Verhalten hinter einem kleinen Interface.

**Interface**:
Alles, was ein Aufrufer über ein Modul wissen muss: Operationen, Invarianten, Reihenfolge, Fehlerfälle, Konfiguration und relevante Leistungsmerkmale.

**Öffentlicher Modulexport**:
Die ausschließlich über das `__init__.py` eines Moduls veröffentlichte Python-Oberfläche. Andere Module dürfen keine Implementierungsdateien per Deep Import umgehen.

**Snapshot-Referenz**:
Ein unveränderlicher, typisierter Verweis auf eine veröffentlichte Datensatzversion. Ein Analyseaufruf pinnt genau eine Referenz für seine gesamte Laufzeit und gibt sie im AnalysisReceipt zurück; große Tabellen werden nicht als frei veränderliche DataFrames zwischen Modulen übergeben.

**Opake ID**:
Ein eigener nicht austauschbarer Typ für Identitäten wie `ImportId`, `SnapshotId`, `AnalysisRunId` oder `LogicalMeasurementId`. UUID-, Hash- oder Stringdarstellung bleibt Implementierungsdetail.

**Speichermodul**:
Das interne Modul mit einem absichtsorientierten Interface für Importveröffentlichung, Snapshot-Auflösung, Analysepersistenz und Overview-Daten. Tabellen-CRUD, Dateipfade und konkrete Speichertechniken bleiben in seiner Implementierung verborgen.

**Single-Writer-Regel**:
Die Datenspeicherinvariante, dass genau eine schreibende Operation gleichzeitig aktiv sein darf, während snapshot-basierte Leser parallel arbeiten dürfen. Ein weiterer Schreiber erhält den erwartbaren Status `store_busy`.

**Seam**:
Die Stelle, an der das Interface eines Moduls liegt und Verhalten durch einen anderen Adapter ausgetauscht werden kann, ohne den Aufrufer zu ändern.

**Adapter**:
Eine konkrete Anbindung an einer Seam. CLI und Streamlit sind zwei Adapter für dasselbe V0.1-Anwendungsmodul.

**Production-CLI-Adapter**:
Der Einstiegspunkt `healthlab` für Import, Analyse und Overview. Er kennt den synthetischen Exportgenerator nicht und bietet menschenlesbare Ausgabe sowie `--json` mit versioniertem Schema. Exitcodes: `0` Erfolg oder No-op, `2` Verwendungsfehler, `3` erwartbar nicht abgeschlossen, `1` technischer Defekt.

**Development-CLI-Adapter**:
Der getrennte Einstiegspunkt `healthlab-dev` für Repository-sichere synthetische Fixtures. Er darf keinen realen Datenspeicher öffnen.

**Composition Root**:
Der einzige Ort, an dem ein Adapter Konfigurationsquellen liest, validiert und die Module von `HealthLab` zusammensetzt. Interne Module lesen keine Umgebungsvariablen oder globalen Settings.

**Receipt**:
Das unveränderliche Ergebnis einer schreibenden Interface-Operation mit stabiler ID, typisiertem Status und datensparsamen Diagnosen. Erwartbare Zustände werden als Receipt-Status und nicht als technische Ausnahme ausgedrückt.

**Overview**:
Das präsentationsneutrale Ansichtsmodell mit dem Zustand `empty`, `ready` oder `provisional`, Zeitreihen, Trends, Unsicherheit, Qualitäts- und Quellenstatus, Analyseverweisen sowie Methodik und Provenienz. Sein aktueller Ergebnisplatz enthält ausschließlich ein Ergebnis des aktiven Datensatz-Snapshots. Fehlt ein solcher Modelllauf, meldet das Overview ausdrücklich, dass kein aktuelles Ergebnis vorhanden ist, und darf das jüngste frühere Ergebnis nur in einem getrennten historischen Platz samt `stale`, Snapshot- und Run-ID sowie Ausführungszeitpunkt anbieten. Bei einem aktuellen vorläufigen Ergebnis darf es zusätzlich getrennt auf das letzte geprüfte historische Ergebnis verweisen. Adapter formatieren diese Trennung, ergänzen aber keine fachliche Logik.

**Analyseergebnisstatus**:
Die typisierte Kennzeichnung eines berechneten Analyseergebnisses durch drei unabhängige Enum-Dimensionen: Aktualität (`current`, `stale`), Datenstatus (`provisional`, `reviewed`) und Modellreife (`exploratory`, `robust`). Sie werden weder als Booleans noch als ein gemeinsames Kreuzprodukt-Enum dargestellt. Nur die Aktualität wird aus der aktiven Snapshot-Referenz abgeleitet und darf zwischen `current` und `stale` wechseln. Der unveränderliche Datenstatus hält zusätzlich typisierte Gründe und Beleg-IDs fest; die unveränderliche Modellreife hält jedes versionierte Kriterium mit Ergebnis, beobachtetem Wert und Schwelle fest. Neue Grund- oder Kriterientypen deuten frühere Ergebnisse nicht um. Der getrennte `AnalysisStatus` beschreibt weiterhin den Ausgang des Modelllaufs und nicht diese Ergebnisdimensionen.

**OverviewSelection**:
Das kleine unveränderliche Auswahlobjekt für den Zeitraum eines Overview. Qualitäts-, Quellen- und Provenienzdaten werden immer vollständig geliefert; visuelles Ein- und Ausblenden bleibt Sache des Adapters.

**Versionierte Analysekonfiguration**:
Ein unveränderliches, typisiertes und validiertes Konfigurationsobjekt eines Analysemoduls. Das externe Objekt enthält nur benutzerrelevante Angaben wie Zeitraum und `analysis_definition_id`; statistische Detailparameter gehören zur versionierten internen Analysedefinition. Serialisierte Form und Schemaversion gehören zum Modelllauf.

**Eingebaute Analysedefinition**:
Eine mit dem Code ausgelieferte, versionierte und getestete statistische Definition. Ihre ID ist unveränderlich; methodische Änderungen erzeugen eine neue ID. Frei editierbare Benutzerdefinitionen liegen außerhalb von V0.1.

**Code-Revision**:
Die Identität des für einen Modelllauf verwendeten Codes aus Git-Commit, Dirty-Flag und Hash des relevanten lokalen Diffs. Der Diff-Inhalt wird nicht im Analyseergebnis gespeichert.

**Kern-MVP**:
Die erste nutzbare Produktstufe aus synthetischem und realem Health-Exportimport, Datenprüfung und den getrennten Kernanalysen für Apple-Ruhepuls und Gewichtsveränderungsrate. Generischer CSV-Import sowie PDF- und Ärzteberichte folgen erst nach Stabilisierung dieses Kerns.

**Datenmodus**:
Die beim Anwendungsstart festgelegte Betriebsart `synthetic` oder `real`, die während einer Sitzung nicht gewechselt werden kann. Jeder Modus besitzt physisch getrennte Daten- und Metadatenspeicher; Daten werden nicht automatisch zwischen ihnen übertragen.

**Datenspeicherinhaber**:
Die genau eine Person, deren Gesundheitsdaten in einem realen Datenspeicher enthalten sind. Der Kern-MVP besitzt weder Benutzerkonten noch Mehrpersonenanalysen.

**Datenspeicher-ID**:
Die stabile, nicht personenbezogene Identität eines realen Datenspeichers und seiner Metadatensicherungen. Ein leerer Speicher darf eine Backup-ID übernehmen; ein nicht leerer Speicher lehnt eine Wiederherstellung mit abweichender ID ab.

**MVP-Zugriffsgrenze**:
Der reale Datenmodus ist im MVP ausschließlich direkt auf dem Mac über `localhost` erreichbar. Netzwerkzugriff wartet auf eine authentifizierte native App; iCloud dient dem Datenaustausch und nicht der Freigabe des lokalen Dashboards.

**Datensparsame Protokollierung**:
Die Standardprotokollierung im realen Datenmodus mit technischen IDs, Status, Zählern und Fehlerklassen, aber ohne Gesundheitswerte, Medikamentennamen oder personenbezogene Pfade. Ausführliche Diagnosedaten benötigen eine bewusste Freigabe.

**Redigiertes Diagnosepaket**:
Ein bewusst erzeugtes Fehleranalysepaket, dessen Inhalt vor dem Speichern in einer Vorschau erscheint und dessen persönliche Felder standardmäßig entfernt oder ersetzt werden. Unredigierte Details benötigen eine ausdrückliche Einzelbestätigung.

**Vollständige lokale Löschung**:
Die vorschaugestützte und ausdrücklich bestätigte Entfernung des gesamten gewählten realen Datenspeichers einschließlich interner Archive und Migrationssicherungen. Sie verspricht kein forensisches Überschreiben von SSD-Blöcken; externe Backups, macOS-Snapshots und Daten in Apple Health bleiben außerhalb ihrer Kontrolle.

## Export- und Importlebenszyklus

**Health-Exportpaket**:
Das vom Benutzer ausgewählte originale Apple-Health-Export-ZIP, das unverändert archiviert wird. Die Anwendung entpackt und importiert es; das Paket ist ein Quellartefakt und noch kein geprüfter Analysedatensatz.

**Synthetisches Health-Exportpaket**:
Ein künstlich erzeugtes, versioniertes Paket, das Struktur, unterstützte Datentypen und typische Qualitätsprobleme eines Apple-Health-XML-Exports nachbildet. Es durchläuft dieselbe Importpipeline wie ein reales Paket und enthält keine persönlichen Gesundheitsdaten.

**Synthetischer Exportgenerator**:
Das getrennte Entwicklungsmodul, das aus versioniertem Szenario, Seed und Zielpfad ein Repository-sicheres Health-Fixture erzeugt. Nur Entwicklungs-CLI und Tests verwenden sein Interface; der reale Datenmodus kann es nicht erreichen.

**Repository-sicheres Health-Fixture**:
Ein vollständig synthetisches oder nachweislich anonymisiertes Testartefakt ohne persönliche Gesundheitsdaten. Nur solche Fixtures dürfen versioniert werden.

**Unterstützter Health-Datentyp**:
Ein Datentyp auf der expliziten Import-Positivliste, dessen Semantik, Einheiten, Quellenregeln und Plausibilitätsbehandlung verstanden sind. Andere Typen bleiben im Originalarchiv und werden ohne blinde Normalisierung katalogisiert.

**MVP-Health-Datentypen**:
Die erste Positivliste: Apple-Ruhepuls, Körpergewicht, aktive und Ruheenergie, Trainings- und Bewegungszeit, Trainingseinheiten und Trainingsart, Schritte, Distanz, Apple-Watch-Schlaf sowie alle verfügbaren Ernährungsdatentypen.

**Importtransaktion**:
Die isolierte Verarbeitung eines Health-Exportpakets vom Parsing bis zur Validierung. Seine Daten werden nur nach vollständigem Erfolg gemeinsam analytisch sichtbar.

**Importmodul**:
Das tiefe interne Modul, das XML-Streaming, Positivliste, Normalisierung, Deduplizierung, Quellversionierung, Staging, Validierung, Veröffentlichung und Quarantäne hinter einer einzelnen Importoperation verbirgt.

**Gehärteter Importvertrag**:
Die Behandlung jedes Exportpakets als nicht vertrauenswürdige Eingabe mit Schutz vor ZIP-Pfadtraversal, Dekompressionsbomben, unerlaubten Einträgen, XML-External-Entities, Netzwerkzugriffen und Schreibzugriffen außerhalb des Staging-Bereichs.

**Ruhepulsanalysemodul**:
Das tiefe interne Modul, das Snapshot-Auflösung, Tagesmerkmale, Modellreifeprüfung, Distributed-Lag-Fit, Moving-Block-Bootstrap, Diagnostik und Ergebnispersistenz hinter einer Analyseoperation verbirgt.

**Overview-Modul**:
Das tiefe interne Lesemodul, das Snapshot- und Ergebniswahl, Statusmarker, Zeitreihen, Provenienz und Methodik zu genau einem präsentationsneutralen `Overview` zusammensetzt.

**Health-Data-Modul**:
Der gemeinsame Besitzer kanonischer Health-Sample-, Provenienz-, Einheiten- und Zeittypen samt ihren Invarianten. Es enthält weder Import- noch Persistenzlogik und ist kein allgemeiner `domain`-Sammelplatz.

**HealthKit-Anti-Corruption-Seam**:
Die Zuordnung von Apple-spezifischen Typbezeichnern, XML-Attributen und Einheiten zu kanonischen `health_data`-Typen. Apple-Details dürfen diese Seam nicht in Speicherung oder Analyse überschreiten.

**Importquarantäne**:
Der geschützte, analytisch unsichtbare Bereich für eine fehlgeschlagene Importtransaktion und ihren Fehlerbericht. Sie bleibt bis zu einem ausdrücklichen erneuten Versuch oder ihrer Löschung erhalten.

**Exportvorkommen**:
Die Beobachtung eines vollständig normalisierten Quellsamples in genau einem erfolgreich verarbeiteten Export. Ein in mehreren Exporten inhaltlich identisches Sample erzeugt mehrere Exportvorkommen, aber keine neue Quellmessungsversion.

**Exportdatum**:
Der im Exportpaket enthaltene Zeitpunkt, der seine fachliche Reihenfolge gegenüber anderen Exporten bestimmt. Importzeitpunkt und Dateisystemzeit ändern diese Reihenfolge nicht; fehlt eine eindeutige Einordnung, bleibt der Export fachlich ungeordnet.

**Geltender Export**:
Der erfolgreich und vollständig verarbeitete Export mit dem eindeutig fachlich neuesten Exportdatum. Ein später importierter, aber älterer oder fachlich ungeordneter Export verdrängt ihn nicht; ein älterer vergleichbarer Export darf jedoch frühere Anwesenheit belegen und dadurch eine Löschvermutung gegenüber dem geltenden Export begründen.

**Logische Quellmessung**:
Die Identität eines Quellsamples über kumulative Exporte hinweg. Eine stabile Quell-ID wird bevorzugt; andernfalls definiert jeder unterstützte Datentyp eine natürliche Identität aus Quelle, Zeit und typabhängigen Merkmalen.

**Quellmessungsversion**:
Eine konkrete, in einem Import beobachtete Version einer logischen Quellmessung. Eine geänderte spätere Version wird für zukünftige Analysen bevorzugt, während frühere Versionen reproduzierbar bleiben.

**Bevorzugte Quellmessungsversion**:
Die Quellmessungsversion einer logischen Quellmessung, die für nachfolgend aufgelöste Datensatz-Snapshots gilt. Ohne Konflikt ist es die im geltenden Export beobachtete Version; offene Löschvermutungen und fachlich ungeordnete Exporte ändern sie nicht.

**Quellmessungskonflikt**:
Mehrere abweichende Exportvorkommen teilen denselben heuristischen Identitätskandidaten, ohne sich durch Exportdatum oder starke Quellen-ID eindeutig ordnen zu lassen. Bis zur ausdrücklichen Benutzerentscheidung bleibt die zuletzt eindeutig bevorzugte Quellmessungsversion wirksam; die Entscheidung wählt entweder eine bevorzugte Version oder trennt die Kandidaten in mehrere wirksame logische Quellmessungen. Die Wahl pinnt keine Version dauerhaft: Eine später eindeutig zuordenbare Version aus einem fachlich neueren geltenden Export wird automatisch bevorzugt. Ein Widerruf öffnet den Konflikt erneut, stellt für nachfolgende Snapshots den letzten vor dem Konflikt eindeutig bevorzugten Zustand wieder her und lässt Entscheidungen an dadurch inaktiven Kandidaten ruhen, ohne eine ältere Konfliktauflösung zu reaktivieren.

**Vermutete Quellenlöschung**:
Ein Datenprüffall, wenn eine früher bekannte logische Quellmessung im geltenden, vergleichbaren Vollexport fehlt. Solange er offen ist, bleibt die bisher bevorzugte Quellmessungsversion wirksam; erst eine ausdrückliche Bestätigung darf den wirksamen Datenbestand ändern.

**Bestätigte Quellenlöschung**:
Die bewusste Benutzerentscheidung, eine vermutete Quellenlöschung für nachfolgend aufgelöste Datensatz-Snapshots wirksam zu machen; die ausgeschlossene logische Quellmessung besitzt dann keinen wirksamen Analysewert, während versionsgebundene Bestätigungen, Korrekturen und lokale Messungsausschlüsse ruhen. Bereits bestehende Snapshots und Analysen bleiben unverändert reproduzierbar; ein Widerruf nimmt dieselbe Quellmessungsversion samt ihrer noch gültigen Entscheidungen wieder auf, während bei einer fachlich neueren wiedererschienenen Version der Prüfablauf für neue Quellmessungsversionen gilt.

**Verworfene Quellenlöschung**:
Die bewusste Benutzerentscheidung, eine vermutete Quellenlöschung nicht wirksam zu machen. Die Quellmessung bleibt wirksam und dieselbe fortdauernde Abwesenheit löst keine erneute Prüfung aus; erst Wiedererscheinen und späteres erneutes Verschwinden begründen eine neue Vermutung.

## Prüfworkflow

**Prüfzyklus**:
Die unveränderlich abgegrenzte Prüfung aus genau einem Auslöser wie Import, Regeländerung oder historischer Rückprüfung. Prüfzyklen dürfen unabhängig voneinander gleichzeitig offen sein und schließen sich jeweils ohne wirksamen offenen Datenprüffall automatisch ab; der gesamte Datenstand bleibt vorläufig, solange irgendein Prüfzyklus einen aktuell wirksamen offenen Datenprüffall enthält.

**Initialer Plausibilitätsregelsatz**:
Die versioniert mit der Software bereitgestellte Regelsammlung, die für einen neuen Datenspeicher oder einen neu unterstützten kanonischen Datentyp die erste Regelversion erzeugt. In V0.2 verwendet Apple-Ruhepuls den einschließlich gültigen festen Bereich von 20 bis 250 bpm sowie den persönlichen Referenzbereich; ein Aktive-Energie-Quellsample verwendet die feste Untergrenze 0 kcal ohne Obergrenze oder persönlichen Referenzbereich. Nur strikt außerhalb einer konfigurierten Grenze liegende Werte werden auffällig. Diese anpassbaren Werte sind Plausibilitätsheuristiken und keine medizinischen Grenzen. Bekannte initiale Datentypen werden nicht erstmals ohne Regelbasis geprüft. Ein Softwareupdate ersetzt weder eine aktive noch eine bewusst deaktivierte Regel; eine neue ausgelieferte Empfehlung wird erst durch ausdrückliche Übernahme zu einer neuen gültigen Regelversion.

**Unbekannter Datentyp**:
Ein erstmals auftretender Datentyp ohne Plausibilitätsregel. Er wird importiert, kann ohne Regel keine Regelverletzung erzeugen und löst einmalig die Frage nach einer Regeldefinition aus.

**Bewusst ungeregelter Datentyp**:
Ein Datentyp, für den der Benutzer ausdrücklich keine aktive Regel definiert oder eine bestehende Regel durch eine unveränderliche Regelversion ohne aktive Prüfbestandteile deaktiviert hat. Die Deaktivierung gilt ab dem üblichen Wochenbeginn; Auffälligkeiten der abgelösten Version bleiben im Audit, bestimmen aber nicht mehr den aktuellen Prüfstatus. Messzeiten im deaktivierten Intervall bleiben ungeregelt. Eine spätere Reaktivierung erzeugt eine neue Regelversion und prüft das deaktivierte Intervall nur durch eine ausdrücklich gestartete historische Rückprüfung.

**Erstregel-Rückprüfung**:
Die einmalige Prüfung der vollständigen vorhandenen Historie eines bewusst ungeregelten Datentyps, sobald seine erste Regel angelegt wird. Diese erste Regelversion gilt ohne untere Zeitgrenze; später importierte ältere Messungen verwenden sie ebenfalls, werden jedoch im jeweiligen Importprüfzyklus geprüft.

**Historische Rückprüfung**:
Ein eigener Prüfzyklus für einen festgehaltenen Zeitraum, eine festgehaltene Plausibilitätsregelversion und den bei Prüfungsbeginn aktuell wirksamen Datensatz-Snapshot, der Auffälligkeiten einer Erstregel-Rückprüfung oder einer bewusst ausgelösten erneuten Prüfung bündelt, ohne frühere Prüfzyklen zu verändern. Standardmäßig verwendet eine bewusst ausgelöste historische Rückprüfung die aktuell gültige Regelversion. Ein persönlicher Referenzbereich wird für jeden Wert ausschließlich aus dessen vorheriger Historie im festgehaltenen Snapshot berechnet. Eine vorhandene Datenbestätigung gilt weiter, wenn Quellmessungsversion, Regelversion und Art der Auffälligkeit identisch sind; andernfalls entsteht ein neuer offener Prüffall. Frühere Auffälligkeiten und Bestätigungen bleiben als Audit-Historie erhalten.

**Regeländerungsprüfung**:
Der eigene Prüfzyklus, der nach einer neuen Plausibilitätsregelversion alle derzeit wirksamen Quellmessungsversionen seit dem Gültigkeitsbeginn in der laufenden Woche neu bewertet. Auffälligkeiten der abgelösten Regelversion bleiben im Audit, bestimmen aber nicht mehr den aktuellen Prüfstatus; Auffälligkeiten der neuen Version beginnen offen.

**Importprüfung**:
Der Prüfzyklus für genau einen Import und dessen Datenprüffälle, Plausibilitätsauffälligkeiten sowie zugehörige Benutzerentscheidungen.

**Abgeschlossene Datenprüfung**:
Der automatisch eintretende Zustand eines Prüfzyklus, sobald er keine wirksamen offenen Datenprüffälle enthält; das kann bereits bei seiner Anlage oder nach der letzten Benutzerentscheidung der Fall sein. Grund und Zeitpunkt des Übergangs bleiben im Audit, während die Neuberechnung von Analysen eine getrennte bewusste Operation bleibt.

**Sammelbestätigung**:
Die atomare Bestätigung einer zuvor angezeigten, gefilterten Menge offener Plausibilitätsauffälligkeiten, beispielsweise nach Zeitraum oder Wertebereich. Die Vorschau materialisiert Filter, Treffer und Anzahl; ändert sich diese Menge vor der Bestätigung, scheitert die Aktion ohne Teilwirkung. Filterkriterien, die exakte Treffermenge und eine gemeinsame Sammelaktions-ID bleiben nachvollziehbar; jede betroffene Auffälligkeit erhält eine eigene, separat widerrufbare Datenbestätigung, während ein Widerruf der Sammelaktion nur ihre noch wirksamen Bestätigungen und keine späteren Entscheidungen aufhebt.

## Speicherung, Sicherung und Migration

**Realer Datenspeicher**:
Der lokale Speicher außerhalb des Repositorys für reale Exportpakete, abgeleitete Gesundheitsdaten und Metadaten. Der MVP verlässt sich auf macOS-Kontenschutz und FileVault und warnt vor einem unverschlüsselten Ziel-Datenträger.

**Metadatensicherung**:
Eine manuelle Sicherung lokaler Zustände, die nicht aus einem Health-Export rekonstruiert werden können, darunter Korrekturen, Bestätigungen, Regelversionen, Medikamentenpläne, Kontextzeiträume und benutzerdefinierte Kategorien.

**Migrationssicherung**:
Eine unmittelbar vor einer Schema-Migration automatisch erzeugte lokale Sicherheitskopie. Sie ist die einzige automatische Backup-Ausnahme im MVP und wird nicht automatisch gelöscht.

**Metadatenwiederherstellung**:
Ein in einer Vorschau gezeigter und bestätigter ID-basierter Abgleich. Gleiche Eintrags-IDs übernehmen die Backup-Version, nur im Backup vorhandene Einträge werden ergänzt und nur lokal vorhandene Einträge bleiben erhalten; die Datenspeicher-IDs müssen übereinstimmen, sofern der lokale Speicher nicht leer ist.

**Löschmarkierung**:
Der versionierte Zustand, dass ein lokaler Metadateneintrag bewusst gelöscht oder deaktiviert wurde. Die Audit-Historie bleibt erhalten und wird mitgesichert und wiederhergestellt.
