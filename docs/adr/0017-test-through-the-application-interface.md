# V0.1 wird primär über das Anwendungs-Interface getestet

End-to-End-Tests öffnen `HealthLab` mit temporärem SQLite-, Parquet- und DuckDB-Speicher und verwenden dieselben drei Operationen wie CLI und Streamlit. Interne Tests bleiben auf anspruchsvolle reine Algorithmen begrenzt; Repository-Mocks und flache Tests jedes Pipeline-Schritts werden vermieden, damit die Tests beobachtbares Verhalten beschreiben und interne Umstrukturierungen überleben.
