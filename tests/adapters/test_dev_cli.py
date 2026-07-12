import hashlib
import json
from pathlib import Path

import pytest

from personal_health_lab.adapters.dev_cli import main
from personal_health_lab.application import DataMode, HealthLab, RuntimeConfig


def test_dev_cli_generates_versioned_metadata_and_checksums(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "fixture"

    exit_code = main(
        [
            "generate",
            "--scenario",
            "lag-signal-v1",
            "--seed",
            "42",
            "--destination",
            str(destination),
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["schema_version"] == "1.0"
    assert output["status"] == "generated"
    assert output["metadata"]["scenario_id"] == "lag-signal-v1"
    assert set(output["checksums"]) == {
        "apple-health-export.zip",
        "scenario-metadata.json",
    }
    assert output["checksums"] == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            destination / "apple-health-export.zip",
            destination / "scenario-metadata.json",
        )
    }


def test_dev_cli_refuses_to_write_inside_a_real_store(tmp_path: Path) -> None:
    real_store = tmp_path / "real"
    config = RuntimeConfig(
        mode=DataMode.REAL,
        synthetic_store=tmp_path / "synthetic",
        real_store=real_store,
    )
    with HealthLab.open(config):
        pass
    files_before = {
        path.relative_to(real_store): path.read_bytes()
        for path in real_store.rglob("*")
        if path.is_file()
    }

    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "generate",
                "--scenario",
                "null-v1",
                "--seed",
                "42",
                "--destination",
                str(real_store / "forbidden-fixture"),
            ]
        )

    files_after = {
        path.relative_to(real_store): path.read_bytes()
        for path in real_store.rglob("*")
        if path.is_file()
    }
    assert exit_info.value.code == 2
    assert files_after == files_before
