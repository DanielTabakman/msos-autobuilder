from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

import msos_autobuilder.lifecycle_evidence as lifecycle
import msos_autobuilder.persistent_host as persistent_host_module
from msos_autobuilder.codex_shadow import CodexShadowReport
from msos_autobuilder.persistent_host import (
    HostJobError,
    HostLockError,
    HostPaths,
    HostProcessLock,
    PersistentHost,
    PersistentHostConfig,
    PersistentHostConfigError,
    approve_pending_job,
    enqueue_manifest,
    load_persistent_host_config,
    parse_host_job,
    sync_git_job_feed,
)
from msos_autobuilder.work_admission import (
    AdmissionRequest,
    AdmissionStatus,
    admit_work,
    objective_identity_from_work,
    release_claim,
)


def _git(path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return proc.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-qm", "init")
    return path


def _manifest(*, allow_changes: bool = False) -> dict[str, Any]:
    return {
        "version": 1,
        "publication_enabled": False,
        "lanes": [
            {
                "task_id": "web-task",
                "lane_id": "web-lane",
                "chapter_id": "chapter-web",
                "branch": "autobuilder/web",
                "layer": "msos-shell",
                "allowed_paths": ["apps/msos-web/**"],
                "instruction": "Inspect the bounded surface.",
                "allow_changes": allow_changes,
            }
        ],
    }


def _job_identity() -> dict[str, Any]:
    return {
        "founder_build_next": {
            "pipeline_id": "ppe",
            "work_item_id": "fixture-work",
            "work_item_source_sha256_v1": "a" * 64,
            "refill_attempt": {
                "generation_id": "refill-12345678",
                "attempt_ordinal": 1,
                "retry_ordinal": 0,
            },
        }
    }


def _write_feed_job(
    tmp_path: Path,
    *,
    job_id: str,
    payload: dict[str, Any],
) -> tuple[Path, Path]:
    feed_repo = _init_repo(tmp_path / f"feed-{job_id}")
    _git(feed_repo, "checkout", "-qb", "jobs")
    job_dir = feed_repo / "jobs" / "approved"
    job_dir.mkdir(parents=True)
    (job_dir / f"{job_id}.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )
    _git(feed_repo, "add", ".")
    _git(feed_repo, "commit", "-qm", f"add {job_id}")
    return feed_repo, job_dir / f"{job_id}.yaml"


def _host_config_with_feed(tmp_path: Path, feed_repo: Path) -> tuple[PersistentHostConfig, Path]:
    return _write_configs(
        tmp_path / "host-case",
        feed={
            "enabled": True,
            "repo_url": str(feed_repo),
            "branch": "jobs",
            "path": "jobs/approved",
            "refresh_seconds": 1,
        },
    )


def _read_single_head(host_root: Path) -> dict[str, Any]:
    heads = list((host_root / "state" / "host-evidence" / "heads" / "execution").glob("*.json"))
    assert len(heads) == 1
    return json.loads(heads[0].read_text(encoding="utf-8"))


def _write_configs(
    root: Path,
    *,
    feed: dict[str, Any] | None = None,
) -> tuple[PersistentHostConfig, Path]:
    root.mkdir(parents=True, exist_ok=True)
    source = _init_repo(root / "source")
    host_root = root / "host"
    workspace_root = root / "workspaces"
    runtime_root = root / "runtime"
    codex_config_path = root / "codex-host.yaml"
    codex_config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "publication_enabled": False,
                "source_repo": str(source),
                "workspace_root": str(workspace_root),
                "runtime_root": str(runtime_root),
                "owner_id": "test-host",
                "reset_workspaces": True,
                "codex": {
                    "sandbox_mode": "workspace-write",
                    "max_concurrency": 2,
                    "timeout_seconds": 30,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    service: dict[str, Any] = {
        "version": 1,
        "publication_enabled": False,
        "host_root": str(host_root),
        "codex_host_config": str(codex_config_path),
        "poll_seconds": 0.01,
        "heartbeat_seconds": 0.01,
    }
    if feed is not None:
        service["job_feed"] = feed
    service_path = root / "service.yaml"
    service_path.write_text(yaml.safe_dump(service, sort_keys=False), encoding="utf-8")
    return load_persistent_host_config(service_path), source


def _fake_runner(
    config: Any,
    specs: tuple[Any, ...],
) -> CodexShadowReport:
    for spec in specs:
        workspace = config.workspace_root / spec.task.lane.lane_id
        subprocess.run(
            ["git", "clone", "-q", str(config.source_repo), str(workspace)],
            check=True,
        )
    return CodexShadowReport(
        status="completed",
        source_head=_git(config.source_repo, "rev-parse", "HEAD"),
        publication_enabled=False,
        owner_id="test-host",
        evidence=(),
    )


def test_job_parser_rejects_publication_and_prompt_files() -> None:
    payload = {
        "version": 1,
        "job_id": "safe-job",
        "approved": True,
        "publication_enabled": False,
        "manifest": _manifest(),
    }
    parse_host_job(yaml.safe_dump(payload))

    payload["manifest"]["publication_enabled"] = True
    with pytest.raises(HostJobError, match="publication"):
        parse_host_job(yaml.safe_dump(payload))

    payload["manifest"] = _manifest()
    payload["manifest"]["lanes"][0]["prompt_file"] = "outside.txt"
    with pytest.raises(HostJobError, match="inline instructions"):
        parse_host_job(yaml.safe_dump(payload))


def test_host_config_rejects_publication(tmp_path: Path) -> None:
    path = tmp_path / "service.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "publication_enabled": True,
                "host_root": str(tmp_path / "host"),
                "codex_host_config": str(tmp_path / "codex.yaml"),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PersistentHostConfigError, match="disabled"):
        load_persistent_host_config(path)


def test_unapproved_job_waits_then_approved_job_completes(tmp_path: Path) -> None:
    config, _ = _write_configs(tmp_path)
    paths = HostPaths.from_root(config.host_root)
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(_manifest()), encoding="utf-8")
    enqueue_manifest(paths, manifest_path, job_id="job-1")

    host = PersistentHost(config, runner=_fake_runner)
    assert host.run_once(sync_feed=False).processed is False
    assert (paths.pending / "job-1.yaml").exists()

    approve_pending_job(paths, "job-1")
    result = host.run_once(sync_feed=False)
    assert result.outcome == "completed"
    report_path = paths.completed / "job-1" / "report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["publication_enabled"] is False
    assert report["outcome"] == "completed"
    assert not (paths.running / "job-1.yaml").exists()


def test_malformed_job_moves_to_failed(tmp_path: Path) -> None:
    config, _ = _write_configs(tmp_path)
    paths = HostPaths.from_root(config.host_root)
    paths.ensure()
    (paths.pending / "bad.yaml").write_text("not: [valid", encoding="utf-8")

    result = PersistentHost(config, runner=_fake_runner).run_once(sync_feed=False)
    assert result.processed is False
    failed = list(paths.failed.iterdir())
    assert len(failed) == 1
    assert (failed[0] / "error.json").exists()


def test_interrupted_running_job_is_recovered(tmp_path: Path) -> None:
    config, _ = _write_configs(tmp_path)
    paths = HostPaths.from_root(config.host_root)
    paths.ensure()
    payload = {
        "version": 1,
        "job_id": "interrupted",
        "approved": True,
        "publication_enabled": False,
        "manifest": _manifest(),
    }
    (paths.running / "interrupted.yaml").write_text(
        yaml.safe_dump(payload),
        encoding="utf-8",
    )

    PersistentHost(config, runner=_fake_runner).initialize()
    assert not (paths.running / "interrupted.yaml").exists()
    error = json.loads(
        (paths.failed / "interrupted" / "error.json").read_text(encoding="utf-8")
    )
    assert error["outcome"] == "interrupted"


def test_duplicate_live_lock_fails_closed(tmp_path: Path) -> None:
    lock_path = tmp_path / "host.lock"
    first = HostProcessLock(lock_path)
    first.acquire()
    try:
        with pytest.raises(HostLockError, match="already running"):
            HostProcessLock(lock_path).acquire()
    finally:
        first.release()


def test_expected_source_head_mismatch_fails_before_runner(tmp_path: Path) -> None:
    config, _ = _write_configs(tmp_path)
    paths = HostPaths.from_root(config.host_root)
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(_manifest()), encoding="utf-8")
    enqueue_manifest(
        paths,
        manifest_path,
        job_id="wrong-head",
        approved=True,
        expected_source_head="deadbee",
    )
    calls = 0

    def runner(config: Any, specs: tuple[Any, ...]) -> CodexShadowReport:
        nonlocal calls
        calls += 1
        return _fake_runner(config, specs)

    result = PersistentHost(config, runner=runner).run_once(sync_feed=False)
    assert result.outcome == "failed"
    assert calls == 0
    assert (paths.failed / "wrong-head" / "error.json").exists()


def test_feed_imports_approved_job_once(tmp_path: Path) -> None:
    feed_repo = _init_repo(tmp_path / "feed")
    _git(feed_repo, "checkout", "-qb", "jobs")
    job_dir = feed_repo / "jobs" / "approved"
    job_dir.mkdir(parents=True)
    payload = {
        "version": 1,
        "job_id": "remote-1",
        "approved": True,
        "publication_enabled": False,
        "manifest": _manifest(),
    }
    (job_dir / "remote-1.yaml").write_text(
        yaml.safe_dump(payload),
        encoding="utf-8",
    )
    _git(feed_repo, "add", ".")
    _git(feed_repo, "commit", "-qm", "add approved job")

    config, _ = _write_configs(
        tmp_path / "host-case",
        feed={
            "enabled": True,
            "repo_url": str(feed_repo),
            "branch": "jobs",
            "path": "jobs/approved",
            "refresh_seconds": 1,
        },
    )
    paths = HostPaths.from_root(config.host_root)
    assert sync_git_job_feed(config, paths) == ("remote-1",)
    assert sync_git_job_feed(config, paths) == ()
    assert (paths.pending / "remote-1.yaml").exists()


def test_running_transition_receipt_replay_uses_stored_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "version": 1,
        "job_id": "running-replay",
        "approved": True,
        "publication_enabled": False,
        **_job_identity(),
        "manifest": _manifest(),
    }
    job_text = yaml.safe_dump(payload, sort_keys=False)
    feed_repo, _source_job = _write_feed_job(tmp_path, job_id="running-replay", payload=payload)
    config, _ = _host_config_with_feed(tmp_path, feed_repo)
    paths = HostPaths.from_root(config.host_root)

    assert sync_git_job_feed(config, paths) == ("running-replay",)
    pending_path = paths.pending / "running-replay.yaml"
    host = PersistentHost(config, runner=_fake_runner)
    timestamps = iter(("2026-07-29T00:00:01+00:00", "2026-07-29T00:00:59+00:00"))
    monkeypatch.setattr(persistent_host_module, "_timestamp", lambda value=None: next(timestamps))

    first = host._claim_next_job()
    assert first is not None
    running_path, _job = first
    receipts = list(
        (paths.state / "host-evidence" / "sources" / "execution" / "running").glob("*.json")
    )
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["job_source_utf8"] == job_text
    assert receipt["job_source_sha256"] == hashlib.sha256(job_text.encode("utf-8")).hexdigest()
    assert receipt["observed_at"]

    head = _read_single_head(config.host_root)
    assert head["producer_sequence"] == 3
    envelope_path = config.host_root / head["envelope_path"]
    first_envelope = envelope_path.read_bytes()
    envelope = json.loads(first_envelope.decode("utf-8"))
    assert envelope["observed_at"] == receipt["observed_at"]
    assert envelope["payload"] == {"execution_outcome": "running"}

    running_path.replace(pending_path)
    second = host._claim_next_job()
    assert second is not None
    assert envelope_path.read_bytes() == first_envelope
    assert json.loads(receipts[0].read_text(encoding="utf-8"))["observed_at"] == receipt[
        "observed_at"
    ]


def test_imported_sequence_failure_blocks_pending_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "version": 1,
        "job_id": "gap-proof",
        "approved": True,
        "publication_enabled": False,
        **_job_identity(),
        "manifest": _manifest(),
    }
    feed_repo, _source_job = _write_feed_job(tmp_path, job_id="gap-proof", payload=payload)
    config, _ = _host_config_with_feed(tmp_path, feed_repo)
    paths = HostPaths.from_root(config.host_root)
    original_replace_head = lifecycle._replace_head
    calls = 0

    def fail_first_head(path: Path, head: dict[str, Any]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("imported head denied")
        original_replace_head(path, head)

    monkeypatch.setattr(lifecycle, "_replace_head", fail_first_head)

    assert sync_git_job_feed(config, paths) == ("gap-proof",)

    assert calls == 2
    assert not list((config.host_root / "state" / "host-evidence" / "heads").rglob("*.json"))
    envelopes = list((config.host_root / "state" / "host-evidence" / "execution").rglob("*.json"))
    assert sorted(
        json.loads(envelope.read_text(encoding="utf-8"))["producer_sequence"]
        for envelope in envelopes
    ) == [1, 2]
    diagnostics = list(
        (
            config.host_root
            / "state"
            / "producer-evidence-errors"
            / "persistent_host"
        ).glob("*.json")
    )
    assert len(diagnostics) == 2
    records = [json.loads(path.read_text(encoding="utf-8")) for path in diagnostics]
    messages = [record["message"] for record in records]
    assert any(record["error_type"] == "LifecycleEvidenceError" for record in records)
    assert any("producer sequence gap" in message for message in messages)
    assert (paths.pending / "gap-proof.yaml").exists()


def test_malformed_refill_metadata_preserves_feed_import_and_writes_diagnostic(
    tmp_path: Path,
) -> None:
    payload = {
        "version": 1,
        "job_id": "bad-refill",
        "approved": True,
        "publication_enabled": False,
        **_job_identity(),
        "manifest": _manifest(),
    }
    payload["founder_build_next"]["refill_attempt"]["attempt_ordinal"] = "not-an-int"
    feed_repo, _source_job = _write_feed_job(tmp_path, job_id="bad-refill", payload=payload)
    config, _ = _host_config_with_feed(tmp_path, feed_repo)
    paths = HostPaths.from_root(config.host_root)

    assert sync_git_job_feed(config, paths) == ("bad-refill",)

    assert (paths.pending / "bad-refill.yaml").exists()
    assert not list((config.host_root / "state" / "host-evidence").rglob("*.json"))
    diagnostics = list(
        (
            config.host_root
            / "state"
            / "producer-evidence-errors"
            / "persistent_host"
        ).glob("*.json")
    )
    assert len(diagnostics) == 1
    diagnostic = json.loads(diagnostics[0].read_text(encoding="utf-8"))
    assert diagnostic["primary_outcome_preserved"] is True
    assert diagnostic["primary_outcome"] == {"outcome": "imported", "job_id": "bad-refill"}
    assert "refill attempt identity is malformed" in diagnostic["message"]


def test_host_completed_and_failed_terminal_evidence_uses_actual_archives(
    tmp_path: Path,
) -> None:
    for job_id, expected_outcome, expected_error in (
        ("host-completed", "completed", None),
        ("host-failed", "failed", "HostJobError"),
    ):
        payload = {
            "version": 1,
            "job_id": job_id,
            "approved": True,
            "publication_enabled": False,
            **_job_identity(),
            "manifest": _manifest(),
        }
        if expected_outcome == "failed":
            payload["expected_source_head"] = "deadbee"
        feed_repo, _source_job = _write_feed_job(tmp_path / job_id, job_id=job_id, payload=payload)
        config, _ = _host_config_with_feed(tmp_path / f"{job_id}-case", feed_repo)
        paths = HostPaths.from_root(config.host_root)
        assert sync_git_job_feed(config, paths) == (job_id,)

        host = PersistentHost(config, runner=_fake_runner)
        result = host.run_once(sync_feed=False)

        assert result.outcome == expected_outcome
        head = _read_single_head(config.host_root)
        assert head["producer_sequence"] == 4
        envelope = json.loads((config.host_root / head["envelope_path"]).read_text("utf-8"))
        assert envelope["payload"]["execution_outcome"] == expected_outcome
        archive = host.last_result["archive"] if host.last_result is not None else ""
        assert envelope["payload"]["host_archive_path"] == Path(archive).relative_to(
            config.host_root
        ).as_posix()
        assert envelope["payload"].get("error_class") == expected_error
        assert (config.host_root / head["envelope_path"]).exists()


def _claim_objective(work_item_id: str = "claim-release-work"):
    return objective_identity_from_work(
        repository="DanielTabakman/Probability-prediction-engine",
        linked_issue=None,
        work_item_id=work_item_id,
        stable_parts={"work_item_id": work_item_id},
        acceptance_contract_sha256="2" * 64,
    )


_CLAIM_PATHS = ("src/engine/claim_release.py", "tests/test_claim_release.py")


def _job_identity_with_claim(
    *,
    objective_sha256: str,
    writer_id: str,
    claim_generation: int,
) -> dict[str, Any]:
    payload = _job_identity()
    payload["founder_build_next"]["work_admission"] = {
        "status": "NEW_WORK_ADMITTED",
        "objective_sha256": objective_sha256,
        "claim_writer_id": writer_id,
        "claim_generation": claim_generation,
        "authorized_paths": list(_CLAIM_PATHS),
    }
    return payload


def _claim_state(host_root: Path, objective_sha256: str) -> dict[str, Any]:
    path = host_root / "state" / "work-admission" / "claims" / f"{objective_sha256}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _run_failing_claimed_job(
    tmp_path: Path,
    *,
    job_id: str,
    objective_sha256: str,
    writer_id: str,
    claim_generation: int,
    claim_setup: Any,
) -> PersistentHostConfig:
    """Import a claimed job packet that fails before the runner, and archive it."""
    payload = {
        "version": 1,
        "job_id": job_id,
        "approved": True,
        "publication_enabled": False,
        # A stale expected head fails the attempt before any lane runs.
        "expected_source_head": "deadbee",
        **_job_identity_with_claim(
            objective_sha256=objective_sha256,
            writer_id=writer_id,
            claim_generation=claim_generation,
        ),
        "manifest": _manifest(),
    }
    feed_repo, _source_job = _write_feed_job(tmp_path / job_id, job_id=job_id, payload=payload)
    config, _ = _host_config_with_feed(tmp_path / f"{job_id}-case", feed_repo)
    paths = HostPaths.from_root(config.host_root)
    claim_setup(config)
    assert sync_git_job_feed(config, paths) == (job_id,)

    result = PersistentHost(config, runner=_fake_runner).run_once(sync_feed=False)
    assert result.outcome == "failed"
    return config


def test_host_execution_failure_releases_the_writer_claim(tmp_path: Path) -> None:
    objective = _claim_objective()
    writer_id = "build-next:claim-release-first"

    def claim_setup(config: PersistentHostConfig) -> None:
        admitted = admit_work(
            AdmissionRequest(
                objective=objective,
                writer_id=writer_id,
                branch="build/auto/claim-release-first",
                authorized_paths=_CLAIM_PATHS,
                claim_root=config.host_root / "state",
            )
        )
        assert admitted.status == AdmissionStatus.NEW_WORK_ADMITTED
        assert admitted.claim is not None
        assert admitted.claim.generation == 1

    config = _run_failing_claimed_job(
        tmp_path,
        job_id="claim-release-failed",
        objective_sha256=objective.objective_sha256,
        writer_id=writer_id,
        claim_generation=1,
        claim_setup=claim_setup,
    )

    claim = _claim_state(config.host_root, objective.objective_sha256)
    assert claim["state"] == "failed"
    assert claim["evidence"]["bounded_failure_disposition"] == "host_execution_failed"
    assert claim["evidence"]["job_id"] == "claim-release-failed"
    assert claim["evidence"]["error_class"] == "HostJobError"

    # A retry is issued under a fresh job id, so it is also a fresh writer. Without the
    # bounded failure disposition above it would be refused forever.
    successor = admit_work(
        AdmissionRequest(
            objective=objective,
            writer_id="build-next:claim-release-successor",
            branch="build/auto/claim-release-successor",
            authorized_paths=_CLAIM_PATHS,
            claim_root=config.host_root / "state",
        )
    )
    assert successor.status == AdmissionStatus.NEW_WORK_ADMITTED
    assert successor.claim is not None
    assert successor.claim.generation == 2


def test_host_execution_failure_does_not_clobber_a_newer_writer_claim(tmp_path: Path) -> None:
    objective = _claim_objective("claim-release-superseded")
    stale_writer = "build-next:claim-release-stale"
    current_writer = "build-next:claim-release-current"

    def claim_setup(config: PersistentHostConfig) -> None:
        state = config.host_root / "state"
        admit_work(
            AdmissionRequest(
                objective=objective,
                writer_id=stale_writer,
                branch="build/auto/claim-release-stale",
                authorized_paths=_CLAIM_PATHS,
                claim_root=state,
            )
        )
        release_claim(
            state,
            objective.objective_sha256,
            writer_id=stale_writer,
            terminal_state="failed",
            evidence={"bounded_failure_disposition": "fixture"},
        )
        current = admit_work(
            AdmissionRequest(
                objective=objective,
                writer_id=current_writer,
                branch="build/auto/claim-release-current",
                authorized_paths=_CLAIM_PATHS,
                claim_root=state,
            )
        )
        assert current.claim is not None
        assert current.claim.generation == 2

    config = _run_failing_claimed_job(
        tmp_path,
        job_id="claim-release-stale-packet",
        objective_sha256=objective.objective_sha256,
        writer_id=stale_writer,
        claim_generation=1,
        claim_setup=claim_setup,
    )

    claim = _claim_state(config.host_root, objective.objective_sha256)
    assert claim["state"] == "active"
    assert claim["writer_id"] == current_writer
    assert claim["generation"] == 2

    refusal = json.loads(
        (
            HostPaths.from_root(config.host_root).failed
            / "claim-release-stale-packet"
            / "claim-release-error.json"
        ).read_text(encoding="utf-8")
    )
    assert refusal["writer_id"] == stale_writer
    assert refusal["claim_generation"] == 1
    assert refusal["error_type"] == "AdmissionError"


def test_host_execution_failure_without_a_claim_records_no_release(tmp_path: Path) -> None:
    payload = {
        "version": 1,
        "job_id": "unclaimed-failure",
        "approved": True,
        "publication_enabled": False,
        "expected_source_head": "deadbee",
        **_job_identity(),
        "manifest": _manifest(),
    }
    feed_repo, _source_job = _write_feed_job(
        tmp_path / "unclaimed", job_id="unclaimed-failure", payload=payload
    )
    config, _ = _host_config_with_feed(tmp_path / "unclaimed-case", feed_repo)
    paths = HostPaths.from_root(config.host_root)
    assert sync_git_job_feed(config, paths) == ("unclaimed-failure",)

    result = PersistentHost(config, runner=_fake_runner).run_once(sync_feed=False)

    assert result.outcome == "failed"
    archive = paths.failed / "unclaimed-failure"
    assert (archive / "error.json").exists()
    assert not (archive / "claim-release-error.json").exists()
    assert not (config.host_root / "state" / "work-admission" / "claims").exists()


def test_unapproved_feed_job_is_not_imported(tmp_path: Path) -> None:
    feed_repo = _init_repo(tmp_path / "feed")
    _git(feed_repo, "checkout", "-qb", "jobs")
    job_dir = feed_repo / "jobs" / "approved"
    job_dir.mkdir(parents=True)
    payload = {
        "version": 1,
        "job_id": "not-approved",
        "approved": False,
        "publication_enabled": False,
        "manifest": _manifest(),
    }
    (job_dir / "not-approved.yaml").write_text(
        yaml.safe_dump(payload),
        encoding="utf-8",
    )
    _git(feed_repo, "add", ".")
    _git(feed_repo, "commit", "-qm", "add unapproved job")

    config, _ = _write_configs(
        tmp_path / "host-case",
        feed={
            "enabled": True,
            "repo_url": str(feed_repo),
            "branch": "jobs",
            "path": "jobs/approved",
        },
    )
    paths = HostPaths.from_root(config.host_root)
    assert sync_git_job_feed(config, paths) == ()
    assert not (paths.pending / "not-approved.yaml").exists()
