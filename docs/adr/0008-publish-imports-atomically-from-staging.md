# Importe werden atomar aus einem Staging-Bereich veröffentlicht

Jedes Health-Exportpaket wird vollständig in einem isolierten Staging-Bereich geparst, normalisiert, dedupliziert und validiert. Parquet-Daten und zugehörige Metadaten werden erst nach Erfolg gemeinsam analytisch sichtbar; ein Absturz oder Validierungsfehler darf keinen teilweise veröffentlichten Import hinterlassen. Beim nächsten Öffnen bleibt der letzte veröffentlichte Snapshot aktiv, und unvollständige Journal- oder Staging-Einträge werden mit Diagnose quarantänisiert statt automatisch fortgesetzt.
