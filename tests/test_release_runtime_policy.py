from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_release_runtime.py"


def test_runtime_check_fails_with_readable_evidence_on_version_mismatch(tmp_path: Path) -> None:
    policy = tmp_path / "python-version.txt"
    evidence = tmp_path / "runtime-parity.json"
    policy.write_text("99.1\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(policy), "--output", str(evidence)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED"
    assert payload["expected_python"] == "99.1"
