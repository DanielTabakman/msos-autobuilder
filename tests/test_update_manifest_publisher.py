from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from msos_autobuilder.self_update_supervisor import parse_update_manifest
from msos_autobuilder.update_manifest_publisher import (
    ReleaseRequestError,
    build_manifest,
    parse_release_request,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.com")
    (repo / "src" / "msos_autobuilder").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (repo / "src" / "msos_autobuilder" / "self_update_supervisor.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _request(commit: str = "self") -> str:
    return yaml.safe_dump(
        {
            "version": 1,
            "release_id": "healthy-witness-v1",
            "approved": True,
            "repository": "DanielTabakman/msos-autobuilder",
            "repo_url": "https://github.com/DanielTabakman/msos-autobuilder.git",
            "commit": commit,
            "required_status_contexts": ["test"],
            "expected_files": [
                "pyproject.toml",
                "src/msos_autobuilder/self_update_supervisor.py",
                "README.md",
            ],
            "supervisor_update": False,
        },
        sort_keys=False,
    )


def test_build_manifest_hashes_exact_commit_and_self_validates(tmp_path: Path) -> None:
    repo, commit = _repository(tmp_path)
    request = parse_release_request(_request())

    manifest = build_manifest(request, repo_root=repo, self_commit=commit)

    parsed = parse_update_manifest(yaml.safe_dump(manifest, sort_keys=False))
    assert parsed.commit == commit
    assert parsed.release_id == "healthy-witness-v1"
    assert parsed.required_status_contexts == ("test",)
    assert {item.path for item in parsed.expected_files} == {
        "pyproject.toml",
        "src/msos_autobuilder/self_update_supervisor.py",
        "README.md",
    }
    assert len({item.sha256 for item in parsed.expected_files}) == 3


def test_explicit_reviewed_commit_may_differ_from_request_merge_commit(tmp_path: Path) -> None:
    repo, target_commit = _repository(tmp_path)
    (repo / "README.md").write_text("request merge\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "request merge")
    request_commit = _git(repo, "rev-parse", "HEAD")

    manifest = build_manifest(
        parse_release_request(_request(target_commit)),
        repo_root=repo,
        self_commit=request_commit,
    )

    assert manifest["commit"] == target_commit


def test_request_rejects_missing_anchor_and_unapproved_or_unsafe_commit() -> None:
    raw = yaml.safe_load(_request())
    raw["expected_files"] = ["pyproject.toml"]
    with pytest.raises(ReleaseRequestError, match="missing required release anchors"):
        parse_release_request(yaml.safe_dump(raw))

    raw = yaml.safe_load(_request())
    raw["approved"] = False
    with pytest.raises(ReleaseRequestError, match="explicitly approved"):
        parse_release_request(yaml.safe_dump(raw))

    raw = yaml.safe_load(_request())
    raw["commit"] = "main"
    with pytest.raises(ReleaseRequestError, match="exact lowercase Git SHA"):
        parse_release_request(yaml.safe_dump(raw))
