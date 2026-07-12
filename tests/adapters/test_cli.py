import json
from pathlib import Path

import pytest

from personal_health_lab.adapters.cli import main
from personal_health_lab.synthetic_export import generate_export


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
        "daily_series": [],
        "message": "Keine Gesundheitsdaten vorhanden.",
        "resting_hr_analysis": None,
        "provenance": {
            "import_count": 0,
            "logical_measurement_count": 0,
            "measurement_version_count": 0,
            "package_count": 0,
            "snapshot_count": 0,
        },
        "runtime_config": {
            "mode": "synthetic",
            "real_store": "<redacted>",
            "synthetic_store": "<redacted>",
        },
        "schema_version": "1.0",
        "status": "empty",
    }


def test_cli_imports_export_and_prints_daily_series(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    common_args = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]

    assert main([*common_args, "import", str(fixture.export_path), "--json"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "committed"
    assert receipt["record_count"] == 1_825

    assert main([*common_args, "overview", "--json"]) == 0
    overview = json.loads(capsys.readouterr().out)
    assert [(series["data_type"], series["unit"]) for series in overview["daily_series"]] == [
        ("active_energy", "kcal"),
        ("apple_resting_heart_rate", "count/min"),
    ]
    assert len(overview["daily_series"][0]["values"]) == 365


def test_cli_runs_and_exposes_the_built_in_lag_analysis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = generate_export("lag-signal-v1", 42, tmp_path / "fixture")
    common_args = [
        "--mode",
        "synthetic",
        "--synthetic-store",
        str(tmp_path / "synthetic"),
        "--real-store",
        str(tmp_path / "real"),
    ]
    assert main([*common_args, "import", str(fixture.export_path)]) == 0
    capsys.readouterr()

    assert main([*common_args, "analyze", "--json"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "completed"
    assert receipt["analysis_definition_id"] == "lag-signal-v1"
    assert len(receipt["result"]["lag_associations"]) == 7
    assert receipt["result"]["model_maturity"] == "robust"
    assert receipt["result"]["methodology"]["bootstrap_method"] == "moving_block"
    assert receipt["result"]["diagnostics"]
    assert all(
        item["pointwise_interval"] and item["simultaneous_band"]
        for item in receipt["result"]["lag_associations"]
    )

    structured_result = json.dumps(receipt["result"], ensure_ascii=False).lower()
    assert all(
        forbidden not in structured_result
        for forbidden in (
            "*",
            "significant",
            "signifikant",
            "causal",
            "kausal",
            "medical",
            "medizin",
            "recommend",
            "empfehl",
            "therap",
        )
    )

    assert main([*common_args, "overview", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)["resting_hr_analysis"]
    assert len(result["lag_associations"]) == 7
    assert result["lag_associations"][0]["direction"] == "negative"

    assert main([*common_args, "analyze"]) == 0
    human_output = capsys.readouterr().out
    assert "simultan" in human_output
    assert "Diagnosen:" in human_output

    assert main([*common_args, "analyze", "--start-date", "2024-12-20", "--json"]) == 3
    insufficient = json.loads(capsys.readouterr().out)
    assert insufficient["status"] == "insufficient_data"
    assert insufficient["result"] is None
