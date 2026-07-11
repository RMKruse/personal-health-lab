# Health-Exportpakete sind nicht vertrauenswürdige Eingaben

Das Importmodul behandelt synthetische und reale ZIP-/XML-Pakete unabhängig von ihrer lokalen Herkunft als untrusted input. Es verhindert Pfadtraversal, begrenzt Dekompressionsgröße und Eintragszahl, erlaubt nur erwartete Eintragstypen, deaktiviert XML-External-Entities und Netzwerkzugriffe und schreibt niemals außerhalb des isolierten Staging-Bereichs. Ein Sicherheitsabbruch liefert `rejected`, entfernt extrahierte Staging-Artefakte und behält nur datensparsame Diagnosemetadaten; die ausgewählte Originaldatei bleibt unverändert.
