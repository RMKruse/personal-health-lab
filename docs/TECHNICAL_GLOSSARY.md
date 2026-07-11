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
Das präsentationsneutrale Ansichtsmodell mit dem Zustand `empty`, `ready` oder `provisional`, Zeitreihen, Trends, Unsicherheit, Qualitäts- und Quellenstatus, Analyseverweisen sowie Methodik und Provenienz. Es zeigt immer den neuesten Stand und verweist bei einem vorläufigen oder veralteten Ergebnis zusätzlich auf das letzte nicht vorläufige Ergebnis. Adapter formatieren es, ergänzen aber keine fachliche Logik.

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

**Logische Quellmessung**:
Die Identität eines Quellsamples über kumulative Exporte hinweg. Eine stabile Quell-ID wird bevorzugt; andernfalls definiert jeder unterstützte Datentyp eine natürliche Identität aus Quelle, Zeit und typabhängigen Merkmalen.

**Quellmessungsversion**:
Eine konkrete, in einem Import beobachtete Version einer logischen Quellmessung. Eine geänderte spätere Version wird für zukünftige Analysen bevorzugt, während frühere Versionen reproduzierbar bleiben.

**Vermutete Quellenlöschung**:
Eine Prüfauffälligkeit, wenn eine früher bekannte logische Quellmessung in einem späteren vollständigen Export fehlt. Das Fehlen allein entfernt nichts; eine Löschung benötigt Bestätigung.

## Prüfworkflow

**Initialer Plausibilitätsregelsatz**:
Die vor dem ersten Gesundheitsdatenimport bereitgestellte Regelsammlung. Bekannte initiale Datentypen werden nicht erstmals ohne Regelbasis geprüft.

**Unbekannter Datentyp**:
Ein erstmals auftretender Datentyp ohne Plausibilitätsregel. Er wird importiert, kann ohne Regel keine Regelverletzung erzeugen und löst einmalig die Frage nach einer Regeldefinition aus.

**Bewusst ungeregelter Datentyp**:
Ein Datentyp, für den der Benutzer ausdrücklich keine Regel definiert. Diese Entscheidung unterdrückt weitere Nachfragen, kann aber manuell aufgehoben werden.

**Erstregel-Rückprüfung**:
Die einmalige Prüfung der vollständigen vorhandenen Historie eines bewusst ungeregelten Datentyps, sobald seine erste Regel angelegt wird.

**Historische Rückprüfung**:
Ein eigener Prüfzyklus, der Auffälligkeiten einer Erstregel-Rückprüfung bündelt, ohne frühere Importprüfungen wieder zu öffnen.

**Importprüfung**:
Der Prüfzyklus für genau einen Import und dessen Plausibilitätsauffälligkeiten, Bestätigungen und Korrekturen.

**Abgeschlossene Datenprüfung**:
Der Zustand nach der bewussten Aktion „Prüfung abschließen und neu berechnen“, wenn alle Auffälligkeiten des aktuellen Prüfzyklus bestätigt oder korrigiert wurden.

**Sammelbestätigung**:
Die Bestätigung einer gefilterten Menge von Plausibilitätsauffälligkeiten, beispielsweise nach Zeitraum oder Wertebereich. Filterkriterien und betroffene Werte bleiben nachvollziehbar; Korrekturen bleiben Einzelaktionen.

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
