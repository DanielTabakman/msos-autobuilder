"""Guarded completion controller for approved product PRs.

This controller deliberately starts where the controlled publisher stops: it consumes an
already published, evidence-backed product PR and may merge only an exact validated head.
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
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .candidate_gate import _atomic_write_json, _safe_segment
from .controlled_publisher import PublisherLock
from .lifecycle_evidence import attempt_identity_from_job_yaml, emit_lifecycle_evidence
from .service_error_lifecycle import record_service_cycle_success, write_service_error_marker
from .work_admission import AdmissionError, release_claim


class CompletionControllerError(RuntimeError):
    """Raised when merge eligibility, merge, verification, or cleanup fails closed."""


AUTHORITY_AUTO_MERGE = "AUTO_MERGE_WHEN_GREEN"
AUTHORITY_FOUNDER_REQUIRED = "FOUNDER_DECISION_REQUIRED"
AUTHORITY_NEVER_MERGE = "NEVER_MERGE"
AUTHORITY_CLASSES = {
    AUTHORITY_AUTO_MERGE,
    AUTHORITY_FOUNDER_REQUIRED,
    AUTHORITY_NEVER_MERGE,
}

CHECK_SUCCESS_STATES = {"success", "neutral", "skipped"}
CHECK_PENDING_STATES = {"pending", "queued", "in_progress", "waiting", "requested"}
CHECK_FAILED_STATES = {"failure", "failed", "error", "timed_out", "action_required"}
CHECK_CANCELLED_STATES = {"cancelled", "canceling", "stale", "startup_failure"}


@dataclass(frozen=True)
class CompletionPlan:
    authority_class: str | None = None
    required_checks: tuple[str, ...] = ()
    merge_method: str = "merge"
    delete_branch: bool = True
    cleanup_paths: tuple[Path, ...] = ()
    managed_cleanup_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class CompletionConfig:
    host_root: Path
    evidence_repo_url: str
    results_branch: str
    product_repo_url: str
    product_repo_full_name: str
    product_base_branch: str
    machine_id: str
    poll_seconds: float
    required_checks: tuple[str, ...]
    merge_method: str
    plans: Mapping[str, CompletionPlan]

    def __post_init__(self) -> None:
        if self.results_branch in {"main", "master"}:
            raise ValueError("completion evidence branch may not be main or master")
        if self.product_base_branch not in {"main", "master"}:
            raise ValueError("product base branch must be main or master")
        if self.merge_method not in {"merge", "squash"}:
            raise ValueError("merge_method must be merge or squash; rebase is not supported in v1")
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if "/" not in self.product_repo_full_name:
            raise ValueError("product_repo_full_name must be owner/name")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CompletionControllerError(f"{label} must be a mapping")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_path(base: Path, value: Any, label: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise CompletionControllerError(f"{label} is required")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _safe_branch(value: Any, *, base_branch: str) -> str:
    branch = str(value or "").strip()
    if not re.fullmatch(r"autobuilder/[A-Za-z0-9._/-]+", branch):
        raise CompletionControllerError(f"unsafe product branch: {branch!r}")
    if ".." in branch or branch.endswith("/") or branch == base_branch:
        raise CompletionControllerError(f"unsafe product branch: {branch!r}")
    return branch


def _load_path_list(base: Path, raw: Any, label: str) -> tuple[Path, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise CompletionControllerError(f"{label} must be a list of paths")
    paths: list[Path] = []
    for item in raw:
        path = Path(item).expanduser()
        if not path.is_absolute():
            path = base / path
        paths.append(path.resolve())
    return tuple(paths)


def load_completion_config(path: str | Path) -> CompletionConfig:
    config_path = Path(path).expanduser().resolve()
    root = _mapping(yaml.safe_load(config_path.read_text(encoding="utf-8")), "completion config")
    if root.get("version") != 1:
        raise CompletionControllerError("only completion config version 1 is supported")
    base = config_path.parent
    base_branch = str(root.get("product_base_branch") or "main").strip()
    required_checks = tuple(str(item).strip() for item in root.get("required_checks") or ())
    if any(not item for item in required_checks):
        raise CompletionControllerError("required_checks contains an empty check name")
    merge_method = str(root.get("merge_method") or "merge").strip()
    plans: dict[str, CompletionPlan] = {}
    raw_plans = root.get("plans", {})
    if raw_plans is not None and not isinstance(raw_plans, dict):
        raise CompletionControllerError("completion plans must be a mapping")
    for raw_job_id, raw_plan in (raw_plans or {}).items():
        job_id = _safe_segment(str(raw_job_id), fallback="job")
        if job_id != str(raw_job_id):
            raise CompletionControllerError(f"unsafe job ID in completion plans: {raw_job_id!r}")
        plan_data = _mapping(raw_plan, f"completion plan {job_id}")
        authority = plan_data.get("authority_class")
        if authority is not None and str(authority) not in AUTHORITY_CLASSES:
            raise CompletionControllerError(f"invalid authority_class for {job_id}")
        plan_checks = tuple(str(item).strip() for item in plan_data.get("required_checks") or ())
        method = str(plan_data.get("merge_method") or merge_method).strip()
        if method not in {"merge", "squash"}:
            raise CompletionControllerError(
                "completion plan merge_method is invalid; rebase is not supported in v1"
            )
        plans[job_id] = CompletionPlan(
            authority_class=str(authority) if authority is not None else None,
            required_checks=plan_checks,
            merge_method=method,
            delete_branch=bool(plan_data.get("delete_branch", True)),
            cleanup_paths=_load_path_list(base, plan_data.get("cleanup_paths"), "cleanup_paths"),
            managed_cleanup_roots=_load_path_list(
                base,
                plan_data.get("managed_cleanup_roots"),
                "managed_cleanup_roots",
            ),
        )
    return CompletionConfig(
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
        required_checks=required_checks,
        merge_method=merge_method,
        plans=plans,
    )


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    accepted: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if proc.returncode not in accepted:
        detail = (proc.stderr or proc.stdout or "command failed").strip()
        raise CompletionControllerError(f"{' '.join(argv)}: {detail}")
    return proc


def _git(repo: Path | None, *args: str, accepted: tuple[int, ...] = (0,)) -> str:
    argv = ["git"]
    if repo is not None:
        argv.extend(["-C", str(repo)])
    argv.extend(args)
    return _run(argv, accepted=accepted).stdout.strip()


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


class CompletionGitHubClient:
    """Small GitHub REST client for exact-head PR merge and bounded cleanup."""

    def __init__(self, repo_full_name: str, token: str) -> None:
        self.repo_full_name = repo_full_name
        self.token = token

    @classmethod
    def from_git_credential(cls, repo_full_name: str) -> CompletionGitHubClient:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "git credential fill failed").strip()
            raise CompletionControllerError(detail)
        values: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        token = values.get("password", "")
        if not token:
            raise CompletionControllerError("Git Credential Manager did not return a token")
        return cls(repo_full_name, token)

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        accepted: tuple[int, ...] = (200, 201),
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
                "User-Agent": "msos-autobuilder-completion-controller",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8")
                if response.status not in accepted:
                    raise CompletionControllerError(f"GitHub API returned {response.status}")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code in accepted:
                return json.loads(body) if body else {}
            raise CompletionControllerError(
                f"GitHub API {method} {path} failed: {exc.code} {body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise CompletionControllerError(f"GitHub API {method} {path} failed: {exc}") from exc

    def get_pull_request(self, number: int) -> dict[str, Any]:
        return _mapping(
            self._request("GET", f"/repos/{self.repo_full_name}/pulls/{number}"),
            "pull request",
        )

    def _graphql(self, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        response = _mapping(
            self._request(
                "POST",
                "/graphql",
                {"query": query, "variables": dict(variables)},
            ),
            "GitHub GraphQL response",
        )
        if response.get("errors"):
            raise CompletionControllerError(
                f"GitHub GraphQL review evidence failed: {response['errors']}"
            )
        return _mapping(response.get("data"), "GitHub GraphQL data")

    def review_evidence(self, number: int) -> dict[str, Any]:
        owner, repo = self.repo_full_name.split("/", 1)
        data = self._graphql(
            """
            query($owner:String!, $repo:String!, $number:Int!) {
              repository(owner:$owner, name:$repo) {
                pullRequest(number:$number) {
                  reviewDecision
                  reviewThreads(first: 100) {
                    pageInfo { hasNextPage endCursor }
                    nodes { isResolved isOutdated }
                  }
                  latestOpinionatedReviews(first: 20) {
                    nodes { state author { login } submittedAt }
                  }
                }
              }
            }
            """,
            {"owner": owner, "repo": repo, "number": number},
        )
        repository = _mapping(data.get("repository"), "GitHub repository")
        pr = _mapping(repository.get("pullRequest"), "GitHub pull request")
        thread_connection = _mapping(pr.get("reviewThreads"), "review threads")
        page_info = _mapping(thread_connection.get("pageInfo"), "review thread pageInfo")
        if page_info.get("hasNextPage") is True:
            raise CompletionControllerError(
                "review-thread evidence is paginated; refusing partial GraphQL evidence"
            )
        threads = thread_connection.get("nodes")
        reviews = _mapping(pr.get("latestOpinionatedReviews"), "latest reviews").get("nodes")
        if not isinstance(threads, list):
            raise CompletionControllerError("review-thread evidence is unavailable")
        if not isinstance(reviews, list):
            raise CompletionControllerError("review-decision evidence is unavailable")
        unresolved = [
            item
            for item in threads
            if isinstance(item, dict)
            and item.get("isResolved") is not True
            and item.get("isOutdated") is not True
        ]
        return {
            "source": "github_graphql",
            "review_decision": str(pr.get("reviewDecision") or "").upper(),
            "unresolved_review_threads": len(unresolved),
            "latest_opinionated_reviews": reviews,
        }

    def checks_for_ref(self, ref: str) -> list[dict[str, Any]]:
        status = self._request("GET", f"/repos/{self.repo_full_name}/commits/{ref}/status")
        runs = self._request(
            "GET",
            f"/repos/{self.repo_full_name}/commits/{ref}/check-runs?per_page=100",
        )
        checks: list[dict[str, Any]] = []
        for item in _mapping(status, "commit status").get("statuses") or ():
            if isinstance(item, dict):
                checks.append(
                    {
                        "name": str(item.get("context") or ""),
                        "state": str(item.get("state") or ""),
                        "source": "status",
                    }
                )
        for item in _mapping(runs, "check runs").get("check_runs") or ():
            if isinstance(item, dict):
                checks.append(
                    {
                        "name": str(item.get("name") or ""),
                        "state": str(item.get("conclusion") or item.get("status") or ""),
                        "source": "check_run",
                    }
                )
        return checks

    def merge_pull_request(
        self,
        number: int,
        *,
        expected_head: str,
        method: str,
    ) -> dict[str, Any]:
        return _mapping(
            self._request(
                "PUT",
                f"/repos/{self.repo_full_name}/pulls/{number}/merge",
                {
                    "sha": expected_head,
                    "merge_method": method,
                },
            ),
            "merge result",
        )

    def delete_branch(self, branch: str) -> dict[str, Any]:
        encoded = urllib.parse.quote(f"heads/{branch}", safe="")
        return _mapping(
            self._request(
                "DELETE",
                f"/repos/{self.repo_full_name}/git/refs/{encoded}",
                accepted=(200, 204, 422),
            ),
            "branch deletion result",
        )


class EvidenceBranch:
    def __init__(self, config: CompletionConfig) -> None:
        self.config = config
        self.checkout = config.host_root / "state" / "completion-results-repo"

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
        _git(self.checkout, "config", "user.name", "MSOS Autobuilder Completion Controller")
        _git(self.checkout, "config", "user.email", "autobuilder-completion@localhost")

    def job_dirs(self) -> tuple[Path, ...]:
        root = self.checkout / "results" / self.config.machine_id
        if not root.exists():
            return ()
        return tuple(sorted(path for path in root.iterdir() if path.is_dir()))

    def publish_report(self, job_dir: Path, payload: Mapping[str, Any]) -> str:
        report_path = job_dir / "completion-report.json"
        _atomic_write_json(report_path, payload)
        relative = report_path.relative_to(self.checkout).as_posix()
        _git(self.checkout, "add", "--", relative)
        changed = _run(
            ["git", "-C", str(self.checkout), "diff", "--cached", "--quiet"],
            accepted=(0, 1),
        ).returncode
        if changed == 0:
            return _git(self.checkout, "rev-parse", "HEAD")
        _git(self.checkout, "commit", "-m", f"Record guarded completion {job_dir.name}")
        push = _run(
            [
                "git",
                "-C",
                str(self.checkout),
                "push",
                "origin",
                f"HEAD:{self.config.results_branch}",
            ],
            accepted=(0, 1),
        )
        if push.returncode != 0:
            _git(self.checkout, "pull", "--rebase", "origin", self.config.results_branch)
            _git(self.checkout, "push", "origin", f"HEAD:{self.config.results_branch}")
        return _git(self.checkout, "rev-parse", "HEAD")


def _authority_from_job(job: Mapping[str, Any]) -> tuple[str, str]:
    raw = job.get("merge_authority")
    if isinstance(raw, dict):
        authority = str(raw.get("class") or raw.get("authority_class") or "").strip()
        declared_at = str(raw.get("declared_at") or "").strip()
    else:
        founder = job.get("founder_build_next")
        founder_map = founder if isinstance(founder, dict) else {}
        authority_map = founder_map.get("authority")
        if not isinstance(authority_map, dict):
            authority_map = {}
        authority = str(
            authority_map.get("merge_authority_class")
            or authority_map.get("authority_class")
            or ""
        ).strip()
        declared_at = str(authority_map.get("merge_authority_declared_at") or "").strip()
    if authority not in AUTHORITY_CLASSES:
        raise CompletionControllerError("merge authority class is missing or malformed")
    if not declared_at:
        raise CompletionControllerError("merge authority declared_at is required")
    approved_at = str(job.get("approved_at") or job.get("submitted_at") or "").strip()
    if approved_at and declared_at > approved_at:
        raise CompletionControllerError(
            "merge authority was declared after implementation approval"
        )
    return authority, declared_at


def _allowed_paths_from_job(job: Mapping[str, Any]) -> tuple[str, ...]:
    contract = job.get("candidate_validation")
    if isinstance(contract, dict) and isinstance(contract.get("allowed_changed_paths"), list):
        return tuple(
            sorted(str(item).replace("\\", "/") for item in contract["allowed_changed_paths"])
        )
    manifest = job.get("manifest")
    lanes = manifest.get("lanes") if isinstance(manifest, dict) else None
    allowed: set[str] = set()
    if isinstance(lanes, list):
        for lane in lanes:
            if isinstance(lane, dict) and isinstance(lane.get("allowed_paths"), list):
                allowed.update(str(item).replace("\\", "/") for item in lane["allowed_paths"])
    return tuple(sorted(allowed))


def _forbidden_paths_from_job(job: Mapping[str, Any]) -> tuple[str, ...]:
    contract = job.get("candidate_validation")
    raw = contract.get("forbidden_changed_paths") if isinstance(contract, dict) else None
    forbidden: set[str] = set()
    if isinstance(raw, list):
        forbidden.update(str(item).replace("\\", "/") for item in raw)
    manifest = job.get("manifest")
    lanes = manifest.get("lanes") if isinstance(manifest, dict) else None
    if isinstance(lanes, list):
        for lane in lanes:
            if isinstance(lane, dict) and isinstance(lane.get("forbidden_paths"), list):
                forbidden.update(str(item).replace("\\", "/") for item in lane["forbidden_paths"])
    return tuple(sorted(forbidden))


def _path_matches(path: str, pattern: str) -> bool:
    normalized = path.replace("\\", "/")
    prefix = pattern.replace("\\", "/").rstrip("/")
    return normalized == prefix or normalized.startswith(prefix + "/")


def _validate_paths(job: Mapping[str, Any], changed_paths: Sequence[str]) -> None:
    allowed = _allowed_paths_from_job(job)
    forbidden = _forbidden_paths_from_job(job)
    if not allowed:
        raise CompletionControllerError("path authority is missing allowed paths")
    unauthorized = [
        path for path in changed_paths if not any(_path_matches(path, item) for item in allowed)
    ]
    if unauthorized:
        raise CompletionControllerError(f"changed paths exceed authority: {unauthorized}")
    blocked = [
        path for path in changed_paths if any(_path_matches(path, item) for item in forbidden)
    ]
    if blocked:
        raise CompletionControllerError(f"changed paths include forbidden paths: {blocked}")


def _validate_required_checks(
    checks: Sequence[Mapping[str, Any]],
    required: Sequence[str],
) -> dict[str, dict[str, str]]:
    by_name: dict[str, dict[str, str]] = {}
    for raw in checks:
        name = str(raw.get("name") or "").strip()
        state = str(raw.get("state") or "").strip().lower()
        if name:
            by_name[name] = {"name": name, "state": state, "source": str(raw.get("source") or "")}
    evidence: dict[str, dict[str, str]] = {}
    for name in required:
        item = by_name.get(name)
        if item is None:
            raise CompletionControllerError(f"required check is missing: {name}")
        state = item["state"]
        if state in CHECK_SUCCESS_STATES:
            evidence[name] = item
            continue
        if state in CHECK_PENDING_STATES:
            raise CompletionControllerError(f"required check is pending: {name}")
        if state in CHECK_CANCELLED_STATES:
            raise CompletionControllerError(f"required check is cancelled: {name}")
        if state in CHECK_FAILED_STATES:
            raise CompletionControllerError(f"required check failed: {name}")
        raise CompletionControllerError(f"required check has unknown state: {name}={state}")
    return evidence


def _validate_completion_readiness(
    job_dir: Path,
    *,
    job_id: str,
    pr_number: int,
    expected_head: str,
) -> dict[str, Any]:
    path = job_dir / "completion-readiness.json"
    if not path.exists():
        raise CompletionControllerError("completion readiness conflict evidence is missing")
    readiness = _mapping(json.loads(path.read_text(encoding="utf-8")), "completion readiness")
    if (
        readiness.get("version") != 1
        or readiness.get("producer") != "controlled_publisher"
        or readiness.get("job_id") != job_id
        or int(readiness.get("pr_number", -1)) != pr_number
        or str(readiness.get("product_commit") or "").lower() != expected_head
    ):
        raise CompletionControllerError("completion readiness evidence is stale or contradictory")
    for key in (
        "canon_conflict",
        "evidence_conflict",
        "ownership_conflict",
        "founder_decision_required",
    ):
        if readiness.get(key) is not False:
            raise CompletionControllerError(f"completion readiness blocks merge: {key}")
    return readiness


def _validate_revision_lineage(
    job_dir: Path,
    *,
    job_id: str,
    expected_head: str,
    gate_report_sha256: str,
    publication_report_sha256: str,
) -> dict[str, Any]:
    path = job_dir / "revision-lineage.json"
    if not path.exists():
        raise CompletionControllerError("revision lineage evidence is missing")
    lineage = _mapping(json.loads(path.read_text(encoding="utf-8")), "revision lineage")
    if (
        lineage.get("version") != 1
        or lineage.get("producer") != "controlled_publisher"
        or lineage.get("job_id") != job_id
        or str(lineage.get("product_commit") or "").lower() != expected_head
        or lineage.get("gate_report_sha256") != gate_report_sha256
        or lineage.get("publication_report_sha256") != publication_report_sha256
    ):
        raise CompletionControllerError("revision lineage evidence is stale or contradictory")
    disposition = str(lineage.get("revision_disposition") or "")
    if disposition not in {"not_applicable", "terminal"}:
        raise CompletionControllerError("revision lineage is not terminal")
    if disposition == "not_applicable" and lineage.get("not_applicable_evidence") is not True:
        raise CompletionControllerError("revision lineage lacks explicit not-applicable evidence")
    return lineage


def _validate_claim_release_handoff(
    job: Mapping[str, Any],
    publication: Mapping[str, Any],
) -> dict[str, Any]:
    handoff = _mapping(publication.get("claim_release_handoff"), "claim release handoff")
    if handoff.get("handoff") != "msos-autobuilder.work_admission.claim_release.v1":
        raise CompletionControllerError("publication lacks canonical claim release handoff")
    if handoff.get("consumer") != "completion-controller":
        raise CompletionControllerError("claim release handoff targets the wrong consumer")
    admission = _mapping(publication.get("work_admission"), "publication work_admission")
    claim = _mapping(admission.get("claim"), "publication work_admission claim")
    founder = _mapping(job.get("founder_build_next"), "founder_build_next")
    job_admission = _mapping(
        founder.get("work_admission"),
        "founder_build_next.work_admission",
    )
    for key in ("objective_sha256", "writer_id"):
        claim_key = "claim_writer_id" if key == "writer_id" else key
        if handoff.get(key) != claim.get(key) or handoff.get(key) != job_admission.get(claim_key):
            raise CompletionControllerError(f"claim release handoff {key} is not trusted")
    generation = handoff.get("claim_generation")
    if (
        not isinstance(generation, int)
        or generation != claim.get("generation")
        or generation != job_admission.get("claim_generation")
    ):
        raise CompletionControllerError("claim release handoff generation is not trusted")
    if sorted(handoff.get("authorized_paths") or []) != sorted(
        job_admission.get("authorized_paths") or []
    ):
        raise CompletionControllerError("claim release handoff paths are not trusted")
    if handoff.get("verified_completion_terminal_state") != "merged":
        raise CompletionControllerError("claim release handoff does not authorize merged release")
    return dict(handoff)


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


class CompletionController:
    def __init__(
        self,
        config: CompletionConfig,
        *,
        github_client: CompletionGitHubClient | None = None,
    ) -> None:
        self.config = config
        self.host_root = config.host_root.expanduser().resolve()
        self.state = self.host_root / "state"
        self.evidence = EvidenceBranch(config)
        self.product = self.state / "completion-product-repo"
        self.ledger_path = self.state / "completion-controller-seen.json"
        self.lock_path = self.state / "completion-controller.lock"
        self.github_client = github_client
        self._last_error_marker_written = False

    def _client(self) -> CompletionGitHubClient:
        if self.github_client is None:
            self.github_client = CompletionGitHubClient.from_git_credential(
                self.config.product_repo_full_name
            )
        return self.github_client

    def _load_ledger(self) -> dict[str, dict[str, Any]]:
        if not self.ledger_path.exists():
            return {}
        raw = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise CompletionControllerError("completion ledger must be a mapping")
        return {str(key): _mapping(value, "completion ledger entry") for key, value in raw.items()}

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
        _git(self.product, "fetch", "--no-tags", "origin", self.config.product_base_branch)
        default_sha = _git(self.product, "rev-parse", f"origin/{self.config.product_base_branch}")
        _git(self.product, "checkout", "--detach", default_sha)
        _git(self.product, "reset", "--hard", default_sha)
        _git(self.product, "clean", "-fd")
        return default_sha

    def _load_job_evidence(self, job_dir: Path) -> dict[str, Any]:
        job = _mapping(
            yaml.safe_load((job_dir / "job.yaml").read_text(encoding="utf-8")),
            "job.yaml",
        )
        report = _mapping(
            json.loads((job_dir / "report.json").read_text(encoding="utf-8")),
            "report",
        )
        integrity = _mapping(
            json.loads((job_dir / "result-integrity.json").read_text(encoding="utf-8")),
            "result-integrity",
        )
        gate = _mapping(
            json.loads((job_dir / "gate-report.json").read_text(encoding="utf-8")),
            "gate-report",
        )
        publication = _mapping(
            json.loads((job_dir / "publication-report.json").read_text(encoding="utf-8")),
            "publication-report",
        )
        if report.get("outcome") != "completed":
            raise CompletionControllerError("originating result report is not completed")
        relay = _mapping(report.get("relay"), "relay evidence")
        if relay.get("complete_patch_reconstruction") is not True:
            raise CompletionControllerError("relay evidence is incomplete")
        report_sha = _sha256_file(job_dir / "report.json")
        if integrity.get("corrected_report_sha256") != report_sha:
            raise CompletionControllerError("relay integrity hash does not match report")
        if gate.get("status") != "passed" or gate.get("state") not in {None, "candidate_passed"}:
            raise CompletionControllerError("candidate gate did not pass")
        if gate.get("policy_blocks") not in ([], None) or gate.get("errors") not in ([], None):
            raise CompletionControllerError("candidate gate has unresolved blockers or errors")
        if gate.get("source_report_sha256") != report_sha:
            raise CompletionControllerError(
                "candidate gate source hash does not match relay report"
            )
        if publication.get("status") != "published-draft":
            raise CompletionControllerError("controlled publication did not produce a draft PR")
        if (
            publication.get("merge_enabled") is not False
            or publication.get("main_write_enabled") is not False
        ):
            raise CompletionControllerError("publication report granted forbidden authority")
        expected_head = str(publication.get("product_commit") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{40}", expected_head):
            raise CompletionControllerError("publication report is missing product_commit")
        if str(publication.get("gate_report_sha256") or "") != _sha256_file(
            job_dir / "gate-report.json"
        ):
            raise CompletionControllerError(
                "publication report gate hash does not match gate evidence"
            )
        changed = tuple(
            str(item).replace("\\", "/") for item in publication.get("changed_paths") or ()
        )
        gate_changed = tuple(
            sorted(str(item).replace("\\", "/") for item in gate.get("changed_paths") or ())
        )
        if tuple(sorted(changed)) != gate_changed:
            raise CompletionControllerError("publication and gate changed paths disagree")
        _validate_paths(job, changed)
        readiness = _validate_completion_readiness(
            job_dir,
            job_id=job_dir.name,
            pr_number=int(publication.get("pr_number")),
            expected_head=expected_head,
        )
        gate_sha = _sha256_file(job_dir / "gate-report.json")
        publication_sha = _sha256_file(job_dir / "publication-report.json")
        revision_lineage = _validate_revision_lineage(
            job_dir,
            job_id=job_dir.name,
            expected_head=expected_head,
            gate_report_sha256=gate_sha,
            publication_report_sha256=publication_sha,
        )
        claim_release_handoff = _validate_claim_release_handoff(job, publication)
        return {
            "job": job,
            "report": report,
            "integrity": integrity,
            "gate": gate,
            "publication": publication,
            "completion_readiness": readiness,
            "revision_lineage": revision_lineage,
            "claim_release_handoff": claim_release_handoff,
            "changed_paths": tuple(sorted(changed)),
            "gate_report_sha256": gate_sha,
            "report_sha256": report_sha,
            "publication_report_sha256": publication_sha,
        }

    def _assert_pr_eligible(
        self,
        pr: Mapping[str, Any],
        review_evidence: Mapping[str, Any],
        readiness: Mapping[str, Any],
        *,
        branch: str,
        base: str,
        expected_head: str,
    ) -> None:
        head = _mapping(pr.get("head"), "pull request head")
        pr_base = _mapping(pr.get("base"), "pull request base")
        if pr.get("state") != "open":
            raise CompletionControllerError("pull request is not open")
        if head.get("ref") != branch or str(head.get("sha") or "").lower() != expected_head:
            raise CompletionControllerError("pull request head changed after publication")
        if pr_base.get("ref") != base:
            raise CompletionControllerError("pull request base branch changed")
        mergeable_state = str(pr.get("mergeable_state") or "").lower()
        if pr.get("mergeable") is False or mergeable_state in {"dirty", "blocked", "unknown"}:
            raise CompletionControllerError("pull request is not mergeable")
        unresolved_threads = review_evidence.get("unresolved_review_threads")
        if not isinstance(unresolved_threads, int):
            raise CompletionControllerError("review-thread evidence is unavailable")
        if unresolved_threads != 0:
            raise CompletionControllerError("pull request has unresolved review threads")
        review_decision = str(review_evidence.get("review_decision") or "").upper()
        if review_decision != "APPROVED":
            raise CompletionControllerError(
                "pull request lacks required APPROVED review decision"
            )
        if readiness.get("canon_conflict") or readiness.get("evidence_conflict") or readiness.get(
            "ownership_conflict"
        ):
            raise CompletionControllerError(
                "pull request has unresolved canon/evidence/ownership conflict"
            )
        if readiness.get("founder_decision_required"):
            raise CompletionControllerError("pull request discovered a new founder decision")

    def _verify_after_merge(
        self,
        *,
        pr_number: int,
        expected_head: str,
        method: str,
    ) -> dict[str, Any]:
        self._prepare_product()
        pr = self._client().get_pull_request(pr_number)
        if pr.get("merged") is not True:
            raise CompletionControllerError("pull request is not merged after merge call")
        merge_commit = str(pr.get("merge_commit_sha") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", merge_commit):
            raise CompletionControllerError("merged pull request lacks merge_commit_sha")
        default_sha = _git(self.product, "rev-parse", f"origin/{self.config.product_base_branch}")
        contains_merge = _run(
            [
                "git",
                "-C",
                str(self.product),
                "merge-base",
                "--is-ancestor",
                merge_commit,
                default_sha,
            ],
            accepted=(0, 1),
        ).returncode == 0
        contains_head = _run(
            [
                "git",
                "-C",
                str(self.product),
                "merge-base",
                "--is-ancestor",
                expected_head,
                default_sha,
            ],
            accepted=(0, 1),
        ).returncode == 0
        if not contains_merge:
            raise CompletionControllerError("merge commit is not on default branch")
        if method == "merge" and not contains_head:
            raise CompletionControllerError("validated head is not preserved on default branch")
        return {
            "pr_number": pr_number,
            "merge_commit": merge_commit,
            "default_branch": self.config.product_base_branch,
            "default_branch_head": default_sha,
            "validated_head_ancestor": contains_head,
            "merge_commit_on_default": contains_merge,
        }

    def _cleanup(self, *, branch: str, plan: CompletionPlan) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if plan.delete_branch:
            try:
                self._client().delete_branch(branch)
                results.append({"action": "delete_branch", "branch": branch, "status": "completed"})
            except Exception as exc:
                results.append(
                    {
                        "action": "delete_branch",
                        "branch": branch,
                        "status": "maintenance_required",
                        "error": str(exc),
                    }
                )
        disposable_root = (self.host_root / "disposable-workspaces").resolve()
        roots = tuple(root.resolve() for root in plan.managed_cleanup_roots)
        if plan.cleanup_paths and not roots:
            raise CompletionControllerError("cleanup paths require managed_cleanup_roots")
        for root in roots:
            if root == disposable_root or not _is_relative_to(root, disposable_root):
                raise CompletionControllerError(
                    f"managed cleanup root is not an authorized disposable workspace root: {root}"
                )
            if root == self.host_root or root == self.state or self.state in root.parents:
                raise CompletionControllerError(
                    f"managed cleanup root targets protected state: {root}"
                )
        for raw_path in plan.cleanup_paths:
            path = raw_path.resolve()
            if path in roots or not any(_is_relative_to(path, root) for root in roots):
                raise CompletionControllerError(
                    f"cleanup path is outside approved managed roots: {path}"
                )
            if (
                path in {self.host_root, self.state, self.evidence.checkout, self.product}
                or self.state in path.parents
                or path == self.host_root.parent
            ):
                raise CompletionControllerError(f"cleanup path targets protected state: {path}")
            if raw_path.exists() and raw_path.is_symlink():
                raise CompletionControllerError(f"cleanup path is a symlink: {raw_path}")
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
                results.append({"action": "remove_path", "path": str(path), "status": "completed"})
            except Exception as exc:
                results.append(
                    {
                        "action": "remove_path",
                        "path": str(path),
                        "status": "maintenance_required",
                        "error": str(exc),
                    }
                )
        return results

    def _terminalize(self, *, job_id: str, payload: Mapping[str, Any]) -> Path:
        path = self.state / "completion-terminal-work-items" / f"{job_id}.json"
        _atomic_write_json(path, dict(payload))
        return path

    def _emit_completion_lifecycle(
        self,
        *,
        job_dir: Path,
        terminal_path: Path,
        terminal: Mapping[str, Any],
        results_commit: str,
    ) -> Any:
        identity = attempt_identity_from_job_yaml(job_dir / "job.yaml")
        if identity is None:
            raise CompletionControllerError("completion terminalization lacks attempt identity")
        return emit_lifecycle_evidence(
            self.host_root,
            evidence_kind="publication_review.disposition",
            identity=identity,
            source_path=terminal_path,
            payload={
                "publication_review_disposition": "merged",
                "reason_code": "publication_review.merged.verified.v1",
                "merged_pr": str(terminal.get("pr_number")),
                "product_branch": str(terminal.get("product_branch")),
                "product_commit": terminal.get("validated_head"),
                "merge_commit": terminal.get("merge_commit"),
                "default_branch": terminal.get("default_branch"),
                "results_commit": results_commit,
            },
            final=True,
            closed_status="final",
            observed_at=str(terminal["completed_at"]),
        )

    def _release_completion_claim(
        self,
        *,
        terminal: Mapping[str, Any],
        lifecycle_result: Any,
        results_commit: str,
    ) -> dict[str, Any]:
        handoff = _mapping(terminal.get("claim_release_handoff"), "claim release handoff")
        try:
            released = release_claim(
                self.state,
                str(handoff.get("objective_sha256") or ""),
                writer_id=str(handoff.get("writer_id") or ""),
                terminal_state="merged",
                expected_generation=int(handoff.get("claim_generation")),
                evidence={
                    "release_handoff_consumer": "completion-controller",
                    "terminal_job_id": terminal.get("job_id"),
                    "verified_pr": terminal.get("pr_number"),
                    "validated_head": terminal.get("validated_head"),
                    "merge_commit": terminal.get("merge_commit"),
                    "publication_review_lifecycle_evidence_id": lifecycle_result.evidence_id,
                    "publication_review_lifecycle_sha256": lifecycle_result.envelope_sha256,
                    "completion_results_commit": results_commit,
                },
            )
        except AdmissionError as exc:
            raise CompletionControllerError("completion claim release failed closed") from exc
        return asdict(released)

    def _save_ledger_entry(self, job_id: str, entry: Mapping[str, Any]) -> None:
        ledger = self._load_ledger()
        ledger[job_id] = dict(entry)
        self._save_ledger(ledger)

    def _escalation_entry(
        self,
        *,
        job_id: str,
        authority: str,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "status": "escalated",
            "job_id": job_id,
            "authority_class": authority,
            "reason": reason,
            "recorded_at": _utc_now(),
            "merge_performed": False,
        }

    def _digest(self, payload: Mapping[str, Any]) -> str:
        cleanup = payload.get("cleanup")
        cleanup_state = "completed"
        if isinstance(cleanup, list) and any(
            isinstance(item, dict) and item.get("status") != "completed" for item in cleanup
        ):
            cleanup_state = "maintenance required"
        return "\n".join(
            [
                "Merged automatically",
                f"Work: {payload.get('work_item_id') or payload.get('job_id')}",
                f"PR: {payload.get('pr_number')}",
                f"Merge commit: {payload.get('merge_commit')}",
                "Validation: candidate gate + publication + required checks passed",
                f"Cleanup: {cleanup_state}",
                "Decision required: none",
            ]
        )

    def complete_job(self, job_dir: Path, plan: CompletionPlan) -> dict[str, Any]:
        job_id = job_dir.name
        evidence = self._load_job_evidence(job_dir)
        job = evidence["job"]
        authority, authority_declared_at = _authority_from_job(job)
        if plan.authority_class is not None and plan.authority_class != authority:
            raise CompletionControllerError(
                "configured authority conflicts with immutable job authority"
            )
        if authority == AUTHORITY_FOUNDER_REQUIRED:
            return self._escalation_entry(
                job_id=job_id,
                authority=authority,
                reason="FOUNDER_DECISION_REQUIRED blocks automatic merge",
            )
        if authority == AUTHORITY_NEVER_MERGE:
            return self._escalation_entry(
                job_id=job_id,
                authority=authority,
                reason="NEVER_MERGE blocks automatic merge",
            )
        publication = evidence["publication"]
        pr_number = int(publication.get("pr_number"))
        branch = _safe_branch(
            publication.get("product_branch"),
            base_branch=self.config.product_base_branch,
        )
        expected_head = str(publication["product_commit"]).lower()
        required_checks = plan.required_checks or self.config.required_checks
        if not required_checks:
            raise CompletionControllerError("no required GitHub checks configured")
        pr = self._client().get_pull_request(pr_number)
        review = self._client().review_evidence(pr_number)
        self._assert_pr_eligible(
            pr,
            review,
            evidence["completion_readiness"],
            branch=branch,
            base=self.config.product_base_branch,
            expected_head=expected_head,
        )
        checks = _validate_required_checks(
            self._client().checks_for_ref(expected_head),
            required_checks,
        )
        reread = self._client().get_pull_request(pr_number)
        reread_review = self._client().review_evidence(pr_number)
        self._assert_pr_eligible(
            reread,
            reread_review,
            evidence["completion_readiness"],
            branch=branch,
            base=self.config.product_base_branch,
            expected_head=expected_head,
        )
        intent = {
            "version": 1,
            "status": "merge_intent_prepared",
            "job_id": job_id,
            "pr_number": pr_number,
            "validated_head": expected_head,
            "product_branch": branch,
            "merge_method": plan.merge_method,
            "checks": checks,
            "review_evidence": reread_review,
            "completion_readiness": evidence["completion_readiness"],
            "revision_lineage": evidence["revision_lineage"],
            "prepared_at": _utc_now(),
        }
        self._save_ledger_entry(job_id, intent)
        merge = self._client().merge_pull_request(
            pr_number,
            expected_head=expected_head,
            method=plan.merge_method,
        )
        if merge.get("merged") is not True:
            raise CompletionControllerError("GitHub did not report a successful merge")
        self._save_ledger_entry(
            job_id,
            {**intent, "status": "merge_accepted", "merge_result": merge},
        )
        verification = self._verify_after_merge(
            pr_number=pr_number,
            expected_head=expected_head,
            method=plan.merge_method,
        )
        self._save_ledger_entry(
            job_id,
            {
                **intent,
                "status": "merge_verified",
                "merge_result": merge,
                "verification": verification,
            },
        )
        cleanup = self._cleanup(branch=branch, plan=plan)
        self._save_ledger_entry(
            job_id,
            {
                **intent,
                "status": "cleanup_recorded",
                "merge_result": merge,
                "verification": verification,
                "cleanup": cleanup,
            },
        )
        terminal = {
            "version": 1,
            "job_id": job_id,
            "work_item_id": (
                _mapping(job.get("founder_build_next"), "founder_build_next").get("work_item_id")
                if isinstance(job.get("founder_build_next"), dict)
                else job_id
            ),
            "status": "merged",
            "authority_class": authority,
            "authority_declared_at": authority_declared_at,
            "completed_at": _utc_now(),
            "pr_number": pr_number,
            "validated_head": expected_head,
            "product_branch": branch,
            "merge_method": plan.merge_method,
            "merge_commit": verification["merge_commit"],
            "default_branch": self.config.product_base_branch,
            "default_branch_head": verification["default_branch_head"],
            "checks": checks,
            "review_evidence": reread_review,
            "changed_paths": list(evidence["changed_paths"]),
            "gate_report_sha256": evidence["gate_report_sha256"],
            "publication_report_sha256": evidence["publication_report_sha256"],
            "revision_lineage": evidence["revision_lineage"],
            "claim_release_handoff": evidence["claim_release_handoff"],
            "cleanup": cleanup,
        }
        terminal_path = self._terminalize(job_id=job_id, payload=terminal)
        terminal["terminal_path"] = str(terminal_path)
        terminal["founder_digest"] = self._digest(terminal)
        return terminal

    def _recover_existing(
        self,
        *,
        job_dir: Path,
        plan: CompletionPlan,
        existing: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        status = str(existing.get("status") or "")
        if status == "merged":
            self._verify_after_merge(
                pr_number=int(existing["pr_number"]),
                expected_head=str(existing["validated_head"]),
                method=str(existing["merge_method"]),
            )
            return None
        if status not in {
            "merge_intent_prepared",
            "merge_accepted",
            "merge_verified",
            "cleanup_recorded",
        }:
            raise CompletionControllerError(f"unknown completion ledger status: {status}")
        job_id = job_dir.name
        evidence = self._load_job_evidence(job_dir)
        pr_number = int(existing["pr_number"])
        expected_head = str(existing["validated_head"]).lower()
        if expected_head != str(evidence["publication"]["product_commit"]).lower():
            raise CompletionControllerError(
                "completion ledger validated head conflicts with evidence"
            )
        merge_result: Mapping[str, Any] | None = (
            _mapping(existing.get("merge_result"), "merge result")
            if isinstance(existing.get("merge_result"), dict)
            else None
        )
        if status == "merge_intent_prepared":
            pr = self._client().get_pull_request(pr_number)
            if pr.get("merged") is True:
                pass
            elif pr.get("state") == "open":
                publication = evidence["publication"]
                branch = _safe_branch(
                    publication.get("product_branch"),
                    base_branch=self.config.product_base_branch,
                )
                review = self._client().review_evidence(pr_number)
                self._assert_pr_eligible(
                    pr,
                    review,
                    evidence["completion_readiness"],
                    branch=branch,
                    base=self.config.product_base_branch,
                    expected_head=expected_head,
                )
                _validate_required_checks(
                    self._client().checks_for_ref(expected_head),
                    list(existing.get("checks") or {}),
                )
                merge_result = self._client().merge_pull_request(
                    pr_number,
                    expected_head=expected_head,
                    method=str(existing["merge_method"]),
                )
                if merge_result.get("merged") is not True:
                    raise CompletionControllerError(
                        "GitHub did not report a successful recovered merge"
                    )
                self._save_ledger_entry(
                    job_id,
                    {**dict(existing), "status": "merge_accepted", "merge_result": merge_result},
                )
            else:
                raise CompletionControllerError(
                    "prepared merge intent cannot recover because pull request changed or closed"
                )
        verification = (
            _mapping(existing.get("verification"), "completion verification")
            if isinstance(existing.get("verification"), dict)
            else self._verify_after_merge(
                pr_number=pr_number,
                expected_head=expected_head,
                method=str(existing["merge_method"]),
            )
        )
        cleanup = (
            list(existing["cleanup"])
            if isinstance(existing.get("cleanup"), list)
            else self._cleanup(
                branch=_safe_branch(
                    evidence["publication"].get("product_branch"),
                    base_branch=self.config.product_base_branch,
                ),
                plan=plan,
            )
        )
        job = evidence["job"]
        authority, authority_declared_at = _authority_from_job(job)
        terminal = {
            "version": 1,
            "job_id": job_id,
            "work_item_id": (
                _mapping(job.get("founder_build_next"), "founder_build_next").get("work_item_id")
                if isinstance(job.get("founder_build_next"), dict)
                else job_id
            ),
            "status": "merged",
            "authority_class": authority,
            "authority_declared_at": authority_declared_at,
            "completed_at": _utc_now(),
            "pr_number": pr_number,
            "validated_head": expected_head,
            "product_branch": _safe_branch(
                evidence["publication"].get("product_branch"),
                base_branch=self.config.product_base_branch,
            ),
            "merge_method": str(existing["merge_method"]),
            "merge_commit": verification["merge_commit"],
            "default_branch": self.config.product_base_branch,
            "default_branch_head": verification["default_branch_head"],
            "checks": dict(existing.get("checks") or {}),
            "review_evidence": dict(existing.get("review_evidence") or {}),
            "changed_paths": list(evidence["changed_paths"]),
            "gate_report_sha256": evidence["gate_report_sha256"],
            "publication_report_sha256": evidence["publication_report_sha256"],
            "revision_lineage": evidence["revision_lineage"],
            "claim_release_handoff": evidence["claim_release_handoff"],
            "cleanup": cleanup,
            "recovered_from_status": status,
        }
        terminal_path = self._terminalize(job_id=job_id, payload=terminal)
        terminal["terminal_path"] = str(terminal_path)
        terminal["founder_digest"] = self._digest(terminal)
        return terminal

    def run_once(self) -> tuple[str, ...]:
        self._last_error_marker_written = False
        self.state.mkdir(parents=True, exist_ok=True)
        cycle_started_at = _utc_now()
        with PublisherLock(self.lock_path):
            self.evidence.prepare()
            ledger = self._load_ledger()
            completed: list[str] = []
            escalated: list[str] = []
            verified: list[str] = []
            for job_dir in self.evidence.job_dirs():
                job_id = job_dir.name
                if self.config.plans and job_id not in self.config.plans:
                    continue
                plan = self.config.plans.get(job_id, CompletionPlan(
                    required_checks=self.config.required_checks,
                    merge_method=self.config.merge_method,
                ))
                existing = ledger.get(job_id)
                if existing:
                    if existing.get("status") == "escalated":
                        escalated.append(job_id)
                        verified.append(job_id)
                        continue
                    recovered = self._recover_existing(
                        job_dir=job_dir,
                        plan=plan,
                        existing=existing,
                    )
                    if recovered is not None:
                        results_commit = self.evidence.publish_report(job_dir, recovered)
                        recovered["results_commit"] = results_commit
                        lifecycle_result = self._emit_completion_lifecycle(
                            job_dir=job_dir,
                            terminal_path=Path(str(recovered["terminal_path"])),
                            terminal=recovered,
                            results_commit=results_commit,
                        )
                        recovered["claim_release"] = self._release_completion_claim(
                            terminal=recovered,
                            lifecycle_result=lifecycle_result,
                            results_commit=results_commit,
                        )
                        ledger[job_id] = recovered
                        self._save_ledger(ledger)
                        completed.append(job_id)
                    verified.append(job_id)
                    continue
                try:
                    result = self.complete_job(job_dir, plan)
                    results_commit = self.evidence.publish_report(job_dir, result)
                    result["results_commit"] = results_commit
                    if result.get("status") != "escalated":
                        lifecycle_result = self._emit_completion_lifecycle(
                            job_dir=job_dir,
                            terminal_path=Path(str(result["terminal_path"])),
                            terminal=result,
                            results_commit=results_commit,
                        )
                        result["claim_release"] = self._release_completion_claim(
                            terminal=result,
                            lifecycle_result=lifecycle_result,
                            results_commit=results_commit,
                        )
                    ledger[job_id] = result
                    self._save_ledger(ledger)
                    if result.get("status") == "escalated":
                        escalated.append(job_id)
                    else:
                        completed.append(job_id)
                    verified.append(job_id)
                except (CompletionControllerError, OSError, KeyError, TypeError, ValueError) as exc:
                    self._write_error_marker(
                        exc,
                        associated={
                            "job_id": job_id,
                            "repository": self.config.product_repo_full_name,
                        },
                    )
                    raise
            record_service_cycle_success(
                state_root=self.state,
                host_root=self.host_root,
                service="completion",
                cycle_started_at=cycle_started_at,
                associated_jobs=verified,
                terminal_evidence={
                    "completed_jobs": completed,
                    "escalated_jobs": escalated,
                    "verified_jobs": verified,
                },
            )
            return tuple(completed)

    def _write_error_marker(
        self,
        exc: BaseException,
        *,
        associated: Mapping[str, Any] | None = None,
    ) -> None:
        write_service_error_marker(
            state_root=self.state,
            host_root=self.host_root,
            service="completion",
            marker_name="completion-controller-error.json",
            error_type=type(exc).__name__,
            message=str(exc),
            associated=associated,
            extra={"merge_enabled": True, "exact_head_guard": True},
            exception=exc,
        )
        self._last_error_marker_written = True

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except CompletionControllerError as exc:
                if not self._last_error_marker_written:
                    self._write_error_marker(exc, associated={"scope": "global"})
            time.sleep(self.config.poll_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="msos-autobuilder-completion-controller")
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    controller = CompletionController(load_completion_config(args.config))
    if args.once:
        completed = controller.run_once()
        print(
            json.dumps(
                {
                    "status": "completed",
                    "completed_jobs": list(completed),
                    "merge_enabled": True,
                    "exact_head_guard": True,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    controller.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
