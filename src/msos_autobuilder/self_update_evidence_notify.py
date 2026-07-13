"""Notify the canonical GitHub issue about relayed self-update evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SAFE_TEXT_RE = re.compile(r"[^A-Za-z0-9._-]+")


class EvidenceNotificationError(RuntimeError):
    """Raised when relayed evidence cannot be converted into a GitHub notification."""


@dataclass(frozen=True)
class EvidenceNotification:
    marker: str
    body: str


def _safe_text(value: Any, *, fallback: str) -> str:
    text = _SAFE_TEXT_RE.sub("-", str(value or "").strip()).strip(".-")
    return text[:160] or fallback


def _load_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceNotificationError(f"invalid JSON evidence: {path}") from exc
    if not isinstance(raw, dict):
        raise EvidenceNotificationError(f"JSON evidence must be an object: {path}")
    return raw


def build_notification(
    notification_path: Path,
    *,
    repository: str,
    evidence_branch: str = "results",
    evidence_root: Path,
) -> EvidenceNotification:
    notification = _load_object(notification_path)
    report_path = notification_path.parent / "update-report.json"
    report = _load_object(report_path)

    attempt_id = _safe_text(notification.get("attempt_id"), fallback="unknown-attempt")
    outcome = _safe_text(report.get("outcome") or notification.get("outcome"), fallback="unknown")
    requested_commit = _safe_text(
        report.get("requested_commit") or report.get("commit"),
        fallback="no-commit",
    )
    manifest_hash = _safe_text(report.get("manifest_sha256"), fallback="no-manifest")
    identity = requested_commit if requested_commit != "no-commit" else manifest_hash
    marker = f"<!-- msos-self-update:{identity}:{outcome} -->"

    try:
        relative_dir = notification_path.parent.resolve().relative_to(evidence_root.resolve())
    except ValueError as exc:
        raise EvidenceNotificationError(
            "notification path escapes the configured evidence root"
        ) from exc
    quoted_path = urllib.parse.quote(relative_dir.as_posix(), safe="/")
    evidence_url = f"https://github.com/{repository}/tree/{evidence_branch}/{quoted_path}"
    attention = bool(notification.get("requires_founder_attention", False))
    status = "requires founder attention" if attention else "recorded successfully"
    body = "\n".join(
        [
            "### Autobuilder self-update evidence",
            "",
            f"- **Outcome:** `{outcome}`",
            f"- **Requested commit:** `{requested_commit}`",
            f"- **Attempt:** `{attempt_id}`",
            f"- **Status:** {status}",
            f"- **Evidence:** {evidence_url}",
            "",
            marker,
        ]
    )
    return EvidenceNotification(marker=marker, body=body)


class GitHubIssueClient:
    def __init__(self, repository: str, issue_number: int, token: str) -> None:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
            raise EvidenceNotificationError("repository must use owner/name form")
        if issue_number <= 0:
            raise EvidenceNotificationError("issue_number must be positive")
        if not token:
            raise EvidenceNotificationError("GitHub token is required")
        self.repository = repository
        self.issue_number = issue_number
        self.token = token

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "msos-self-update-evidence-notifier/1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise EvidenceNotificationError(f"GitHub issue API request failed: {exc}") from exc

    def comment_bodies(self) -> tuple[str, ...]:
        bodies: list[str] = []
        page = 1
        while True:
            url = (
                f"https://api.github.com/repos/{self.repository}/issues/"
                f"{self.issue_number}/comments?per_page=100&page={page}"
            )
            payload = self._request(url)
            if not isinstance(payload, list):
                raise EvidenceNotificationError("GitHub issue comments response must be a list")
            bodies.extend(str(item.get("body") or "") for item in payload if isinstance(item, dict))
            if len(payload) < 100:
                return tuple(bodies)
            page += 1

    def post_comment(self, body: str) -> None:
        url = (
            f"https://api.github.com/repos/{self.repository}/issues/"
            f"{self.issue_number}/comments"
        )
        self._request(url, method="POST", payload={"body": body})


def discover_notifications(evidence_root: Path) -> tuple[Path, ...]:
    if not evidence_root.exists():
        return ()
    return tuple(sorted(evidence_root.glob("*/self-updates/*/notification.json")))


def notify_missing(
    paths: Iterable[Path],
    *,
    repository: str,
    issue_number: int,
    token: str,
    evidence_root: Path,
    evidence_branch: str = "results",
) -> tuple[str, ...]:
    client = GitHubIssueClient(repository, issue_number, token)
    existing = "\n".join(client.comment_bodies())
    posted: list[str] = []
    for path in paths:
        notification = build_notification(
            path,
            repository=repository,
            evidence_branch=evidence_branch,
            evidence_root=evidence_root,
        )
        if notification.marker in existing:
            continue
        client.post_comment(notification.body)
        existing += "\n" + notification.body
        posted.append(notification.marker)
    return tuple(posted)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--evidence-branch", default="results")
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    evidence_root = Path(args.evidence_root).resolve()
    token = os.environ.get(args.token_env, "")
    posted = notify_missing(
        discover_notifications(evidence_root),
        repository=args.repository,
        issue_number=args.issue_number,
        token=token,
        evidence_root=evidence_root,
        evidence_branch=args.evidence_branch,
    )
    print(json.dumps({"status": "completed", "posted_markers": list(posted)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
