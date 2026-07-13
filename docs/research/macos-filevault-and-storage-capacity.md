# macOS: FileVault- und Speicherplatz-Prüfbarkeit

## Kurzantwort

V0.2 kann einen **existierenden, beschreibbaren lokalen Zielpfad** ohne neue Abhängigkeit
ausreichend konservativ prüfen:

1. Den tatsächlich aufgelösten Pfad mit `statfs(2)` dem gemounteten Dateisystem und dessen
   Quellgerät zuordnen. Für die Python-Implementierung kann `/bin/df -P <pfad>` nur diese
   Zuordnung liefern; die Speicherzahl selbst kommt nicht aus dessen formatiertem Text.
2. Gehört das Ziel zum Start-/Data-Volume-Group, `fdesetup isactive` als stärksten
   FileVault-Befund verwenden. Andere lokale Quellgeräte mit
   `/usr/sbin/diskutil info -plist <gerät>` prüfen und die typisierte plist-Ausgabe mit `plistlib`
   lesen. Nur `FileVault == true` ist dort ein positiver FileVault-Befund.
   `Encryption == true` allein genügt ausdrücklich nicht.
3. Bei APFS zusätzlich `diskutil apfs list -plist <container>` auswerten. Während
   `CryptoMigrationOn == true` ist der Schutz im Übergang und nicht als vollständig bestätigt
   zu behandeln.
4. Den für den laufenden, nicht privilegierten Prozess nutzbaren Platz unmittelbar vor der
   Operation als `os.statvfs(pfad).f_bavail * os.statvfs(pfad).f_frsize` bestimmen. Die
   Operation muss trotzdem `ENOSPC` sicher behandeln.

Das Ergebnis darf kein Boolean sein, sondern mindestens `protected`, `unprotected`,
`transitioning` oder `unknown`. Netzwerkziele, nicht gemountete beziehungsweise gesperrte
Volumes, fehlende plist-Schlüssel und nicht auflösbare Storage-Schichten sind `unknown` und
werden für reale Daten nicht stillschweigend als geschützt behandelt. Ob der jeweilige Zustand
warnt oder blockiert, entscheidet der nachgelagerte betriebliche Schutzvertrag.

## Warum „verschlüsselt“ nicht „FileVault-geschützt“ bedeutet

Auf Macs mit Apple-Chip oder T2-Chip ist der interne Speicher auch bei ausgeschaltetem
FileVault hardwaregebunden verschlüsselt. Erst FileVault bindet den Schutz zusätzlich an
Anmeldedaten beziehungsweise einen Wiederherstellungsschlüssel. Apple beschreibt für den
ausgeschalteten Zustand ausdrücklich, dass der Volume-Schlüssel nur durch die Hardware-UID
geschützt ist; bei eingeschaltetem FileVault kommt das Benutzergeheimnis hinzu.
[Apple Platform Deployment: Intro to FileVault](https://support.apple.com/guide/deployment/intro-to-filevault-dep82064ec40/1/web/1.0),
[Apple: Protect data on your Mac with FileVault](https://support.apple.com/en-euro/guide/mac-help/-mh11785/mac)

Deshalb sind die öffentlichen Foundation- und Disk-Arbitration-Eigenschaften
[`volumeIsEncryptedKey`](https://developer.apple.com/documentation/foundation/urlresourcekey/volumeisencryptedkey)
und
[`kDADiskDescriptionMediaEncryptedKey`](https://developer.apple.com/documentation/diskarbitration/kdadiskdescriptionmediaencryptedkey)
stabile Indikatoren für **irgendeine** Volume-Verschlüsselung, aber kein Beweis für
FileVault-Credential-Schutz. Sie eignen sich als Diagnose, nicht als alleiniger Gatekeeper.

Auch ein externes Ziel ist nicht automatisch geschützt. Apple verlangt bei einem
passwortgeschützten internen oder externen Datenträger ein verschlüsseltes Dateisystemformat;
bei einem externen Datenträger muss das Passwort nach dem Anschließen eingegeben werden.
[Apple Disk Utility: Encrypt and protect a storage device](https://support.apple.com/guide/disk-utility/encrypt-protect-a-storage-device-password-dskutl35612/22.7/mac/26)

## Belastbare lokale Schnittstellen

| Schnittstelle | Belastbare Aussage | Grenze für V0.2 |
|---|---|---|
| `statfs(2)` / `fstatfs(2)` | `f_mntonname` ist der Mountpoint, `f_mntfromname` die Quelle; `f_bavail` sind für nicht privilegierte Prozesse verfügbare Blöcke. | Ein APFS-Snapshot kann statt eines einfachen Gerätenamens `snapshot@/dev/disk…` liefern. Ein schreibbares Ziel auf einem Snapshot ist ohnehin abzulehnen. [Apple-Manpage `statfs(2)`](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/statfs.2.html) |
| Python `os.statvfs()` | Stabiler Standardbibliothekszugriff auf `statvfs(3)`; `f_bavail` zählt freie Blöcke für unprivilegierte Nutzer und `f_frsize` deren Größe. | Die Zahl ist nur eine Momentaufnahme und kann bis zum Schreiben sinken. [Python-Dokumentation](https://docs.python.org/3/library/os.html#os.statvfs) |
| Foundation URL resource values | `volumeURLKey` ordnet einen Pfad seinem Volume zu; `volumeIsLocalKey`, `volumeIsEncryptedKey`, `volumeAvailableCapacityKey` und `volumeSupportsFileCloningKey` sind öffentliche APIs. | Das Projekt besitzt keine Objective-C-/Swift-Brücke. Eine neue PyObjC-Abhängigkeit nur für diese Prüfung ist für V0.2 unnötig. [`volumeURLKey`](https://developer.apple.com/documentation/foundation/urlresourcekey/volumeurlkey), [`volumeIsLocalKey`](https://developer.apple.com/documentation/foundation/urlresourcekey/volumeislocalkey), [`volumeAvailableCapacityKey`](https://developer.apple.com/documentation/foundation/urlresourcekey/volumeavailablecapacitykey), [`volumeSupportsFileCloningKey`](https://developer.apple.com/documentation/foundation/urlresourcekey/volumesupportsfilecloningkey) |
| `fdesetup isactive` | Apple dokumentiert per lokaler Manpage einen skriptbaren Exitstatus und `true`/`false` für den aktuellen FileVault-Zustand. | Stärkster Befund für das Start-/Data-Volume-Group, aber kein allgemeiner Zielgeräte-API: Der Befehl nimmt kein beliebiges Backup-Gerät an. `fdesetup status` ist menschenlesbar und darf nicht für fremde Ziele umgedeutet werden. Apple verweist für den Vertrag selbst auf `man fdesetup`. [Apple Platform Deployment: fdesetup](https://support.apple.com/de-de/guide/deployment/dep0a2cb7686/web) |
| `diskutil info -plist` | Apples lokale `diskutil(8)`-Manpage fordert Skripte ausdrücklich auf, plist-Ausgaben zu verwenden. Der Befehl akzeptiert BSD-Gerät, Geräteknoten oder Mountpoint und liefert getrennte `FileVault`- und `Encryption`-Werte. | Die Manpage dokumentiert nicht jeden plist-Schlüsselnamen als eigenes API-Symbol. Fehlender Schlüssel, unbekannter Typ, Fehler oder zukünftige Schemaänderung müssen deshalb `unknown` ergeben; niemals auf menschenlesbare Ausgabe zurückfallen. |
| `diskutil apfs list -plist` | Liefert Container, Physical Stores und Volumes einschließlich beobachtbarer Felder für FileVault, laufende Crypto-Migration, Reserve, Quota und Kapazität. | Nur für APFS. Gerätekennungen sind opaque und können sich über Neustarts oder Attach-Zyklen ändern; innerhalb einer Prüfung per UUID beziehungsweise plist-Beziehung zuordnen, nicht aus `disk3s5` selbst rechnen. |

Die Aussagen zu `diskutil` und `fdesetup` wurden zusätzlich gegen die von Apple mit macOS
26.3.1 ausgelieferten lokalen Manpages geprüft (`man 8 diskutil`, `man 8 fdesetup`). Dabei wurde
`diskutil info -plist /dev/disk3s5` mit getrennten booleschen Feldern `Encryption` und
`FileVault` sowie `diskutil apfs list -plist` mit `CryptoMigrationOn`, `CapacityReserve` und
`CapacityQuota` beobachtet. Das ist ein Kompatibilitätsbefund, kein stärkerer Vertrag als die
Manpage; deshalb bleibt der Fail-closed-Fallback notwendig.

## Pfad-, Mount- und Gerätezuordnung

Die Prüfung gilt für den Ort, an den tatsächlich geschrieben wird, nicht für einen konfigurierten
Textpfad:

1. Symlinks auflösen und den tiefsten bereits existierenden Zielordner prüfen; nach dem Erzeugen
   eines neuen Ordners erneut prüfen.
2. Schreibbarkeit und Read-only-Mount ablehnen, bevor Verschlüsselung oder Kapazität bewertet
   werden.
3. Mit `statfs` den konkreten Mount bestimmen. Das ist auf aktuellen macOS-Installationen wichtig,
   weil `/` ein versiegelter, schreibgeschützter APFS-Snapshot sein kann, während Benutzerdaten auf
   dem Data-Volume liegen. `diskutil info -plist /` kann deshalb etwa `FreeSpace = 0` melden und ist
   keine Speicherplatzquelle für einen Pfad im Benutzerverzeichnis.
4. Nur ein lokales `/dev/disk…` an `diskutil info -plist` übergeben. Die `diskutil(8)`-Manpage
   warnt davor, APFS-Gerätekennungen aus Nummern abzuleiten, und verlangt die Zuordnung über
   Listing/plist. Persistente Identität kommt aus Volume-/Container-UUIDs, nicht aus der laufenden
   `diskN`-Nummer.

`/bin/df -P <pfad>` ist für die Python-V0.2 ein kleiner, POSIX-formatierter Adapter um diese
Mount-Zuordnung: aus der Datenzeile wird nur ein lokales Quellgerät der Form `/dev/disk…`
akzeptiert. Ein anderer oder nicht eindeutig parsebarer Wert ist `unknown`. Alternativ wäre ein
kleiner nativer `statfs`-Wrapper möglich, aber für V0.2 nicht nötig.

Netzwerk-Mounts sind prinzipiell nicht lokal entscheidbar: macOS kann Mount und freien Platz des
Clients melden, aber nicht beweisen, ob der entfernte Server Daten im Ruhezustand mit FileVault
oder gleichwertig schützt. Dasselbe gilt für mehrschichtige Sonderfälle, in denen ein unverschlüsselt
gemountetes Dateisystem nur als Datei auf einem verschlüsselten Host-Volume liegt. V0.2 sollte
solche Ziele nicht rekursiv erraten, sondern `unknown` melden.

## APFS-Kapazität, Reserve und Quota

APFS-Volumes besitzen keine unabhängige feste Kapazität. Alle Volumes eines Containers
konkurrieren um dessen freien Platz; Reserve und Quota setzen lediglich eine garantierte
Untergrenze beziehungsweise eine Obergrenze pro Volume. Apples aktuelle Disk-Utility-Dokumentation
beschreibt Space Sharing sowie beide Größen explizit.
[Apple: Add, delete, or erase APFS volumes](https://support.apple.com/guide/disk-utility/add-delete-or-erase-apfs-volumes-dskua9e6a110/mac)

V0.2 sollte deshalb keine eigene Formel aus `CapacityFree`, Summe der Volume-Größen,
`CapacityReserve` und `CapacityQuota` rekonstruieren. Der zielbezogene `f_bavail`-Wert ist für den
aufrufenden Prozess die richtige konservative Basis; `ATTR_VOL_SPACEAVAIL` beschreibt genau diese
Semantik als freien Platz abzüglich systemseitig reservierten Platzes.
[Apple-Manpage `getattrlist(2)`](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/getattrlist.2.html)

`volumeAvailableCapacityForImportantUsageKey` darf nicht als garantiert verfügbarer Platz in die
harte Freigabe eingehen: Apple zählt dort erwartbar freigebbaren Cache beziehungsweise purgeable
space mit und nennt die Ressourcen letztlich ersetzbar. Für einen irreversiblen Gesundheitsdaten-
Write ist das eine weiche Zusatzanzeige, keine Reserve.
[Apple Developer Documentation](https://developer.apple.com/documentation/foundation/urlresourcekey/volumeavailablecapacityforimportantusagekey)

## Import- und Copy-on-write-Budget

Die harte Vorprüfung lautet:

```text
required_peak = estimated_new_allocated_bytes + operation_overhead + safety_margin
admit only if required_peak <= max(0, available_to_process - app_minimum_free_reserve)
```

`available_to_process` ist der unmittelbar vor dem exklusiven Schreibvorgang neu gelesene
`f_bavail * f_frsize`-Wert. `app_minimum_free_reserve` ist eine Produktregel für nach der Operation
verbleibenden Betriebsraum und nicht mit einer APFS-Volume-Reserve zu verwechseln. Der Schätzwert
muss auf Allokationsgrößen aufrunden und temporäre Manifest-, SQLite-, DuckDB- und Parquet-Arbeit
einschließen, soweit sie auf demselben Volume anfällt.

Für den aktuellen Import ist mindestens der **vollständige neue Parquet-Snapshot** zu budgetieren:
[`LocalStore._publish_import`](../../src/personal_health_lab/storage/_store.py) vereinigt aktiven
Snapshot und neue Records in einer temporären Tabelle und schreibt anschließend
`combined_samples` vollständig als neue `samples.parquet` in Staging. Ein bloßes Delta-Budget
wäre falsch.

Copy-on-write reduziert dieses Budget nicht verlässlich. APFS unterstützt Clones, aber
`clonefile(2)` funktioniert nur auf demselben Dateisystem (`EXDEV` sonst), nicht jedes Volume
unterstützt es, und spätere Änderungen am Clone können erst dann zusätzliche Blöcke allozieren und
mit `ENOSPC` scheitern. Apple beschreibt Clones und Copy-on-write als APFS-Funktionen, nicht als
Kapazitätsgarantie. [Apple File System Guide: Features](https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/APFS_Guide/Features/Features.html),
[Apple File System Guide: Tools and APIs](https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/APFS_Guide/ToolsandAPIs/ToolsandAPIs.html)

Für eine spätere echte Clone-Migration darf V0.2 `volumeSupportsFileCloningKey` beziehungsweise
`VOL_CAP_INT_CLONE` nur als Capability-Prüfung verwenden. Die sichere Obergrenze bleibt die
vollständig neu zu allozierende Ausgabe; ein kleineres empirisches Budget ist erst mit gemessener
Worst-case-Allokation und robustem Abbruch/Rollback vertretbar.

## Entscheidung für V0.2

V0.2 sollte die Prüfung als einen konservativen Preflight direkt vor Import, Migration oder
Backup ausführen:

- **FileVault:** Für ein Ziel im Start-/Data-Volume-Group `fdesetup isactive`; für andere
  tatsächlich gemountete lokale Zielgeräte `diskutil`-plist. Dort positiv nur bei explizitem
  `FileVault == true`, `Encryption == true`, nicht gesperrtem Volume und keiner laufenden
  APFS-Crypto-Migration.
- **Kapazität:** `os.statvfs`/`f_bavail * f_frsize`; niemals `diskutil FreeSpace`, Container-Free
  oder Finder-/`df`-Anzeigewerte für die Freigabe verwenden.
- **Budget:** vollständige Staging-Ausgabe plus Overhead plus konfigurierter Mindestrest; Clone-
  Einsparung zunächst null ansetzen.
- **Fehlerzustand:** Remote, Read-only, locked, transitioning, fehlende Felder, nicht auflösbare
  Mountschicht oder Befehlsfehler werden sichtbar als `unknown` behandelt; Warnung oder Blockade
  legt der nachgelagerte betriebliche Schutzvertrag fest.
- **Laufzeit:** Nach positivem Preflight weiter alle Schreibfehler, insbesondere `ENOSPC`, atomar
  abbrechen und den aktiven Snapshot unverändert lassen; freie Kapazität ist nie reserviert und
  kann zwischen Prüfung und Schreiben von anderen Prozessen oder APFS-Geschwister-Volumes
  verbraucht werden.

Nicht nötig sind eine neue Verschlüsselungsschicht, PyObjC, IOKit-Graphrekonstruktion oder eine
allgemeine Storage-Topologie-Engine. Diese werden erst relevant, wenn Netzwerkziele oder komplexe
verschachtelte Disk-Images ausdrücklich unterstützt werden sollen.

Diese Recherche legt nur den technischen Evidenzzustand fest. Ob `unprotected`, `transitioning`
oder `unknown` im jeweiligen Ablauf warnt oder blockiert, bleibt der separaten betrieblichen
Schutzregel vorbehalten.
