from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_identity_witness_falls_back_to_record_module(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    viz = candidate / "src" / "viz"
    viz.mkdir(parents=True)
    (candidate / "src" / "__init__.py").write_text("", encoding="utf-8")
    (viz / "__init__.py").write_text("", encoding="utf-8")
    (viz / "frozen_evaluation_record.py").write_text(
        '''
def build_frozen_evaluation_record(*, verification, expiry_str):
    return {
        "snapshot_id": "22222222-2222-4222-8222-222222222222",
        "created_at_utc": "2026-07-13T00:00:00+00:00",
        "expiry": expiry_str,
        "payload_schema_version": "ppe_frozen_eval_v1",
        "record_header": {
            "snapshot_id": "22222222-2222-4222-8222-222222222222",
            "payload_schema_version": "ppe_frozen_eval_v1",
        },
    }


def build_snapshot_review_payload(*, record, review):
    return {
        "schema_version": "snapshot_review_v1",
        "snapshot_id": record["snapshot_id"],
        "record_header": dict(record["record_header"]),
        "review": review or {},
    }


def validate_snapshot_review_payload(payload):
    if payload["snapshot_id"] != payload["record_header"]["snapshot_id"]:
        raise ValueError("snapshot identity mismatch")
    return payload
'''.lstrip(),
        encoding="utf-8",
    )

    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "check_frozen_evaluation_candidate.py"
    )
    proc = subprocess.run(
        [sys.executable, str(script), str(candidate)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "snapshot identity mismatch rejected" in proc.stdout
