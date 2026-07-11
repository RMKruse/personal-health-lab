# Synthetische und reale Datenmodi isolieren

Die Anwendung startet entweder im Modus `synthetic` oder `real`; der Modus ist für die gesamte Sitzung unveränderlich und verwendet jeweils eigene Datenverzeichnisse und Metadatenspeicher. Die Auswahl erfolgt über Startkonfiguration statt über einen hardcodierten Quellcode-Schalter und wird beim Öffnen des Anwendungsmoduls gebunden, damit Entwicklungsdaten und persönliche Gesundheitsdaten weder versehentlich vermischt noch gemeinsam analysiert werden.
