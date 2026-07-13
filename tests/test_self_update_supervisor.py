from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from msos_autobuilder.self_update_supervisor import (
    CheckResult,
    ExpectedFile,
    ManifestError,
    ReleaseBuilder,
    StagedRelease,
    SupervisorConfig,
    SupervisorError,
    UpdateManifest,
    UpdateSupervisor,
    _write_active_pointer,
    compute_manifest_sha256,
    parse_update_manifest,
    verify_expected_files,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _manifest_dict(*, commit: str = "a" * 40, file_hash: str | None = None) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": 1,
        "release_id": "release-1",
        "approved": True,
        "repository": "DanielTabakman/msos-autobuilder",
        "repo_url": "https://github.com/DanielTabakman/msos-autobuilder.git",
        "commit": commit,
        "required_status_contexts": ["CI", "Windows Smoke"],
        "expected_files": [
            {"path": "pyproject.toml", "sha256": file_hash or ("b" * 64)},
            {
                "path": "src/msos_autobuilder/self_update_supervisor.py",
                "sha256": "c" * 64,
            },
        ],
        "supervisor_update": False,
    }
    raw["manifest_sha256"] = compute_manifest_sha256(raw)
    return raw


def _manifest_text(**kwargs: Any) -> str:
    return yaml.safe_dump(_manifest_dict(**kwargs), sort_keys=False)


def _config(tmp_path: Path) -> SupervisorConfig:
    return SupervisorConfig(
        supervisor_root=tmp_path / "supervisor",
        host_root=tmp_path / "host",
        repo_url="https://github.com/DanielTabakman/msos-autobuilder.git",
        repository="DanielTabakman/msos-autobuilder",
        task_controller_script=tmp_path / "task-control.ps1",
        managed_tasks=(
            __import__(
                "msos_autobuilder.self_update_supervisor", fromlist=["ManagedTask"]
            ).ManagedTask("host", "MSOS Autobuilder Host"),
            __import__(
                "msos_autobuilder.self_update_supervisor", fromlist=["ManagedTask"]
            ).ManagedTask("relay", "MSOS Autobuilder Result Relay"),
        ),
        health_timeout_seconds=0.1,
        health_poll_seconds=0.01,
    )


def _release(config: SupervisorConfig, commit: str) -> Path:
    path = config.versions_root / commit
    path.mkdir(parents=True)
    (path / "release.json").write_text(
        json.dumps({"version": 1, "commit": commit}) + "\n", encoding="utf-8"
    )
    return path


def test_manifest_requires_approval_exact_commit_and_canonical_hash() -> None:
    manifest = parse_update_manifest(_manifest_text())
    assert manifest.commit == "a" * 40
    assert manifest.approved is True
    assert manifest.supervisor_update is False

    unapproved = _manifest_dict()
    unapproved["approved"] = False
    unapproved["manifest_sha256"] = compute_manifest_sha256(unapproved)
    with pytest.raises(ManifestError, match="explicitly approved"):
        parse_update_manifest(yaml.safe_dump(unapproved))

    short_commit = _manifest_dict()
    short_commit["commit"] = "a" * 39
    short_commit["manifest_sha256"] = compute_manifest_sha256(short_commit)
    with pytest.raises(ManifestError, match="exact 40-character"):
        parse_update_manifest(yaml.safe_dump(short_commit))

    tampered = _manifest_dict()
    tampered["release_id"] = "changed-after-hash"
    with pytest.raises(ManifestError, match="does not match"):
        parse_update_manifest(yaml.safe_dump(tampered))


def test_manifest_forbids_same_transaction_supervisor_replacement() -> None:
    raw = _manifest_dict()
    raw["supervisor_update"] = True
    raw["manifest_sha256"] = compute_manifest_sha256(raw)
    with pytest.raises(ManifestError, match="may not replace the executing supervisor"):
        parse_update_manifest(yaml.safe_dump(raw))


def test_manifest_rejects_repo_urls_with_embedded_credentials() -> None:
    raw = _manifest_dict()
    raw["repo_url"] = "https://token@example.com/repo.git"
    raw["manifest_sha256"] = compute_manifest_sha256(raw)
    with pytest.raises(ManifestError, match="embedded credentials"):
        parse_update_manifest(yaml.safe_dump(raw))


def test_expected_files_are_path_safe_and_hash_verified(tmp_path: Path) -> None:
    (tmp_path / "good.txt").write_text("content", encoding="utf-8")
    verify_expected_files(tmp_path, [ExpectedFile("good.txt", _sha("content"))])

    with pytest.raises(SupervisorError, match="hash mismatch"):
        verify_expected_files(tmp_path, [ExpectedFile("good.txt", "0" * 64)])

    with pytest.raises(SupervisorError, match="escapes release root"):
        verify_expected_files(tmp_path, [ExpectedFile("../outside.txt", "0" * 64)])


@dataclass
class FakeBuilder:
    staged: StagedRelease | None = None
    error: Exception | None = None
    calls: int = 0

    def stage(self, manifest: UpdateManifest) -> StagedRelease:
        self.calls += 1
        if self.error:
            raise self.error
        assert self.staged is not None
        return self.staged


class FakeTasks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def stop(self, task_names: Sequence[str]) -> None:
        self.calls.append(("stop", tuple(task_names)))

    def start(self, task_names: Sequence[str]) -> None:
        self.calls.append(("start", tuple(task_names)))

    def states(self, task_names: Sequence[str]) -> dict[str, str]:
        return {name: "Running" for name in task_names}


class FakeHealth:
    def __init__(self, outcomes: list[dict[str, Any] | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def wait_for(self, commit: str, not_before: datetime) -> dict[str, Any]:
        assert not_before.tzinfo is UTC
        self.calls.append(commit)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _supervisor(
    config: SupervisorConfig,
    builder: FakeBuilder,
    tasks: FakeTasks,
    health: FakeHealth,
) -> UpdateSupervisor:
    return UpdateSupervisor(
        config,
        release_builder=builder,  # type: ignore[arg-type]
        task_controller=tasks,
        health_verifier=health,  # type: ignore[arg-type]
    )


def test_staging_failure_never_stops_managed_tasks(tmp_path: Path) -> None:
    config = _config(tmp_path)
    previous = _release(config, "1" * 40)
    _write_active_pointer(config, "1" * 40, previous)
    builder = FakeBuilder(error=SupervisorError("tests failed"))
    tasks = FakeTasks()
    health = FakeHealth([])

    report, report_path = _supervisor(config, builder, tasks, health).apply(_manifest_text())

    assert report.outcome == "failed_before_cutover"
    assert report.cutover == {"performed": False}
    assert tasks.calls == []
    assert json.loads(config.active_pointer.read_text())["commit"] == "1" * 40
    assert report_path.is_file()
    notification = config.notifications_root / report_path.name
    assert json.loads(notification.read_text())["requires_founder_attention"] is True


def test_successful_cutover_changes_one_pointer_and_records_ledger(tmp_path: Path) -> None:
    config = _config(tmp_path)
    previous = _release(config, "1" * 40)
    current = _release(config, "a" * 40)
    _write_active_pointer(config, "1" * 40, previous)
    builder = FakeBuilder(staged=StagedRelease("a" * 40, current, ()))
    tasks = FakeTasks()
    health = FakeHealth([{"task_states": {"host": "Running"}}])

    report, report_path = _supervisor(config, builder, tasks, health).apply(_manifest_text())

    assert report.outcome == "success"
    assert [call[0] for call in tasks.calls] == ["stop", "start"]
    assert json.loads(config.active_pointer.read_text())["commit"] == "a" * 40
    ledger = json.loads(config.ledger_path.read_text())
    assert ledger["commits"]["a" * 40]["outcome"] == "success"
    assert (
        ledger["commits"]["a" * 40]["report_sha256"]
        == hashlib.sha256(report_path.read_bytes()).hexdigest()
    )
    notification = config.notifications_root / report_path.name
    assert json.loads(notification.read_text())["requires_founder_attention"] is False
    assert json.loads(report_path.read_text())["rollback"] == {"performed": False}


def test_failed_health_automatically_restores_previous_release(tmp_path: Path) -> None:
    config = _config(tmp_path)
    previous = _release(config, "1" * 40)
    broken = _release(config, "a" * 40)
    _write_active_pointer(config, "1" * 40, previous)
    builder = FakeBuilder(staged=StagedRelease("a" * 40, broken, ()))
    tasks = FakeTasks()
    health = FakeHealth(
        [SupervisorError("new release witness missing"), {"task_states": {"host": "Running"}}]
    )

    report, _ = _supervisor(config, builder, tasks, health).apply(_manifest_text())

    assert report.outcome == "rolled_back"
    assert report.health["passed"] is False
    assert report.rollback["passed"] is True
    assert [call[0] for call in tasks.calls] == ["stop", "start", "stop", "start"]
    assert json.loads(config.active_pointer.read_text())["commit"] == "1" * 40
    ledger = json.loads(config.ledger_path.read_text())
    assert ledger["commits"]["a" * 40]["outcome"] == "rolled_back"


def test_repeat_exact_commit_is_safe_and_does_not_cut_over_again(tmp_path: Path) -> None:
    config = _config(tmp_path)
    previous = _release(config, "1" * 40)
    current = _release(config, "a" * 40)
    _write_active_pointer(config, "1" * 40, previous)
    first_builder = FakeBuilder(staged=StagedRelease("a" * 40, current, ()))
    first_tasks = FakeTasks()
    first_health = FakeHealth([{}])
    first, _ = _supervisor(config, first_builder, first_tasks, first_health).apply(_manifest_text())
    assert first.outcome == "success"

    second_builder = FakeBuilder(error=AssertionError("must not restage"))
    second_tasks = FakeTasks()
    second_health = FakeHealth([])
    second, _ = _supervisor(config, second_builder, second_tasks, second_health).apply(
        _manifest_text()
    )

    assert second.outcome == "already_applied"
    assert second_builder.calls == 0
    assert second_tasks.calls == []


def test_rolled_back_commit_is_blocked_from_repeated_cutover(tmp_path: Path) -> None:
    config = _config(tmp_path)
    previous = _release(config, "1" * 40)
    broken = _release(config, "a" * 40)
    _write_active_pointer(config, "1" * 40, previous)
    first = _supervisor(
        config,
        FakeBuilder(staged=StagedRelease("a" * 40, broken, ())),
        FakeTasks(),
        FakeHealth([SupervisorError("broken"), {}]),
    )
    report, _ = first.apply(_manifest_text())
    assert report.outcome == "rolled_back"

    tasks = FakeTasks()
    builder = FakeBuilder(error=AssertionError("blocked commits must not restage"))
    repeated, _ = _supervisor(config, builder, tasks, FakeHealth([])).apply(_manifest_text())
    assert repeated.outcome == "blocked_after_rollback"
    assert tasks.calls == []
    assert builder.calls == 0


class RecordingStatusVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def verify(self, repository: str, commit: str, contexts: Sequence[str]) -> None:
        self.calls.append((repository, commit, tuple(contexts)))


def _hybrid_executor(argv: Sequence[str], cwd: Path, timeout: float) -> CheckResult:
    if argv[0] == "git":
        result = subprocess.run(
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
            name="raw",
            argv=tuple(argv),
            cwd=str(cwd),
            returncode=result.returncode,
            duration_seconds=0.0,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return CheckResult(
        name="raw",
        argv=tuple(argv),
        cwd=str(cwd),
        returncode=0,
        duration_seconds=0.0,
    )


def test_release_builder_fetches_and_verifies_only_exact_commit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    module = source / "src" / "msos_autobuilder" / "self_update_supervisor.py"
    module.parent.mkdir(parents=True)
    module.write_text("# fixture supervisor\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "pyproject.toml", str(module.relative_to(source))], cwd=source, check=True
    )
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=source, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    file_hash = hashlib.sha256((source / "pyproject.toml").read_bytes()).hexdigest()

    config = _config(tmp_path)
    config = SupervisorConfig(
        **{
            **config.__dict__,
            "repo_url": str(source),
            "repository": "fixture/repo",
        }
    )
    raw = _manifest_dict(commit=commit, file_hash=file_hash)
    raw["repository"] = "fixture/repo"
    raw["repo_url"] = str(source)
    raw["expected_files"][1]["sha256"] = hashlib.sha256(module.read_bytes()).hexdigest()
    raw["manifest_sha256"] = compute_manifest_sha256(raw)
    manifest = parse_update_manifest(yaml.safe_dump(raw))
    verifier = RecordingStatusVerifier()
    builder = ReleaseBuilder(
        config,
        status_verifier=verifier,
        command_executor=_hybrid_executor,
    )

    staged = builder.stage(manifest)

    assert staged.commit == commit
    assert staged.release_path.name == commit
    assert json.loads((staged.release_path / "release.json").read_text())["commit"] == commit
    assert verifier.calls == [("fixture/repo", commit, ("CI", "Windows Smoke"))]
    reused = builder.stage(manifest)
    assert reused.reused is True


def test_public_update_manifest_example_is_canonically_hashed() -> None:
    example = Path(__file__).resolve().parents[1] / "config" / "update_manifest.example.yaml"
    manifest = parse_update_manifest(example.read_text(encoding="utf-8"))
    assert manifest.approved is True
    assert manifest.commit == "0" * 40
    assert manifest.supervisor_update is False
