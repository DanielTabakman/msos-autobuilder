"""Build an approved exact-commit update manifest from a reviewed release request."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .self_update_supervisor import compute_manifest_sha256, parse_update_manifest

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REQUIRED_PATHS = {
    "pyproject.toml",
    "src/msos_autobuilder/self_update_supervisor.py",
}


class ReleaseRequestError(ValueError):
    """Raised when a reviewed update request cannot safely produce a manifest."""


@dataclass(frozen=True)
class ReleaseRequest:
    release_id: str
    repository: str
    repo_url: str
    commit: str
    required_status_contexts: tuple[str, ...]
    expected_files: tuple[str, ...]


def _safe_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseRequestError(f"{field} must be a mapping")
    return dict(value)


def _safe_relative_path(value: Any, field: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    candidate = Path(text)
    if not text or candidate.is_absolute() or ".." in candidate.parts:
        raise ReleaseRequestError(f"{field} must be a safe relative path")
    return text


def parse_release_request(text: str) -> ReleaseRequest:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ReleaseRequestError("invalid release request YAML") from exc
    raw = _safe_mapping(loaded, "release request")
    if raw.get("version") != 1:
        raise ReleaseRequestError("only release request version 1 is supported")
    if raw.get("approved") is not True:
        raise ReleaseRequestError("release request must be explicitly approved")
    if raw.get("supervisor_update", False) is not False:
        raise ReleaseRequestError("release request may not replace the stable supervisor")

    release_id = str(raw.get("release_id") or "").strip()
    if not _SAFE_ID_RE.fullmatch(release_id):
        raise ReleaseRequestError("release_id is missing or unsafe")
    repository = str(raw.get("repository") or "").strip()
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
        raise ReleaseRequestError("repository must use owner/name form")
    repo_url = str(raw.get("repo_url") or "").strip()
    if not repo_url:
        raise ReleaseRequestError("repo_url is required")
    commit = str(raw.get("commit") or "self").strip()
    if commit != "self" and not _COMMIT_RE.fullmatch(commit):
        raise ReleaseRequestError("commit must be 'self' or an exact lowercase Git SHA")

    contexts_raw = raw.get("required_status_contexts")
    if not isinstance(contexts_raw, list) or not contexts_raw:
        raise ReleaseRequestError("required_status_contexts must be a non-empty list")
    contexts: list[str] = []
    for index, item in enumerate(contexts_raw):
        context = str(item or "").strip()
        if not context:
            raise ReleaseRequestError(f"required_status_contexts[{index}] is empty")
        if context in contexts:
            raise ReleaseRequestError(f"duplicate status context: {context}")
        contexts.append(context)

    expected_raw = raw.get("expected_files")
    if not isinstance(expected_raw, list) or not expected_raw:
        raise ReleaseRequestError("expected_files must be a non-empty list")
    expected_files: list[str] = []
    for index, item in enumerate(expected_raw):
        path = _safe_relative_path(item, f"expected_files[{index}]")
        if path in expected_files:
            raise ReleaseRequestError(f"duplicate expected file path: {path}")
        expected_files.append(path)
    missing = sorted(_REQUIRED_PATHS - set(expected_files))
    if missing:
        raise ReleaseRequestError(
            "expected_files is missing required release anchors: " + ", ".join(missing)
        )

    return ReleaseRequest(
        release_id=release_id,
        repository=repository,
        repo_url=repo_url,
        commit=commit,
        required_status_contexts=tuple(contexts),
        expected_files=tuple(expected_files),
    )


def _run_git(repo_root: Path, *args: str, binary: bool = False) -> bytes | str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=not binary,
        encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
        shell=False,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        stderr = completed.stderr
        detail = stderr.decode("utf-8", errors="replace") if binary else str(stderr or "")
        raise ReleaseRequestError(f"git {' '.join(args)} failed: {detail.strip()}")
    return completed.stdout


def resolve_commit(repo_root: Path, requested: str, self_commit: str) -> str:
    candidate = self_commit if requested == "self" else requested
    if not _COMMIT_RE.fullmatch(candidate):
        raise ReleaseRequestError("resolved commit must be an exact lowercase Git SHA")
    resolved = str(_run_git(repo_root, "rev-parse", "--verify", f"{candidate}^{{commit}}")).strip()
    if resolved != candidate:
        raise ReleaseRequestError("resolved commit identity differs from the approved request")
    return resolved


def _blob_sha256(repo_root: Path, commit: str, path: str) -> str:
    content = _run_git(repo_root, "show", f"{commit}:{path}", binary=True)
    assert isinstance(content, bytes)
    return hashlib.sha256(content).hexdigest()


def build_manifest(
    request: ReleaseRequest,
    *,
    repo_root: Path,
    self_commit: str,
) -> dict[str, Any]:
    commit = resolve_commit(repo_root, request.commit, self_commit)
    expected_files = [
        {"path": path, "sha256": _blob_sha256(repo_root, commit, path)}
        for path in request.expected_files
    ]
    manifest: dict[str, Any] = {
        "version": 1,
        "release_id": request.release_id,
        "approved": True,
        "repository": request.repository,
        "repo_url": request.repo_url,
        "commit": commit,
        "required_status_contexts": list(request.required_status_contexts),
        "expected_files": expected_files,
        "supervisor_update": False,
    }
    manifest["manifest_sha256"] = compute_manifest_sha256(manifest)
    parse_update_manifest(yaml.safe_dump(manifest, sort_keys=False))
    return manifest


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(dict(manifest), sort_keys=False), encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--self-commit", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    request_path = Path(args.request).resolve()
    request = parse_release_request(request_path.read_text(encoding="utf-8"))
    manifest = build_manifest(
        request,
        repo_root=Path(args.repo_root).resolve(),
        self_commit=args.self_commit,
    )
    write_manifest(Path(args.output).resolve(), manifest)
    print(
        json.dumps(
            {
                "status": "completed",
                "release_id": manifest["release_id"],
                "commit": manifest["commit"],
                "manifest_sha256": manifest["manifest_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
