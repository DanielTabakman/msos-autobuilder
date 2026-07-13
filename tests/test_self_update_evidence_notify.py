from __future__ import annotations

import hashlib
import json
from pathlib import Path

from msos_autobuilder.self_update_evidence_notify import (
    EvidenceNotificationError,
    build_notification,
    discover_notifications,
)


def _write_fixture(root: Path) -> Path:
    attempt = root / "fixture-machine" / "self-updates" / "attempt-1"
    attempt.mkdir(parents=True)
    report_path = attempt / "update-report.json"
    report_path.write_text(
        json.dumps(
            {
                "attempt_id": "attempt-1",
                "outcome": "rolled_back",
                "requested_commit": "a" * 40,
                "manifest_sha256": "b" * 64,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    notification_path = attempt / "notification.json"
    notification_path.write_text(
        json.dumps(
            {
                "attempt_id": "attempt-1",
                "outcome": "rolled_back",
                "requires_founder_attention": True,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (attempt / "relay.json").write_text(
        json.dumps(
            {
                "version": 1,
                "attempt_id": "attempt-1",
                "outcome": "rolled_back",
                "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                "notification_sha256": hashlib.sha256(
                    notification_path.read_bytes()
                ).hexdigest(),
                "publication_enabled": False,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return notification_path


def test_notification_builds_deduplicated_issue_marker_and_evidence_link(tmp_path: Path) -> None:
    root = tmp_path / "results"
    notification_path = _write_fixture(root)

    assert discover_notifications(root) == (notification_path,)
    notification = build_notification(
        notification_path,
        repository="DanielTabakman/msos-autobuilder",
        evidence_root=root,
    )
    assert notification.marker == f"<!-- msos-self-update:{'a' * 40}:rolled_back -->"
    assert "requires founder attention" in notification.body
    assert (
        "https://github.com/DanielTabakman/msos-autobuilder/tree/results/"
        "fixture-machine/self-updates/attempt-1"
    ) in notification.body
    assert notification.marker in notification.body


def test_notification_rejects_tampered_results_branch_evidence(tmp_path: Path) -> None:
    root = tmp_path / "results"
    notification_path = _write_fixture(root)
    report_path = notification_path.parent / "update-report.json"
    report_path.write_text(report_path.read_text(encoding="utf-8") + " ", encoding="utf-8")

    import pytest

    with pytest.raises(EvidenceNotificationError, match="SHA-256 bindings"):
        build_notification(
            notification_path,
            repository="DanielTabakman/msos-autobuilder",
            evidence_root=root,
        )
