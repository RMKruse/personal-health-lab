# Overview ist die einzige Leseprojektion für Adapter

CLI und Streamlit laden Zeitreihen, Trends, Unsicherheit, Qualitätsstatus, Provenienz und Methodik ausschließlich über das tiefe `overview`-Modul. Das Modul erzeugt ein präsentationsneutrales `Overview`; direkte Storage- oder Analyseabfragen aus Adaptern sind verboten, damit Auswahl- und Interpretationslogik nicht zwischen Darstellungen auseinanderläuft.
