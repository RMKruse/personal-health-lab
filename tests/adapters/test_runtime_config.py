import json
from pathlib import Path

from personal_health_lab.adapters._config import load_runtime_config
from personal_health_lab.application import DataMode


def test_runtime_config_uses_documented_precedence(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "mode": "real",
                "real_store": str(tmp_path / "real-from-file"),
                "synthetic_store": str(tmp_path / "synthetic-from-file"),
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(
        explicit_mode=DataMode.SYNTHETIC,
        config_file=config_file,
        environ={"HEALTHLAB_REAL_STORE": str(tmp_path / "real-from-environment")},
    )

    assert config.mode is DataMode.SYNTHETIC
    assert config.synthetic_store == (tmp_path / "synthetic-from-file").resolve()
    assert config.real_store == (tmp_path / "real-from-environment").resolve()
