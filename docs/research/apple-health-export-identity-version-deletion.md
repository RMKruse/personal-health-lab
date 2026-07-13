# Apple-Health-Export: Identität, Versionen und Löschungen

## Kurzantwort

Für den manuellen XML-Export dokumentiert Apple öffentlich nur, dass **alle Gesundheits- und Fitnessdaten als XML** exportiert werden. Apple veröffentlicht dafür weder ein stabiles Schema noch einen Vertrag für Objekt-UUIDs, Änderungsstände oder Löschmarkierungen. Deshalb darf V0.2 aus zwei kumulativen Exporten weder eine sichere Quellmessungsidentität noch eine sichere Quellmessungsversion oder Quellenlöschung behaupten.

HealthKit selbst besitzt stärkere Identitäts- und Änderungsmechanismen, aber diese sind API-Eigenschaften und kein belegter Vertrag des XML-Exports:

- HealthKit weist jedem `HKObject` eine UUID zu; Objekte sind grundsätzlich unveränderlich. [Apple: `HKObject`](https://developer.apple.com/documentation/healthkit/hkobject), [Apple: `HKObject.uuid`](https://developer.apple.com/documentation/healthkit/hkobject/uuid)
- `sourceRevision` beschreibt die Version der erzeugenden App beziehungsweise des Geräts, nicht die Version einer Messung. [Apple: `HKObject.sourceRevision`](https://developer.apple.com/documentation/healthkit/hkobject/sourcerevision), [Apple: `HKSourceRevision`](https://developer.apple.com/documentation/healthkit/hksourcerevision)
- Optional können schreibende Quellen einen Sync-Identifier und eine Sync-Version als Metadaten setzen. Das ist keine universelle HealthKit-Identität und für den XML-Export nicht zugesichert. [Apple: `HKMetadataKeySyncIdentifier`](https://developer.apple.com/documentation/healthkit/hkmetadatakeysyncidentifier)
- Nur Anchored Queries liefern explizit hinzugefügte und gelöschte Objekte samt Anker; ein `HKDeletedObject` trägt die UUID des gelöschten Objekts. [Apple: `HKAnchoredObjectQuery`](https://developer.apple.com/documentation/healthkit/hkanchoredobjectquery), [Apple: `HKDeletedObject.uuid`](https://developer.apple.com/documentation/healthkit/hkdeletedobject/uuid)
- Apples Benutzerhandbuch verspricht für „Export All Health Data“ lediglich XML, nicht die obigen API-Semantiken. [Apple: Share your data in Health on iPhone](https://support.apple.com/en-euro/guide/iphone/iph5ede58c3d/ios)

## Belastbare Einordnung

| Beobachtung in Exporten | Zulässige Aussage in V0.2 | Nicht zulässige Aussage |
|---|---|---|
| identischer, vollständig normalisierter Datensatz erscheint erneut | dasselbe **Exportvorkommen** wurde erneut beobachtet | dieselbe HealthKit-UUID wurde bestätigt |
| gleicher natürlicher Identitätskandidat, aber abweichender Inhalt | möglicher Nachfolger oder Konflikt derselben logischen Quellmessung | bewiesene Quellmessungsversion |
| früheres Vorkommen fehlt in einem späteren, vollständig und vergleichbar verarbeiteten Export | **vermutete Quellenlöschung** zur Prüfung | bewiesene Löschung in HealthKit |
| `sourceVersion` ändert sich | Version der erzeugenden App/des Geräts änderte sich | Messung wurde geändert |
| `creationDate` oder gleichnamiges Exportattribut ändert sich | Exportinhalt unterscheidet sich | belastbarer Änderungszeitpunkt oder Revisionszähler |
| Sync-Identifier/-Version ist als Metadatum vorhanden | quellenspezifischer starker Identitätskandidat | universell vorhandene Apple-Identität |

Eine vermutete Quellenlöschung darf nur entstehen, wenn beide Exporte als vollständig und vergleichbar gelten: gleiches Exportformat, erfolgreicher vollständiger Parse, gleiche unterstützte Typ- und Mappingversion und kein Quarantäne- oder Abbruchzustand. Sie bleibt reversibel, falls die Messung in einem späteren Export wieder erscheint.

## Auswirkung auf den Iststand

Der aktuelle Import bildet die logische ID aus Datentyp, Start, Ende, `sourceName` und `device`; die Versions-ID ergänzt `creationDate`, `sourceVersion`, Wert und Einheit ([`health_import`](../../src/personal_health_lab/health_import/__init__.py)). Der Akzeptanztest modelliert eine Änderung über einen anderen Wert und eine andere `sourceVersion` ([`test_health_import`](../../tests/acceptance/test_health_import.py)).

Diese Regeln sind brauchbare synthetische Heuristiken, aber keine durch Apple belegten Exportverträge:

- Gleichzeitige echte Messungen derselben Quelle können auf denselben natürlichen Schlüssel fallen.
- Eine App-/Geräteaktualisierung kann `sourceVersion` ändern, ohne dass sich die Messung ändert.
- `creationDate` ist nicht als Revisionszeitpunkt des XML-Exports dokumentiert.
- Fehlende Datensätze werden derzeit nur behalten; eine vergleichbare Exportabdeckung und eine vermutete Quellenlöschung werden nicht modelliert.

ADR [„Wiederkehrende Quellmessungen werden versioniert“](../adr/0009-version-source-measurements-across-exports.md) bleibt tragfähig, wenn „stabile Quell-ID“ als optionaler starker Schlüssel und „datentypspezifische natürliche Identität“ ausdrücklich als heuristischer Kandidat mit Konfliktzustand verstanden wird.

## Mindestvertrag für V0.2

V0.2 sollte vier getrennte Tatsachen speichern und nicht ineinander umdeuten:

1. **Exportvorkommen:** der unveränderte, normalisierte Datensatz in genau einem Import.
2. **Identitätskandidat:** optionaler starker Quellen-Identifier, sonst versionierte natürliche Identitätsheuristik samt Vertrauensklasse.
3. **Versionskandidat:** abweichender Inhalt unter demselben Identitätskandidaten; bevorzugte Version erst nach deterministischer Regel beziehungsweise Benutzerentscheidung.
4. **Vermutete Quellenlöschung:** Abwesenheit in einem späteren vergleichbaren Voll-Export, mit erstem/letztem Beobachtungsimport, Status und möglicher Wiederkehr.

V0.2 braucht keine allgemeine probabilistische Verknüpfung. Ein kleiner geschlossener Zustand (`exact`, `strong_source_id`, `natural_candidate`, `ambiguous`) reicht; `ambiguous` erzeugt einen prüfbaren Konflikt statt automatischer Zusammenführung.

## Versionierte synthetische Fixtures

Die kleinste belastbare Matrix umfasst:

1. identischer Folgeexport: nur erneutes Exportvorkommen, keine neue Version;
2. additiver kumulativer Export: bestehende Vorkommen plus neue Messung;
3. gleicher natürlicher Kandidat, geänderter Wert bei unveränderter `sourceVersion`;
4. unveränderter Datensatz bei geänderter `sourceVersion`: keine neue Messungsversion;
5. zwei legitime, natürlich kollidierende Messungen: `ambiguous`, keine stille Zusammenführung;
6. optionaler Sync-Identifier vorhanden, fehlend und widersprüchlich;
7. Messung fehlt in einem validen vergleichbaren Folgeexport: vermutete Quellenlöschung;
8. dieselbe Messung erscheint später wieder: Vermutung wird aufgehoben, Historie bleibt;
9. unvollständiger, abgebrochener oder mit anderer Mappingversion verarbeiteter Folgeexport: keine Löschvermutungen;
10. mehrere frühere Versionen fehlen gemeinsam: jede Vermutung bleibt an Exportvergleich und logische Messung gebunden.

Der reale V0.5-Pilot muss diese konservativen Annahmen gegen mindestens zwei nacheinander erzeugte, sanitärisierte Apple-Health-Exporte prüfen. Erst dieser Befund darf stärkere XML-spezifische Garantien begründen.
