# Der Import ist ein tiefes Modul

Das interne Importmodul akzeptiert nur einen lokalen Paketpfad und verbirgt Lesbarkeits- und Formatprüfung, XML-Streaming, Positivliste, Normalisierung, Deduplizierung, Quellversionierung, Staging, Validierung, atomare Veröffentlichung und Quarantäne hinter einer einzelnen Importoperation. Einzelne Pipeline-Schritte dürfen interne Seams besitzen, werden aber nicht als flache öffentliche Module exponiert, deren Reihenfolge CLI, Streamlit oder das Anwendungsmodul kennen müssten.
