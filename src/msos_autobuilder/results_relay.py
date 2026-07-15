"""Relay completed Autobuilder jobs to a review-only Git branch.

The relay is intentionally separate from product publication. It reconstructs complete
workspace patches (including untracked files), verifies them against Codex evidence, and
pushes immutable review artifacts to a non-product results branch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


class ResultsRelayError(RuntimeError):
    """Raised when a result cannot be reconstructed or relayed safely."""


@dataclass(frozen=True)
class ResultsRelayConfig:
    host_root: Path
    repo_url: str
    branch: str = "results"
    machine_id: str = ""
    poll_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.branch in {"main", "master"}:
            raise ValueError("results relay may not target a default product branch")
        if not self.repo_url.strip():
            raise ValueError("repo_url is required")
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_segment(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return cleaned[:96] or fallback


def _run_git(
    repo: Path | None,
    *args: str,
    env: dict[str, str] | None = None,
    accepted: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    command = ["git"]
    if repo is not None:
        command.extend(["-C", str(repo)])
    command.extend(args)
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        env=env,
        check=False,
    )
    if proc.returncode not in accepted:
        detail = (proc.stderr or proc.stdout or "git command failed").strip()
        raise ResultsRelayError(detail)
    return proc


def _changed_paths(repo: Path) -> tuple[str, ...]:
    paths: set[str] = set()
    for args in (
        ("diff", "--name-only", "--relative"),
        ("diff", "--cached", "--name-only", "--relative"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        output = _run_git(repo, *args).stdout
        paths.update(line for line in output.splitlines() if line)
    return tuple(sorted(path.replace("\\", "/") for path in paths))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_patch_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def _load_workspace_root(host_root: Path) -> Path:
    host_config = host_root / "host.yaml"
    if not host_config.exists():
        raise ResultsRelayError(f"Codex host config not found: {host_config}")
    raw = yaml.safe_load(host_config.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ResultsRelayError("Codex host config must be a mapping")
    workspace_root = str(raw.get("workspace_root") or "").strip()
    if not workspace_root:
        raise ResultsRelayError("Codex host config is missing workspace_root")
    return Path(workspace_root).expanduser().resolve()


def build_complete_patch(workspace: Path) -> tuple[str, tuple[str, ...]]:
    """Return a binary-capable patch that includes tracked and untracked changes."""

    workspace = workspace.resolve()
    if not (workspace / ".git").exists():
        raise ResultsRelayError(f"workspace is not a Git checkout: {workspace}")

    changed_paths = _changed_paths(workspace)
    if not changed_paths:
        return "", ()

    descriptor, raw_index_path = tempfile.mkstemp(
        prefix="msos-autobuilder-relay-index-",
        suffix=".tmp",
    )
    os.close(descriptor)
    index_path = Path(raw_index_path)
    index_path.unlink(missing_ok=True)
    environment = dict(os.environ)
    environment["GIT_INDEX_FILE"] = str(index_path)
    try:
        _run_git(workspace, "read-tree", "HEAD", env=environment)
        _run_git(workspace, "add", "-N", "--all", env=environment)
        patch = _run_git(
            workspace,
            "diff",
            "--binary",
            "--no-ext-diff",
            "HEAD",
            env=environment,
        ).stdout
    finally:
        index_path.unlink(missing_ok=True)
        Path(f"{index_path}.lock").unlink(missing_ok=True)

    if not patch:
        raise ResultsRelayError(
            f"workspace reports {len(changed_paths)} changed path(s) but produced no patch"
        )
    return patch, changed_paths


def _evidence_by_task(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    codex_report = report.get("codex_report")
    if not isinstance(codex_report, dict):
        raise ResultsRelayError("report is missing codex_report")
    evidence = codex_report.get("evidence")
    if not isinstance(evidence, list):
        raise ResultsRelayError("report codex_report.evidence must be a list")
    result: dict[str, dict[str, Any]] = {}
    for item in evidence:
        if not isinstance(item, dict):
            raise ResultsRelayError("report evidence entries must be mappings")
        task_id = str(item.get("task_id") or "").strip()
        if not task_id or task_id in result:
            raise ResultsRelayError("report evidence contains a missing or duplicate task_id")
        result[task_id] = item
    return result


def reconstruct_job(
    job_dir: Path,
    workspace_root: Path,
    destination: Path,
    machine_id: str,
) -> None:
    """Copy a completed job and replace incomplete patches with verified full patches."""

    report_path = job_dir / "report.json"
    job_path = job_dir / "job.yaml"
    if not report_path.exists() or not job_path.exists():
        raise ResultsRelayError(f"completed job is missing report.json or job.yaml: {job_dir}")

    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("outcome") != "completed":
        raise ResultsRelayError(f"completed job has an invalid report: {job_dir}")

    patch_entries = report.get("patches")
    if not isinstance(patch_entries, list):
        raise ResultsRelayError("report patches must be a list")
    evidence = _evidence_by_task(report)

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    shutil.copy2(job_path, destination / "job.yaml")
    shutil.copy2(report_path, destination / "source-report.json")
    source_report_sha256 = _sha256_file(destination / "source-report.json")

    completed_patches: list[dict[str, Any]] = []
    canonical_patch_hashes: dict[str, str] = {}
    for raw_entry in patch_entries:
        if not isinstance(raw_entry, dict):
            raise ResultsRelayError("report patch entries must be mappings")
        entry = dict(raw_entry)
        task_id = str(entry.get("task_id") or "").strip()
        lane_id = str(entry.get("lane_id") or "").strip()
        if not task_id or not lane_id or task_id not in evidence:
            raise ResultsRelayError("patch entry does not match report evidence")

        expected_paths = tuple(
            sorted(
                str(path).replace("\\", "/")
                for path in evidence[task_id].get("changed_paths", [])
            )
        )
        workspace = workspace_root / lane_id
        patch_text, actual_paths = build_complete_patch(workspace)
        normalized_actual = tuple(sorted(path.replace("\\", "/") for path in actual_paths))
        if normalized_actual != expected_paths:
            raise ResultsRelayError(
                f"workspace drift for {task_id}: expected {list(expected_paths)}, "
                f"found {list(normalized_actual)}"
            )

        patch_name = f"{task_id}.patch"
        if patch_text:
            patch_path = destination / "patches" / patch_name
            _atomic_write_text(patch_path, patch_text)
            canonical_patch_sha256 = _sha256_bytes(_canonical_patch_bytes(patch_path))
            entry["patch_file"] = f"patches/{patch_name}"
            entry["patch_sha256"] = canonical_patch_sha256
            canonical_patch_hashes[task_id] = canonical_patch_sha256
        else:
            entry["patch_file"] = None
            entry["patch_sha256"] = None
        entry["changed_paths"] = list(normalized_actual)
        entry["complete_patch"] = True
        completed_patches.append(entry)

    report["patches"] = completed_patches
    report["relay"] = {
        "version": 1,
        "machine_id": machine_id,
        "relayed_at": _utc_now(),
        "complete_patch_reconstruction": True,
        "source_report_role": "original-worker-report-noncanonical-for-patch-identity",
        "canonical_report_role": "relay-corrected-canonical-downstream-report",
        "source_report_sha256": source_report_sha256,
        "canonical_patch_sha256_by_task": canonical_patch_hashes,
        "publication_enabled": False,
    }
    _atomic_write_json(destination / "report.json", report)
    integrity = {
        "version": 1,
        "source_report_sha256": source_report_sha256,
        "corrected_report_sha256": _sha256_file(destination / "report.json"),
        "canonical_patch_sha256_by_task": canonical_patch_hashes,
        "publication_enabled": False,
    }
    _atomic_write_json(destination / "result-integrity.json", integrity)


class GitResultsSink:
    """A single-branch Git sink that cannot target main/master."""

    def __init__(self, config: ResultsRelayConfig) -> None:
        self.config = config
        self.checkout = config.host_root / "state" / "results-relay-repo"

    def _prepare(self) -> None:
        if not (self.checkout / ".git").exists():
            if self.checkout.exists():
                shutil.rmtree(self.checkout)
            self.checkout.parent.mkdir(parents=True, exist_ok=True)
            _run_git(
                None,
                "clone",
                "--single-branch",
                "--branch",
                self.config.branch,
                "--no-tags",
                self.config.repo_url,
                str(self.checkout),
            )
        else:
            _run_git(self.checkout, "fetch", "--no-tags", "origin", self.config.branch)
            ahead = int(
                _run_git(
                    self.checkout,
                    "rev-list",
                    "--count",
                    f"origin/{self.config.branch}..HEAD",
                ).stdout.strip()
                or "0"
            )
            if ahead:
                _run_git(self.checkout, "push", "origin", f"HEAD:{self.config.branch}")
                _run_git(self.checkout, "fetch", "--no-tags", "origin", self.config.branch)
            _run_git(
                self.checkout,
                "checkout",
                "-B",
                self.config.branch,
                f"origin/{self.config.branch}",
            )
            _run_git(self.checkout, "reset", "--hard", f"origin/{self.config.branch}")
            _run_git(self.checkout, "clean", "-fd")

        _run_git(self.checkout, "config", "user.name", "MSOS Autobuilder Result Relay")
        _run_git(self.checkout, "config", "user.email", "autobuilder-relay@localhost")

    def publish(self, staging_job: Path, job_id: str, machine_id: str) -> str:
        self._prepare()
        relative = Path("results") / machine_id / job_id
        destination = self.checkout / relative
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staging_job, destination)

        _run_git(self.checkout, "add", "--", relative.as_posix())
        changed = _run_git(
            self.checkout,
            "diff",
            "--cached",
            "--quiet",
            accepted=(0, 1),
        ).returncode
        if changed == 0:
            return _run_git(self.checkout, "rev-parse", "HEAD").stdout.strip()

        _run_git(self.checkout, "commit", "-m", f"Relay Autobuilder result {job_id}")
        commit = _run_git(self.checkout, "rev-parse", "HEAD").stdout.strip()
        _run_git(self.checkout, "push", "origin", f"HEAD:{self.config.branch}")
        return commit


class ResultsRelay:
    def __init__(self, config: ResultsRelayConfig) -> None:
        self.config = config
        self.host_root = config.host_root.expanduser().resolve()
        self.machine_id = _safe_segment(
            config.machine_id or socket.gethostname(),
            fallback="windows-host",
        )
        self.completed = self.host_root / "queue" / "completed"
        self.state = self.host_root / "state"
        self.staging = self.state / "results-relay-staging"
        self.ledger_path = self.state / "results-relay-seen.json"
        self.workspace_root = _load_workspace_root(self.host_root)
        self.sink = GitResultsSink(config)

    def _load_ledger(self) -> dict[str, str]:
        if not self.ledger_path.exists():
            return {}
        raw = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
        ):
            raise ResultsRelayError("results relay ledger must map job IDs to commit SHAs")
        return raw

    def _save_ledger(self, ledger: dict[str, str]) -> None:
        _atomic_write_json(self.ledger_path, ledger)

    def run_once(self) -> tuple[str, ...]:
        self.completed.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        ledger = self._load_ledger()
        relayed: list[str] = []
        for job_dir in sorted(path for path in self.completed.iterdir() if path.is_dir()):
            job_id = _safe_segment(job_dir.name, fallback="job")
            if job_id in ledger:
                continue
            staging_job = self.staging / job_id
            reconstruct_job(
                job_dir,
                self.workspace_root,
                staging_job,
                self.machine_id,
            )
            commit = self.sink.publish(staging_job, job_id, self.machine_id)
            ledger[job_id] = commit
            self._save_ledger(ledger)
            relayed.append(job_id)
        return tuple(relayed)

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except ResultsRelayError as exc:
                error_path = self.state / "results-relay-error.json"
                _atomic_write_json(
                    error_path,
                    {
                        "recorded_at": _utc_now(),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "publication_enabled": False,
                    },
                )
            time.sleep(self.config.poll_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="msos-autobuilder-results-relay")
    parser.add_argument("--host-root", required=True)
    parser.add_argument("--repo-url", required=True)
    parser.add_argument("--branch", default="results")
    parser.add_argument("--machine-id", default="")
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = ResultsRelayConfig(
        host_root=Path(args.host_root),
        repo_url=args.repo_url,
        branch=args.branch,
        machine_id=args.machine_id,
        poll_seconds=args.poll_seconds,
    )
    relay = ResultsRelay(config)
    if args.once:
        relayed = relay.run_once()
        print(
            json.dumps(
                {
                    "status": "completed",
                    "relayed_jobs": list(relayed),
                    "publication_enabled": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    relay.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
