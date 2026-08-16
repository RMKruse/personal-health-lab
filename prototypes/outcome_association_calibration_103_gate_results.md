# V0.4-Prototyp: vorläufiges synthetisches Reife-Gate

Version: `v0.4-outcome-association-calibration-2-gate-1`.

## Ergebnis

90 von 90 unabhängigen Fällen bestanden das Daten-/Design-Gate (100,0 %). Innerhalb des Gates betrug die gemeinsame Abdeckung 98,9 %, der familienweise Null-Fehlalarm 0,0 %.
Ohne Gate betrug die gemeinsame Abdeckung 98.9%.

Nicht verwendete Diagnose-Ausschlüsse: influence=47, smoothing_change=6, block_change=13, structure_break_sensitivity=6

Das verworfene Diagnose-Gate hätte nur 35 Fälle (38,9 %) behalten und dort 97,1 % Abdeckung erreicht. Es erkannte den einzigen Fehlschlag nicht und wird daher nicht Teil der Definition. Einfluss und Sensitivität bleiben Warnhinweise.

## Bootstrap-Stabilität

Vier vorab ausgewählte schwierige Fälle wurden mit 2.000 erfolgreichen Refits für die Primär- und die verdoppelte Blocklänge nachgerechnet. Alle 16.000 Refits waren erfolgreich. Die Quantilstabilität lag zwischen 0,0003 und 0,0553, die maximale Änderung der Band-Halbbreite durch Blockverdopplung zwischen 0,0081 und 0,0150. Der einzige bei 120 Replikaten unbedeckte Fall blieb unbedeckt; die Holdout-Gesamtabdeckung von 98,9 % ist daher die maßgebliche Aussage.

Vorläufige Freigaberegel: 2.000 erfolgreiche Refits innerhalb höchstens 2.020 Versuchen. 120 Replikate dienen nur der Holdout-Kalibrierung, nicht der Ausgabefreigabe.

## Vorläufige Definition

```json
{
  "minimum_history_days": 365,
  "minimum_paired_density": 0.4,
  "maximum_observed_gap_days": 18,
  "maximum_residual_acf": 0.55,
  "maximum_resting_measurement_error_sd": 0.35,
  "maximum_weight_measurement_error_sd": 0.084,
  "structural_breaks": "none",
  "device_transitions": "none",
  "minimum_bootstrap_success_rate": 0.99
}
```

## Zwingende Neukalibrierung mit echten Daten

V0.5 muss reale Dichte, Lücken, Restabhängigkeit, Gerätewechsel, Strukturbrüche und Messfehler mit dieser synthetischen Hülle vergleichen. Liegt auch nur eine Dimension außerhalb, werden zuerst die synthetischen Szenarien erweitert und danach Kernel, Bandregel, Untergrenzen, Replikzahl und Gates als vollständige neue Version kalibriert. Vorher werden reale Zusammenhänge ausschließlich explorativ ausgegeben.

Diese Schwellen sind keine Aussagen über echte Gesundheitsdaten. Sie definieren nur, wann die V0.4-Ausgabe innerhalb der getesteten synthetischen Hülle als belastbar statt explorativ markiert werden darf.

## Reproduktion

```bash
uv run python prototypes/outcome_association_calibration_103.py --mode gate
```
