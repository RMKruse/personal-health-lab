# Ein realer Datenspeicher enthält genau eine Person

Der Kern-MVP unterstützt genau eine Person pro realem Datenspeicher und führt deshalb keine `subject_id`, Benutzerkonten oder Mandantentrennung im fachlichen Kern. Diese Grenze hält Import, Korrekturen und N-of-1-Analysen einfach; eine spätere Mehrpersonenfähigkeit erfordert eine bewusste Migration statt einer vorgetäuschten generischen Abstraktion.
