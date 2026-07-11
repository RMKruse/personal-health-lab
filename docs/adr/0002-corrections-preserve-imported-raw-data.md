# Korrekturen bewahren importierte Rohdaten

Benutzerkorrekturen überschreiben oder löschen importierte Gesundheitsdaten nicht, sondern erzeugen begründete, zeitgestempelte Ersatzwerte mit Verweis auf das Original. Dadurch bleiben Provenienz und frühere Analysen reproduzierbar; von einer Korrektur abhängige Ergebnisse werden als veraltet markiert und mit einem neuen Datensatz-Snapshot neu berechnet. Korrekturen gelten im MVP ausschließlich lokal; ein Rückschreiben nach HealthKit bleibt ein mögliches langfristiges Ziel.
