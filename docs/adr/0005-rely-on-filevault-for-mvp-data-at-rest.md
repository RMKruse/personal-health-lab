# Im MVP schützt FileVault reale Daten im Ruhezustand

Der MVP implementiert keine eigene Verschlüsselungsschicht für den realen Datenspeicher oder manuelle Metadatensicherungen, sondern verlässt sich auf das macOS-Benutzerkonto und FileVault. Die Anwendung prüft den gewählten Ziel-Datenträger und warnt deutlich, wenn er nicht verschlüsselt ist; dadurch bleibt der Prototyp technisch überschaubar, ohne die Schutzannahme zu verbergen.
