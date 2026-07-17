from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from msos_autobuilder import candidate_gate as gate
from msos_autobuilder.candidate_gate_revisions import _record_global_error_if_needed
from msos_autobuilder.service_error_lifecycle import write_service_error_marker


def _write_wrapper_config(tmp_path: Path) -> Path:
    host_root = tmp_path / "host"
    source = tmp_path / "source"
    source.mkdir()
    config = {
        "version": 1,
        "publication_enabled": False,
        "host_root": str(host_root),
        "source_repo": str(source),
        "results_repo_url": str(tmp_path / "results.git"),
        "results_branch": "results",
        "machine_id": "test-host",
        "poll_seconds": 1,
        "plans": {},
    }
    path = tmp_path / "candidate-gate.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def test_revision_wrapper_preserves_inner_associated_marker(tmp_path: Path) -> None:
    config_path = _write_wrapper_config(tmp_path)
    host_root = tmp_path / "host"
    exc = gate.CandidateGateError("inner job failure")
    write_service_error_marker(
        state_root=host_root / "state",
        host_root=host_root,
        service="gate",
        marker_name="candidate-gate-error.json",
        error_type=type(exc).__name__,
        message=str(exc),
        associated={"job_id": "job-1", "candidate_id": "job-1"},
        extra={"publication_enabled": False},
        exception=exc,
    )
    marker = host_root / "state" / "candidate-gate-error.json"
    before = marker.read_bytes()
    before_sha = hashlib.sha256(before).hexdigest()

    assert _record_global_error_if_needed(config_path, exc) is False

    assert marker.read_bytes() == before
    assert hashlib.sha256(marker.read_bytes()).hexdigest() == before_sha
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["associated"]["job_id"] == "job-1"


def test_revision_wrapper_pre_job_failure_records_global_marker(tmp_path: Path) -> None:
    config_path = _write_wrapper_config(tmp_path)
    exc = gate.CandidateGateError("config expansion failed")

    assert _record_global_error_if_needed(config_path, exc) is True

    marker = json.loads(
        (tmp_path / "host" / "state" / "candidate-gate-error.json").read_text(
            encoding="utf-8"
        )
    )
    assert marker["service"] == "gate"
    assert marker["associated"] == {"scope": "global"}
    assert marker["publication_enabled"] is False
