from __future__ import annotations

import json
from pathlib import Path

from msos_autobuilder.self_update_evidence_notify import (
    build_notification,
    discover_notifications,
)


def test_notification_builds_deduplicated_issue_marker_and_evidence_link(tmp_path: Path) -> None:
    root = tmp_path / "results"
    attempt = root / "fixture-machine" / "self-updates" / "attempt-1"
    attempt.mkdir(parents=True)
    (attempt / "update-report.json").write_text(
        json.dumps(
            {
                "attempt_id": "attempt-1",
                "outcome": "rolled_back",
                "requested_commit": "a" * 40,
                "manifest_sha256": "b" * 64,
            }
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
            }
        )
        + "\n",
        encoding="utf-8",
    )

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
