"""Repo-local immutable approved job packets for Autobuilder admission."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .managed_source import normalize_github_repository


class JobPacketError(ValueError):
    """Raised when an approved job packet is missing, ambiguous, or redirected."""


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_CATALOG_RELPATH = "jobs/catalog"


@dataclass(frozen=True)
class ApprovedJobPacket:
    version: int
    pipeline_id: str
    work_item_id: str
    order: int
    eligible: bool
    target_repository: str
    target_source_commit: str
    target_remote_url: str
    adapter: str
    allowed_paths: tuple[str, ...]
    native_slice: Mapping[str, Any]
    prerequisites: Mapping[str, Any]
    phase_plan: str | None
    dependency_source_sha256: str
    work_item_source_sha256_v1: str
    authority: Mapping[str, Any]
    validation: Mapping[str, Any]
    packet_sha256: str
    source_path: str
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class AdmittedTargetIdentity:
    target_repository: str
    target_source_commit: str
    target_remote_url: str
    work_item_id: str
    pipeline_id: str
    allowed_paths: tuple[str, ...]
    adapter: str
    dependency_source_sha256: str
    validation_identity: str
    packet_sha256: str


def _sha256_json(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _required_str(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    text = str(value or "").strip()
    if not text or not isinstance(value, str):
        raise JobPacketError(f"approved job packet {key} is missing")
    return text


def _safe_relative_path(value: Any, label: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise JobPacketError(f"{label} must be a safe relative path")
    return path.as_posix()


def _required_commit(value: Any) -> str:
    text = str(value or "").replace("\ufeff", "").strip().lower()
    if not _COMMIT_RE.fullmatch(text):
        raise JobPacketError("target_source_commit must be an exact 40-character commit SHA")
    return text


def _required_repository(value: Any) -> str:
    text = str(value or "").strip()
    if not _REPO_RE.fullmatch(text):
        raise JobPacketError("target_repository is missing or malformed")
    return text


def _required_target_remote_url(
    target_repository: str,
    remote_url: Any,
    *,
    default_if_missing: bool = True,
) -> str:
    repository = _required_repository(target_repository)
    text = str(remote_url or "").strip()
    if not text:
        if not default_if_missing:
            raise JobPacketError("target remote URL is missing")
        text = f"https://github.com/{repository}.git"
    fetched_repository = normalize_github_repository(text)
    if fetched_repository is None:
        return text
    if fetched_repository != repository:
        raise JobPacketError("target_remote_url does not match target_repository")
    return text


def _required_id(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not _ID_RE.fullmatch(text):
        raise JobPacketError(f"{label} is missing or malformed")
    return text


def _required_work_item_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or any(char in text for char in "\r\n\x00") or len(text) > 256:
        raise JobPacketError("work_item_id is missing or malformed")
    return text


def _required_sha256(value: Any, label: str) -> str:
    text = str(value or "").replace("\ufeff", "").strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise JobPacketError(f"{label} is missing or malformed")
    return text


def _required_paths(raw: Any, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise JobPacketError(f"{label} must be a non-empty list")
    paths = tuple(_safe_relative_path(item, label) for item in raw)
    if len(set(paths)) != len(paths):
        raise JobPacketError(f"{label} has duplicate paths")
    return paths


def _mapping(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise JobPacketError(f"{label} must be an object")
    return dict(raw)


def canonical_packet_identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "adapter": raw.get("adapter"),
        "allowed_paths": raw.get("allowed_paths"),
        "dependency_source_sha256": raw.get("dependency_source_sha256"),
        "native_slice": raw.get("native_slice"),
        "pipeline_id": raw.get("pipeline_id"),
        "target_repository": raw.get("target_repository"),
        "target_remote_url": raw.get("target_remote_url"),
        "target_source_commit": raw.get("target_source_commit"),
        "work_item_id": raw.get("work_item_id"),
        "validation": raw.get("validation"),
    }


def parse_approved_job_packet(
    raw: Mapping[str, Any],
    *,
    source_path: str = "packet",
) -> ApprovedJobPacket:
    if raw.get("version") != 1:
        raise JobPacketError("approved job packet must be version 1")
    pipeline_id = _required_id(raw.get("pipeline_id"), "pipeline_id")
    work_item_id = _required_work_item_id(raw.get("work_item_id"))
    order = raw.get("order", 0)
    if not isinstance(order, int) or isinstance(order, bool) or order < 0:
        raise JobPacketError("approved job packet order must be a non-negative integer")
    eligible = raw.get("eligible", True)
    if not isinstance(eligible, bool):
        raise JobPacketError("approved job packet eligible must be a boolean")
    target_repository = _required_repository(raw.get("target_repository"))
    target_source_commit = _required_commit(raw.get("target_source_commit"))
    target_remote_url = _required_target_remote_url(
        target_repository,
        raw.get("target_remote_url"),
    )
    adapter = _required_id(raw.get("adapter"), "adapter")
    allowed_paths = _required_paths(raw.get("allowed_paths"), "allowed_paths")
    native_slice = _mapping(raw.get("native_slice"), "native_slice")
    if not str(native_slice.get("slice_id") or native_slice.get("sliceId") or "").strip():
        raise JobPacketError("native_slice is missing slice identity")
    prerequisites = (
        _mapping(raw.get("prerequisites"), "prerequisites")
        if raw.get("prerequisites") is not None
        else {}
    )
    phase_plan = None
    if raw.get("phase_plan") not in (None, ""):
        phase_plan = _safe_relative_path(raw.get("phase_plan"), "phase_plan")
    dependency_source_sha256 = _required_sha256(
        raw.get("dependency_source_sha256"),
        "dependency_source_sha256",
    )
    authority = _mapping(raw.get("authority"), "authority") if raw.get("authority") else {
        "publication_enabled": False,
        "merge_enabled": False,
        "product_main_write_enabled": False,
    }
    if (
        authority.get("merge_enabled") is True
        or authority.get("product_main_write_enabled") is True
    ):
        raise JobPacketError("approved job packet must not enable merge or product-main write")
    if authority.get("publication_enabled") is True:
        raise JobPacketError("approved job packet must not enable publication")
    validation = _mapping(raw.get("validation"), "validation") if raw.get("validation") else {}
    identity = canonical_packet_identity(
        {
            **dict(raw),
            "adapter": adapter,
            "allowed_paths": list(allowed_paths),
            "dependency_source_sha256": dependency_source_sha256,
            "native_slice": native_slice,
            "pipeline_id": pipeline_id,
            "target_repository": target_repository,
            "target_remote_url": target_remote_url,
            "target_source_commit": target_source_commit,
            "work_item_id": work_item_id,
            "validation": validation,
        }
    )
    work_item_source = raw.get("work_item_source_sha256_v1")
    work_item_source_sha256 = (
        _required_sha256(work_item_source, "work_item_source_sha256_v1")
        if work_item_source not in (None, "")
        else _sha256_json(identity)
    )
    packet_sha256 = _sha256_json(
        {
            **identity,
            "order": order,
            "eligible": eligible,
            "authority": authority,
            "prerequisites": prerequisites,
            "phase_plan": phase_plan,
            "work_item_source_sha256_v1": work_item_source_sha256,
        }
    )
    return ApprovedJobPacket(
        version=1,
        pipeline_id=pipeline_id,
        work_item_id=work_item_id,
        order=order,
        eligible=eligible,
        target_repository=target_repository,
        target_source_commit=target_source_commit,
        target_remote_url=target_remote_url,
        adapter=adapter,
        allowed_paths=allowed_paths,
        native_slice=native_slice,
        prerequisites=prerequisites,
        phase_plan=phase_plan,
        dependency_source_sha256=dependency_source_sha256,
        work_item_source_sha256_v1=work_item_source_sha256,
        authority=authority,
        validation=validation,
        packet_sha256=packet_sha256,
        source_path=source_path,
        raw=dict(raw),
    )


def load_packet_dir(root: Path) -> tuple[ApprovedJobPacket, ...]:
    if not root.exists():
        return ()
    if not root.is_dir():
        raise JobPacketError(f"job packet catalog is not a directory: {root}")
    packets: list[ApprovedJobPacket] = []
    for path in sorted(root.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JobPacketError(f"approved job packet is not valid JSON: {path}") from exc
        if not isinstance(raw, dict):
            raise JobPacketError(f"approved job packet must be an object: {path}")
        packets.append(parse_approved_job_packet(raw, source_path=path.as_posix()))
    return tuple(packets)


def select_next_packet(
    packets: Sequence[ApprovedJobPacket],
    *,
    exclude_work_item_ids: Sequence[str] = (),
) -> ApprovedJobPacket | None:
    excluded = {str(item).strip() for item in exclude_work_item_ids if str(item).strip()}
    eligible = [packet for packet in packets if packet.eligible]
    seen: dict[str, str] = {}
    for packet in eligible:
        key = f"{packet.pipeline_id}:{packet.work_item_id}"
        if key in seen:
            raise JobPacketError(
                f"duplicate work item {packet.work_item_id!r} is not exactly one READY item"
            )
        seen[key] = packet.packet_sha256
    ranked = [
        packet
        for packet in eligible
        if packet.work_item_id not in excluded
    ]
    ranked.sort(key=lambda packet: (packet.order, packet.pipeline_id, packet.work_item_id))
    return ranked[0] if ranked else None


def freeze_admitted_identity(packet: ApprovedJobPacket) -> AdmittedTargetIdentity:
    validation_identity = _sha256_json(
        {
            "adapter": packet.adapter,
            "allowed_paths": list(packet.allowed_paths),
            "dependency_source_sha256": packet.dependency_source_sha256,
            "native_slice": dict(packet.native_slice),
            "validation": dict(packet.validation),
        }
    )
    return AdmittedTargetIdentity(
        target_repository=packet.target_repository,
        target_source_commit=packet.target_source_commit,
        target_remote_url=packet.target_remote_url,
        work_item_id=packet.work_item_id,
        pipeline_id=packet.pipeline_id,
        allowed_paths=packet.allowed_paths,
        adapter=packet.adapter,
        dependency_source_sha256=packet.dependency_source_sha256,
        validation_identity=validation_identity,
        packet_sha256=packet.packet_sha256,
    )


def assert_identity_not_redirected(
    admitted: AdmittedTargetIdentity,
    packet: ApprovedJobPacket,
) -> None:
    current = freeze_admitted_identity(packet)
    if (
        current.target_repository != admitted.target_repository
        or current.target_source_commit != admitted.target_source_commit
        or current.target_remote_url != admitted.target_remote_url
        or current.work_item_id != admitted.work_item_id
        or current.pipeline_id != admitted.pipeline_id
        or current.packet_sha256 != admitted.packet_sha256
        or current.validation_identity != admitted.validation_identity
        or current.allowed_paths != admitted.allowed_paths
    ):
        raise JobPacketError(
            "target identity cannot redirect after immutable admission identity is established"
        )


def _git(repo: Path | None, *args: str, accepted: tuple[int, ...] = (0,)) -> str:
    argv = ["git"]
    if repo is not None:
        argv.extend(["-C", str(repo)])
    argv.extend(args)
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if proc.returncode not in accepted:
        detail = (proc.stderr or proc.stdout or "command failed").strip()
        raise JobPacketError(f"{' '.join(argv)}: {detail}")
    return proc.stdout.strip()


def prove_declared_commit_fetchable(
    *,
    target_repository: str,
    target_source_commit: str,
    remote_url: str,
) -> str:
    """Prove the frozen commit is still fetchable without following target main."""
    _required_repository(target_repository)
    commit = _required_commit(target_source_commit)
    url = _required_target_remote_url(
        target_repository,
        remote_url,
        default_if_missing=False,
    )
    local = Path(url)
    if local.exists():
        git_dir = local / ".git"
        if git_dir.exists():
            kind = _git(local, "cat-file", "-t", commit)
        else:
            kind = _git(None, "--git-dir", str(local), "cat-file", "-t", commit)
        if kind != "commit":
            raise JobPacketError(
                f"declared target commit {commit} is not a commit object on {url}"
            )
        return commit
    # Remote URLs are bound by packet identity; exact-commit fetch happens at execution
    # and must not follow a moving target branch.
    return commit


def fetch_declared_target(
    *,
    target_repository: str,
    target_source_commit: str,
    destination: Path,
    remote_url: str,
) -> str:
    """Fetch only the declared repository at the declared exact commit after admission."""
    _required_repository(target_repository)
    commit = _required_commit(target_source_commit)
    url = _required_target_remote_url(
        target_repository,
        remote_url,
        default_if_missing=False,
    )
    prove_declared_commit_fetchable(
        target_repository=target_repository,
        target_source_commit=commit,
        remote_url=url,
    )
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _git(None, "clone", "--no-tags", url, str(destination))
    cloned_origin = _git(destination, "remote", "get-url", "origin")
    cloned_repository = normalize_github_repository(cloned_origin)
    if cloned_repository is not None and cloned_repository != target_repository:
        raise JobPacketError(
            "fetched target origin does not match admitted target_repository"
        )
    _git(destination, "checkout", "--detach", commit)
    head = _git(destination, "rev-parse", "HEAD").lower()
    if head != commit:
        raise JobPacketError(
            f"fetched target HEAD {head} does not match frozen commit {commit}"
        )
    branch = _git(destination, "symbolic-ref", "-q", "--short", "HEAD", accepted=(0, 1))
    if branch:
        raise JobPacketError(
            "admitted target checkout followed a moving branch instead of the frozen commit"
        )
    return head
