# Modul-Interfaces verwenden typisierte unveränderliche Werte

Zwischen Modulen werden ausschließlich unveränderliche typisierte Wertobjekte, Receipts und Snapshot-Referenzen ausgetauscht. Import-, Snapshot-, Analyse- und Messidentitäten besitzen getrennte opake ID-Typen. Frei strukturierte Dictionaries, DataFrames, Arrow-Tabellen und SQL-Zeilen bleiben Implementierungsdetails des besitzenden Moduls, damit Schemas explizit bleiben und Aufrufer weder Speicherform noch Mutabilität übernehmen.
