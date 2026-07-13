"""Fail-safe exact-commit self-update supervisor for the Windows Autobuilder host.

The installed copy of this module runs from a stable environment outside every managed
Autobuilder release. Managed releases are immutable version directories selected by one
atomic active-release pointer. The supervisor never updates its own executing bootstrap.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REQUIRED_MANIFEST_PATHS = {
    "pyproject.toml",
    "src/msos_autobuilder/self_update_supervisor.py",
}


class ManifestError(ValueError):
    """Raised when an update manifest is malformed, unapproved, or unverifiable."""


class SupervisorError(RuntimeError):
    """Raised when staging, cutover, health verification, or rollback fails."""


class StagingError(SupervisorError):
    """Raised with the checks completed before version staging failed."""

    def __init__(self, message: str, checks: Sequence[Any] | None = None) -> None:
        super().__init__(message)
        self.checks = tuple(checks or ())


@dataclass(frozen=True)
class ExpectedFile:
    path: str
    sha256: str


@dataclass(frozen=True)
class UpdateManifest:
    version: int
    release_id: str
    approved: bool
    repository: str
    repo_url: str
    commit: str
    required_status_contexts: tuple[str, ...]
    expected_files: tuple[ExpectedFile, ...]
    manifest_sha256: str
    supervisor_update: bool = False


@dataclass(frozen=True)
class ManagedTask:
    service: str
    task_name: str


@dataclass(frozen=True)
class SupervisorConfig:
    supervisor_root: Path
    host_root: Path
    repo_url: str
    repository: str
    task_controller_script: Path
    release_probe_script: Path
    managed_tasks: tuple[ManagedTask, ...]
    health_timeout_seconds: float = 90.0
    health_poll_seconds: float = 2.0
    health_stability_seconds: float = 10.0
    github_token_env: str = "GITHUB_TOKEN"

    @property
    def versions_root(self) -> Path:
        return self.supervisor_root / "versions"

    @property
    def state_root(self) -> Path:
        return self.supervisor_root / "state"

    @property
    def active_pointer(self) -> Path:
        return self.state_root / "active-release.json"

    @property
    def ledger_path(self) -> Path:
        return self.state_root / "update-ledger.json"

    @property
    def previous_pointer(self) -> Path:
        return self.state_root / "previous-release.json"

    @property
    def reports_root(self) -> Path:
        return self.supervisor_root / "reports"

    @property
    def notifications_root(self) -> Path:
        return self.supervisor_root / "notifications"

    @property
    def witnesses_root(self) -> Path:
        return self.state_root / "service-witnesses"

    @property
    def lock_path(self) -> Path:
        return self.state_root / "update.lock"


@dataclass(frozen=True)
class CheckResult:
    name: str
    argv: tuple[str, ...]
    cwd: str
    returncode: int
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""

    @property
    def passed(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class StagedRelease:
    commit: str
    release_path: Path
    checks: tuple[CheckResult, ...]
    reused: bool = False


@dataclass
class UpdateReport:
    version: int
    attempt_id: str
    attempted_at: str
    release_id: str
    requested_commit: str
    manifest_sha256: str
    previous_commit: str | None = None
    previous_release_path: str | None = None
    staged_release_path: str | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    cutover: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    rollback: dict[str, Any] = field(default_factory=dict)
    outcome: str = "started"
    errors: list[str] = field(default_factory=list)


class StatusVerifier(Protocol):
    def verify(self, repository: str, commit: str, contexts: Sequence[str]) -> None: ...


class TaskController(Protocol):
    def stop(self, task_names: Sequence[str]) -> None: ...

    def start(self, task_names: Sequence[str]) -> None: ...

    def states(self, task_names: Sequence[str]) -> Mapping[str, str]: ...


CommandExecutor = Callable[[Sequence[str], Path, float], CheckResult]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat()


def _redact(text: str) -> str:
    redacted = re.sub(
        r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@",
        r"\1[redacted]@",
        text,
    )
    redacted = re.sub(
        r"(?i)\b(authorization\s*[:=]\s*bearer|token|access_token|password|api[_-]?key)"
        r"(\s*[:=]\s*)([^\s&]+)",
        r"\1\2[redacted]",
        redacted,
    )
    redacted = re.sub(r"\b(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{16,}\b", "[redacted]", redacted)
    return redacted


def _bounded(text: str, limit: int = 16_000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...[truncated]...\n" + text[-limit // 2 :]


def _safe_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{field_name} must be a mapping")
    return dict(value)


def _safe_relative_path(value: Any, field_name: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    candidate = Path(text)
    if not text or candidate.is_absolute() or ".." in candidate.parts:
        raise ManifestError(f"{field_name} must be a safe relative path")
    return text


def _canonical_manifest_payload(raw: Mapping[str, Any]) -> bytes:
    payload = dict(raw)
    payload.pop("manifest_sha256", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_manifest_sha256(raw: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_manifest_payload(raw)).hexdigest()


def _reject_embedded_credentials(repo_url: str) -> None:
    if "://" not in repo_url:
        return
    parsed = urllib.parse.urlsplit(repo_url)
    if parsed.username is not None or parsed.password is not None:
        raise ManifestError("repo_url may not contain embedded credentials")


def parse_update_manifest(text: str) -> UpdateManifest:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError("invalid update manifest YAML") from exc
    raw = _safe_mapping(loaded, "update manifest")
    if raw.get("version") != 1:
        raise ManifestError("only update manifest version 1 is supported")
    if raw.get("approved") is not True:
        raise ManifestError("update manifest must be explicitly approved")
    if raw.get("supervisor_update", False) is not False:
        raise ManifestError(
            "the managed-release transaction may not replace the executing supervisor"
        )

    release_id = str(raw.get("release_id") or "").strip()
    if not _SAFE_ID_RE.fullmatch(release_id):
        raise ManifestError("release_id is missing or unsafe")
    repository = str(raw.get("repository") or "").strip()
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
        raise ManifestError("repository must use owner/name form")
    repo_url = str(raw.get("repo_url") or "").strip()
    if not repo_url:
        raise ManifestError("repo_url is required")
    _reject_embedded_credentials(repo_url)
    commit = str(raw.get("commit") or "").strip()
    if not _COMMIT_RE.fullmatch(commit):
        raise ManifestError("commit must be an exact 40-character lowercase Git SHA")

    contexts_raw = raw.get("required_status_contexts")
    if not isinstance(contexts_raw, list) or not contexts_raw:
        raise ManifestError("required_status_contexts must be a non-empty list")
    contexts: list[str] = []
    for index, context in enumerate(contexts_raw):
        value = str(context or "").strip()
        if not value:
            raise ManifestError(f"required_status_contexts[{index}] is empty")
        if value in contexts:
            raise ManifestError(f"duplicate required status context: {value}")
        contexts.append(value)

    expected_raw = raw.get("expected_files")
    if not isinstance(expected_raw, list) or not expected_raw:
        raise ManifestError("expected_files must be a non-empty list")
    expected: list[ExpectedFile] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(expected_raw):
        entry = _safe_mapping(item, f"expected_files[{index}]")
        path = _safe_relative_path(entry.get("path"), f"expected_files[{index}].path")
        digest = str(entry.get("sha256") or "").strip().lower()
        if not _SHA256_RE.fullmatch(digest):
            raise ManifestError(f"expected_files[{index}].sha256 must be lowercase SHA-256")
        if path in seen_paths:
            raise ManifestError(f"duplicate expected file path: {path}")
        seen_paths.add(path)
        expected.append(ExpectedFile(path=path, sha256=digest))

    missing_required = sorted(_REQUIRED_MANIFEST_PATHS - seen_paths)
    if missing_required:
        raise ManifestError(
            "expected_files is missing required release anchors: " + ", ".join(missing_required)
        )

    supplied_hash = str(raw.get("manifest_sha256") or "").strip().lower()
    if not _SHA256_RE.fullmatch(supplied_hash):
        raise ManifestError("manifest_sha256 must be a lowercase SHA-256 digest")
    calculated_hash = compute_manifest_sha256(raw)
    if supplied_hash != calculated_hash:
        raise ManifestError("manifest_sha256 does not match canonical manifest contents")

    return UpdateManifest(
        version=1,
        release_id=release_id,
        approved=True,
        repository=repository,
        repo_url=repo_url,
        commit=commit,
        required_status_contexts=tuple(contexts),
        expected_files=tuple(expected),
        manifest_sha256=supplied_hash,
        supervisor_update=False,
    )


def load_supervisor_config(path: str | Path) -> SupervisorConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SupervisorError("invalid supervisor config YAML") from exc
    raw = _safe_mapping(loaded, "supervisor config")
    if raw.get("version") != 1:
        raise SupervisorError("only supervisor config version 1 is supported")

    def resolve(value: Any, field_name: str) -> Path:
        text = str(value or "").strip()
        if not text:
            raise SupervisorError(f"{field_name} is required")
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = config_path.parent / candidate
        return candidate.resolve()

    repository = str(raw.get("repository") or "").strip()
    repo_url = str(raw.get("repo_url") or "").strip()
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
        raise SupervisorError("repository must use owner/name form")
    if not repo_url:
        raise SupervisorError("repo_url is required")
    try:
        _reject_embedded_credentials(repo_url)
    except ManifestError as exc:
        raise SupervisorError(str(exc)) from exc

    tasks_raw = raw.get("managed_tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise SupervisorError("managed_tasks must be a non-empty list")
    tasks: list[ManagedTask] = []
    services: set[str] = set()
    names: set[str] = set()
    for index, item in enumerate(tasks_raw):
        entry = _safe_mapping(item, f"managed_tasks[{index}]")
        service = str(entry.get("service") or "").strip()
        task_name = str(entry.get("task_name") or "").strip()
        if not _SAFE_ID_RE.fullmatch(service):
            raise SupervisorError(f"managed_tasks[{index}].service is unsafe")
        if not task_name:
            raise SupervisorError(f"managed_tasks[{index}].task_name is required")
        if service in services or task_name in names:
            raise SupervisorError("managed task services and task names must be unique")
        services.add(service)
        names.add(task_name)
        tasks.append(ManagedTask(service=service, task_name=task_name))

    health_timeout = float(raw.get("health_timeout_seconds", 90.0))
    health_poll = float(raw.get("health_poll_seconds", 2.0))
    health_stability = float(raw.get("health_stability_seconds", 10.0))
    if health_timeout <= 0 or health_poll <= 0 or health_stability < 0:
        raise SupervisorError(
            "health timeout and poll interval must be positive and stability non-negative"
        )
    if health_stability >= health_timeout:
        raise SupervisorError("health stability window must be shorter than the health timeout")

    return SupervisorConfig(
        supervisor_root=resolve(raw.get("supervisor_root"), "supervisor_root"),
        host_root=resolve(raw.get("host_root"), "host_root"),
        repo_url=repo_url,
        repository=repository,
        task_controller_script=resolve(raw.get("task_controller_script"), "task_controller_script"),
        release_probe_script=resolve(raw.get("release_probe_script"), "release_probe_script"),
        managed_tasks=tuple(tasks),
        health_timeout_seconds=health_timeout,
        health_poll_seconds=health_poll,
        health_stability_seconds=health_stability,
        github_token_env=str(raw.get("github_token_env") or "GITHUB_TOKEN").strip(),
    )


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


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_expected_files(root: Path, expected_files: Iterable[ExpectedFile]) -> None:
    resolved_root = root.resolve()
    for expected in expected_files:
        candidate = (resolved_root / expected.path).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise SupervisorError(f"expected file escapes release root: {expected.path}") from exc
        if not candidate.is_file():
            raise SupervisorError(f"expected release file is missing: {expected.path}")
        actual = _file_sha256(candidate)
        if actual != expected.sha256:
            raise SupervisorError(
                f"expected release file hash mismatch for {expected.path}: "
                f"expected {expected.sha256}, got {actual}"
            )


def default_command_executor(argv: Sequence[str], cwd: Path, timeout: float) -> CheckResult:
    started = time.monotonic()
    completed = subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )
    return CheckResult(
        name="command",
        argv=tuple(str(part) for part in argv),
        cwd=str(cwd),
        returncode=completed.returncode,
        duration_seconds=time.monotonic() - started,
        stdout=_bounded(_redact(completed.stdout)),
        stderr=_bounded(_redact(completed.stderr)),
    )


def _run_named_check(
    executor: CommandExecutor,
    name: str,
    argv: Sequence[str],
    cwd: Path,
    timeout: float,
) -> CheckResult:
    result = executor(argv, cwd, timeout)
    return CheckResult(
        name=name,
        argv=tuple(str(part) for part in argv),
        cwd=str(cwd),
        returncode=result.returncode,
        duration_seconds=result.duration_seconds,
        stdout=result.stdout,
        stderr=result.stderr,
    )


class GitHubCommitStatusVerifier:
    """Verify required status/check names on one exact public GitHub commit."""

    def __init__(self, *, token: str | None = None, timeout_seconds: float = 30.0) -> None:
        self._token = token
        self._timeout_seconds = timeout_seconds

    def _get_json(self, url: str, *, accept: str) -> dict[str, Any]:
        headers = {
            "Accept": accept,
            "User-Agent": "msos-autobuilder-self-update-supervisor/1",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise SupervisorError(f"could not verify GitHub commit status: {exc}") from exc

    def verify(self, repository: str, commit: str, contexts: Sequence[str]) -> None:
        base = f"https://api.github.com/repos/{repository}/commits/{commit}"
        status_payload = self._get_json(f"{base}/status", accept="application/vnd.github+json")
        check_payload = self._get_json(f"{base}/check-runs", accept="application/vnd.github+json")
        observed: dict[str, str] = {}
        for status in status_payload.get("statuses", []):
            context = str(status.get("context") or "")
            if context and context not in observed:
                observed[context] = str(status.get("state") or "")
        for check in check_payload.get("check_runs", []):
            name = str(check.get("name") or "")
            if name and name not in observed:
                observed[name] = str(check.get("conclusion") or check.get("status") or "")

        failures = [
            context
            for context in contexts
            if observed.get(context) not in {"success", "neutral", "skipped"}
        ]
        if failures:
            details = ", ".join(f"{name}={observed.get(name, 'missing')}" for name in failures)
            raise SupervisorError(f"required GitHub commit checks are not successful: {details}")


class ReleaseBuilder:
    """Fetch, verify, install, and test one exact commit in an immutable directory."""

    def __init__(
        self,
        config: SupervisorConfig,
        *,
        status_verifier: StatusVerifier,
        command_executor: CommandExecutor = default_command_executor,
        powershell_executable: str | None = None,
    ) -> None:
        self.config = config
        self.status_verifier = status_verifier
        self.command_executor = command_executor
        self.powershell_executable = (
            powershell_executable or shutil.which("powershell.exe") or shutil.which("pwsh")
        )

    def _git(
        self, args: Sequence[str], cwd: Path, name: str, timeout: float = 180.0
    ) -> CheckResult:
        result = _run_named_check(
            self.command_executor,
            name,
            ["git", *args],
            cwd,
            timeout,
        )
        if not result.passed:
            raise StagingError(
                f"{name} failed with exit {result.returncode}: {result.stderr or result.stdout}",
                (result,),
            )
        return result

    def stage(self, manifest: UpdateManifest) -> StagedRelease:
        if (
            manifest.repository != self.config.repository
            or manifest.repo_url != self.config.repo_url
        ):
            raise SupervisorError("manifest repository identity does not match supervisor config")
        final_path = self.config.versions_root / manifest.commit
        release_marker = final_path / "release.json"
        if final_path.exists() and release_marker.is_file():
            marker = _load_json(release_marker, {})
            if marker.get("commit") != manifest.commit:
                raise SupervisorError("existing version directory has a mismatched release marker")
            marker_manifest = marker.get("manifest_sha256")
            if marker_manifest and marker_manifest != manifest.manifest_sha256:
                raise SupervisorError("existing version is bound to a different approved manifest")
            self.status_verifier.verify(
                manifest.repository, manifest.commit, manifest.required_status_contexts
            )
            verify_expected_files(final_path, manifest.expected_files)
            return StagedRelease(manifest.commit, final_path, (), reused=True)

        if final_path.exists():
            active = _load_json(self.config.active_pointer, {})
            active_path = (
                Path(str(active.get("release_path") or "")) if isinstance(active, dict) else Path()
            )
            if active_path and active_path.resolve() == final_path.resolve():
                raise SupervisorError(
                    "active release directory is incomplete and may not be replaced"
                )
            shutil.rmtree(final_path)
        self.config.versions_root.mkdir(parents=True, exist_ok=True)
        staging_path = final_path
        staging_path.mkdir(parents=True, exist_ok=False)
        checks: list[CheckResult] = []
        try:
            checks.append(self._git(["init", "--quiet"], staging_path, "git-init"))
            checks.append(
                self._git(
                    ["remote", "add", "origin", manifest.repo_url],
                    staging_path,
                    "git-add-origin",
                )
            )
            checks.append(
                self._git(
                    [
                        "-c",
                        "core.autocrlf=false",
                        "fetch",
                        "--depth",
                        "1",
                        "origin",
                        manifest.commit,
                    ],
                    staging_path,
                    "git-fetch-exact-commit",
                    timeout=300.0,
                )
            )
            checks.append(
                self._git(
                    ["-c", "core.autocrlf=false", "checkout", "--detach", "FETCH_HEAD"],
                    staging_path,
                    "git-checkout-exact-commit",
                )
            )
            head_result = _run_named_check(
                self.command_executor,
                "git-rev-parse-head",
                ["git", "rev-parse", "HEAD"],
                staging_path,
                30.0,
            )
            checks.append(head_result)
            if not head_result.passed or head_result.stdout.strip() != manifest.commit:
                raise SupervisorError("staged Git HEAD does not equal the approved exact commit")

            verify_expected_files(staging_path, manifest.expected_files)
            if not self.config.release_probe_script.is_file():
                raise SupervisorError(
                    f"stable release health probe is missing: {self.config.release_probe_script}"
                )

            venv_path = staging_path / ".venv"
            venv_python = (
                venv_path / "Scripts" / "python.exe"
                if os.name == "nt"
                else venv_path / "bin" / "python"
            )
            stage_commands: list[tuple[str, list[str], float]] = [
                ("create-version-venv", [sys.executable, "-m", "venv", str(venv_path)], 300.0),
                (
                    "install-release",
                    [
                        str(venv_python),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "-e",
                        f"{staging_path}[dev]",
                    ],
                    900.0,
                ),
                ("ruff", [str(venv_python), "-m", "ruff", "check", "."], 300.0),
                ("pytest", [str(venv_python), "-m", "pytest", "-q"], 1800.0),
                (
                    "release-health-probe",
                    [
                        str(venv_python),
                        str(self.config.release_probe_script),
                        str(staging_path),
                    ],
                    120.0,
                ),
            ]
            for name, argv, timeout in stage_commands:
                result = _run_named_check(self.command_executor, name, argv, staging_path, timeout)
                checks.append(result)
                if not result.passed:
                    raise SupervisorError(
                        f"staging check {name} failed with exit {result.returncode}: "
                        f"{result.stderr or result.stdout}"
                    )

            if os.name == "nt":
                if not self.powershell_executable:
                    raise SupervisorError("PowerShell is required for Windows parser checks")
                parser_script = (
                    "$ErrorActionPreference='Stop'; $failed=@(); "
                    "Get-ChildItem -Path . -Recurse -Filter *.ps1 | ForEach-Object { "
                    "$tokens=$null; $errors=$null; "
                    "[System.Management.Automation.Language.Parser]::ParseFile("
                    "$_.FullName,[ref]$tokens,[ref]$errors) | Out-Null; "
                    "if($errors.Count -gt 0){$failed += $_.FullName} }; "
                    "if($failed.Count -gt 0){$failed | Write-Error; exit 1}"
                )
                result = _run_named_check(
                    self.command_executor,
                    "powershell-parser",
                    [self.powershell_executable, "-NoProfile", "-Command", parser_script],
                    staging_path,
                    300.0,
                )
                checks.append(result)
                if not result.passed:
                    raise SupervisorError("PowerShell parser checks failed")

            # A long staging run must not activate a commit whose required review status
            # changed while tests were running.
            self.status_verifier.verify(
                manifest.repository, manifest.commit, manifest.required_status_contexts
            )
            _atomic_write_json(
                staging_path / "release.json",
                {
                    "version": 1,
                    "release_id": manifest.release_id,
                    "commit": manifest.commit,
                    "manifest_sha256": manifest.manifest_sha256,
                    "staged_at": _timestamp(),
                },
            )
            return StagedRelease(manifest.commit, final_path, tuple(checks), reused=False)
        except Exception as exc:
            shutil.rmtree(staging_path, ignore_errors=True)
            if isinstance(exc, StagingError):
                raise StagingError(str(exc), [*checks, *exc.checks]) from exc
            raise StagingError(str(exc), checks) from exc


class PowerShellTaskController:
    """Control only the configured scheduled tasks through an external stable script."""

    def __init__(self, script_path: Path, *, executable: str | None = None) -> None:
        self.script_path = script_path
        self.executable = executable or shutil.which("powershell.exe") or shutil.which("pwsh")
        if not self.executable:
            raise SupervisorError("PowerShell is required for Windows task control")
        if not self.script_path.is_file():
            raise SupervisorError(f"task controller script not found: {self.script_path}")

    def _invoke(self, action: str, task_names: Sequence[str]) -> Any:
        payload = json.dumps(list(task_names), separators=(",", ":"))
        completed = subprocess.run(
            [
                self.executable,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.script_path),
                "-Action",
                action,
            ],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
        )
        if completed.returncode != 0:
            detail = _redact(completed.stderr or completed.stdout)
            raise SupervisorError(f"scheduled-task {action} failed: {detail}")
        if action == "states":
            try:
                return json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise SupervisorError("task controller returned invalid state JSON") from exc
        return None

    def stop(self, task_names: Sequence[str]) -> None:
        self._invoke("stop", task_names)

    def start(self, task_names: Sequence[str]) -> None:
        self._invoke("start", task_names)

    def states(self, task_names: Sequence[str]) -> Mapping[str, str]:
        raw = self._invoke("states", task_names)
        if not isinstance(raw, dict):
            raise SupervisorError("task state response must be an object")
        return {str(key): str(value) for key, value in raw.items()}


class FileHealthVerifier:
    """Require running tasks and fresh release-bound witnesses from stable task wrappers."""

    def __init__(self, config: SupervisorConfig, task_controller: TaskController) -> None:
        self.config = config
        self.task_controller = task_controller

    def wait_for(self, commit: str, not_before: datetime) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.health_timeout_seconds
        task_names = [task.task_name for task in self.config.managed_tasks]
        last_detail: dict[str, Any] = {}
        healthy_since: float | None = None
        while time.monotonic() < deadline:
            observed_at = time.monotonic()
            states = dict(self.task_controller.states(task_names))
            witnesses: dict[str, Any] = {}
            healthy = True
            for task in self.config.managed_tasks:
                state = states.get(task.task_name, "Missing")
                if state.lower() != "running":
                    healthy = False
                witness_path = self.config.witnesses_root / f"{task.service}.json"
                witness = _load_json(witness_path, {})
                witnesses[task.service] = witness
                try:
                    started_at = datetime.fromisoformat(str(witness.get("started_at")))
                except (TypeError, ValueError):
                    started_at = datetime.min.replace(tzinfo=UTC)
                if (
                    witness.get("release_commit") != commit
                    or witness.get("state") != "running"
                    or started_at < not_before
                    or not isinstance(witness.get("child_pid"), int)
                ):
                    healthy = False
            last_detail = {"task_states": states, "witnesses": witnesses}
            if healthy:
                if healthy_since is None:
                    healthy_since = observed_at
                if observed_at - healthy_since >= self.config.health_stability_seconds:
                    return {
                        **last_detail,
                        "stability_seconds": self.config.health_stability_seconds,
                    }
            else:
                healthy_since = None
            time.sleep(self.config.health_poll_seconds)
        raise SupervisorError(
            "managed tasks did not produce a complete post-cutover health witness: "
            + json.dumps(last_detail, sort_keys=True)
        )


def _read_active_pointer(config: SupervisorConfig) -> dict[str, Any]:
    raw = _load_json(config.active_pointer, {})
    if not isinstance(raw, dict):
        raise SupervisorError("active release pointer is malformed")
    commit = str(raw.get("commit") or "")
    release_path = Path(str(raw.get("release_path") or ""))
    if not _COMMIT_RE.fullmatch(commit) or not release_path.is_dir():
        raise SupervisorError(
            "active release pointer is missing or invalid; rollback is unavailable"
        )
    marker = _load_json(release_path / "release.json", {})
    if marker.get("commit") != commit:
        raise SupervisorError("active release marker does not match active release pointer")
    return raw


def _write_active_pointer(config: SupervisorConfig, commit: str, release_path: Path) -> None:
    marker = _load_json(release_path / "release.json", {})
    if marker.get("commit") != commit:
        raise SupervisorError("cannot activate a release without a matching immutable marker")
    _atomic_write_json(
        config.active_pointer,
        {
            "version": 1,
            "commit": commit,
            "release_path": str(release_path.resolve()),
            "activated_at": _timestamp(),
        },
    )


def _write_immutable_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise SupervisorError(f"immutable evidence already exists: {path}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _write_immutable_report(config: SupervisorConfig, report: UpdateReport) -> Path:
    path = config.reports_root / f"{report.attempt_id}.json"
    return _write_immutable_json(path, asdict(report))


def _write_update_notification(
    config: SupervisorConfig, report: UpdateReport, report_path: Path
) -> Path:
    path = config.notifications_root / f"{report.attempt_id}.json"
    return _write_immutable_json(
        path,
        {
            "version": 1,
            "type": "autobuilder-self-update",
            "attempt_id": report.attempt_id,
            "outcome": report.outcome,
            "requested_commit": report.requested_commit,
            "report_path": str(report_path),
            "requires_founder_attention": report.outcome not in {"success", "already_applied"},
            "recorded_at": _timestamp(),
        },
    )


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def _exclusive_update_lock(config: SupervisorConfig):
    config.state_root.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            descriptor = os.open(config.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError as exc:
            try:
                existing = _load_json(config.lock_path, {})
            except (OSError, ValueError, json.JSONDecodeError):
                existing = {}
            pid = existing.get("pid") if isinstance(existing, dict) else None
            if isinstance(pid, int) and _pid_is_running(pid):
                raise SupervisorError(
                    "another self-update supervisor attempt is already running"
                ) from exc
            config.lock_path.unlink(missing_ok=True)
    else:
        raise SupervisorError("could not acquire the self-update supervisor lock")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "started_at": _timestamp()}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        config.lock_path.unlink(missing_ok=True)


class UpdateSupervisor:
    def __init__(
        self,
        config: SupervisorConfig,
        *,
        release_builder: ReleaseBuilder,
        task_controller: TaskController,
        health_verifier: FileHealthVerifier,
    ) -> None:
        self.config = config
        self.release_builder = release_builder
        self.task_controller = task_controller
        self.health_verifier = health_verifier

    def _restore_previous_release(
        self,
        task_names: Sequence[str],
        previous_commit: str,
        previous_release_path: Path,
    ) -> tuple[dict[str, Any], list[str]]:
        started = _utc_now()
        errors: list[str] = []
        evidence: dict[str, Any] = {
            "performed": True,
            "restored_commit": previous_commit,
        }
        try:
            self.task_controller.stop(task_names)
        except Exception as exc:
            errors.append(f"rollback stop failed: {exc}")
        try:
            _write_active_pointer(self.config, previous_commit, previous_release_path)
            evidence["pointer_restored"] = True
        except Exception as exc:
            errors.append(f"rollback pointer restore failed: {exc}")
            evidence["pointer_restored"] = False
        try:
            self.task_controller.start(task_names)
            evidence["tasks_started"] = True
        except Exception as exc:
            errors.append(f"rollback start failed: {exc}")
            evidence["tasks_started"] = False
        try:
            evidence.update(self.health_verifier.wait_for(previous_commit, started))
        except Exception as exc:
            errors.append(f"rollback health failed: {exc}")
        evidence["passed"] = not errors
        if errors:
            evidence["errors"] = errors
        return evidence, errors

    def apply(self, manifest_text: str) -> tuple[UpdateReport, Path]:
        attempt_id = _utc_now().strftime("%Y%m%dT%H%M%S.%fZ") + f"-{uuid.uuid4().hex[:12]}"
        try:
            manifest = parse_update_manifest(manifest_text)
        except Exception as exc:
            report = UpdateReport(
                version=1,
                attempt_id=attempt_id,
                attempted_at=_timestamp(),
                release_id="invalid-manifest",
                requested_commit="",
                manifest_sha256=hashlib.sha256(manifest_text.encode("utf-8")).hexdigest(),
                outcome="rejected_manifest",
                cutover={"performed": False},
                errors=[str(exc)],
            )
            report_path = _write_immutable_report(self.config, report)
            _write_update_notification(self.config, report, report_path)
            return report, report_path

        report = UpdateReport(
            version=1,
            attempt_id=attempt_id,
            attempted_at=_timestamp(),
            release_id=manifest.release_id,
            requested_commit=manifest.commit,
            manifest_sha256=manifest.manifest_sha256,
        )
        ledger_entry: dict[str, Any] | None = None
        report_path: Path | None = None
        try:
            with _exclusive_update_lock(self.config):
                if (
                    manifest.repository != self.config.repository
                    or manifest.repo_url != self.config.repo_url
                ):
                    raise SupervisorError(
                        "manifest repository does not match stable supervisor config"
                    )
                ledger = _load_json(self.config.ledger_path, {"version": 1, "commits": {}})
                if ledger.get("version") != 1 or not isinstance(ledger.get("commits"), dict):
                    raise SupervisorError("update ledger is malformed")
                prior_entry = ledger["commits"].get(manifest.commit)
                if prior_entry:
                    if prior_entry.get("manifest_sha256") != manifest.manifest_sha256:
                        raise SupervisorError(
                            "the exact commit is already bound to a different approved manifest"
                        )
                    prior_report_id = str(prior_entry.get("report_attempt_id") or "")
                    prior_report_hash = str(prior_entry.get("report_sha256") or "")
                    prior_report = self.config.reports_root / f"{prior_report_id}.json"
                    if (
                        not prior_report_id
                        or not _SHA256_RE.fullmatch(prior_report_hash)
                        or not prior_report.is_file()
                        or _file_sha256(prior_report) != prior_report_hash
                    ):
                        raise SupervisorError(
                            "update ledger entry is missing its immutable report evidence"
                        )
                    report.outcome = (
                        "already_applied"
                        if prior_entry.get("outcome") == "success"
                        else "blocked_after_rollback"
                    )
                    report.cutover = {"performed": False, "reason": report.outcome}
                    report_path = _write_immutable_report(self.config, report)
                    _write_update_notification(self.config, report, report_path)
                    return report, report_path

                active = _read_active_pointer(self.config)
                previous_commit = str(active["commit"])
                previous_release_path = Path(str(active["release_path"]))
                report.previous_commit = previous_commit
                report.previous_release_path = str(previous_release_path)

                try:
                    staged = self.release_builder.stage(manifest)
                except StagingError as exc:
                    report.checks = [
                        asdict(check) | {"passed": check.passed} for check in exc.checks
                    ]
                    raise
                report.staged_release_path = str(staged.release_path)
                report.checks = [
                    asdict(check) | {"passed": check.passed} for check in staged.checks
                ]

                task_names = [task.task_name for task in self.config.managed_tasks]
                cutover_started = _utc_now()
                _atomic_write_json(self.config.previous_pointer, active)
                report.cutover = {
                    "performed": True,
                    "at": _timestamp(cutover_started),
                    "from_commit": previous_commit,
                    "to_commit": staged.commit,
                    "pointer_swapped": False,
                    "tasks_started": False,
                }
                try:
                    self.task_controller.stop(task_names)
                    _write_active_pointer(self.config, staged.commit, staged.release_path)
                    report.cutover["pointer_swapped"] = True
                    self.task_controller.start(task_names)
                    report.cutover["tasks_started"] = True
                    health = self.health_verifier.wait_for(staged.commit, cutover_started)
                    report.health = {"passed": True, **health}
                    report.rollback = {"performed": False}
                    report.outcome = "success"
                    ledger_entry = {
                        "manifest_sha256": manifest.manifest_sha256,
                        "outcome": "success",
                        "report_attempt_id": attempt_id,
                        "recorded_at": _timestamp(),
                    }
                except Exception as cutover_exc:
                    report.cutover["error"] = str(cutover_exc)
                    report.health = {"passed": False, "error": str(cutover_exc)}
                    rollback, rollback_errors = self._restore_previous_release(
                        task_names, previous_commit, previous_release_path
                    )
                    report.rollback = rollback
                    report.outcome = "rolled_back" if not rollback_errors else "rollback_failed"
                    report.errors.append(str(cutover_exc))
                    report.errors.extend(rollback_errors)
                    ledger_entry = {
                        "manifest_sha256": manifest.manifest_sha256,
                        "outcome": report.outcome,
                        "report_attempt_id": attempt_id,
                        "recorded_at": _timestamp(),
                    }

                report_path = _write_immutable_report(self.config, report)
                _write_update_notification(self.config, report, report_path)
                if ledger_entry is not None:
                    ledger_entry["report_sha256"] = _file_sha256(report_path)
                    ledger["commits"][manifest.commit] = ledger_entry
                    _atomic_write_json(self.config.ledger_path, ledger)
        except Exception as exc:
            if not report.errors or report.errors[-1] != str(exc):
                report.errors.append(str(exc))
            if report.outcome == "started":
                report.outcome = "failed_before_cutover"
                report.cutover = {"performed": False}
            if report_path is None:
                report_path = _write_immutable_report(self.config, report)
                _write_update_notification(self.config, report, report_path)
        assert report_path is not None
        return report, report_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    apply_parser = subparsers.add_parser("apply", help="apply one approved exact-commit manifest")
    apply_parser.add_argument("--config", required=True)
    apply_parser.add_argument("--manifest", required=True)

    rollback_parser = subparsers.add_parser(
        "rollback", help="restore the recorded previous release"
    )
    rollback_parser.add_argument("--config", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = load_supervisor_config(args.config)
    task_controller = PowerShellTaskController(config.task_controller_script)
    health_verifier = FileHealthVerifier(config, task_controller)
    if args.command == "rollback":
        active = _read_active_pointer(config)
        previous = _load_json(config.previous_pointer, {})
        previous_commit = str(previous.get("commit") or "")
        previous_path = Path(str(previous.get("release_path") or ""))
        if not _COMMIT_RE.fullmatch(previous_commit) or not previous_path.is_dir():
            raise SupervisorError("no valid previous release is recorded for manual rollback")
        task_names = [task.task_name for task in config.managed_tasks]
        started = _utc_now()
        task_controller.stop(task_names)
        _write_active_pointer(config, previous_commit, previous_path)
        task_controller.start(task_names)
        health = health_verifier.wait_for(previous_commit, started)
        _atomic_write_json(config.previous_pointer, active)
        print(
            json.dumps(
                {"outcome": "manual_rollback", "commit": previous_commit, "health": health},
                sort_keys=True,
            )
        )
        return 0

    token = os.environ.get(config.github_token_env) or None
    builder = ReleaseBuilder(
        config,
        status_verifier=GitHubCommitStatusVerifier(token=token),
    )
    supervisor = UpdateSupervisor(
        config,
        release_builder=builder,
        task_controller=task_controller,
        health_verifier=health_verifier,
    )
    report, report_path = supervisor.apply(Path(args.manifest).read_text(encoding="utf-8"))
    print(json.dumps({"outcome": report.outcome, "report": str(report_path)}, sort_keys=True))
    return 0 if report.outcome in {"success", "already_applied", "blocked_after_rollback"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
