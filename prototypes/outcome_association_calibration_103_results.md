# V0.4-Prototyp: Trend- und Abweichungszusammenhänge

Version: `v0.4-outcome-association-calibration-1`; Modus: `full`.

## Kalibrierungsergebnis

Der beste Kandidat ist `triangular` mit Bandbreite `Fenster × 1.0` und Randregel `truncate`.
Über die Bootstrap-Kalibrierungsfälle betrug die gemeinsame Abdeckung 90.0%, der familienweise Null-Fehlalarm 0.0% und die Refit-Ausfallquote 0.00%.
Die globale Kalibrierung ist damit nicht bestanden: Ziel sind mindestens 95% gemeinsame Abdeckung und eine praktisch informative Halbbreite von höchstens 0,5. Die Kombination darf noch nicht als `robust` eingefroren werden.
Die Ergebnisse sind eine HITL-Entscheidungsgrundlage, keine produktive Implementierung.

## Kandidatenvergleich (beste sechs)

| Kandidat | RMSE | Bias | Ausfall | Glättungsänderung | Einfluss | Laufzeit s |
|---|---:|---:|---:|---:|---:|---:|
| `triangular-1-truncate` | 0.224 | 0.004 | 0.0% | 0.138 | 0.172 | 12.8 |
| `epanechnikov-1-truncate` | 0.223 | 0.005 | 0.0% | 0.147 | 0.174 | 13.3 |
| `tricube-1-truncate` | 0.226 | 0.000 | 0.0% | 0.135 | 0.166 | 15.4 |
| `tricube-1-two_sided` | 0.227 | 0.009 | 0.0% | 0.186 | 0.208 | 14.5 |
| `tricube-1.25-truncate` | 0.223 | 0.008 | 0.0% | 0.206 | 0.178 | 18.4 |
| `triangular-1.25-truncate` | 0.222 | 0.011 | 0.0% | 0.212 | 0.182 | 15.3 |

## Bootstrap-Regel

Geprüft wurden zirkuläre gepaarte Residual-Moving-Blocks mit `ceil(n^(1/3))` und den Faktoren 0,5/1/2. Jede Replik refittet beide Zerlegungen je Fenster; ein gemeinsamer nichtstudentisierter Maximalabweichungs-Kritischwert schützt alle acht Primärergebnisse. Wie beim Lag-Prototyp schützt die versionierte synthetische Untergrenze `0.753` zusätzlich den vom reinen Residual-Bootstrap nicht erfassten Glättungsbias.
Mit nur 120 Repliken lag die mediane Änderung des 95%-Quantils zwischen Halb- und Gesamtlauf noch bei 0.154; diese Stufe validiert daher keine produktive Mindestzahl. Die drei Primärregeln benötigten zusammen 129.7 Sekunden.
Die Änderung des rohen Kritischwerts über die drei Blockregeln betrug median 0.107 und maximal 0.304; damit ist auch der Kandidatengrenzwert 0,1 noch nicht stabil bestanden.
Für den nächsten Kalibrierungsschritt ist 2.000 erfolgreiche Refits innerhalb höchstens 2.020 Versuchen je Primär- und Faktor-2-Sensitivitätslauf der konservative Prüfkandidat; ein Lauf ohne 2.000 Erfolge würde kein statistisches Ergebnis liefern.

## Kandidaten für Modellreifeschwellen

```json
{
  "7": {
    "minimum_paired_days": 169.0,
    "minimum_density": 0.46,
    "minimum_effective_blocks": 12.0,
    "maximum_gap_days": 33.0,
    "maximum_residual_acf": 0.31,
    "maximum_influence": 0.09,
    "maximum_smoothing_change": 0.2,
    "maximum_block_critical_change": 0.1,
    "minimum_bootstrap_success_rate": 0.99
  },
  "14": {
    "minimum_paired_days": 175.0,
    "minimum_density": 0.48,
    "minimum_effective_blocks": 12.0,
    "maximum_gap_days": 20.0,
    "maximum_residual_acf": 0.35,
    "maximum_influence": 0.11,
    "maximum_smoothing_change": 0.2,
    "maximum_block_critical_change": 0.1,
    "minimum_bootstrap_success_rate": 0.99
  },
  "30": {
    "minimum_paired_days": 175.0,
    "minimum_density": 0.48,
    "minimum_effective_blocks": 12.0,
    "maximum_gap_days": 20.0,
    "maximum_residual_acf": 0.43,
    "maximum_influence": 0.2,
    "maximum_smoothing_change": 0.2,
    "maximum_block_critical_change": 0.1,
    "minimum_bootstrap_success_rate": 0.99
  },
  "90": {
    "minimum_paired_days": 175.0,
    "minimum_density": 0.48,
    "minimum_effective_blocks": 12.0,
    "maximum_gap_days": 20.0,
    "maximum_residual_acf": 0.52,
    "maximum_influence": 0.88,
    "maximum_smoothing_change": 0.2,
    "maximum_block_critical_change": 0.1,
    "minimum_bootstrap_success_rate": 0.99
  }
}
```

Zusätzlich zwingend: definierte Variation beider Steigungs- und Abweichungsreihen, lokale 2×2-Pivots ≥ `1e-6`, Bandbreiten-Nachbarschaft ±25 %, Blocklängen-Faktor 2, sichtbare Gerätewechsel und offene Datenprüffälle. `robust` verlangt alle Kriterien; ein stabiles Nullergebnis darf robust sein.
Die Schwellen sind noch nicht freigegeben, weil die globale Kalibrierung nicht bestanden ist.

## Offene HITL-Entscheidung

1. **Sparsame Definition beibehalten:** `robust` nur innerhalb einer engeren synthetischen Hülle zulassen und Strukturbrüche, Gerätewechsel, starke Autokorrelation, große Lücken oder hohe Messfehler-Sensitivität zwingend als explorativ behandeln; anschließend die gemeinsame Untergrenze nur auf dieser Hülle neu kalibrieren.
2. **Methodenprototyp erweitern:** vor dem Einfrieren studentisierte oder bias-korrigierte gemeinsame Bänder und eine explizite Strukturbruch-Sensitivität testen. Eine neue Statistikabhängigkeit ist dafür noch nicht nötig, aber die Modellfamilie wird komplexer.

## Methodische Grenze

Der Prototyp verwendet ausschließlich die Python-Standardbibliothek. Das genügt für den lokal-linearen 2×2-Fit, Pearson-Korrelation, Diagnostik und den gepaarten Block-Bootstrap. Eine Statistikabhängigkeit ist nur neu zu prüfen, falls eine größere Kalibrierung die Abdeckung oder numerische Stabilität dieses Kandidaten widerlegt.

## Primärquellen und Vorentscheidung

- Fan (1992), lokal-lineare Regression und Randverhalten: https://doi.org/10.1080/01621459.1992.10476255
- Künsch (1989), Moving-Block-Bootstrap: https://doi.org/10.1214/aos/1176347265
- Politis & White (2004), Blocklängenwahl: https://doi.org/10.1081/ETC-120028836
- Morris, White & Crowther (2019), Simulationsstudien: https://doi.org/10.1002/sim.8086
- Vorentscheidung: `docs/research/outcome-trend-deviation-association-methods.md`

## Reproduktion

```bash
uv run python prototypes/outcome_association_calibration_103.py --mode full
```
