"""Controlled publisher for passed MSOS Autobuilder candidates.

The publisher is intentionally narrow: it can create one product branch, one commit, and
one draft pull request for a configured passed candidate. It cannot write the product base
branch, force-push, mark a pull request ready, or merge.
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
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .candidate_gate import GateCheck, _atomic_write_json, _bounded, _safe_segment, run_check


class PublisherError(RuntimeError):
    """Raised when publication evidence or repository state fails closed."""


@dataclass(frozen=True)
class PublishPlan:
    branch: str
    title: str
    commit_message: str
    checks: tuple[GateCheck, ...]


@dataclass(frozen=True)
class PublisherConfig:
    host_root: Path
    evidence_repo_url: str
    results_branch: str
    product_repo_url: str
    product_repo_full_name: str
    product_base_branch: str
    machine_id: str
    poll_seconds: float
    plans: Mapping[str, PublishPlan]
    draft_pr_publication_enabled: bool
    merge_enabled: bool
    main_write_enabled: bool

    def __post_init__(self) -> None:
        if not self.draft_pr_publication_enabled:
            raise ValueError("controlled publisher requires draft_pr_publication_enabled=true")
        if self.merge_enabled:
            raise ValueError("controlled publisher may not enable merge authority")
        if self.main_write_enabled:
            raise ValueError("controlled publisher may not enable product main writes")
        if self.results_branch in {"main", "master"}:
            raise ValueError("publisher evidence branch may not be main or master")
        if self.product_base_branch not in {"main", "master"}:
            raise ValueError("product base branch must be main or master")
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if "/" not in self.product_repo_full_name:
            raise ValueError("product_repo_full_name must be owner/name")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PublisherError(f"{label} must be a mapping")
    return value


def _resolve_path(base: Path, value: Any, label: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise PublisherError(f"{label} is required")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _safe_relative_cwd(value: Any) -> str:
    text = str(value or ".").strip() or "."
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise PublisherError("check cwd must be a safe relative path")
    return path.as_posix()


def _safe_branch(value: Any, *, base_branch: str) -> str:
    branch = str(value or "").strip()
    if not re.fullmatch(r"autobuilder/[A-Za-z0-9._/-]+", branch):
        raise PublisherError(f"unsafe publisher branch: {branch!r}")
    if ".." in branch or branch.endswith("/") or branch == base_branch:
        raise PublisherError(f"unsafe publisher branch: {branch!r}")
    return branch


def load_publisher_config(path: str | Path) -> PublisherConfig:
    config_path = Path(path).expanduser().resolve()
    root = _mapping(yaml.safe_load(config_path.read_text(encoding="utf-8")), "publisher config")
    if root.get("version") != 1:
        raise PublisherError("only publisher config version 1 is supported")

    base = config_path.parent
    base_branch = str(root.get("product_base_branch") or "main").strip()
    plans_raw = _mapping(root.get("plans"), "publisher plans")
    plans: dict[str, PublishPlan] = {}
    for raw_job_id, raw_plan in plans_raw.items():
        job_id = _safe_segment(str(raw_job_id), fallback="job")
        if job_id != str(raw_job_id):
            raise PublisherError(f"unsafe job ID in publisher plans: {raw_job_id!r}")
        plan_data = _mapping(raw_plan, f"publisher plan {job_id}")
        checks_raw = plan_data.get("checks")
        if not isinstance(checks_raw, list) or not checks_raw:
            raise PublisherError(f"publisher plan {job_id} must declare checks")
        checks: list[GateCheck] = []
        for index, raw_check in enumerate(checks_raw):
            check = _mapping(raw_check, f"publisher plan {job_id} check {index}")
            name = str(check.get("name") or "").strip()
            argv_raw = check.get("argv")
            if not name or not isinstance(argv_raw, list) or not argv_raw:
                raise PublisherError(f"publisher plan {job_id} check {index} requires name and argv")
            argv = tuple(str(item) for item in argv_raw)
            if not all(argv):
                raise PublisherError("publisher check argv contains an empty value")
            timeout = int(check.get("timeout_seconds", 600))
            if timeout <= 0:
                raise PublisherError("publisher check timeout_seconds must be positive")
            checks.append(
                GateCheck(
                    name=name,
                    argv=argv,
                    cwd=_safe_relative_cwd(check.get("cwd", ".")),
                    timeout_seconds=timeout,
                )
            )
        plans[job_id] = PublishPlan(
            branch=_safe_branch(plan_data.get("branch"), base_branch=base_branch),
            title=str(plan_data.get("title") or "").strip(),
            commit_message=str(plan_data.get("commit_message") or "").strip(),
            checks=tuple(checks),
        )
        if not plans[job_id].title or not plans[job_id].commit_message:
            raise PublisherError(f"publisher plan {job_id} requires title and commit_message")

    return PublisherConfig(
        host_root=_resolve_path(base, root.get("host_root"), "host_root"),
        evidence_repo_url=str(root.get("evidence_repo_url") or "").strip(),
        results_branch=str(root.get("results_branch") or "results").strip(),
        product_repo_url=str(root.get("product_repo_url") or "").strip(),
        product_repo_full_name=str(root.get("product_repo_full_name") or "").strip(),
        product_base_branch=base_branch,
        machine_id=_safe_segment(
            str(root.get("machine_id") or socket.gethostname()),
            fallback="windows-host",
        ),
        poll_seconds=float(root.get("poll_seconds", 30.0)),
        plans=plans,
        draft_pr_publication_enabled=bool(root.get("draft_pr_publication_enabled", False)),
        merge_enabled=bool(root.get("merge_enabled", False)),
        main_write_enabled=bool(root.get("main_write_enabled", False)),
    )


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    accepted: tuple[int, ...] = (0,),
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(argv),
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
        env=dict(env) if env is not None else None,
    )
    if proc.returncode not in accepted:
        detail = (proc.stderr or proc.stdout or "command failed").strip()
        raise PublisherError(f"{' '.join(argv)}: {detail}")
    return proc


def _git(
    repo: Path | None,
    *args: str,
    accepted: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    argv = ["git"]
    if repo is not None:
        argv.extend(["-C", str(repo)])
    argv.extend(args)
    return _run(argv, accepted=accepted)


def _git_env(repo: Path, env: Mapping[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    argv = ["git", "-C", str(repo), *args]
    return _run(argv, env=env)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_patch_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def _patch_sha256(path: Path) -> str:
    return _sha256_bytes(_canonical_patch_bytes(path))


def _changed_paths(repo: Path) -> tuple[str, ...]:
    paths: set[str] = set()
    for args in (
        ("diff", "--name-only", "--relative"),
        ("diff", "--cached", "--name-only", "--relative"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        output = _git(repo, *args).stdout
        paths.update(line.replace("\\", "/") for line in output.splitlines() if line)
    return tuple(sorted(paths))


class PublisherLock:
    """Cross-platform process lock released automatically on process exit."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> PublisherLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        self.handle.write(b"0")
        self.handle.flush()
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise PublisherError("another controlled publisher process holds the writer lock") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class GitHubDraftClient:
    """Minimal GitHub REST client using the existing Git Credential Manager secret."""

    def __init__(self, repo_full_name: str, token: str) -> None:
        self.repo_full_name = repo_full_name
        self.token = token
        self.owner = repo_full_name.split("/", 1)[0]

    @classmethod
    def from_git_credential(cls, repo_full_name: str) -> GitHubDraftClient:
        credential = _run(
            ["git", "credential", "fill"],
            input_text="protocol=https\nhost=github.com\n\n",
        ).stdout
        values: dict[str, str] = {}
        for line in credential.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        token = values.get("password", "")
        if not token:
            raise PublisherError("Git Credential Manager did not return a GitHub token")
        return cls(repo_full_name, token)

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.github.com{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "msos-autobuilder-controlled-publisher",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise PublisherError(f"GitHub API {method} {path} failed: {exc.code} {body}") from exc
        except urllib.error.URLError as exc:
            raise PublisherError(f"GitHub API {method} {path} failed: {exc}") from exc

    def find_pull_requests(self, branch: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                "state": "all",
                "head": f"{self.owner}:{branch}",
                "per_page": "10",
            }
        )
        result = self._request("GET", f"/repos/{self.repo_full_name}/pulls?{query}")
        if not isinstance(result, list):
            raise PublisherError("GitHub pull-request query returned an invalid payload")
        return [item for item in result if isinstance(item, dict)]

    def create_draft(
        self,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            f"/repos/{self.repo_full_name}/pulls",
            {
                "title": title,
                "head": branch,
                "base": base,
                "body": body,
                "draft": True,
                "maintainer_can_modify": False,
            },
        )
        return _mapping(result, "created pull request")


class EvidenceBranch:
    def __init__(self, config: PublisherConfig) -> None:
        self.config = config
        self.checkout = config.host_root / "state" / "publisher-results-repo"

    def prepare(self) -> None:
        if not (self.checkout / ".git").exists():
            if self.checkout.exists():
                shutil.rmtree(self.checkout)
            self.checkout.parent.mkdir(parents=True, exist_ok=True)
            _git(
                None,
                "-c",
                "core.autocrlf=false",
                "clone",
                "--single-branch",
                "--branch",
                self.config.results_branch,
                "--no-tags",
                self.config.evidence_repo_url,
                str(self.checkout),
            )
        else:
            _git(self.checkout, "config", "core.autocrlf", "false")
            _git(self.checkout, "fetch", "--no-tags", "origin", self.config.results_branch)
            _git(
                self.checkout,
                "checkout",
                "-B",
                self.config.results_branch,
                f"origin/{self.config.results_branch}",
            )
            _git(self.checkout, "reset", "--hard", f"origin/{self.config.results_branch}")
            _git(self.checkout, "clean", "-fd")
        _git(self.checkout, "config", "core.autocrlf", "false")
        _git(self.checkout, "config", "user.name", "MSOS Autobuilder Controlled Publisher")
        _git(self.checkout, "config", "user.email", "autobuilder-publisher@localhost")

    def job_dir(self, job_id: str) -> Path:
        return self.checkout / "results" / self.config.machine_id / job_id

    def publish_report(self, job_dir: Path, payload: Mapping[str, Any]) -> str:
        report_path = job_dir / "publication-report.json"
        _atomic_write_json(report_path, payload)
        relative = report_path.relative_to(self.checkout).as_posix()
        _git(self.checkout, "add", "--", relative)
        changed = _git(
            self.checkout,
            "diff",
            "--cached",
            "--quiet",
            accepted=(0, 1),
        ).returncode
        if changed == 0:
            return _git(self.checkout, "rev-parse", "HEAD").stdout.strip()
        _git(self.checkout, "commit", "-m", f"Record controlled publication {job_dir.name}")
        push = _git(
            self.checkout,
            "push",
            "origin",
            f"HEAD:{self.config.results_branch}",
            accepted=(0, 1),
        )
        if push.returncode != 0:
            _git(self.checkout, "pull", "--rebase", "origin", self.config.results_branch)
            _git(self.checkout, "push", "origin", f"HEAD:{self.config.results_branch}")
        return _git(self.checkout, "rev-parse", "HEAD").stdout.strip()


class ControlledPublisher:
    def __init__(
        self,
        config: PublisherConfig,
        *,
        github_client: GitHubDraftClient | None = None,
    ) -> None:
        self.config = config
        self.host_root = config.host_root.expanduser().resolve()
        self.state = self.host_root / "state"
        self.evidence = EvidenceBranch(config)
        self.product = self.state / "publisher-product-repo"
        self.ledger_path = self.state / "controlled-publisher-seen.json"
        self.lock_path = self.state / "controlled-publisher.lock"
        self.github_client = github_client

    def _client(self) -> GitHubDraftClient:
        if self.github_client is None:
            self.github_client = GitHubDraftClient.from_git_credential(
                self.config.product_repo_full_name
            )
        return self.github_client

    def _load_ledger(self) -> dict[str, dict[str, Any]]:
        if not self.ledger_path.exists():
            return {}
        raw = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise PublisherError("controlled publisher ledger must be a mapping")
        ledger: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise PublisherError("controlled publisher ledger entries are invalid")
            ledger[key] = dict(value)
        return ledger

    def _save_ledger(self, ledger: Mapping[str, Any]) -> None:
        _atomic_write_json(self.ledger_path, ledger)

    def _prepare_product(self) -> str:
        if not (self.product / ".git").exists():
            if self.product.exists():
                shutil.rmtree(self.product)
            self.product.parent.mkdir(parents=True, exist_ok=True)
            _git(
                None,
                "-c",
                "core.autocrlf=false",
                "clone",
                "--no-tags",
                self.config.product_repo_url,
                str(self.product),
            )
        _git(self.product, "config", "core.autocrlf", "false")
        _git(self.product, "config", "user.name", "MSOS Autobuilder Controlled Publisher")
        _git(self.product, "config", "user.email", "autobuilder-publisher@localhost")
        _git(self.product, "fetch", "--no-tags", "origin", self.config.product_base_branch)
        base_ref = f"origin/{self.config.product_base_branch}"
        _git(self.product, "checkout", "--detach", base_ref)
        _git(self.product, "reset", "--hard", base_ref)
        _git(self.product, "clean", "-fd")
        return _git(self.product, "rev-parse", base_ref).stdout.strip()

    def _load_evidence(
        self,
        job_dir: Path,
    ) -> tuple[dict[str, Any], dict[str, Any], str, str, tuple[str, ...]]:
        gate_path = job_dir / "gate-report.json"
        source_path = job_dir / "report.json"
        if not gate_path.is_file() or not source_path.is_file():
            raise PublisherError("publication evidence is missing gate-report.json or report.json")

        gate_sha = _sha256_file(gate_path)
        source_sha = _sha256_file(source_path)
        gate_report = _mapping(
            json.loads(gate_path.read_text(encoding="utf-8")),
            "gate report",
        )
        source_report = _mapping(
            json.loads(source_path.read_text(encoding="utf-8")),
            "source report",
        )

        if gate_report.get("status") != "passed":
            raise PublisherError("only passed gate reports may be published")
        if gate_report.get("state") not in {None, "candidate_passed"}:
            raise PublisherError("gate report is not in candidate_passed state")
        if gate_report.get("publication_enabled", False) is not False:
            raise PublisherError("gate report publication must have remained disabled")
        if gate_report.get("product_write_performed", False) is not False:
            raise PublisherError("gate report indicates a product write")
        if gate_report.get("workspace_removed") is not True:
            raise PublisherError("gate report did not remove the disposable workspace")
        if gate_report.get("policy_blocks") not in ([], None):
            raise PublisherError("gate report contains policy blockers")
        if gate_report.get("errors") not in ([], None):
            raise PublisherError("gate report contains errors")
        checks = gate_report.get("checks")
        if not isinstance(checks, list) or not checks:
            raise PublisherError("gate report must contain checks")
        if not all(isinstance(item, dict) and item.get("passed") is True for item in checks):
            raise PublisherError("gate report contains a failed check")

        if source_report.get("outcome") != "completed":
            raise PublisherError("source report is not completed")
        if source_report.get("publication_enabled", False) is not False:
            raise PublisherError("source report publication must have remained disabled")
        relay = _mapping(source_report.get("relay"), "relay evidence")
        if relay.get("complete_patch_reconstruction") is not True:
            raise PublisherError("source report lacks complete patch reconstruction")
        if gate_report.get("source_report_sha256") != source_sha:
            raise PublisherError("source report SHA-256 does not match the gate evidence")
        integrity_path = job_dir / "result-integrity.json"
        if not integrity_path.is_file():
            raise PublisherError("publication evidence is missing result-integrity.json")
        integrity = _mapping(
            json.loads(integrity_path.read_text(encoding="utf-8")),
            "result integrity",
        )
        if integrity.get("corrected_report_sha256") != source_sha:
            raise PublisherError("corrected report SHA-256 does not match integrity evidence")
        source_report_hash = integrity.get("source_report_sha256") or relay.get("source_report_sha256")
        if source_report_hash and source_report_hash == source_sha:
            raise PublisherError(
                "publisher requires the corrected canonical report, not source-report-only evidence"
            )

        gate_paths = gate_report.get("changed_paths")
        if not isinstance(gate_paths, list) or not all(isinstance(item, str) for item in gate_paths):
            raise PublisherError("gate changed_paths must be a list of strings")
        expected_paths = tuple(sorted(item.replace("\\", "/") for item in gate_paths))

        source_entries = source_report.get("patches")
        gate_entries = gate_report.get("patches")
        if not isinstance(source_entries, list) or not source_entries:
            raise PublisherError("source report must contain patches")
        if not isinstance(gate_entries, list) or not gate_entries:
            raise PublisherError("gate report must contain patch evidence")

        gate_by_task = {
            str(item.get("task_id")): item
            for item in gate_entries
            if isinstance(item, dict) and item.get("task_id")
        }
        union: set[str] = set()
        for raw_entry in source_entries:
            entry = _mapping(raw_entry, "source patch entry")
            if entry.get("complete_patch") is not True:
                raise PublisherError("publisher requires complete_patch=true")
            task_id = str(entry.get("task_id") or "")
            gate_entry = _mapping(gate_by_task.get(task_id), f"gate patch entry {task_id}")
            for key in ("patch_file", "patch_sha256", "changed_paths"):
                if gate_entry.get(key) != entry.get(key):
                    raise PublisherError(f"gate/source patch evidence mismatch for {task_id}: {key}")
            changed = entry.get("changed_paths")
            if not isinstance(changed, list) or not all(isinstance(item, str) for item in changed):
                raise PublisherError("patch changed_paths must be a list of strings")
            normalized = {item.replace("\\", "/") for item in changed}
            overlap = union & normalized
            if overlap:
                raise PublisherError(f"candidate patches overlap on paths: {sorted(overlap)}")
            union.update(normalized)

            patch_file = str(entry.get("patch_file") or "")
            relative = Path(patch_file)
            if not patch_file or relative.is_absolute() or ".." in relative.parts:
                raise PublisherError("patch_file must be a safe relative path")
            patch_path = (job_dir / relative).resolve()
            try:
                patch_path.relative_to(job_dir.resolve())
            except ValueError as exc:
                raise PublisherError("patch_file escaped the evidence directory") from exc
            if not patch_path.is_file():
                raise PublisherError(f"patch file not found: {patch_file}")
            actual_sha = _patch_sha256(patch_path)
            if actual_sha != entry.get("patch_sha256"):
                raise PublisherError(
                    f"patch hash mismatch for {patch_file}: "
                    f"expected {entry.get('patch_sha256')}, got {actual_sha}"
                )

        if tuple(sorted(union)) != expected_paths:
            raise PublisherError("gate/source changed path evidence does not agree")
        return gate_report, source_report, gate_sha, source_sha, expected_paths

    def _apply_patches(
        self,
        job_dir: Path,
        source_report: Mapping[str, Any],
        expected_paths: tuple[str, ...],
    ) -> None:
        entries = source_report.get("patches")
        if not isinstance(entries, list):
            raise PublisherError("source report patches must be a list")
        for raw_entry in entries:
            entry = _mapping(raw_entry, "source patch entry")
            patch_path = job_dir / str(entry["patch_file"])
            canonical = _canonical_patch_bytes(patch_path)
            with tempfile.NamedTemporaryFile(suffix=".patch", delete=False) as handle:
                handle.write(canonical)
                temporary = Path(handle.name)
            try:
                _git(self.product, "apply", "--check", "--binary", str(temporary))
                _git(self.product, "apply", "--binary", str(temporary))
            finally:
                temporary.unlink(missing_ok=True)
        actual_paths = _changed_paths(self.product)
        if actual_paths != expected_paths:
            raise PublisherError(
                f"publisher path drift: expected {list(expected_paths)}, found {list(actual_paths)}"
            )
        _git(self.product, "diff", "--check")

    def _remote_branch_sha(self, branch: str) -> str | None:
        output = _git(
            self.product,
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
        ).stdout.strip()
        if not output:
            return None
        lines = output.splitlines()
        if len(lines) != 1:
            raise PublisherError(f"unexpected remote branch response for {branch}")
        return lines[0].split()[0]

    def _validate_existing_pr(
        self,
        *,
        branch: str,
        expected_commit: str,
    ) -> dict[str, Any] | None:
        pulls = self._client().find_pull_requests(branch)
        if not pulls:
            return None
        if len(pulls) != 1:
            raise PublisherError(f"expected at most one product PR for {branch}, found {len(pulls)}")
        pull = pulls[0]
        head = _mapping(pull.get("head"), "pull request head")
        base = _mapping(pull.get("base"), "pull request base")
        if pull.get("state") != "open":
            raise PublisherError("existing product PR is not open")
        if pull.get("draft") is not True:
            raise PublisherError("existing product PR is not draft")
        if head.get("ref") != branch or head.get("sha") != expected_commit:
            raise PublisherError("existing product PR head drifted")
        if base.get("ref") != self.config.product_base_branch:
            raise PublisherError("existing product PR base drifted")
        return pull

    def _pr_body(
        self,
        *,
        job_id: str,
        gate_report: Mapping[str, Any],
        gate_sha: str,
        source_sha: str,
        base_head: str,
        commit_sha: str,
        expected_paths: tuple[str, ...],
        checks: Sequence[Mapping[str, Any]],
    ) -> str:
        check_lines = [
            f"- `{item.get('name')}`: passed (exit {item.get('returncode')})"
            for item in checks
        ]
        path_lines = [f"- `{path}`" for path in expected_paths]
        body = "\n".join(
            [
                "## Controlled Autobuilder candidate",
                "",
                "This draft PR was created by the single controlled publisher after a passed",
                "disposable candidate gate and a second publication-time validation on current main.",
                "",
                "## Evidence",
                "",
                f"- Job: `{job_id}`",
                f"- Candidate source HEAD: `{gate_report.get('source_head')}`",
                f"- Product base HEAD used: `{base_head}`",
                f"- Product commit: `{commit_sha}`",
                f"- Gate report SHA-256: `{gate_sha}`",
                f"- Source report SHA-256: `{source_sha}`",
                "",
                "## Publication-time checks",
                "",
                *check_lines,
                "",
                "## Changed paths",
                "",
                *path_lines,
                "",
                "## Safety",
                "",
                "- Created as a draft.",
                "- No merge authority was used or granted.",
                "- Product `main` was not written.",
                "- The branch was pushed without force.",
                "",
                f"<!-- msos-autobuilder-controlled-publisher:{job_id} -->",
            ]
        )
        if "ppe-automerge: true" in body:
            raise PublisherError("draft PR body unexpectedly contains an automerge marker")
        return body

    def _verify_ledger_entry(
        self,
        *,
        job_id: str,
        gate_sha: str,
        entry: Mapping[str, Any],
    ) -> None:
        if entry.get("gate_report_sha256") != gate_sha:
            raise PublisherError(f"passed gate report changed after publication: {job_id}")
        branch = str(entry.get("branch") or "")
        commit_sha = str(entry.get("commit_sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
            raise PublisherError("publisher ledger commit SHA is invalid")
        self._prepare_product()
        if self._remote_branch_sha(branch) != commit_sha:
            raise PublisherError("published product branch drifted after publication")
        pull = self._validate_existing_pr(branch=branch, expected_commit=commit_sha)
        if pull is None or pull.get("number") != entry.get("pr_number"):
            raise PublisherError("published product PR drifted after publication")

    def publish_job(
        self,
        job_id: str,
        plan: PublishPlan,
    ) -> tuple[dict[str, Any], str]:
        job_dir = self.evidence.job_dir(job_id)
        gate_report, source_report, gate_sha, source_sha, expected_paths = self._load_evidence(
            job_dir
        )
        source_head = str(gate_report.get("source_head") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", source_head):
            raise PublisherError("gate report is missing a full source_head SHA")

        base_head = self._prepare_product()
        ancestor = _git(
            self.product,
            "merge-base",
            "--is-ancestor",
            source_head,
            base_head,
            accepted=(0, 1),
        ).returncode
        if ancestor != 0:
            raise PublisherError("candidate source HEAD is not an ancestor of product main")

        changed_since_source = {
            line.replace("\\", "/")
            for line in _git(
                self.product,
                "diff",
                "--name-only",
                f"{source_head}..{base_head}",
            ).stdout.splitlines()
            if line
        }
        overlap = changed_since_source & set(expected_paths)
        if overlap:
            raise PublisherError(
                f"product main changed candidate paths since source HEAD: {sorted(overlap)}"
            )

        _git(self.product, "checkout", "-B", plan.branch, base_head)
        self._apply_patches(job_dir, source_report, expected_paths)
        checks = [run_check(self.product, check) for check in plan.checks]
        if not all(item.get("passed") is True for item in checks):
            failed = [item for item in checks if item.get("passed") is not True]
            raise PublisherError(
                "publication-time product checks failed: "
                + "; ".join(
                    f"{item.get('name')}: {_bounded(str(item.get('stderr') or item.get('stdout')))}"
                    for item in failed
                )
            )
        if _changed_paths(self.product) != expected_paths:
            raise PublisherError("publication checks changed product paths")
        if _git(self.product, "rev-parse", "HEAD").stdout.strip() != base_head:
            raise PublisherError("publication checks created a product commit")

        _git(self.product, "add", "--", *expected_paths)
        staged_paths = tuple(
            sorted(
                line.replace("\\", "/")
                for line in _git(
                    self.product,
                    "diff",
                    "--cached",
                    "--name-only",
                ).stdout.splitlines()
                if line
            )
        )
        if staged_paths != expected_paths:
            raise PublisherError(
                f"staged path drift: expected {list(expected_paths)}, found {list(staged_paths)}"
            )

        commit_time = str(gate_report.get("finished_at") or "").strip()
        if not commit_time:
            raise PublisherError("gate report is missing finished_at for deterministic commit")
        env = dict(os.environ)
        env["GIT_AUTHOR_DATE"] = commit_time
        env["GIT_COMMITTER_DATE"] = commit_time
        _git_env(self.product, env, "commit", "-m", plan.commit_message)
        commit_sha = _git(self.product, "rev-parse", "HEAD").stdout.strip()

        committed_paths = tuple(
            sorted(
                line.replace("\\", "/")
                for line in _git(
                    self.product,
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    commit_sha,
                ).stdout.splitlines()
                if line
            )
        )
        if committed_paths != expected_paths:
            raise PublisherError("created product commit contains unexpected paths")

        _git(self.product, "fetch", "--no-tags", "origin", self.config.product_base_branch)
        latest_base = _git(
            self.product,
            "rev-parse",
            f"origin/{self.config.product_base_branch}",
        ).stdout.strip()
        if latest_base != base_head:
            raise PublisherError("product main moved during publication; retry on the next cycle")

        remote_sha = self._remote_branch_sha(plan.branch)
        if remote_sha is None:
            _git(
                self.product,
                "push",
                "origin",
                f"{commit_sha}:refs/heads/{plan.branch}",
            )
        elif remote_sha != commit_sha:
            raise PublisherError("product branch already exists with different content")

        pull = self._validate_existing_pr(branch=plan.branch, expected_commit=commit_sha)
        if pull is None:
            body = self._pr_body(
                job_id=job_id,
                gate_report=gate_report,
                gate_sha=gate_sha,
                source_sha=source_sha,
                base_head=base_head,
                commit_sha=commit_sha,
                expected_paths=expected_paths,
                checks=checks,
            )
            pull = self._client().create_draft(
                branch=plan.branch,
                base=self.config.product_base_branch,
                title=plan.title,
                body=body,
            )
            if pull.get("draft") is not True or pull.get("state") != "open":
                raise PublisherError("GitHub did not create an open draft pull request")
            self._validate_existing_pr(branch=plan.branch, expected_commit=commit_sha)

        publication_report = {
            "version": 1,
            "job_id": job_id,
            "status": "published-draft",
            "published_at": _utc_now(),
            "draft": True,
            "merge_enabled": False,
            "main_write_enabled": False,
            "product_base_branch": self.config.product_base_branch,
            "product_base_head": base_head,
            "product_branch": plan.branch,
            "product_commit": commit_sha,
            "pr_number": pull.get("number"),
            "pr_url": pull.get("html_url"),
            "gate_report_sha256": gate_sha,
            "source_report_sha256": source_sha,
            "changed_paths": list(expected_paths),
            "checks": checks,
        }
        results_commit = self.evidence.publish_report(job_dir, publication_report)
        return publication_report, results_commit

    def run_once(self) -> tuple[str, ...]:
        self.state.mkdir(parents=True, exist_ok=True)
        with PublisherLock(self.lock_path):
            self.evidence.prepare()
            ledger = self._load_ledger()
            processed: list[str] = []
            for job_id, plan in self.config.plans.items():
                job_dir = self.evidence.job_dir(job_id)
                gate_path = job_dir / "gate-report.json"
                if not gate_path.exists():
                    continue
                gate_sha = _sha256_file(gate_path)
                existing = ledger.get(job_id)
                if existing:
                    self._verify_ledger_entry(
                        job_id=job_id,
                        gate_sha=gate_sha,
                        entry=existing,
                    )
                    continue
                report, results_commit = self.publish_job(job_id, plan)
                ledger[job_id] = {
                    "gate_report_sha256": gate_sha,
                    "source_report_sha256": report["source_report_sha256"],
                    "branch": report["product_branch"],
                    "commit_sha": report["product_commit"],
                    "pr_number": report["pr_number"],
                    "pr_url": report["pr_url"],
                    "results_commit": results_commit,
                }
                self._save_ledger(ledger)
                processed.append(job_id)
            return tuple(processed)

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except PublisherError as exc:
                _atomic_write_json(
                    self.state / "controlled-publisher-error.json",
                    {
                        "recorded_at": _utc_now(),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "draft_pr_publication_enabled": True,
                        "merge_enabled": False,
                        "main_write_enabled": False,
                    },
                )
            time.sleep(self.config.poll_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="msos-autobuilder-controlled-publisher")
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    publisher = ControlledPublisher(load_publisher_config(args.config))
    if args.once:
        processed = publisher.run_once()
        print(
            json.dumps(
                {
                    "status": "completed",
                    "processed_jobs": list(processed),
                    "draft_pr_publication_enabled": True,
                    "merge_enabled": False,
                    "main_write_enabled": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    publisher.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
