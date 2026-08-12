# Technisches und betriebliches Glossar

Dieses Glossar enthält technische, betriebliche und lieferumfangsbezogene Begriffe. Die fachliche Sprache der persönlichen Gesundheitsanalyse steht in [CONTEXT.md](../CONTEXT.md).

## Produkt- und Laufzeitgrenzen

**Modul**:
Ein Teil des Systems mit genau einem Interface und einer verborgenen Implementierung. Ein tiefes Modul bündelt viel Verhalten hinter einem kleinen Interface.

**Anwendungsmodul**:
Das einzige öffentliche Produktionsmodul für CLI und Streamlit. Es besitzt Sitzung und Arbeitsbereichsstatus, den gemeinsamen Vorschau-/Ausführungsablauf einschließlich Plan-Fingerprint, Freigabestatus, Preflight-Neuprüfung und Dispatch sowie die einmalige Übersetzung interner Ergebnisse und Fehler; präsentationsneutrale Projektionen und opake IDs der besitzenden Module exportiert es gezielt weiter.

**Interface**:
Alles, was ein Aufrufer über ein Modul wissen muss: Operationen, Invarianten, Reihenfolge, Fehlerfälle, Konfiguration und relevante Leistungsmerkmale.

**Leseprojektion**:
Ein unveränderliches, vollständig typisiertes und präsentationsneutrales Ansichtsmodell für genau einen fachlichen Lesezweck. V0.2 veröffentlicht über `application` die Projektionen Arbeitsbereichsstatus, Overview, Datenprüfung, Datenprüffalldetail, Plausibilitätsregeln, Sicherung und Wiederherstellung sowie Migration und Diagnose; Adapter formatieren Werte und Codes, ergänzen aber weder Zulässigkeits- noch Auswahl- oder Interpretationslogik. Frei strukturierte Dictionaries, DataFrames, SQL-Zeilen, Speicherpfade und fertige UI-Texte gehören nicht in eine Leseprojektion.

**Öffentlicher Modulexport**:
Die ausschließlich über das `__init__.py` eines Moduls veröffentlichte Python-Oberfläche. Andere Module dürfen keine Implementierungsdateien per Deep Import umgehen.

**Snapshot-Referenz**:
Ein unveränderlicher, typisierter Verweis auf eine veröffentlichte Datensatzversion. Ein Analyseaufruf pinnt genau eine Referenz für seine gesamte Laufzeit und gibt sie im AnalysisReceipt zurück; große Tabellen werden nicht als frei veränderliche DataFrames zwischen Modulen übergeben.

**Opake ID**:
Ein eigener nicht austauschbarer Typ für Identitäten wie `ImportId`, `SnapshotId`, `AnalysisRunId` oder `LogicalMeasurementId`. UUID-, Hash- oder Stringdarstellung bleibt Implementierungsdetail.

**Speichermodul**:
Das interne Modul mit einem absichtsorientierten Interface für Betriebsfakten, Volume- und FileVault-Befunde, exklusiven Writer-Lock, Persistenz, harte Artefaktvalidierung und atomare Snapshot-Aktivierung. Die fachliche Snapshot-Auflösung, Preflight-Freigabe und Regeln anderer Module besitzt es nicht; Tabellen-CRUD, Dateipfade und konkrete Speichertechniken bleiben in seiner Implementierung verborgen.

**Datenqualitätsmodul**:
Das tiefe interne Modul `data_quality`, das Plausibilitätsregelversionen, Prüfzyklen, Datenprüffälle, Benutzerentscheidungen und die fachliche Auflösung wirksamer Analysewerte und offener Prüffälle in einen Datensatz-Snapshot besitzt. `storage` schreibt, validiert und aktiviert sein Ergebnis, entscheidet aber nicht dessen Inhalt.

**Wiederherstellungsmodul**:
Das tiefe interne Modul `recovery`, das Metadatensicherung, zweiphasige Wiederherstellung, Quellenreferenzabgleich und atomare Overlay-Aktivierung besitzt. Benötigte Health-Exporte bleiben hinter `health_import`; Sicherungsmigrationen verwendet das Modul über `migration`, den abschließenden Snapshot über `data_quality`.

**Migrationsmodul**:
Das tiefe interne Modul `migration`, das den gemeinsamen Vorwärtsmigrationsvertrag für Datenspeicher, Datensatz-Snapshots und Metadatensicherungen sowie Migrationsplan, Staging, Schrittkette, Zielvalidierung und zulässigen Rollback besitzt.

**Single-Writer-Regel**:
Die Datenspeicherinvariante, dass genau eine schreibende Operation gleichzeitig aktiv sein darf, während snapshot-basierte Leser parallel arbeiten dürfen. Ein weiterer Schreiber erhält den erwartbaren Status `store_busy`.

**Seam**:
Die Stelle, an der das Interface eines Moduls liegt und Verhalten durch einen anderen Adapter ausgetauscht werden kann, ohne den Aufrufer zu ändern.

**Adapter**:
Eine konkrete Anbindung an einer Seam. CLI und Streamlit sind zwei Adapter für dasselbe Anwendungsmodul und importieren dessen öffentliche Requests, Projektionen, IDs, Enums und Ausnahmen ausschließlich über `personal_health_lab.application`.

**Production-CLI-Adapter**:
Der Einstiegspunkt `healthlab` für die fachlichen Schreibaufträge und Leseprojektionen. Er kennt den synthetischen Exportgenerator nicht und bietet menschenlesbare Ausgabe sowie `--json` mit versioniertem Schema. Exitcodes: `0` Erfolg oder No-op, `2` Verwendungsfehler, `3` erwartbar nicht abgeschlossen, `1` technischer Defekt.

**Development-CLI-Adapter**:
Der getrennte Einstiegspunkt `healthlab-dev` für Repository-sichere synthetische Fixtures. Er darf keinen realen Datenspeicher öffnen.

**Composition Root**:
Der einzige Ort, an dem ein Adapter Konfigurationsquellen liest, validiert und die Module von `HealthLab` zusammensetzt. Interne Module lesen keine Umgebungsvariablen oder globalen Settings.

**Receipt**:
Das unveränderliche Ergebnis der ausdrücklichen Ausführung eines Schreibauftrags mit stabiler Operations-ID, bestätigtem Plan-Fingerprint, typisiertem Ergebnis, finalem Preflight und datensparsamen Diagnosen. `WriteReceipt.result` ist eine geschlossene Union aus nicht gestarteten und operationsspezifischen Ergebnissen; erwartbare Zustände werden darin und nicht als technische Ausnahme ausgedrückt.

**Schreibauftrag**:
Ein unveränderlicher, vollständig typisierter fachlicher Eingabewert der geschlossenen V0.2-Union `WriteRequest`. `HealthLab.preview_write` erzeugt daraus eine Schreibvorschau; `HealthLab.execute_write` erhält denselben Auftrag und den erwarteten Plan-Fingerprint, berechnet den Plan neu und führt nur den unveränderten Plan aus. Ein separates Bestätigungsargument existiert nicht: Die ausdrückliche fingerprint-gebundene Ausführung bestätigt alle im Plan aufgeführten Gründe gemeinsam.

**Arbeitsbereichsstatus**:
Die globale schreibgeschützte Leseprojektion eines geöffneten `HealthLab` mit Datenmodus, Datenspeicher-ID, Betriebszustand und den fachlich zulässigen Lese- und Schreiboperationen. Mindestens `ready`, `migration_required` und `restore_pending` sind erwartbare Zustände; das Öffnen migriert niemals implizit und lässt in eingeschränkten Zuständen die benötigten Status- und Diagnosezugriffe zu.

**Projektionsunverfügbarkeit**:
Das typisierte erwartbare Ergebnis `ProjectionUnavailable`, wenn eine Leseprojektion im aktuellen Arbeitsbereichszustand oder Datenmodus nicht zulässig oder nicht vorhanden ist. Es enthält einen Code und datensparsame Diagnosen; nur ein beschädigter oder technisch nicht diagnostizierbarer Speicher löst eine Ausnahme aus.

**Overview**:
Das präsentationsneutrale Ansichtsmodell mit dem Zustand `empty`, `ready` oder `provisional`, Zeitreihen, Trends, Unsicherheit, Qualitäts- und Quellenstatus, Analyseverweisen sowie Methodik und Provenienz. Sein aktueller Ergebnisplatz enthält ausschließlich ein Ergebnis des aktiven Datensatz-Snapshots. Fehlt ein solcher Modelllauf, kennzeichnet das Overview dies typisiert und darf das jüngste frühere Ergebnis nur in einem getrennten historischen Platz samt `stale`, Snapshot- und Run-ID sowie Ausführungszeitpunkt anbieten. Bei einem aktuellen vorläufigen Ergebnis darf es zusätzlich getrennt auf das letzte geprüfte historische Ergebnis verweisen. Adapter formatieren Codes und Werte, ergänzen aber weder fertige Anwendungstexte noch fachliche Logik.

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
Die genau eine Person, deren Gesundheitsdaten in einem realen Datenspeicher enthalten sind. Beim ersten realen Import bestätigt der Nutzer diese Einpersonenzuordnung; sie wird dauerhaft, sobald der Import reale Daten veröffentlicht oder potenziell personenbezogene Artefakte quarantänisiert. Eine vollständig bereinigte Ablehnung sowie `store_busy` oder ein Abbruch vor dem Lesen lassen den Speicher unzugeordnet. Bei jedem späteren realen Import bestätigt der Nutzer, dass der gewählte Export derselben Person gehört. V0.2 leitet keinen Personenfingerprint aus Exportmerkmalen ab und behauptet keine automatische Personenerkennung. Synthetische Importe benötigen keine Einpersonenbestätigung. Der Kern-MVP besitzt weder Benutzerkonten noch Mehrpersonenanalysen.

**Datenspeicher-ID**:
Die beim Anlegen erzeugte stabile, zufällige und nicht personenbezogene Identität eines realen oder synthetischen Datenspeichers. Datenmodus und ID bleiben unveränderlich gebunden. Bei der bestätigten V0.2-Bestandsmigration erhält jeder ältere Speicher einmalig eine solche ID; ein nicht leerer realer Speicher verlangt zugleich die Einpersonenbestätigung, ein leerer bleibt unzugeordnet. Nur ein neuer, noch leerer realer Speicher darf bei einer Wiederherstellung die gesicherte Datenspeicher-ID übernehmen; ein bestehender Speicher mit abweichender ID lehnt sie ab. Eine vollständige Dateikopie behält dieselbe ID, aber V0.2 führt Kopien weder zusammen noch synchronisiert es sie.

**Metadatensicherung**:
Ein manuell erzeugtes, portables Overlay der nicht aus Quellexporten und versioniertem Code reproduzierbaren Zustände eines realen Datenspeichers. Synthetische Speicher werden nicht gesichert oder restauriert; sie entstehen aus versionierten Fixtures. Die geschlossene V0.3-Positivliste umfasst Manifest und Datenspeicher-ID, Plausibilitätsregelversionen, vollständige Prüf- und Begründungsfakten, alle Benutzer- und Sammelentscheidungen samt Treffermengen, sämtliche manuellen Kontext- und Medikamentenrevisionen samt Katalogen, die lückenlose Auditfolge, wirksame Quellenklassifikations- und Ableitungsstände, die aktive Revisionsbindung, Snapshot-Stichtag, ursprüngliche aktive Snapshot-ID sowie die zur Rekonstruktion benötigten Import- und Quellenreferenzen und Inhaltsfingerabdrücke. Quellexportpakete, vollständige Rohmessungen, Parquet-Snapshots, Analyseartefakte, aktive Snapshot-Zeiger, Caches, Staging, Quarantäneinhalt, Logs, Laufzeitkonfiguration, Pfade und Secrets gehören nicht dazu; spätere Metadaten benötigen eine versionierte Schemaerweiterung. Da der Sicherungsinhalt Gesundheitsdaten und Freitext enthalten kann, benötigt er denselben Schutz wie der reale Datenspeicher.

**Sicherungs-ID**:
Die einmalige Identität einer vollständig und eigenständig wiederherstellbaren Metadatensicherung. Ihr Manifest bindet sie an Datenspeicher-ID, Sicherungs- und Speicherschemaversion, Erstellungszeit, Audit-Höchststand und Hash des kanonischen Inhalts; dieselbe Sicherungs-ID mit demselben Hash erneut anzuwenden ist ein No-op, mit anderem Inhalt wird sie abgelehnt. V0.2 kennt keine inkrementellen Sicherungsketten.

**Sicherungsschema-Vorwärtsmigration**:
Die lückenlose und getestete Umwandlung einer älteren unterstützten Metadatensicherung in einer Staging-Arbeitskopie auf das aktuelle Sicherungsschema. Originaldatei, ursprüngliche Sicherungs-ID und ursprünglicher Hash bleiben dokumentiert; die migrierte Arbeitskopie erhält einen eigenen Hash und darf erst nach vollständiger Integritäts-, Referenz- und Inhaltsvalidierung verwendet werden. Die Migrationskette erscheint im Wiederherstellungsplan und wird durch dessen eine Bestätigung abgedeckt; sie verlangt keine zweite Bestätigung. Neuere unbekannte Versionen, fehlende Migrationsschritte, Downgrades und Best-effort-Importe werden abgelehnt.

**Sicherungslöschmarkierung**:
Ein unveränderliches Auditereignis mit eigener ID, das die exakte ID eines gelöschten, widerrufenen, abgelösten oder deaktivierten Metadatenobjekts dauerhaft unwirksam hält. Metadaten-IDs werden nie wiederverwendet; eine spätere fachliche Neuerstellung erhält eine neue ID. Gleiche IDs mit gleichem Inhalt werden idempotent zusammengeführt, bei abweichendem Inhalt liegt ein harter Konflikt vor. Die Sicherungslöschmarkierung ist keine vermutete Quellenlöschung.

**Auditposition**:
Die vom einzigen Schreiber transaktional vergebene, streng steigende Reihenfolge unveränderlicher Auditereignisse eines Datenspeichers. Ziel-IDs beschreiben die fachliche Beziehung, während ausschließlich die Auditposition „später“ bestimmt; Zeitstempel bleiben informativ. Eine Metadatensicherung enthält die lückenlose Folge bis zu ihrem Audit-Höchststand, und nach Wiederherstellung wird sie dahinter fortgesetzt; Lücken, doppelte Positionen, Vorwärtsreferenzen und widersprüchliche Ereignisse blockieren die Aktivierung.

**V0.3-Auditumfang**:
Genau ein globales unveränderliches Auditereignis pro erfolgreich abgeschlossener Schreiboperation. Ein manueller Kontext- oder Medikamentenschreibvorgang verweist darin auf seine neue Revision; Importe und materialisierte Ableitungen erzeugen keine Auditereignisse pro Datenzeile. Deren Historie bleibt stattdessen über Snapshot, Datensatzmanifest, Quellmessungsversionen, beitragende Identitäten und Ableitungsvertrags-ID reproduzierbar.

**Manuelle Revisionshülle**:
Der gemeinsame unveränderliche SQLite-Kopf jeder Kontext- oder Medikamentenrevision mit logischer ID, Revisions-ID, geschlossenem Objekttyp, unmittelbarer Vorgängerrevision, Status, erzeugender Operation, Zeitpunkt und Payload-Hash. Fachwerte liegen ausschließlich in getrennten streng typisierten Tabellen für Kontextabdeckung, Kataloge, Zeiträume, Stress, Regime, Einnahmeabweichungen und Bedarfseinnahmen; eine universelle JSON-Payload oder breite Nullspaltentabelle existiert nicht. Snapshot-Bindungen referenzieren die Revisionshülle und enthalten pro logischer Identität höchstens die genau eine im Snapshot wirksame Revision.

**Metadatenwiederherstellung**:
Die zweiphasige Punkt-in-Zeit-Anwendung einer Metadatensicherung auf einen neuen realen Datenspeicher. Vor bewusster Bestätigung weist sie Erstellungszeit und Audit-Höchststand aus und bestätigt, dass Sicherung sowie nachzuliefernde Health-Exporte derselben Person gehören; spätere ungesicherte Entscheidungen gelten nach Totalverlust als verloren. Der noch leere Speicher übernimmt zuerst die gesicherte Datenspeicher-ID und Einpersonenzuordnung als ausstehend und hält das Overlay ebenfalls ausstehend; in diesem Zustand sind nur benötigte Quellimporte, Statusprüfung und der vollständige Abbruch mit Verwerfen des neuen Speichers zulässig. Jeder Quellimport bestätigt erneut dieselbe Person und rekonstruiert zunächst nur Exportvorkommen, Quellmessungen und Quellmessungsversionen, ohne einen aktiven Datensatz-Snapshot oder fachliche Prüffälle zu veröffentlichen. Sind alle referenzierten Fachobjekt-IDs mit ihren unveränderlichen Inhaltsfingerabdrücken vorhanden und sämtliche internen Referenzen geschlossen, werden Overlay, Regelversionen und Einpersonenzuordnung vollständig und atomar aktiviert; danach werden von der Sicherung nicht abgedeckte neuere Quellmessungsversionen regulär geprüft und genau ein aktiver Snapshot aufgelöst. Andernfalls bleibt das Overlay mit Diagnose ausstehend; Teilaktivierung, konkurrierende Benutzerentscheidungen, Analysen und das Einmischen einer weiteren abweichenden Sicherung finden nicht statt.

**Wiederherstellungsidentität**:
Die deterministische Rekonstruktion von Quellmessungs- und Quellmessungsversion-IDs mit der in der Metadatensicherung gebundenen versionierten Quellenidentitäts- und Mappingregel. Heuristisches Neuverknüpfen ähnlicher Messungen ist unzulässig; Benutzer-, Revisions- und Auditobjekte übernehmen ihre gesicherten typisierten IDs unverändert, und spätere Migrationen bewahren bestehende Fachobjekt-IDs oder dokumentieren eine explizite auditierte Zuordnung. Der neu materialisierte Datensatz-Snapshot erhält dagegen eine neue Snapshot-ID und hält Sicherungs-ID sowie ursprüngliche Snapshot-ID als Herkunft fest, weil er fachlich gleichwertig, aber kein byteidentisches wiederverwendetes Artefakt ist.

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
Das tiefe interne Modul, das Auswahl und Pinning einer veröffentlichten Snapshot-Referenz, Tagesmerkmale, Modellreifeprüfung, Distributed-Lag-Fit, Moving-Block-Bootstrap, Diagnostik und Ergebnispersistenz hinter einer Analyseoperation verbirgt.

**Overview-Modul**:
Das tiefe interne Lesemodul, das Snapshot- und Ergebniswahl, Statusmarker, Zeitreihen, Provenienz und Methodik zu einem präsentationsneutralen `Overview` zusammensetzt. `Overview` bleibt eine fokussierte V0.2-Leseprojektion unter mehreren und wird wie die Projektionen von `data_quality`, `recovery` und `migration` ausschließlich über `application` an Adapter veröffentlicht.

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
Die atomare Bestätigung einer zuvor angezeigten, gefilterten Menge offener Plausibilitätsauffälligkeiten, beispielsweise nach Zeitraum oder Wertebereich. Die Vorschau materialisiert Filter, stabile Fall-IDs, entscheidungsrelevante Werte und Anzahl; die vollständige exakte Treffermenge bleibt ohne stille Kürzung prüfbar, wobei Streamlit sie paginieren und die CLI den nativen Pager verwenden darf. V0.2 setzt keine künstliche Mengenobergrenze. Ändert sich die Menge vor der Bestätigung, scheitert die Aktion ohne Teilwirkung. Filterkriterien, exakte Treffermenge, Plan-Fingerprint und gemeinsame Sammelaktions-ID bleiben nachvollziehbar; jede betroffene Auffälligkeit erhält eine eigene, separat widerrufbare Datenbestätigung, während ein Widerruf der Sammelaktion nur ihre noch wirksamen Bestätigungen und keine späteren Entscheidungen aufhebt.

## Speicherung, Sicherung und Migration

**Realer Datenspeicher**:
Der lokale Speicher außerhalb des Repositorys für reale Exportpakete, abgeleitete Gesundheitsdaten und Metadaten. Der MVP verlässt sich auf macOS-Kontenschutz und FileVault und warnt vor einem unverschlüsselten Ziel-Datenträger.

**FileVault-Schutzstatus**:
Der unmittelbar vor einer realen Schreiboperation für den tatsächlich verwendeten Zielpfad ermittelte Zustand `protected`, `unprotected`, `transitioning` oder `unknown`. `protected` lässt die Operation ohne Sicherheitswarnung fortfahren; jeder andere Zustand verlangt eine deutliche Warnung und ausdrückliche Bestätigung, blockiert V0.2 aber nicht allein. Die Bestätigung bindet genau den angezeigten Preflight und eine Schreiboperation; spätere Operationen prüfen und bestätigen erneut. Bei Migration oder Wiederherstellung geht die Warnung in deren ohnehin erforderliche Gesamtbestätigung ein, ohne einen zweiten Dialog zu erzeugen. Ein technisch nicht beschreibbares, gesperrtes oder schreibgeschütztes Ziel blockiert unabhängig vom Schutzstatus. Im synthetischen Modus findet keine FileVault-Prüfung statt.

**Speicherplatz-Preflight**:
Das harte Kapazitätstor unmittelbar vor Import, Metadatensicherung, Wiederherstellung oder Migration unter dem exklusiven Schreib-Lock. Es vergleicht den für den Prozess verfügbaren Platz jedes tatsächlichen Schreibzielvolumens mit der konservativ geschätzten vollständigen Staging-Ausgabe und dem Operations-Overhead. Darauf kommen `max(25 % der Operationsschätzung, 256 MiB)` Sicherheitsmarge und 1 GiB danach verbleibender Mindestrest. Import prüft den aktiven Datenspeicher, Backup das gewählte Sicherungsziel, Wiederherstellung den neuen realen Datenspeicher und Migration den Datenspeicher einschließlich internem Staging und Migrationssicherung. Nur gelesene Health-Exporte und Wiederherstellungssicherungen sind keine Gates. Plan und Diagnose zeigen Schätzung, Marge, Mindestrest und verfügbaren Platz; die pro Operation versionierte Schätzmethode muss eine konservative Obergrenze liefern. `insufficient` und `unknown` blockieren die Operation in realem wie synthetischem Modus; V0.2 kennt weder einen zusätzlichen Warnbereich für knappen, aber freigegebenen Platz noch UI-Regler für diese Konstanten. Ein trotz positivem Preflight auftretendes `ENOSPC` bricht atomar ab und lässt den aktiven Zustand unverändert.

**Operationsschätzung**:
Die mit einer unveränderlichen Methoden-ID versionierte Obergrenze der zusätzlich allozierten Bytes einer Schreiboperation auf genau einem tatsächlichen Zielvolume. Sie ist das Maximum über die Operationsphasen und addiert darin nur gleichzeitig lebende neue oder neu materialisierte Artefaktfamilien, SQLite-Wachstum, Journale, Sicherungen, Manifest- und Verzeichniskosten sowie begrenzte Scratch-Artefakte. Bereits vorhandene, nachweislich unverändert referenzierte immutable Dateien und nur gelesene Eingaben sind Basis; angenommene Copy-on-write-Ersparnisse zählen dagegen nie. Eine Migration budgetiert den vollständigen Zielzustand. Jeder beteiligte Writer und jeder registrierte Migrationsschritt muss einen aus kanonisch gezählten Eingabebytes, Dateizahlen, SQLite-Seiten oder fest begrenztem Scratch ableitbaren Output-Bound liefern. Fehlt ein Bound, ist die Schätzung `unknown` und der Speicherplatz-Preflight blockiert. Eine Änderung von Dateiformat, Writer-Einstellungen, Liveness oder Bound-Formel erzeugt eine neue Methoden-ID.

**Allokationsmessung**:
Der Nachweis einer Operationsschätzung mit versionierten synthetischen Fixtures auf einem echten Ziel-Dateisystem. Gemessen wird in jeder Operationsphase der Spitzenwert tatsächlich allozierter Blöcke als `st_blocks × 512`; ein Fixture besteht nur, wenn dieser Wert die Operationsschätzung nicht überschreitet. Das normale V0.2-Fixture und ein größerer Allokations-Stressfall sind für jede Methoden-ID verpflichtend. Die Messung kalibriert und regressionsprüft die Schätzung, ersetzt aber weder den unmittelbar vor dem Schreiben mit `statvfs` ermittelten verfügbaren Platz noch den atomaren `ENOSPC`-Fehlerpfad.

**Plan-Fingerprint**:
Der deterministische Inhaltsfingerabdruck einer vollständigen Schreibvorschau aus typisierten fachlichen Eingaben, materialisierten Treffermengen, aufgelöstem Ziel, Freigabestatus und entscheidungsrelevanten Preflight-Fakten. Er ist keine persistente Plan-ID und keine Autorisierung. Die interaktive CLI bestätigt ihn innerhalb desselben Aufrufs; die JSON-CLI wiederholt für den Ausführungsaufruf dieselben Argumente und übergibt den erwarteten Fingerprint, den die Anwendung unmittelbar neu berechnet. Jede geänderte Eingabe, Treffermenge oder verschlechterte Betriebsbedingung verlangt eine neue Vorschau.

**Schreibvorschau**:
Die nebenwirkungsfreie, lock-freie Vorschau jeder V0.2-Schreiboperation, auch bei `ready` und geschütztem Ziel. Sämtliche fachlichen Eingaben stehen vor ihr fest, werden danach gesperrt und gehen mit aufgelöstem Ziel, Freigabestatus, FileVault-Befund, gegebenenfalls Kapazitätsrechnung, materialisierten Treffermengen und nötigen Bestätigungen in den Plan-Fingerprint ein. Bearbeiten oder ein Streamlit-Seitenwechsel verwirft die Vorschau; eine bloß angezeigte Vorschau wird nicht gespeichert. CLI und Streamlit rendern denselben typisierten Plan und führen ihn über dieselbe Anwendungsoperation aus. Nach der Bestätigung erwirbt die Anwendung den exklusiven Schreib-Lock und wiederholt den Preflight unmittelbar vor dem Schreiben. Unzureichende oder unbekannte Kapazität blockiert dann; ein geändertes Zielvolume oder verschlechterter beziehungsweise anders unbekannter FileVault-Befund entwertet die Bestätigung und verlangt eine neue Vorschau. Eine Verbesserung zu `protected` darf fortfahren. Erst danach beginnt die atomare Schreiboperation. Deren bestehendes Receipt oder Auditereignis hält finalen Schutzstatus, Kapazitätswerte, Schätzmethodenversion, erteilte Bestätigungen und Ergebniscode fest; vor dem Start blockierte Versuche bleiben auf Receipt und datensparsames Log begrenzt. Reale Nachweise enthalten keine unredigierten Pfade oder Gesundheitswerte.

**Schreibfreigabestatus**:
Der erwartbare Ausgang von Schreibvorschau oder abschließender Neuprüfung: `ready` darf starten, `confirmation_required` verlangt eine fehlende oder wegen geänderten Risikos veraltete FileVault- beziehungsweise Einpersonenbestätigung, `blocked` bezeichnet unzureichende oder unbekannte Kapazität sowie ein gesperrtes, schreibgeschütztes oder unbeschreibbares Ziel, und `store_busy` bezeichnet den nicht erhaltenen exklusiven Schreib-Lock. Ein typisierter Diagnosecode nennt den konkreten Grund. CLI und Streamlit stellen denselben Zustand dar; die CLI verwendet Exitcode `3`. Nur unerwartete technische Defekte werden Ausnahmen.

**Schema-Version**:
Eine pro Artefaktart getrennte, monoton steigende positive Ganzzahl für Datenspeicher, Datensatz-Snapshot oder Metadatensicherung. Ausführbar sind ausschließlich mit der Software ausgelieferte und getestete Schritte zwischen unmittelbar benachbarten Versionen; Kompatibilitätsbereiche, Versionssprünge, Plugins und benutzerdefinierte Migrationsskripte gehören nicht zu V0.2.

**Datenspeicherschema-Vorwärtsmigration**:
Die lückenlose und getestete Umwandlung eines unterstützten älteren Datenspeicherschemas auf die aktuelle Version. Das Öffnen eines älteren Datenspeichers verändert ihn nicht: CLI und Streamlit zeigen zuerst Migrationsplan, Migrationssicherung und Speicherbedarf und verlangen eine ausdrückliche Bestätigung. Bis zum erfolgreichen Abschluss sind sämtliche fachlichen Lese- und Schreiboperationen gesperrt; zulässig bleiben nur Versionsdiagnose, Migrationsplan, Migration und Abbruch. Eine bestätigte Migration über mehrere Versionen ist genau eine Schreiboperation mit einer Migrationssicherung; ihre registrierten Einzelschritte laufen gemeinsam im Staging, und erst das vollständig validierte Zielschema wird einmal aktiviert. Neuere unbekannte Versionen, fehlende Migrationsschritte, Downgrades und Best-effort-Öffnungen werden abgelehnt.

**Gemeinsamer Migrationsvertrag**:
Der einzige Ablauf für Vorwärtsmigrationen von Datenspeichern, Datensatz-Snapshots und Metadatensicherungen: Schema-Version erkennen, lückenlose registrierte Schrittkette planen, Vorschau und gegebenenfalls Bestätigung einholen, erforderliche Sicherung und Speicherplatz prüfen, ausschließlich im Staging umwandeln, das Ziel vollständig validieren und genau einmal aktivieren oder als neue Arbeitskopie übergeben. Eine V0.3-Datenspeichermigration sichert SQLite einmal und migriert SQLite sowie den vollständigen Parquet-Zielsnapshot als einen gemeinsam validierten Zielzustand; Teilaktivierung ist unzulässig. Die drei Artefaktarten besitzen getrennte Schema-Versionen, aber keine konkurrierenden Migrationsmechanismen.

**Migrationszielvalidierung**:
Das harte Aktivierungstor nach einer Vorwärtsmigration. Es prüft mindestens Manifest und Inhalts-Hashes, SQLite-Integrität und Fremdschlüssel, erwartete Parquet-Schemas und Zeilenzahlen, geschlossene typisierte IDs und Referenzen sowie das vollständige Öffnen durch den aktuellen Leser. Jeder Verstoß verhindert die Aktivierung; es gibt keinen Warnungs- oder Best-effort-Modus, und Diagnosen enthalten keine Gesundheitswerte.

**Datensatzmanifest**:
Die unveränderliche, versionierte Beschreibung genau eines Datensatz-Snapshots mit Schema-Version, enthaltenen Parquet-Dateien, Inhalts-Hashes, den exakt gebundenen SQLite-Revisions-IDs und Validierungsangaben. SQLite bleibt alleinige Quelle für manuelle Kontext- und Medikamentenrevisionen samt Audit; Parquet besitzt kanonische Importdaten und materialisierte Ableitungen. Eine Copy-on-write-Schema-Migration erzeugt einen neuen Snapshot mit neuer Snapshot-ID; der alte Snapshot und seine Analyseverweise bleiben unverändert. Ein Datensatzmanifest aktiviert keinen Snapshot.

**V0.3-Parquet-Artefaktfamilie**:
Ein fest benanntes und hart typisiertes Snapshot-Artefakt für genau eine geschlossene kanonische Datensatz- oder Ableitungsfamilie. Numerische Samples teilen sich unabhängig vom HealthKit-Typ ein Artefakt; Schlafintervalle und Workouts besitzen wegen ihrer anderen Struktur eigene Artefakte, ebenso fachlich verschieden geformte Ableitungen. V0.3 erzeugt weder Dateien pro HealthKit-Typ noch eine universelle JSON- oder Wide-Table-Struktur.

**V0.3-Aufbewahrungsgrenze**:
V0.3 bereinigt keine historischen Datensatz-Snapshots, manuellen Revisionen, Auditereignisse, referenzierten Parquet-Artefakte, Analysebezüge oder Migrationssicherungen automatisch. Eine spätere Löschung ist ein eigener, ausdrücklich bestätigter und referenzbewusster Lebenszyklus; bloße Unerreichbarkeit vom aktiven Snapshot erlaubt keine Entfernung.

**Ableitungsneuveröffentlichung**:
Die normale V0.3-Schreiboperation nach einer Änderung der wirksamen Quellenklassifikation oder eines Ableitungsvertrags. Sie bindet die Änderung im Plan-Fingerprint, verwendet unveränderte kanonische Artefakte wieder, materialisiert ausschließlich betroffene Ableitungsfamilien neu, validiert den vollständigen Zielsnapshot und aktiviert ihn atomar. Eine Regeländerung wird nie erst bei einem zufälligen späteren Import wirksam.

**Ableitungslineage**:
Die vollständige flache Zuordnung einer stabilen `derived_record_id` und ihrer `derivation_contract_id` zu allen exakt beitragenden logischen Identitäten und Quellversions- oder manuellen Revisions-IDs samt geschlossener Beitragsrolle. Die Lineage ist ein eigenes hart typisiertes Parquet-Artefakt; Ableitungstabellen duplizieren keine verschachtelten ID-Listen.

**Snapshot-Stichtag**:
Der einzige zeitzonenbewusste Zeitpunkt, den jede Schreibvorschau für ihren möglichen neuen Datensatz-Snapshot bindet. Bei unveränderter Ausführung wird er nicht neu bestimmt: `medication_as_of` entspricht diesem Zeitpunkt, während `context_as_of_date` daraus in der ebenfalls gebundenen Kontextzeitzone abgeleitet wird. Dadurch kann das Fortschreiten der Uhr innerhalb einer Schreiboperation keinen fachlich gemischten Snapshot erzeugen.

**Snapshot-Aktivierung**:
Der einzige Commitpunkt einer Snapshot-Veröffentlichung: Nachdem sämtliche Parquet-Artefakte und das Datensatzmanifest vollständig geschrieben, hart validiert und per `fsync` dauerhaft gemacht wurden, schreibt genau eine SQLite-Transaktion Snapshot-Katalog, Revisionsbindungen, Auditereignis und Aktivierungshistorie und wechselt zuletzt die aktive Snapshot-Referenz. Nur diese Referenz bestimmt den aktiven Datensatz-Snapshot; Dateien und Datensatzmanifeste neben dem Katalog sind keine konkurrierenden Aktivierungsquellen. Ein Abbruch vor dem SQLite-Commit lässt den alten Snapshot aktiv und höchstens quarantänisierbare verwaiste Artefakte zurück.

**Migrationssicherung**:
Eine unmittelbar vor einer bestätigten Datenspeicherschema-Vorwärtsmigration automatisch erzeugte lokale Sicherheitskopie des führenden SQLite-Katalogs einschließlich der zuvor aktiven Snapshot-Referenz. Die unveränderten alten Parquet-Dateien werden nicht dupliziert, sondern bis zur ausdrücklichen Bereinigung erhalten. Die Migrationssicherung ist die einzige automatische Backup-Ausnahme im MVP und wird nicht automatisch gelöscht.

**Migrationsrollback**:
Die ausdrücklich bestätigte Wiederherstellung des durch eine Migrationssicherung festgehaltenen SQLite-Katalogs und der früheren aktiven Snapshot-Referenz. Sie ist nur zulässig, solange seit der zugehörigen Migration keine weitere Schreiboperation erfolgreich abgeschlossen wurde; andernfalls wird sie abgelehnt, statt spätere Zustände zu verwerfen. V0.3 führt weiterhin keine Rückwärtsmigration aus.

**Migrationsquarantäne**:
Der geschützte, analytisch unsichtbare Bereich für Staging-Artefakte und datensparsame Diagnosen einer fehlgeschlagenen oder unterbrochenen Migration. Der zuvor aktive Zustand bleibt unverändert; ein neuer Versuch beginnt frisch und setzt die quarantänisierte Migration nicht fort.
