# HealthKit bleibt hinter der Import-Mapping-Seam

Nur `health_import` kennt HealthKit-Typbezeichner, XML-Attribute, Quelleneinheiten und Apple-spezifische Exportdetails. Eine Anti-Corruption-Seam ordnet unterstützte Werte kanonischen `health_data`-Typen und Provenienz zu; Speicherung, Analyse und Overview dürfen keine HealthKit-XML-Struktur oder Quellennamen als fachliche Typen übernehmen.
