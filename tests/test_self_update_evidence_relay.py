from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from msos_autobuilder.self_update_evidence_relay import (
    EvidenceRelayError,
    SelfUpdateEvidenceRelay,
    load_evidence_relay_config,
)


def _git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


def _results_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote))
    seed = tmp_path / "seed"
    _git("clone", str(remote), str(seed))
    _git("config", "user.name", "Fixture", cwd=seed)
    _git("config", "user.email", "fixture@example.com", cwd=seed)
    (seed / "README.md").write_text("results\n", encoding="utf-8")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-m", "seed results", cwd=seed)
    _git("push", "origin", "HEAD:results", cwd=seed)
    return remote


def _config(tmp_path: Path, remote: Path) -> Path:
    supervisor_root = tmp_path / "supervisor"
    path = tmp_path / "supervisor.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "supervisor_root": str(supervisor_root),
                "repo_url": str(remote),
                "evidence_repo_url": str(remote),
                "evidence_branch": "results",
                "machine_id": "fixture-machine",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _evidence(supervisor_root: Path, attempt_id: str = "attempt-1") -> tuple[Path, Path]:
    report = supervisor_root / "reports" / f"{attempt_id}.json"
    notification = supervisor_root / "notifications" / f"{attempt_id}.json"
    report.parent.mkdir(parents=True)
    notification.parent.mkdir(parents=True)
    report.write_text(
        json.dumps(
            {
                "version": 1,
                "attempt_id": attempt_id,
                "outcome": "success",
                "requested_commit": "a" * 40,
                "manifest_sha256": "b" * 64,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    notification.write_text(
        json.dumps(
            {
                "version": 1,
                "attempt_id": attempt_id,
                "outcome": "success",
                "report_path": str(report),
                "requires_founder_attention": False,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return report, notification


def test_relay_publishes_immutable_report_and_notification_once(tmp_path: Path) -> None:
    remote = _results_remote(tmp_path)
    config = load_evidence_relay_config(_config(tmp_path, remote))
    report, notification = _evidence(config.supervisor_root)
    relay = SelfUpdateEvidenceRelay(config)

    assert relay.run_once() == ("attempt-1",)
    assert relay.run_once() == ()

    review = tmp_path / "review"
    _git("clone", "--branch", "results", str(remote), str(review))
    destination = review / "results" / "fixture-machine" / "self-updates" / "attempt-1"
    assert (destination / "update-report.json").read_bytes() == report.read_bytes()
    assert (destination / "notification.json").read_bytes() == notification.read_bytes()
    metadata = json.loads((destination / "relay.json").read_text(encoding="utf-8"))
    assert metadata["report_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
    assert metadata["notification_sha256"] == hashlib.sha256(notification.read_bytes()).hexdigest()
    assert metadata["publication_enabled"] is False

    ledger = json.loads(config.ledger_path.read_text(encoding="utf-8"))
    assert ledger["attempts"]["attempt-1"]["results_commit"]


def test_relay_rejects_source_mutation_after_ledger_binding(tmp_path: Path) -> None:
    remote = _results_remote(tmp_path)
    config = load_evidence_relay_config(_config(tmp_path, remote))
    report, _ = _evidence(config.supervisor_root)
    relay = SelfUpdateEvidenceRelay(config)
    relay.run_once()

    report.write_text(report.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(EvidenceRelayError, match="ledger evidence mismatch"):
        relay.run_once()


def test_relay_rejects_default_branch_and_escaping_report_path(tmp_path: Path) -> None:
    remote = _results_remote(tmp_path)
    config_path = _config(tmp_path, remote)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["evidence_branch"] = "main"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(EvidenceRelayError, match="non-default branch"):
        load_evidence_relay_config(config_path)

    raw["evidence_branch"] = "results"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    config = load_evidence_relay_config(config_path)
    outside = tmp_path / "outside.json"
    outside.write_text('{"attempt_id":"attempt-1","outcome":"success"}\n', encoding="utf-8")
    notification = config.notifications_root / "attempt-1.json"
    notification.parent.mkdir(parents=True)
    notification.write_text(
        json.dumps(
            {
                "attempt_id": "attempt-1",
                "outcome": "success",
                "report_path": str(outside),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceRelayError, match="escapes reports root"):
        SelfUpdateEvidenceRelay(config).run_once()
