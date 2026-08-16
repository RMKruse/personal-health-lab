# V0.4-Prototyp: Trend- und Abweichungszusammenhänge

Version: `v0.4-outcome-association-calibration-2`; Modus: `full`.

## Antwort

Der beste Zerlegungskandidat bleibt `triangular`, Bandbreite `Fenster × 1.0`, Randregel `truncate`.
Keine der fünf gemeinsamen Bandregeln besteht die vollständige synthetische Hülle. Ziel waren mindestens 95 % gemeinsame Abdeckung, höchstens 5 % familienweiser Null-Fehlalarm und mediane Halbbreite höchstens 0,5.
Die Refit-Ausfallquote betrug 0.00%; das Defizit ist methodisch.

## Unsicherheitsvergleich

| Regel | Abdeckung | Null-Fehlalarm | Median-Halbbreite | Median-Blockänderung |
|---|---:|---:|---:|---:|
| nichtstudentisiert + globale Untergrenze | 90.0% | 0.0% | 0.753 | 0.107 |
| studentisiert + maßspezifische Untergrenzen | 76.7% | 0.0% | 0.494 | 0.132 |
| bias-korrigiert studentisiert | 73.3% | 0.0% | 0.489 | 0.135 |
| Fisher-z studentisiert | 70.0% | 0.0% | 0.377 | 0.088 |
| Fisher-z bias-korrigiert studentisiert | 73.3% | 0.0% | 0.377 | 0.080 |

Die präziseste brauchbare Richtung ist Fisher-z bias-korrigiert studentisiert: 73,3 % Abdeckung, 0 % Null-Fehlalarm, Halbbreite 0,377 und Blockänderung 0,080. Die breite alte Regel erreicht 90 %, benötigt aber Halbbreite 0,753. Beides verfehlt das Gate.

## Strukturbruch-Sensitivität

Die Änderung nach Ausschluss der stärksten Bruchumgebung trennt die Baseline (Median 0.033) von gemeinsamen Strukturbrüchen (0.166) und gemeinsamen Gerätewechseln (0.114).
Sie ist als Reifediagnose nützlich, macht die Bandregel aber nicht global belastbar; besonders gemeinsame Brüche, hohe Messfehler, dünne Beobachtung und große Lücken bleiben problematisch.

## Zerlegungskandidaten (beste sechs)

| Kandidat | RMSE | Bias | Glättungsänderung | Einfluss | Laufzeit s |
|---|---:|---:|---:|---:|---:|
| `triangular-1-truncate` | 0.224 | 0.004 | 0.138 | 0.172 | 12.8 |
| `epanechnikov-1-truncate` | 0.223 | 0.005 | 0.147 | 0.174 | 13.3 |
| `tricube-1-truncate` | 0.226 | 0.000 | 0.135 | 0.166 | 15.5 |
| `tricube-1-two_sided` | 0.227 | 0.009 | 0.186 | 0.208 | 14.5 |
| `tricube-1.25-truncate` | 0.223 | 0.008 | 0.206 | 0.178 | 18.5 |
| `triangular-1.25-truncate` | 0.222 | 0.011 | 0.212 | 0.182 | 15.3 |

## Unfreigegebene Reifeschwellen-Kandidaten

```json
{
  "7": {
    "minimum_paired_days": 169.0,
    "minimum_density": 0.46,
    "minimum_effective_blocks": 12.0,
    "maximum_gap_days": 33.0,
    "maximum_residual_acf": 0.31,
    "maximum_influence": 0.09,
    "maximum_structure_break_score": 10.47,
    "maximum_structure_break_sensitivity": 0.04,
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
    "maximum_structure_break_score": 8.6,
    "maximum_structure_break_sensitivity": 0.11,
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
    "maximum_structure_break_score": 10.21,
    "maximum_structure_break_sensitivity": 0.28,
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
    "maximum_structure_break_score": 12.92,
    "maximum_structure_break_sensitivity": 0.46,
    "maximum_smoothing_change": 0.2,
    "maximum_block_critical_change": 0.1,
    "minimum_bootstrap_success_rate": 0.99
  }
}
```

Es wird noch keine Replikzahl oder Reifeschwelle eingefroren. 120 Repliken genügen für den Methodenvergleich, nicht für eine produktive Quantilstabilitätsfreigabe; 2.000 bleibt nur ein Prüfkandidat.

## Methodische Grenze und HITL-Entscheidung

Die Python-Standardbibliothek genügt numerisch weiterhin vollständig. Eine neue Abhängigkeit löst weder unbekannten Messfehler noch fehlende Identifikation. Nach Ausschöpfung der sparsamen Bandvarianten bleiben zwei fachliche Wege: belastbar nur innerhalb explizit bestandener Reifediagnosen, oder die Zerlegungsfamilie selbst neu öffnen.

## Primärquellen

- Fan (1992): https://doi.org/10.1080/01621459.1992.10476255
- Künsch (1989): https://doi.org/10.1214/aos/1176347265
- Politis & White (2004): https://doi.org/10.1081/ETC-120028836
- Morris, White & Crowther (2019): https://doi.org/10.1002/sim.8086

## Reproduktion

```bash
uv run python prototypes/outcome_association_calibration_103.py --mode full
```
