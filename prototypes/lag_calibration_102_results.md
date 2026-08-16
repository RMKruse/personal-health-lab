# V0.4 lag calibration prototype

Version: `v0.4-lag-calibration-1` · Mode: `full`

PROTOTYPE — throw away after its decisions are captured in the Wayfinder ticket.

## Result

### Horizon 1-7

- Candidate: `{'basis_size': 7, 'ridge_penalty': 1.0, 'smooth_penalty': 3.0}`
- Synthetic-bias floor for the studentized simultaneous critical value: 5.708.
- Block rule: `max(ceil(L/3), ceil(1 x n^(1/3)))`; selected length range [5, 10] days.
- Bias 0.003 bpm; RMSE 0.135 bpm.
- Pointwise coverage 92.3%; simultaneous coverage 88.3%; null false alarm 16.7%.
- Bootstrap failure rate 0.00%; runtime 74.3s.
- Candidate maturity gate: {'maximum_residual_acf': 0.35, 'minimum_complete_rows': 100, 'minimum_effective_blocks': 18, 'minimum_input_completeness': 0.6, 'minimum_positive_training_days': 10}; retained 29 cases.
- Gated coverage: pointwise 90.5%; simultaneous 93.1%; null false alarm 0.0%.

Factor slices:

```json
{
  "context_lag": {
    "0": {
      "bootstrap_failure_rate": 0.0,
      "cases": 30,
      "mean_band_width": 1.2525222135153504,
      "mean_bias": -0.007754630330344291,
      "mean_rmse": 0.12998507689734515,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.926984126984127,
      "runtime_seconds": 35.970446502324194,
      "simultaneous_coverage": 1.0
    },
    "2": {
      "bootstrap_failure_rate": 0.0,
      "cases": 30,
      "mean_band_width": 1.277841057656199,
      "mean_bias": 0.01327000811124155,
      "mean_rmse": 0.140173930196801,
      "null_false_alarm": 0.3333333333333333,
      "pointwise_coverage": 0.9190476190476191,
      "runtime_seconds": 38.32897174777463,
      "simultaneous_coverage": 0.7666666666666667
    }
  },
  "days": {
    "180": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.5561155424705928,
      "mean_bias": -0.005009193642313044,
      "mean_rmse": 0.1602134117596043,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9357142857142857,
      "runtime_seconds": 15.846756250131875,
      "simultaneous_coverage": 1.0
    },
    "365": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.3379066969801925,
      "mean_bias": 0.006828773722962919,
      "mean_rmse": 0.14064306207445104,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.930952380952381,
      "runtime_seconds": 23.393274792004377,
      "simultaneous_coverage": 0.85
    },
    "730": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.9015226673065388,
      "mean_bias": 0.0064534865906960125,
      "mean_rmse": 0.10438203680716396,
      "null_false_alarm": 0.5,
      "pointwise_coverage": 0.9023809523809524,
      "runtime_seconds": 35.05938720796257,
      "simultaneous_coverage": 0.8
    }
  },
  "feature_correlation": {
    "0.2": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.971668079683653,
      "mean_bias": 0.004001344412887598,
      "mean_rmse": 0.10418594123622855,
      "null_false_alarm": 0.25,
      "pointwise_coverage": 0.9238095238095239,
      "runtime_seconds": 25.251397581305355,
      "simultaneous_coverage": 0.75
    },
    "0.75": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.329175731092306,
      "mean_bias": -0.0028644754358025893,
      "mean_rmse": 0.1394317568715686,
      "null_false_alarm": 0.25,
      "pointwise_coverage": 0.9142857142857143,
      "runtime_seconds": 22.28633633442223,
      "simultaneous_coverage": 0.95
    },
    "0.95": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.494701095981365,
      "mean_bias": 0.007136197694260882,
      "mean_rmse": 0.1616208125334221,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.930952380952381,
      "runtime_seconds": 26.76168433437124,
      "simultaneous_coverage": 0.95
    }
  },
  "gap_days": {
    "0": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.1652981570042569,
      "mean_bias": 0.006716279091319824,
      "mean_rmse": 0.12562836666409152,
      "null_false_alarm": 0.25,
      "pointwise_coverage": 0.9309523809523809,
      "runtime_seconds": 29.482568497769535,
      "simultaneous_coverage": 0.85
    },
    "14": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.26546964816305,
      "mean_bias": -0.0035212490985998234,
      "mean_rmse": 0.148093622846114,
      "null_false_alarm": 0.25,
      "pointwise_coverage": 0.8904761904761905,
      "runtime_seconds": 21.637040498666465,
      "simultaneous_coverage": 0.8
    },
    "35": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.3647771015900176,
      "mean_bias": 0.00507803667862589,
      "mean_rmse": 0.1315165211310138,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9476190476190476,
      "runtime_seconds": 23.179809253662825,
      "simultaneous_coverage": 1.0
    }
  },
  "noise_sd": {
    "0.55": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.7881798180844795,
      "mean_bias": 0.006820268395854407,
      "mean_rmse": 0.0906508569189014,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9357142857142856,
      "runtime_seconds": 23.46908220788464,
      "simultaneous_coverage": 0.75
    },
    "1.0": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.261041910780182,
      "mean_bias": 0.005638948405916113,
      "mean_rmse": 0.13920081198471557,
      "null_false_alarm": 0.5,
      "pointwise_coverage": 0.9261904761904761,
      "runtime_seconds": 27.76101029664278,
      "simultaneous_coverage": 0.9
    },
    "1.7": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.7463231778926622,
      "mean_bias": -0.004186150130424634,
      "mean_rmse": 0.17538684173760236,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9071428571428571,
      "runtime_seconds": 23.069325745571405,
      "simultaneous_coverage": 1.0
    }
  },
  "overlap_ar": {
    "0.15": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.193820308258969,
      "mean_bias": -0.002364799173319656,
      "mean_rmse": 0.129781906733426,
      "null_false_alarm": 0.5,
      "pointwise_coverage": 0.9095238095238095,
      "runtime_seconds": 24.40704083442688,
      "simultaneous_coverage": 0.9
    },
    "0.55": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.0294230434377825,
      "mean_bias": 0.004775280002322244,
      "mean_rmse": 0.10427904627216507,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9571428571428571,
      "runtime_seconds": 24.042789414525032,
      "simultaneous_coverage": 0.75
    },
    "0.85": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.5723015550605726,
      "mean_bias": 0.005862585842343301,
      "mean_rmse": 0.1711775576356282,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9023809523809524,
      "runtime_seconds": 25.849588001146913,
      "simultaneous_coverage": 1.0
    }
  },
  "profile": {
    "delayed": {
      "bootstrap_failure_rate": 0.0,
      "cases": 12,
      "mean_band_width": 1.301642447554509,
      "mean_bias": -0.00030789679522361287,
      "mean_rmse": 0.1411397326420857,
      "null_false_alarm": null,
      "pointwise_coverage": 0.9404761904761906,
      "runtime_seconds": 14.939833207987249,
      "simultaneous_coverage": 0.9166666666666666
    },
    "null": {
      "bootstrap_failure_rate": 0.0,
      "cases": 12,
      "mean_band_width": 1.2959636594815709,
      "mean_bias": 0.002656034199743472,
      "mean_rmse": 0.12346874817326592,
      "null_false_alarm": 0.16666666666666666,
      "pointwise_coverage": 0.9603174603174603,
      "runtime_seconds": 14.848334834445268,
      "simultaneous_coverage": 0.8333333333333334
    },
    "sharp": {
      "bootstrap_failure_rate": 0.0,
      "cases": 12,
      "mean_band_width": 1.2586217190939106,
      "mean_bias": -0.003600780928028078,
      "mean_rmse": 0.14349939173190587,
      "null_false_alarm": null,
      "pointwise_coverage": 0.8968253968253967,
      "runtime_seconds": 14.826221458613873,
      "simultaneous_coverage": 0.9166666666666666
    },
    "sign_change": {
      "bootstrap_failure_rate": 0.0,
      "cases": 12,
      "mean_band_width": 1.1666062331377316,
      "mean_bias": 0.0059766285887562975,
      "mean_rmse": 0.12404316366961825,
      "null_false_alarm": null,
      "pointwise_coverage": 0.9087301587301587,
      "runtime_seconds": 14.853224663529545,
      "simultaneous_coverage": 0.9166666666666666
    },
    "smooth": {
      "bootstrap_failure_rate": 0.0,
      "cases": 12,
      "mean_band_width": 1.3030741186611512,
      "mean_bias": 0.00906445938699507,
      "mean_rmse": 0.1432464815184897,
      "null_false_alarm": null,
      "pointwise_coverage": 0.9087301587301587,
      "runtime_seconds": 14.83180408552289,
      "simultaneous_coverage": 0.8333333333333334
    }
  },
  "residual_ar": {
    "0.0": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.3076543504569054,
      "mean_bias": 0.0126190374970989,
      "mean_rmse": 0.12060349948500844,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9357142857142857,
      "runtime_seconds": 23.79038870567456,
      "simultaneous_coverage": 0.95
    },
    "0.35": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.2674182401583014,
      "mean_bias": -0.0025062632691420944,
      "mean_rmse": 0.1502739779505316,
      "null_false_alarm": 0.25,
      "pointwise_coverage": 0.8928571428571429,
      "runtime_seconds": 25.433697998989373,
      "simultaneous_coverage": 0.85
    },
    "0.65": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.2204723161421172,
      "mean_bias": -0.0018397075566109159,
      "mean_rmse": 0.13436103320567927,
      "null_false_alarm": 0.25,
      "pointwise_coverage": 0.9404761904761905,
      "runtime_seconds": 25.075331545434892,
      "simultaneous_coverage": 0.85
    }
  },
  "training_support": {
    "0.05": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.226564931610361,
      "mean_bias": 0.006211324995832516,
      "mean_rmse": 0.11906642667842786,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9452380952380952,
      "runtime_seconds": 25.446059708017856,
      "simultaneous_coverage": 0.9
    },
    "0.15": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.3122323093015644,
      "mean_bias": 0.0021535441839013137,
      "mean_rmse": 0.1446865398430643,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9214285714285715,
      "runtime_seconds": 25.41140050208196,
      "simultaneous_coverage": 0.95
    },
    "0.3": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.2567476658453987,
      "mean_bias": -9.18025083879392e-05,
      "mean_rmse": 0.1414855441197272,
      "null_false_alarm": 0.5,
      "pointwise_coverage": 0.9023809523809524,
      "runtime_seconds": 23.441958039999008,
      "simultaneous_coverage": 0.8
    }
  }
}
```

Bootstrap-count stability (95th percentile absolute critical-value difference from 2,000):

```json
{
  "1000": 0.0,
  "250": 0.0,
  "500": 0.0
}
```

### Horizon 1-30

- Candidate: `{'basis_size': 9, 'ridge_penalty': 1.0, 'smooth_penalty': 300.0}`
- Synthetic-bias floor for the studentized simultaneous critical value: 6.454.
- Block rule: `max(ceil(L/3), ceil(1 x n^(1/3)))`; selected length range [10, 10] days.
- Bias 0.003 bpm; RMSE 0.081 bpm.
- Pointwise coverage 94.2%; simultaneous coverage 95.0%; null false alarm 8.3%.
- Bootstrap failure rate 0.00%; runtime 102.4s.
- Candidate maturity gate: {'maximum_gap_days': 14, 'minimum_complete_rows': 120, 'minimum_effective_blocks': 11, 'minimum_input_completeness': 0.65, 'minimum_positive_training_days': 10}; retained 31 cases.
- Gated coverage: pointwise 92.7%; simultaneous 93.5%; null false alarm 0.0%.

Factor slices:

```json
{
  "context_lag": {
    "0": {
      "bootstrap_failure_rate": 0.0,
      "cases": 30,
      "mean_band_width": 0.5421157836910339,
      "mean_bias": 0.0042989004978109735,
      "mean_rmse": 0.06415510125026057,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9374074074074074,
      "runtime_seconds": 53.8187495409511,
      "simultaneous_coverage": 0.9333333333333333
    },
    "2": {
      "bootstrap_failure_rate": 0.0,
      "cases": 30,
      "mean_band_width": 0.8316282587486283,
      "mean_bias": 0.0011635117169433965,
      "mean_rmse": 0.09688396121246942,
      "null_false_alarm": 0.16666666666666666,
      "pointwise_coverage": 0.9466666666666667,
      "runtime_seconds": 48.5499979974702,
      "simultaneous_coverage": 0.9666666666666667
    }
  },
  "days": {
    "180": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 1.039856021539634,
      "mean_bias": -0.001967896011173143,
      "mean_rmse": 0.11927227161569334,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9733333333333334,
      "runtime_seconds": 23.62250349856913,
      "simultaneous_coverage": 1.0
    },
    "365": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.6555349642480375,
      "mean_bias": 0.00815572004843917,
      "mean_rmse": 0.08252300893136945,
      "null_false_alarm": 0.25,
      "pointwise_coverage": 0.9277777777777777,
      "runtime_seconds": 32.63698299974203,
      "simultaneous_coverage": 0.95
    },
    "730": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.3652250778718218,
      "mean_bias": 0.0020057942848655304,
      "mean_rmse": 0.039763313147032193,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.925,
      "runtime_seconds": 46.10926104011014,
      "simultaneous_coverage": 0.9
    }
  },
  "feature_correlation": {
    "0.2": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.586671589141796,
      "mean_bias": 0.005194151397309063,
      "mean_rmse": 0.06794069497368099,
      "null_false_alarm": 0.25,
      "pointwise_coverage": 0.9327777777777777,
      "runtime_seconds": 33.52710466692224,
      "simultaneous_coverage": 0.9
    },
    "0.75": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.6535362132217313,
      "mean_bias": 0.0022732527139321915,
      "mean_rmse": 0.07930973358255015,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9483333333333333,
      "runtime_seconds": 37.47099337587133,
      "simultaneous_coverage": 0.95
    },
    "0.95": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.8204082612959658,
      "mean_bias": 0.0007262142108903017,
      "mean_rmse": 0.09430816513786386,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9450000000000001,
      "runtime_seconds": 31.37064949562773,
      "simultaneous_coverage": 1.0
    }
  },
  "gap_days": {
    "0": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.5278706509365573,
      "mean_bias": 2.9789439992807324e-05,
      "mean_rmse": 0.06138790626690682,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9316666666666666,
      "runtime_seconds": 39.88652245653793,
      "simultaneous_coverage": 0.9
    },
    "14": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.7590119798122312,
      "mean_bias": 0.009096198252314992,
      "mean_rmse": 0.08294435591405394,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9494444444444445,
      "runtime_seconds": 31.327410208061337,
      "simultaneous_coverage": 1.0
    },
    "35": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.7737334329107044,
      "mean_bias": -0.0009323693701762433,
      "mean_rmse": 0.09722633151313423,
      "null_false_alarm": 0.25,
      "pointwise_coverage": 0.945,
      "runtime_seconds": 31.154814873822033,
      "simultaneous_coverage": 0.95
    }
  },
  "noise_sd": {
    "0.55": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.4242848840273264,
      "mean_bias": 0.008729714269430578,
      "mean_rmse": 0.047607645490482076,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9305555555555556,
      "runtime_seconds": 34.83480116352439,
      "simultaneous_coverage": 0.95
    },
    "1.0": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.6886544207861462,
      "mean_bias": 0.009226978435539199,
      "mean_rmse": 0.08256609441661296,
      "null_false_alarm": 0.25,
      "pointwise_coverage": 0.9516666666666665,
      "runtime_seconds": 33.573812540154904,
      "simultaneous_coverage": 0.9
    },
    "1.7": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.9476767588460205,
      "mean_bias": -0.00976307438283822,
      "mean_rmse": 0.11138485378699994,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9438888888888888,
      "runtime_seconds": 33.96013383474201,
      "simultaneous_coverage": 1.0
    }
  },
  "overlap_ar": {
    "0.15": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.75746759293568,
      "mean_bias": 1.0022610328273492e-05,
      "mean_rmse": 0.09377518734713017,
      "null_false_alarm": 0.25,
      "pointwise_coverage": 0.9266666666666667,
      "runtime_seconds": 35.418636289425194,
      "simultaneous_coverage": 0.95
    },
    "0.55": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.7134214143966404,
      "mean_bias": 0.0028624803830350778,
      "mean_rmse": 0.08093236445636827,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9622222222222222,
      "runtime_seconds": 31.918619042262435,
      "simultaneous_coverage": 1.0
    },
    "0.85": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.5897270563271726,
      "mean_bias": 0.005321115328768207,
      "mean_rmse": 0.06685104189059655,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9372222222222222,
      "runtime_seconds": 35.031492206733674,
      "simultaneous_coverage": 0.9
    }
  },
  "profile": {
    "delayed": {
      "bootstrap_failure_rate": 0.0,
      "cases": 12,
      "mean_band_width": 0.7420868097724463,
      "mean_bias": 0.008230571261625148,
      "mean_rmse": 0.08693245594558224,
      "null_false_alarm": null,
      "pointwise_coverage": 0.9481481481481482,
      "runtime_seconds": 20.438756083138287,
      "simultaneous_coverage": 1.0
    },
    "null": {
      "bootstrap_failure_rate": 0.0,
      "cases": 12,
      "mean_band_width": 0.6325419809740014,
      "mean_bias": 0.00691055363614377,
      "mean_rmse": 0.06867656593988143,
      "null_false_alarm": 0.08333333333333333,
      "pointwise_coverage": 0.9398148148148149,
      "runtime_seconds": 20.47747683385387,
      "simultaneous_coverage": 0.9166666666666666
    },
    "sharp": {
      "bootstrap_failure_rate": 0.0,
      "cases": 12,
      "mean_band_width": 0.7157599234486695,
      "mean_bias": -0.0009168088246256754,
      "mean_rmse": 0.08723091416977337,
      "null_false_alarm": null,
      "pointwise_coverage": 0.9092592592592593,
      "runtime_seconds": 20.36007728986442,
      "simultaneous_coverage": 0.8333333333333334
    },
    "sign_change": {
      "bootstrap_failure_rate": 0.0,
      "cases": 12,
      "mean_band_width": 0.6775311043299013,
      "mean_bias": -0.003499257996248105,
      "mean_rmse": 0.08259892310229597,
      "null_false_alarm": null,
      "pointwise_coverage": 0.9555555555555556,
      "runtime_seconds": 20.587843623477966,
      "simultaneous_coverage": 1.0
    },
    "smooth": {
      "bootstrap_failure_rate": 0.0,
      "cases": 12,
      "mean_band_width": 0.666440287574137,
      "mean_bias": 0.0029309724599907872,
      "mean_rmse": 0.077158796999292,
      "null_false_alarm": null,
      "pointwise_coverage": 0.9574074074074076,
      "runtime_seconds": 20.50459370808676,
      "simultaneous_coverage": 1.0
    }
  },
  "residual_ar": {
    "0.0": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.6359859057347386,
      "mean_bias": -0.0009581543084190007,
      "mean_rmse": 0.057533374289246854,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9327777777777777,
      "runtime_seconds": 33.94820920517668,
      "simultaneous_coverage": 1.0
    },
    "0.35": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.5936437083445484,
      "mean_bias": -0.002705025075750154,
      "mean_rmse": 0.0721907630081276,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9427777777777777,
      "runtime_seconds": 36.877349293325096,
      "simultaneous_coverage": 0.95
    },
    "0.65": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.8309864495802062,
      "mean_bias": 0.011856797706300711,
      "mean_rmse": 0.11183445639672054,
      "null_false_alarm": 0.25,
      "pointwise_coverage": 0.9505555555555556,
      "runtime_seconds": 31.543189039919525,
      "simultaneous_coverage": 0.9
    }
  },
  "training_support": {
    "0.05": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.6820639759253724,
      "mean_bias": 0.003358656693692149,
      "mean_rmse": 0.08036376254833974,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9272222222222222,
      "runtime_seconds": 34.323194082360715,
      "simultaneous_coverage": 0.95
    },
    "0.15": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.5056791827480204,
      "mean_bias": 0.008815077799116111,
      "mean_rmse": 0.06206696240581293,
      "null_false_alarm": 0.0,
      "pointwise_coverage": 0.9411111111111111,
      "runtime_seconds": 36.52258008252829,
      "simultaneous_coverage": 0.95
    },
    "0.3": {
      "bootstrap_failure_rate": 0.0,
      "cases": 20,
      "mean_band_width": 0.8728729049861002,
      "mean_bias": -0.003980116170676703,
      "mean_rmse": 0.09912786873994231,
      "null_false_alarm": 0.25,
      "pointwise_coverage": 0.9577777777777777,
      "runtime_seconds": 31.522973373532295,
      "simultaneous_coverage": 0.95
    }
  }
}
```

Bootstrap-count stability (95th percentile absolute critical-value difference from 2,000):

```json
{
  "1000": 0.04867565897546289,
  "250": 0.040184636170014,
  "500": 0.09228839976456671
}
```

## Interpretation limits

This is a calibration experiment, not production analysis. The synthetic mechanisms are versioned in the script; thresholds are valid only for that envelope. `robust` still means stable conditional association, never causality or medical validity.

Raw scenario-level results are in `lag_calibration_102_results.json`.
