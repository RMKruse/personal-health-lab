import hashlib
import json
import subprocess
import sys
from pathlib import Path


def test_v03_evidence_pins_release_inputs_without_personal_values(tmp_path: Path) -> None:
    script = Path(__file__).parents[2] / "scripts/v03_acceptance_evidence.py"
    outputs = (tmp_path / "first.json", tmp_path / "second.json")
    for output in outputs:
        subprocess.run([sys.executable, str(script), str(output)], check=True)
    first, second = (json.loads(output.read_text(encoding="utf-8")) for output in outputs)
    v02_output = tmp_path / "v02.json"
    subprocess.run(
        [sys.executable, str(script.with_name("v02_acceptance_evidence.py")), str(v02_output)],
        check=True,
    )

    assert first == second
    assert first["missing"] == []
    assert first["git_commit"]
    assert first["python"]["implementation"]
    assert first["python"]["version"]
    assert first["environment_lock_sha256"]
    assert first["v02_matrix_sha256"]
    assert first["v02_evidence_sha256"] == hashlib.sha256(v02_output.read_bytes()).hexdigest()
    assert first["v03_matrix_sha256"]
    assert first["contracts"]
    assert first["cases"]
    assert first["fixtures"]
    assert set(first["methods"]) == {
        "activity-derivation/v1",
        "cow-migration/v1",
        "full-snapshot-import/v1",
        "manual-snapshot/v1",
        "metadata-backup/v1",
        "restore-activate/v1",
        "restore-source-import/v1",
        "restore-start/v1",
    }
    assert {gate["status"] for gate in first["ci"]["gates"]} == {"success"}
    serialized = str(first).lower()
    assert "healthlab_synthetic_store" not in serialized
    assert "healthlab_real_store" not in serialized
