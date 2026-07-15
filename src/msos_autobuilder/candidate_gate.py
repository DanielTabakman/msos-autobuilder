"""Apply relayed patches in a disposable product clone and run declared checks.

This module is deliberately review-only. It may clone the product source, apply patches,
and run local validation commands, but it cannot create product commits, branches, pushes,
or pull requests. Gate reports are written only to a dedicated non-product results branch.
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
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .validation_contract import (
    ValidationContractError,
    load_validation_contract,
)


class CandidateGateError(RuntimeError):
    """Raised when candidate input or execution violates the gate contract."""


@dataclass(frozen=True)
class GateCheck:
    name: str
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: int = 600
    required: bool = True
    phase: str = "check"


@dataclass(frozen=True)
class GatePlan:
    checks: tuple[GateCheck, ...]
    policy_blocks: tuple[str, ...] = ()
    source: str = "configured"
    contract_sha256: str | None = None
    bootstrap: tuple[GateCheck, ...] = ()


@dataclass(frozen=True)
class CandidateEnvironment:
    path: Path
    python: Path


@dataclass(frozen=True)
class CandidateGateConfig:
    host_root: Path
    results_repo_url: str
    results_branch: str
    source_repo: Path
    machine_id: str
    poll_seconds: float
    plans: Mapping[str, GatePlan]

    def __post_init__(self) -> None:
        if self.results_branch in {"main", "master"}:
            raise ValueError("candidate gate may not target main or master")
        if not self.results_repo_url.strip():
            raise ValueError("results_repo_url is required")
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_segment(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return cleaned[:96] or fallback


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CandidateGateError(f"{label} must be a mapping")
    return value


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
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


def _run_git(
    repo: Path | None,
    *args: str,
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
        check=False,
    )
    if proc.returncode not in accepted:
        detail = (proc.stderr or proc.stdout or "git command failed").strip()
        raise CandidateGateError(detail)
    return proc


def _changed_paths(repo: Path) -> tuple[str, ...]:
    paths: set[str] = set()
    for args in (
        ("diff", "--name-only", "--relative"),
        ("diff", "--cached", "--name-only", "--relative"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        output = _run_git(repo, *args).stdout
        paths.update(line.replace("\\", "/") for line in output.splitlines() if line)
    return tuple(sorted(paths))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_patch_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _resolve_path(base: Path, value: Any, label: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise CandidateGateError(f"{label} is required")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _load_source_repo(host_root: Path, config_root: dict[str, Any], base: Path) -> Path:
    configured = config_root.get("source_repo")
    if configured:
        return _resolve_path(base, configured, "source_repo")
    host_config = host_root / "host.yaml"
    if not host_config.exists():
        raise CandidateGateError(f"host config not found: {host_config}")
    raw = _mapping(yaml.safe_load(host_config.read_text(encoding="utf-8")), "host config")
    return _resolve_path(host_config.parent, raw.get("source_repo"), "host source_repo")


def _safe_relative_cwd(value: Any) -> str:
    text = str(value or ".").strip() or "."
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise CandidateGateError("check cwd must be a safe relative path")
    return path.as_posix()


def load_candidate_gate_config(path: str | Path) -> CandidateGateConfig:
    config_path = Path(path).expanduser().resolve()
    root = _mapping(yaml.safe_load(config_path.read_text(encoding="utf-8")), "candidate gate config")
    if root.get("version") != 1:
        raise CandidateGateError("only candidate gate config version 1 is supported")
    if root.get("publication_enabled", False) is not False:
        raise CandidateGateError("candidate gate publication must remain disabled")

    base = config_path.parent
    host_root = _resolve_path(base, root.get("host_root"), "host_root")
    plans_raw = _mapping(root.get("plans"), "plans")
    plans: dict[str, GatePlan] = {}
    for raw_job_id, raw_plan in plans_raw.items():
        job_id = _safe_segment(str(raw_job_id), fallback="job")
        if job_id != str(raw_job_id):
            raise CandidateGateError(f"unsafe job ID in plans: {raw_job_id!r}")
        plan_data = _mapping(raw_plan, f"plan {job_id}")
        checks_raw = plan_data.get("checks")
        if not isinstance(checks_raw, list) or not checks_raw:
            raise CandidateGateError(f"plan {job_id} must declare at least one check")
        checks: list[GateCheck] = []
        for index, raw_check in enumerate(checks_raw):
            check = _mapping(raw_check, f"plan {job_id} check {index}")
            name = str(check.get("name") or "").strip()
            argv_raw = check.get("argv")
            if not name or not isinstance(argv_raw, list) or not argv_raw:
                raise CandidateGateError(f"plan {job_id} check {index} requires name and argv")
            argv = tuple(str(item) for item in argv_raw)
            if not all(argv):
                raise CandidateGateError(f"plan {job_id} check {index} argv contains an empty value")
            timeout = int(check.get("timeout_seconds", 600))
            if timeout <= 0:
                raise CandidateGateError("check timeout_seconds must be positive")
            checks.append(
                GateCheck(
                    name=name,
                    argv=argv,
                    cwd=_safe_relative_cwd(check.get("cwd", ".")),
                    timeout_seconds=timeout,
                    required=bool(check.get("required", True)),
                    phase=str(check.get("phase") or "check"),
                )
            )
        blocks_raw = plan_data.get("policy_blocks", [])
        if not isinstance(blocks_raw, list) or not all(isinstance(item, str) for item in blocks_raw):
            raise CandidateGateError(f"plan {job_id} policy_blocks must be a list of strings")
        plans[job_id] = GatePlan(
            checks=tuple(checks),
            policy_blocks=tuple(item.strip() for item in blocks_raw if item.strip()),
        )

    return CandidateGateConfig(
        host_root=host_root,
        results_repo_url=str(root.get("results_repo_url") or "").strip(),
        results_branch=str(root.get("results_branch") or "results").strip(),
        source_repo=_load_source_repo(host_root, root, base),
        machine_id=_safe_segment(
            str(root.get("machine_id") or socket.gethostname()),
            fallback="windows-host",
        ),
        poll_seconds=float(root.get("poll_seconds", 30.0)),
        plans=plans,
    )


def _bounded(text: str | None, limit: int = 20_000) -> str:
    value = text or ""
    if len(value) <= limit:
        return value
    return value[-limit:]


def _check_environment(candidate: Path) -> dict[str, str]:
    baseline = (
        "PATH",
        "HOME",
        "USERPROFILE",
        "SYSTEMROOT",
        "LOCALAPPDATA",
        "APPDATA",
        "TEMP",
        "TMP",
    )
    environment = {key: os.environ[key] for key in baseline if key in os.environ}
    environment["PYTHONUTF8"] = "1"
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(candidate) + (os.pathsep + existing if existing else "")
    return environment


def _candidate_python(env: CandidateEnvironment) -> str:
    return str(env.python)


def _resolve_argv(check: GateCheck, env: CandidateEnvironment | None) -> list[str]:
    argv = list(check.argv)
    if env is not None and argv:
        executable = argv[0].strip().lower().replace("\\", "/").rsplit("/", 1)[-1]
        if executable.endswith(".exe"):
            executable = executable[:-4]
        if executable in {"python", "python3", "py"}:
            argv[0] = _candidate_python(env)
    return argv


def run_check(
    candidate: Path,
    check: GateCheck,
    *,
    candidate_env: CandidateEnvironment | None = None,
) -> dict[str, Any]:
    cwd = (candidate / check.cwd).resolve()
    try:
        cwd.relative_to(candidate.resolve())
    except ValueError as exc:
        raise CandidateGateError(f"check {check.name!r} cwd escaped candidate") from exc
    if not cwd.is_dir():
        return {
            "name": check.name,
            "argv": _resolve_argv(check, candidate_env),
            "cwd": check.cwd,
            "required": check.required,
            "phase": check.phase,
            "passed": False,
            "skipped": False,
            "returncode": None,
            "timed_out": False,
            "stdout": "",
            "stderr": f"check cwd does not exist: {check.cwd}",
        }

    started = _utc_now()
    try:
        proc = subprocess.run(
            _resolve_argv(check, candidate_env),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=check.timeout_seconds,
            env=_check_environment(candidate),
            check=False,
        )
        return {
            "name": check.name,
            "argv": _resolve_argv(check, candidate_env),
            "cwd": check.cwd,
            "required": check.required,
            "phase": check.phase,
            "started_at": started,
            "finished_at": _utc_now(),
            "passed": proc.returncode == 0,
            "skipped": False,
            "returncode": proc.returncode,
            "timed_out": False,
            "stdout": _bounded(proc.stdout),
            "stderr": _bounded(proc.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "name": check.name,
            "argv": _resolve_argv(check, candidate_env),
            "cwd": check.cwd,
            "required": check.required,
            "phase": check.phase,
            "started_at": started,
            "finished_at": _utc_now(),
            "passed": False,
            "skipped": False,
            "returncode": None,
            "timed_out": True,
            "stdout": _bounded(exc.stdout if isinstance(exc.stdout, str) else ""),
            "stderr": _bounded(exc.stderr if isinstance(exc.stderr, str) else ""),
        }
    except OSError as exc:
        return {
            "name": check.name,
            "argv": _resolve_argv(check, candidate_env),
            "cwd": check.cwd,
            "required": check.required,
            "phase": check.phase,
            "started_at": started,
            "finished_at": _utc_now(),
            "passed": False,
            "skipped": False,
            "returncode": None,
            "timed_out": False,
            "stdout": "",
            "stderr": str(exc),
        }


class ResultsBranch:
    """Read and write only candidate evidence on a dedicated results branch."""

    def __init__(self, config: CandidateGateConfig) -> None:
        self.config = config
        self.checkout = config.host_root / "state" / "candidate-gate-results-repo"

    def prepare(self) -> None:
        if not (self.checkout / ".git").exists():
            if self.checkout.exists():
                shutil.rmtree(self.checkout)
            self.checkout.parent.mkdir(parents=True, exist_ok=True)
            _run_git(
                None,
                "clone",
                "--single-branch",
                "--branch",
                self.config.results_branch,
                "--no-tags",
                self.config.results_repo_url,
                str(self.checkout),
            )
        else:
            _run_git(self.checkout, "fetch", "--no-tags", "origin", self.config.results_branch)
            _run_git(
                self.checkout,
                "checkout",
                "-B",
                self.config.results_branch,
                f"origin/{self.config.results_branch}",
            )
            _run_git(
                self.checkout,
                "reset",
                "--hard",
                f"origin/{self.config.results_branch}",
            )
            _run_git(self.checkout, "clean", "-fd")
        _run_git(self.checkout, "config", "user.name", "MSOS Autobuilder Candidate Gate")
        _run_git(self.checkout, "config", "user.email", "autobuilder-gate@localhost")

    def job_dirs(self) -> tuple[Path, ...]:
        root = self.checkout / "results" / self.config.machine_id
        if not root.exists():
            return ()
        return tuple(sorted(path for path in root.iterdir() if path.is_dir()))

    def publish_report(self, job_dir: Path, payload: Mapping[str, Any]) -> str:
        report_path = job_dir / "gate-report.json"
        _atomic_write_json(report_path, payload)
        relative = report_path.relative_to(self.checkout).as_posix()
        _run_git(self.checkout, "add", "--", relative)
        changed = _run_git(
            self.checkout,
            "diff",
            "--cached",
            "--quiet",
            accepted=(0, 1),
        ).returncode
        if changed == 0:
            return _run_git(self.checkout, "rev-parse", "HEAD").stdout.strip()
        job_id = job_dir.name
        _run_git(self.checkout, "commit", "-m", f"Record candidate gate result {job_id}")
        commit = _run_git(self.checkout, "rev-parse", "HEAD").stdout.strip()
        push = _run_git(
            self.checkout,
            "push",
            "origin",
            f"HEAD:{self.config.results_branch}",
            accepted=(0, 1),
        )
        if push.returncode != 0:
            _run_git(self.checkout, "pull", "--rebase", "origin", self.config.results_branch)
            _run_git(self.checkout, "push", "origin", f"HEAD:{self.config.results_branch}")
            commit = _run_git(self.checkout, "rev-parse", "HEAD").stdout.strip()
        return commit


class CandidateGate:
    def __init__(self, config: CandidateGateConfig) -> None:
        self.config = config
        self.host_root = config.host_root.expanduser().resolve()
        self.state = self.host_root / "state"
        self.workspace_root = self.state / "candidate-gate-workspaces"
        self.ledger_path = self.state / "candidate-gate-seen.json"
        self.results = ResultsBranch(config)

    def _load_ledger(self) -> dict[str, dict[str, str]]:
        if not self.ledger_path.exists():
            return {}
        raw = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise CandidateGateError("candidate gate ledger must be a mapping")
        ledger: dict[str, dict[str, str]] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise CandidateGateError("candidate gate ledger entries are invalid")
            source_sha = value.get("source_report_sha256")
            commit = value.get("results_commit")
            if not isinstance(source_sha, str) or not isinstance(commit, str):
                raise CandidateGateError("candidate gate ledger entries are incomplete")
            ledger[key] = {"source_report_sha256": source_sha, "results_commit": commit}
        return ledger

    def _save_ledger(self, ledger: Mapping[str, Any]) -> None:
        _atomic_write_json(self.ledger_path, ledger)

    def _clone_candidate(self, job_id: str, source_head: str) -> Path:
        candidate = self.workspace_root / _safe_segment(job_id, fallback="job")
        if candidate.exists():
            shutil.rmtree(candidate)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if not (self.config.source_repo / ".git").exists():
            raise CandidateGateError(f"source_repo is not a Git checkout: {self.config.source_repo}")
        _run_git(None, "clone", "--no-hardlinks", "--no-tags", str(self.config.source_repo), str(candidate))
        _run_git(candidate, "checkout", "--detach", source_head)
        actual = _run_git(candidate, "rev-parse", "HEAD").stdout.strip()
        if actual != source_head:
            raise CandidateGateError(f"candidate source mismatch: expected {source_head}, got {actual}")
        _run_git(candidate, "remote", "remove", "origin")
        return candidate

    def _create_candidate_environment(self, candidate: Path) -> CandidateEnvironment:
        env_path = candidate / ".msos-candidate-env"
        proc = subprocess.run(
            [sys.executable, "-m", "venv", str(env_path)],
            cwd=candidate,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
        if proc.returncode != 0:
            raise CandidateGateError(
                "candidate environment creation failed: "
                + _bounded(proc.stderr or proc.stdout)
            )
        python = (
            env_path / "Scripts" / "python.exe"
            if os.name == "nt"
            else env_path / "bin" / "python"
        )
        if not python.is_file():
            raise CandidateGateError(f"candidate environment Python not found: {python}")
        return CandidateEnvironment(path=env_path, python=python)

    def _load_input(self, job_dir: Path) -> tuple[dict[str, Any], str]:
        report_path = job_dir / "report.json"
        if not report_path.exists():
            raise CandidateGateError("relayed job is missing report.json")
        source_sha = _sha256_file(report_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or report.get("outcome") != "completed":
            raise CandidateGateError("relayed report is not a completed job")
        if report.get("publication_enabled", False) is not False:
            raise CandidateGateError("relayed report publication must remain disabled")
        relay = _mapping(report.get("relay"), "relay evidence")
        if relay.get("complete_patch_reconstruction") is not True:
            raise CandidateGateError("relayed report does not contain complete patches")
        integrity_path = job_dir / "result-integrity.json"
        if integrity_path.exists():
            integrity = _mapping(
                json.loads(integrity_path.read_text(encoding="utf-8")),
                "result integrity",
            )
            if integrity.get("corrected_report_sha256") != source_sha:
                raise CandidateGateError(
                    "corrected report SHA-256 does not match integrity evidence"
                )
        return report, source_sha

    def _load_generic_input(self, job_dir: Path) -> tuple[dict[str, Any], str]:
        report, report_sha = self._load_input(job_dir)
        source_path = job_dir / "source-report.json"
        integrity_path = job_dir / "result-integrity.json"
        if not source_path.is_file():
            raise CandidateGateError("generic build-next result is missing source-report.json")
        if not integrity_path.is_file():
            raise CandidateGateError("generic build-next result is missing result-integrity.json")
        source_sha = _sha256_file(source_path)
        integrity = _mapping(
            json.loads(integrity_path.read_text(encoding="utf-8")),
            "result integrity",
        )
        if integrity.get("corrected_report_sha256") != report_sha:
            raise CandidateGateError("corrected report SHA-256 does not match integrity evidence")
        if integrity.get("source_report_sha256") != source_sha:
            raise CandidateGateError("source report SHA-256 does not match integrity evidence")
        if source_sha == report_sha:
            raise CandidateGateError("source and corrected reports must remain distinct")
        relay = _mapping(report.get("relay"), "relay evidence")
        if not str(relay.get("source_report_role") or "").startswith("original-worker"):
            raise CandidateGateError("source report role is not explicit")
        if not str(relay.get("canonical_report_role") or "").startswith("relay-corrected"):
            raise CandidateGateError("canonical report role is not explicit")
        task_hashes = integrity.get("canonical_patch_sha256_by_task")
        if not isinstance(task_hashes, dict) or not task_hashes:
            raise CandidateGateError("result integrity must include canonical patch hashes")
        report_tasks: set[str] = set()
        for raw_entry in report.get("patches") or []:
            entry = _mapping(raw_entry, "patch entry")
            task_id = str(entry.get("task_id") or "").strip()
            if not task_id:
                raise CandidateGateError("patch entry is missing task_id")
            report_tasks.add(task_id)
            expected = task_hashes.get(task_id)
            if expected != entry.get("patch_sha256"):
                raise CandidateGateError("report patch hash does not match integrity metadata")
            relative = Path(str(entry.get("patch_file") or ""))
            if not relative.parts or relative.is_absolute() or ".." in relative.parts:
                raise CandidateGateError("patch_file must be a safe relative path")
            patch_path = (job_dir / relative).resolve()
            try:
                patch_path.relative_to(job_dir.resolve())
            except ValueError as exc:
                raise CandidateGateError("patch_file escaped relayed job directory") from exc
            if not patch_path.is_file():
                raise CandidateGateError(f"patch file not found: {entry.get('patch_file')}")
            actual = _canonical_patch_sha256(patch_path)
            if actual != expected:
                raise CandidateGateError("canonical patch bytes do not match integrity metadata")
        if set(task_hashes) != report_tasks:
            raise CandidateGateError("result integrity task map does not match report patches")
        return report, report_sha

    def _plan_from_job_contract(
        self,
        job_dir: Path,
        report: Mapping[str, Any],
        source_head: str,
    ) -> GatePlan:
        job_path = job_dir / "job.yaml"
        if not job_path.exists():
            raise CandidateGateError("generic build-next result is missing job.yaml")
        job = _mapping(yaml.safe_load(job_path.read_text(encoding="utf-8")), "job.yaml")
        contract = load_validation_contract(job.get("candidate_validation"))
        founder = _mapping(job.get("founder_build_next"), "founder_build_next")
        native = _mapping(founder.get("native_slice"), "founder_build_next.native_slice")
        authority = _mapping(founder.get("authority"), "founder_build_next.authority")
        source = _mapping(founder.get("source"), "founder_build_next.source")
        job_id = str(report.get("job_id") or "").strip()
        expected_allowed = tuple(
            sorted(str(path).replace("\\", "/") for path in native.get("touch_set") or [])
        )
        bindings = {
            "job_id": (contract.job_id, job_id, str(job.get("job_id") or "")),
            "pipeline_id": (contract.pipeline_id, str(founder.get("pipeline_id") or "")),
            "work_item_id": (contract.work_item_id, str(founder.get("work_item_id") or "")),
            "native_slice_id": (contract.native_slice_id, str(native.get("slice_id") or "")),
            "adapter": (contract.adapter, str(founder.get("registered_adapter") or "")),
            "target_repository": (contract.target_repository, str(founder.get("repository") or "")),
            "source_commit": (contract.source_commit.lower(), source_head.lower(), str(source.get("commit") or "").lower()),
            "allowed_changed_paths": (contract.allowed_changed_paths, expected_allowed),
            "publication_enabled": (contract.publication_enabled, bool(authority.get("publication_enabled", True)), bool(job.get("publication_enabled", True))),
            "merge_enabled": (contract.merge_enabled, bool(authority.get("merge_enabled", True))),
            "product_main_write_enabled": (
                contract.product_main_write_enabled,
                bool(authority.get("product_main_write_enabled", True)),
            ),
        }
        for label, values in bindings.items():
            if len(set(values)) != 1:
                raise CandidateGateError(f"candidate_validation {label} does not match immutable evidence")
        if contract.source_commit.lower() != source_head.lower():
            raise CandidateGateError("candidate_validation source_commit does not match report")
        changed: set[str] = set()
        for raw_entry in report.get("patches") or []:
            entry = _mapping(raw_entry, "patch entry")
            paths = entry.get("changed_paths")
            if isinstance(paths, list):
                changed.update(str(path).replace("\\", "/") for path in paths)
        allowed = set(contract.allowed_changed_paths)
        if not changed or not changed <= allowed:
            raise CandidateGateError("candidate changed paths exceed candidate_validation authority")
        return GatePlan(
            bootstrap=tuple(
                GateCheck(
                    name=command.name,
                    argv=command.argv,
                    cwd=command.cwd,
                    timeout_seconds=command.timeout_seconds,
                    required=command.required,
                    phase=command.phase,
                )
                for command in contract.bootstrap
            ),
            checks=tuple(
                GateCheck(
                    name=command.name,
                    argv=command.argv,
                    cwd=command.cwd,
                    timeout_seconds=command.timeout_seconds,
                    required=command.required,
                    phase=command.phase,
                )
                for command in contract.checks
            ),
            source="candidate_validation",
            contract_sha256=contract.contract_sha256,
        )

    def _unvalidated_report(
        self,
        job_id: str,
        source_sha: str,
        message: str,
        *,
        started: str,
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "job_id": job_id,
            "status": "unvalidated",
            "state": "awaiting_validation",
            "started_at": started,
            "finished_at": _utc_now(),
            "publication_enabled": False,
            "product_write_performed": False,
            "workspace_removed": True,
            "source_report_sha256": source_sha,
            "checks": [],
            "policy_blocks": [],
            "errors": [{"type": "CandidateGateError", "message": message}],
        }

    def _apply_patches(
        self,
        candidate: Path,
        job_dir: Path,
        report: Mapping[str, Any],
    ) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
        raw_entries = report.get("patches")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise CandidateGateError("relayed report must contain at least one patch")
        expected_paths: set[str] = set()
        patch_evidence: list[dict[str, Any]] = []
        for raw_entry in raw_entries:
            entry = _mapping(raw_entry, "patch entry")
            if entry.get("complete_patch") is not True:
                raise CandidateGateError("candidate gate requires complete_patch=true")
            patch_file = str(entry.get("patch_file") or "").strip()
            expected_sha = str(entry.get("patch_sha256") or "").strip()
            if not patch_file or not expected_sha:
                raise CandidateGateError("patch entry is missing file or SHA-256")
            relative = Path(patch_file)
            if relative.is_absolute() or ".." in relative.parts:
                raise CandidateGateError("patch_file must be a safe relative path")
            patch_path = (job_dir / relative).resolve()
            try:
                patch_path.relative_to(job_dir.resolve())
            except ValueError as exc:
                raise CandidateGateError("patch_file escaped relayed job directory") from exc
            if not patch_path.is_file():
                raise CandidateGateError(f"patch file not found: {patch_file}")
            actual_sha = _sha256_file(patch_path)
            if actual_sha != expected_sha:
                raise CandidateGateError(
                    f"patch hash mismatch for {patch_file}: expected {expected_sha}, got {actual_sha}"
                )
            changed_paths = entry.get("changed_paths")
            if not isinstance(changed_paths, list) or not all(isinstance(item, str) for item in changed_paths):
                raise CandidateGateError("patch changed_paths must be a list of strings")
            normalized = {item.replace("\\", "/") for item in changed_paths}
            overlap = expected_paths & normalized
            if overlap:
                raise CandidateGateError(f"candidate patches overlap on paths: {sorted(overlap)}")
            expected_paths.update(normalized)
            _run_git(candidate, "apply", "--check", "--binary", str(patch_path))
            _run_git(candidate, "apply", "--binary", str(patch_path))
            patch_evidence.append(
                {
                    "task_id": entry.get("task_id"),
                    "patch_file": patch_file,
                    "patch_sha256": actual_sha,
                    "changed_paths": sorted(normalized),
                }
            )

        actual_paths = _changed_paths(candidate)
        if actual_paths != tuple(sorted(expected_paths)):
            raise CandidateGateError(
                f"candidate path drift: expected {sorted(expected_paths)}, found {list(actual_paths)}"
            )
        _run_git(candidate, "diff", "--check")
        return actual_paths, patch_evidence

    def process_job(self, job_dir: Path, plan: GatePlan) -> tuple[dict[str, Any], str]:
        job_id = _safe_segment(job_dir.name, fallback="job")
        started = _utc_now()
        candidate: Path | None = None
        candidate_env: CandidateEnvironment | None = None
        report: dict[str, Any] = {
            "version": 1,
            "job_id": job_id,
            "status": "failed",
            "state": "candidate_failed",
            "started_at": started,
            "finished_at": None,
            "publication_enabled": False,
            "product_write_performed": False,
            "checks": [],
            "bootstrap": [],
            "policy_blocks": list(plan.policy_blocks),
            "plan_source": plan.source,
            "validation_contract_sha256": plan.contract_sha256,
            "errors": [],
        }
        source_sha = ""
        try:
            source_report, source_sha = self._load_input(job_dir)
            codex_report = _mapping(source_report.get("codex_report"), "codex_report")
            source_head = str(codex_report.get("source_head") or "").strip()
            if not re.fullmatch(r"[0-9a-fA-F]{40}", source_head):
                raise CandidateGateError("codex report is missing a full source_head SHA")
            report["source_head"] = source_head
            report["source_report_sha256"] = source_sha
            candidate = self._clone_candidate(job_id, source_head)
            changed_paths, patch_evidence = self._apply_patches(candidate, job_dir, source_report)
            report["changed_paths"] = list(changed_paths)
            report["patches"] = patch_evidence

            if plan.source == "candidate_validation":
                candidate_env = self._create_candidate_environment(candidate)
                report["candidate_environment"] = {
                    "path": str(candidate_env.path),
                    "python": str(candidate_env.python),
                }
                bootstrap = [
                    run_check(candidate, check, candidate_env=candidate_env)
                    for check in plan.bootstrap
                ]
                report["bootstrap"] = bootstrap
                if not all(item.get("passed") is True for item in bootstrap):
                    report["status"] = "failed"
                    report["state"] = "candidate_failed"
                    return report, source_sha
            checks = [
                run_check(candidate, check, candidate_env=candidate_env)
                for check in plan.checks
            ]
            report["checks"] = checks
            head_after = _run_git(candidate, "rev-parse", "HEAD").stdout.strip()
            if head_after != source_head:
                raise CandidateGateError("candidate checks created a product commit")
            passed = all(
                check.get("passed") is True
                for check in checks
                if check.get("required", True) is True
            )
            report["status"] = "passed" if passed and not plan.policy_blocks else "failed"
            report["state"] = (
                "candidate_passed" if report["status"] == "passed" else "candidate_failed"
            )
        except (CandidateGateError, json.JSONDecodeError, OSError, ValueError) as exc:
            report["errors"].append({"type": type(exc).__name__, "message": str(exc)})
        finally:
            env_path = candidate_env.path if candidate_env is not None else None
            if candidate is not None and candidate.exists():
                shutil.rmtree(candidate, ignore_errors=True)
            report["workspace_removed"] = candidate is None or not candidate.exists()
            if env_path is not None:
                report["candidate_environment_removed"] = not env_path.exists()
            report["finished_at"] = _utc_now()
        return report, source_sha

    def process_generic_job(self, job_dir: Path) -> tuple[dict[str, Any], str]:
        job_id = _safe_segment(job_dir.name, fallback="job")
        started = _utc_now()
        try:
            job_path = job_dir / "job.yaml"
            if not job_path.exists():
                raise CandidateGateError("generic build-next result is missing job.yaml")
            job = _mapping(yaml.safe_load(job_path.read_text(encoding="utf-8")), "job.yaml")
            if "candidate_validation" not in job:
                raise CandidateGateError("immutable job predates candidate_validation")
            source_report, source_sha = self._load_generic_input(job_dir)
            codex_report = _mapping(source_report.get("codex_report"), "codex_report")
            source_head = str(codex_report.get("source_head") or "").strip()
            if not re.fullmatch(r"[0-9a-fA-F]{40}", source_head):
                raise CandidateGateError("codex report is missing a full source_head SHA")
            plan = self._plan_from_job_contract(job_dir, source_report, source_head)
        except (
            CandidateGateError,
            ValidationContractError,
            json.JSONDecodeError,
            OSError,
            ValueError,
        ) as exc:
            report_path = job_dir / "report.json"
            source_sha = _sha256_file(report_path) if report_path.exists() else ""
            return self._unvalidated_report(job_id, source_sha, str(exc), started=started), source_sha
        return self.process_job(job_dir, plan)

    def run_once(self) -> tuple[str, ...]:
        self.state.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.results.prepare()
        ledger = self._load_ledger()
        processed: list[str] = []
        for job_dir in self.results.job_dirs():
            job_id = _safe_segment(job_dir.name, fallback="job")
            report_path = job_dir / "report.json"
            if not report_path.exists():
                continue
            source_sha = _sha256_file(report_path)
            ledger_entry = ledger.get(job_id)
            if ledger_entry:
                if ledger_entry["source_report_sha256"] != source_sha:
                    raise CandidateGateError(f"relayed result changed after gate processing: {job_id}")
                continue
            if job_id in self.config.plans:
                gate_report, processed_sha = self.process_job(job_dir, self.config.plans[job_id])
            elif job_id.startswith("build-next-"):
                gate_report, processed_sha = self.process_generic_job(job_dir)
            else:
                continue
            commit = self.results.publish_report(job_dir, gate_report)
            ledger[job_id] = {
                "source_report_sha256": processed_sha or source_sha,
                "results_commit": commit,
            }
            self._save_ledger(ledger)
            processed.append(job_id)
        return tuple(processed)

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except CandidateGateError as exc:
                _atomic_write_json(
                    self.state / "candidate-gate-error.json",
                    {
                        "recorded_at": _utc_now(),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "publication_enabled": False,
                    },
                )
            time.sleep(self.config.poll_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="msos-autobuilder-candidate-gate")
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gate = CandidateGate(load_candidate_gate_config(args.config))
    if args.once:
        processed = gate.run_once()
        print(
            json.dumps(
                {
                    "status": "completed",
                    "processed_jobs": list(processed),
                    "publication_enabled": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    gate.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
