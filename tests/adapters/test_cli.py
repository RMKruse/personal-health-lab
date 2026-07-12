import json
from pathlib import Path

import pytest

from personal_health_lab.adapters.cli import main


def test_cli_prints_empty_overview_as_versioned_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "--mode",
            "synthetic",
            "--synthetic-store",
            str(tmp_path / "synthetic"),
            "--real-store",
            str(tmp_path / "real"),
            "overview",
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output == {
        "message": "Keine Gesundheitsdaten vorhanden.",
        "runtime_config": {
            "mode": "synthetic",
            "real_store": "<redacted>",
            "synthetic_store": "<redacted>",
        },
        "schema_version": "1.0",
        "status": "empty",
    }
