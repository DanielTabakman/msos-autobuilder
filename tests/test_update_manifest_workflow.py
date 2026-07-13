from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "publish-approved-update-manifest.yml"
)


def test_manifest_publication_requires_reviewed_main_request_and_has_bounded_authority() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)

    assert raw["permissions"] == {"contents": "write"}
    assert "pull_request" not in text
    assert "branches: [main]" in text
    assert '"updates/requests/*.yaml"' in text
    assert "Expected exactly one reviewed update request" in text
    assert "refs/heads/*:refs/remotes/origin/*" in text
    assert "update_manifest_publisher" in text
    assert "--self-commit \"$TARGET_SHA\"" in text
    assert "git -C \"$worktree\" push origin HEAD:updates" in text
    assert "--force" not in text
    assert "issues: write" not in text
    assert "pull-requests: write" not in text
