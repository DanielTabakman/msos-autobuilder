from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from time import sleep

import pytest
from test_build_next import _commit_all, _config, _feed_repo, _git, _snapshot, _write_ppe

import msos_autobuilder.refill_controller as refill_controller
from msos_autobuilder.refill_controller import (
    RefillConfig,
    RefillControllerError,
    RefillPolicy,
    RefillService,
    keep_one_running,
    load_refill_generation,
    load_refill_policy,
    pause_builds,
    pause_builds_and_reconcile,
    reconcile_refill,
    resume_builds,
    save_refill_generation,
    save_refill_policy,
)

SOURCE_REPO = "DanielTabakman/Probability-prediction-engine"
EXACT_RELEASE = "a" * 40


def _refill_config(
    tmp_path: Path,
    *,
    ppe: Path | None = None,
    feed: Path | None = None,
) -> RefillConfig:
    ppe_repo = ppe or _write_ppe(tmp_path / "ppe")
    feed_repo = feed or _feed_repo(tmp_path / "feed-work")
    build_config = _config(tmp_path, ppe_repo, feed_repo, host_root=tmp_path / "host")
    return RefillConfig(build_next=build_config)


def _write_host_status(config: RefillConfig) -> None:
    host_root = config.build_next.host_root
    assert host_root is not None
    _write_exact_release_witnesses(config)
    (host_root / "state" / "candidate-gate-results-repo").mkdir(parents=True, exist_ok=True)
    status = host_root / "state" / "host-status.json"
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(
        json.dumps(
            {
                "version": 1,
                "state": "idle",
                "publication_enabled": False,
                "pid": 123,
                "started_at": "2026-07-15T00:00:00+00:00",
                "heartbeat_at": "2999-01-01T00:00:00+00:00",
                "active_job_id": None,
                "queue_counts": {"pending": 0, "running": 0, "completed": 0, "failed": 0},
                "last_result": None,
                "errors": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _write_exact_release_witnesses(
    config: RefillConfig,
    *,
    service_states: dict[str, str] | None = None,
    release_commit: str = EXACT_RELEASE,
    witness_commit: str | None = None,
    activated_at: str = "2026-07-16T00:00:00+00:00",
    started_at: str = "2999-01-01T00:00:00+00:00",
) -> None:
    host_root = config.build_next.host_root
    assert host_root is not None
    supervisor = host_root.parent / ".msos-autobuilder-supervisor"
    release = supervisor / "versions" / release_commit
    release.mkdir(parents=True, exist_ok=True)
    (release / "release.json").write_text(
        json.dumps({"version": 1, "commit": release_commit}) + "\n",
        encoding="utf-8",
    )
    active = supervisor / "state" / "active-release.json"
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text(
        json.dumps(
            {
                "version": 1,
                "commit": release_commit,
                "release_path": str(release),
                "activated_at": activated_at,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    witnesses = supervisor / "state" / "service-witnesses"
    witnesses.mkdir(parents=True, exist_ok=True)
    for service in ("host", "relay", "gate", "revision", "publisher", "refill"):
        (witnesses / f"{service}.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "service": service,
                    "state": (service_states or {}).get(service, "running"),
                    "release_commit": witness_commit or release_commit,
                    "child_pid": 123,
                    "started_at": started_at,
                }
            )
            + "\n",
            encoding="utf-8",
        )


def test_keep_one_reconciles_through_accepted_build_next_path(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "QUEUED"
    assert report.enabled is True
    assert report.desired_capacity == 1
    assert report.build_next_receipt is not None
    assert report.build_next_receipt.job_id is not None
    assert report.feed_awaiting_import == 1
    policy = load_refill_policy(config)
    assert policy.last_decision_evidence is not None
    assert policy.last_decision_evidence["status"] == "QUEUED"
    assert policy.last_decision_evidence["build_next"]["job_id"] == report.build_next_receipt.job_id


def test_existing_running_and_queued_jobs_fill_capacity_without_dispatch(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    keep_one_running(config)
    paths = config.build_next.host_root
    assert paths is not None

    running = paths / "queue" / "running"
    running.mkdir(parents=True, exist_ok=True)
    (running / "manual.yaml").write_text("version: 1\n", encoding="utf-8")
    running_report = reconcile_refill(config)

    (running / "manual.yaml").unlink()
    queued = paths / "queue" / "pending"
    queued.mkdir(parents=True, exist_ok=True)
    (queued / "manual.yaml").write_text("version: 1\n", encoding="utf-8")
    queued_report = reconcile_refill(config)

    assert running_report.status == "RUNNING"
    assert running_report.build_next_receipt is None
    assert queued_report.status == "QUEUED"
    assert queued_report.build_next_receipt is None


def test_pause_and_resume_preserve_workers_and_restore_capacity(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    keep_one_running(config)
    paused = pause_builds(config)

    paused_report = reconcile_refill(config)
    resumed = resume_builds(config)
    resumed_report = reconcile_refill(config)

    assert paused.enabled is False
    assert paused.desired_capacity == 0
    assert paused.resume_desired_capacity == 1
    assert paused_report.status == "PAUSED"
    assert paused_report.decision_evidence["reason"] == "paused"
    assert resumed.enabled is True
    assert resumed.desired_capacity == 1
    assert resumed_report.status == "QUEUED"


def test_queue_and_review_backpressure_fail_closed_before_dispatch(tmp_path: Path) -> None:
    queue_config = _refill_config(tmp_path / "queue")
    _write_host_status(queue_config)
    save_refill_policy(
        queue_config,
        RefillPolicy(enabled=True, desired_capacity=1, queue_cap=0),
    )
    queue_report = reconcile_refill(queue_config)

    review_config = _refill_config(tmp_path / "review")
    _write_host_status(review_config)
    save_refill_policy(
        review_config,
        RefillPolicy(enabled=True, desired_capacity=1, review_cap_per_repository=2),
    )
    host_root = review_config.build_next.host_root
    assert host_root is not None
    for index in range(2):
        report = (
            host_root
            / "state"
            / "candidate-gate-results-repo"
            / "results"
            / "test-host"
            / f"job-{index}"
            / "gate-report.json"
        )
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps(
                {
                    "status": "passed",
                    "state": "candidate_passed",
                    "candidate_validation": {"target_repository": SOURCE_REPO},
                }
            ),
            encoding="utf-8",
        )
        (report.parent / "job.yaml").write_text(
            json.dumps(
                {
                    "version": 1,
                    "job_id": f"job-{index}",
                    "publication_enabled": False,
                    "candidate_validation": {"target_repository": SOURCE_REPO},
                }
            ),
            encoding="utf-8",
        )
    review_report = reconcile_refill(review_config)

    assert queue_report.status == "BACKPRESSURE"
    assert queue_report.decision_evidence["reason"] == "queue_backpressure"
    assert review_report.status == "BACKPRESSURE"
    assert review_report.decision_evidence["reason"] == "review_backpressure"
    assert review_report.awaiting_review[SOURCE_REPO] == 2


def test_fail_closed_build_next_states_are_reported_distinctly(tmp_path: Path) -> None:
    blocked_config = _refill_config(
        tmp_path / "blocked",
        ppe=_write_ppe(tmp_path / "ppe-blocked", snapshot=_snapshot(state="BLOCKED")),
        feed=_feed_repo(tmp_path / "feed-blocked"),
    )
    _write_host_status(blocked_config)
    unfilled_config = _refill_config(
        tmp_path / "unfilled",
        ppe=_write_ppe(tmp_path / "ppe-unfilled", snapshot=_snapshot(state="UNFILLED")),
        feed=_feed_repo(tmp_path / "feed-unfilled"),
    )
    _write_host_status(unfilled_config)
    keep_one_running(blocked_config)
    keep_one_running(unfilled_config)

    blocked = reconcile_refill(blocked_config)
    unfilled = reconcile_refill(unfilled_config)

    assert blocked.status == "BLOCKED"
    assert unfilled.status == "UNFILLED"


def test_policy_rejects_capacity_two_and_recovers_from_disk(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)

    with pytest.raises(RefillControllerError, match="capacity"):
        RefillPolicy(enabled=True, desired_capacity=2)

    keep_one_running(config)
    loaded = load_refill_policy(config)

    assert loaded.enabled is True
    assert loaded.desired_capacity == 1
    assert loaded.queue_cap == 4


def test_resume_requires_prior_founder_target_and_strict_policy_types(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)

    with pytest.raises(RefillControllerError, match="prior founder target"):
        resume_builds(config)

    path = config.build_next.host_root / "state" / "refill-policy.json"
    assert config.build_next.host_root is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "enabled": "false", "desired_capacity": 0}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RefillControllerError, match="enabled must be a boolean"):
        load_refill_policy(config)


def test_stale_host_health_blocks_dispatch(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    assert report.decision_evidence["reason"] == "runtime_health"
    assert report.build_next_receipt is None


def test_feed_checkout_failure_blocks_dispatch_with_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    keep_one_running(config)

    def fail_checkout(*_args: object, **_kwargs: object) -> Path:
        raise RuntimeError("feed authentication failed")

    monkeypatch.setattr("msos_autobuilder.refill_controller._prepare_feed_checkout", fail_checkout)

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    assert report.build_next_receipt is None
    feed = report.decision_evidence["health"]["checks"]["feed_checkout"]
    assert feed["ok"] is False
    assert "feed authentication failed" in feed["error"]


@pytest.mark.parametrize("service", ["relay", "gate"])
def test_stopped_managed_downstream_service_blocks_dispatch(
    tmp_path: Path, service: str
) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    _write_exact_release_witnesses(config, service_states={service: "stopped"})
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    service_check = report.decision_evidence["health"]["checks"]["managed_services"]["services"][
        service
    ]
    assert service_check["ok"] is False
    assert service_check["error"] == "service witness is not running"


def test_mismatched_exact_release_witness_blocks_dispatch(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    _write_exact_release_witnesses(config, witness_commit="b" * 40)
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    service_check = report.decision_evidence["health"]["checks"]["managed_services"]["services"][
        "host"
    ]
    assert service_check["ok"] is False
    assert service_check["error"] == "service witness does not match active release"


def test_publisher_error_state_blocks_dispatch(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    assert config.build_next.host_root is not None
    error = config.build_next.host_root / "state" / "controlled-publisher-error.json"
    error.write_text(json.dumps({"error": "publisher failed"}) + "\n", encoding="utf-8")
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    publisher = report.decision_evidence["health"]["checks"]["publisher_state"]
    assert publisher["ok"] is False


def _write_error_marker(
    config: RefillConfig,
    name: str,
    payload: dict[str, object],
) -> Path:
    assert config.build_next.host_root is not None
    path = config.build_next.host_root / "state" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _write_state_json(config: RefillConfig, name: str, payload: dict[str, object]) -> Path:
    assert config.build_next.host_root is not None
    path = config.build_next.host_root / "state" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _generation_id(
    *,
    release_commit: str = EXACT_RELEASE,
    started_at: str = "2999-01-01T00:00:00+00:00",
    pid: int = 123,
) -> str:
    return hashlib.sha256(f"{release_commit}\n{started_at}\n{pid}\n".encode()).hexdigest()


def test_historical_publisher_error_after_later_exact_release_start_does_not_block(
    tmp_path: Path,
) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    _write_error_marker(
        config,
        "controlled-publisher-error.json",
        {
            "recorded_at": "2026-07-16T23:46:31.078266+00:00",
            "error_type": "PublisherError",
            "message": "GitHub API 503",
            "draft_pr_publication_enabled": True,
            "merge_enabled": False,
            "main_write_enabled": False,
        },
    )
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "QUEUED"
    publisher = report.decision_evidence["health"]["checks"]["publisher_state"]
    assert publisher["ok"] is True
    assert publisher["state"] == "superseded"
    assert publisher["superseded_by"] == "later_healthy_exact_release_service_start"


def test_current_generation_publisher_error_after_current_start_blocks(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    _write_error_marker(
        config,
        "controlled-publisher-error.json",
        {
            "service": "publisher",
            "release_commit": EXACT_RELEASE,
            "witness_started_at": "2999-01-01T00:00:00+00:00",
            "recorded_at": "2999-01-01T00:00:01+00:00",
            "error_type": "PublisherError",
            "message": "current failure",
            "draft_pr_publication_enabled": True,
            "merge_enabled": False,
            "main_write_enabled": False,
        },
    )
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    publisher = report.decision_evidence["health"]["checks"]["publisher_state"]
    assert publisher["ok"] is False
    assert "current-generation" in publisher["error"]


def test_current_generation_error_followed_by_same_generation_success_is_superseded(
    tmp_path: Path,
) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    generation = _generation_id()
    _write_error_marker(
        config,
        "controlled-publisher-error.json",
        {
            "service": "publisher",
            "release_commit": EXACT_RELEASE,
            "witness_started_at": "2999-01-01T00:00:00+00:00",
            "witness_pid": 123,
            "generation_id": generation,
            "recorded_at": "2999-01-01T00:00:01+00:00",
            "error_type": "PublisherError",
            "message": "current failure",
            "draft_pr_publication_enabled": True,
            "merge_enabled": False,
            "main_write_enabled": False,
        },
    )
    _write_state_json(
        config,
        "publisher-service-success.json",
        {
            "version": 1,
            "service": "publisher",
            "release_commit": EXACT_RELEASE,
            "witness_started_at": "2999-01-01T00:00:00+00:00",
            "witness_pid": 123,
            "generation_id": generation,
            "recorded_at": "2999-01-01T00:00:02+00:00",
            "cycle_started_at": "2999-01-01T00:00:01.5+00:00",
            "finished_at": "2999-01-01T00:00:02+00:00",
            "result": "success",
            "associated_jobs": [],
            "terminal_evidence": {"processed_jobs": []},
        },
    )
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "QUEUED"
    publisher = report.decision_evidence["health"]["checks"]["publisher_state"]
    assert publisher["ok"] is True
    assert publisher["superseded_by"] == "later_same_generation_service_success"


def test_idle_revision_success_supersedes_unassociated_same_generation_marker(
    tmp_path: Path,
) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    generation = _generation_id()
    _write_error_marker(
        config,
        "revision-loop-error.json",
        {
            "service": "revision",
            "release_commit": EXACT_RELEASE,
            "witness_started_at": "2999-01-01T00:00:00+00:00",
            "witness_pid": 123,
            "generation_id": generation,
            "recorded_at": "2999-01-01T00:00:01+00:00",
            "error_type": "RevisionLoopError",
            "message": "same generation transient",
            "publication_enabled": False,
        },
    )
    _write_state_json(
        config,
        "revision-service-success.json",
        {
            "version": 1,
            "service": "revision",
            "release_commit": EXACT_RELEASE,
            "witness_started_at": "2999-01-01T00:00:00+00:00",
            "witness_pid": 123,
            "generation_id": generation,
            "recorded_at": "2999-01-01T00:00:02+00:00",
            "cycle_started_at": "2999-01-01T00:00:01.5+00:00",
            "finished_at": "2999-01-01T00:00:02+00:00",
            "result": "success",
            "associated_jobs": [],
            "terminal_evidence": {"revision_jobs": []},
        },
    )
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "QUEUED"
    revision = report.decision_evidence["health"]["checks"]["service_error_markers"]["services"][
        "revision"
    ]
    assert revision["ok"] is True
    assert revision["superseded_by"] == "later_same_generation_service_success"


def test_stale_gate_error_superseded_by_later_terminal_gate_evidence(
    tmp_path: Path,
) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    _write_state_json(
        config,
        "candidate-gate-seen.json",
        {
            "job-1": {
                "source_report_sha256": "2" * 64,
                "results_commit": "1" * 40,
                "processed_at": "2026-07-16T23:44:47.562773+00:00",
                "status": "passed",
                "state": "candidate_passed",
            }
        },
    )
    _write_error_marker(
        config,
        "candidate-gate-error.json",
        {
            "service": "gate",
            "release_commit": EXACT_RELEASE,
            "recorded_at": "2026-07-16T23:43:47.562773+00:00",
            "associated": {"job_id": "job-1"},
            "error_type": "CandidateGateError",
            "message": "transient fetch error",
            "publication_enabled": False,
        },
    )
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "QUEUED"
    gate = report.decision_evidence["health"]["checks"]["service_error_markers"]["services"]["gate"]
    assert gate["ok"] is True
    assert gate["superseded_by"] == "later_authoritative_terminal_job_evidence"


def test_active_gate_error_blocks_refill(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    _write_error_marker(
        config,
        "candidate-gate-error.json",
        {
            "service": "gate",
            "release_commit": EXACT_RELEASE,
            "recorded_at": "2999-01-01T00:00:01+00:00",
            "error_type": "CandidateGateError",
            "message": "current gate failure",
            "publication_enabled": False,
        },
    )
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    gate = report.decision_evidence["health"]["checks"]["service_error_markers"]["services"]["gate"]
    assert gate["ok"] is False


def test_stale_revision_error_superseded_by_later_successful_revision_state(
    tmp_path: Path,
) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    _write_state_json(
        config,
        "revision-loop-seen.json",
        {
            "test-host/job-1": {
                "gate_report_sha256": "3" * 64,
                "revision_job_id": "job-1-revision-1",
                "jobs_commit": "4" * 40,
                "queued_at": "2026-07-16T06:32:32.999341+00:00",
                "source_job_id": "job-1",
            }
        },
    )
    _write_error_marker(
        config,
        "revision-loop-error.json",
        {
            "service": "revision",
            "release_commit": EXACT_RELEASE,
            "recorded_at": "2026-07-16T06:31:32.999341+00:00",
            "associated": {"job_id": "job-1-revision-1"},
            "error_type": "RevisionLoopError",
            "message": "historical transport failure",
            "publication_enabled": False,
        },
    )
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "QUEUED"
    revision = report.decision_evidence["health"]["checks"]["service_error_markers"]["services"][
        "revision"
    ]
    assert revision["ok"] is True
    assert revision["superseded_by"] == "later_authoritative_terminal_job_evidence"


def test_restart_with_associated_active_job_remains_blocking(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    _write_error_marker(
        config,
        "candidate-gate-error.json",
        {
            "service": "gate",
            "recorded_at": "2026-07-16T23:43:47.562773+00:00",
            "associated": {"job_id": "active-job"},
            "error_type": "CandidateGateError",
            "message": "job-specific failure",
            "publication_enabled": False,
        },
    )
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    gate = report.decision_evidence["health"]["checks"]["service_error_markers"]["services"]["gate"]
    assert gate["ok"] is False
    assert "matching job" in gate["error"] or "terminal" in gate["error"]


def test_success_for_other_jobs_does_not_clear_associated_failure(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    generation = _generation_id()
    _write_error_marker(
        config,
        "candidate-gate-error.json",
        {
            "service": "gate",
            "release_commit": EXACT_RELEASE,
            "witness_started_at": "2999-01-01T00:00:00+00:00",
            "witness_pid": 123,
            "generation_id": generation,
            "recorded_at": "2999-01-01T00:00:01+00:00",
            "associated": {"job_id": "failed-job", "candidate_id": "failed-job"},
            "error_type": "CandidateGateError",
            "message": "job-specific failure",
            "publication_enabled": False,
        },
    )
    _write_state_json(
        config,
        "gate-service-success.json",
        {
            "version": 1,
            "service": "gate",
            "release_commit": EXACT_RELEASE,
            "witness_started_at": "2999-01-01T00:00:00+00:00",
            "witness_pid": 123,
            "generation_id": generation,
            "recorded_at": "2999-01-01T00:00:02+00:00",
            "cycle_started_at": "2999-01-01T00:00:01.5+00:00",
            "finished_at": "2999-01-01T00:00:02+00:00",
            "result": "success",
            "associated_jobs": ["other-job"],
        },
    )
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    gate = report.decision_evidence["health"]["checks"]["service_error_markers"]["services"]["gate"]
    assert gate["ok"] is False
    assert "does not identify" in gate["error"] or "matching job" in gate["error"]


def test_restart_with_proven_later_terminal_disposition_is_nonblocking(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    _write_error_marker(
        config,
        "controlled-publisher-error.json",
        {
            "service": "publisher",
            "recorded_at": "2026-07-16T23:46:31.078266+00:00",
            "associated": {"job_id": "published-job"},
            "error_type": "PublisherError",
            "message": "historical job failure",
            "draft_pr_publication_enabled": True,
            "merge_enabled": False,
            "main_write_enabled": False,
        },
    )
    _write_state_json(
        config,
        "controlled-publisher-seen.json",
        {
            "published-job": {
                "gate_report_sha256": "1" * 64,
                "source_report_sha256": "2" * 64,
                "branch": "autobuilder/published-job",
                "commit_sha": "3" * 40,
                "pr_number": 12,
                "pr_url": "https://example.invalid/pull/12",
                "results_commit": "4" * 40,
                "published_at": "2026-07-16T23:47:31.078266+00:00",
                "status": "published-draft",
            }
        },
    )
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "QUEUED"
    publisher = report.decision_evidence["health"]["checks"]["publisher_state"]
    assert publisher["ok"] is True
    assert publisher["superseded_by"] == "later_authoritative_terminal_job_evidence"


def test_ledger_entry_before_marker_cannot_supersede(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    _write_error_marker(
        config,
        "candidate-gate-error.json",
        {
            "service": "gate",
            "recorded_at": "2026-07-16T23:43:47.562773+00:00",
            "associated": {"job_id": "job-1"},
            "error_type": "CandidateGateError",
            "message": "later mutation failure",
            "publication_enabled": False,
        },
    )
    _write_state_json(
        config,
        "candidate-gate-seen.json",
        {
            "job-1": {
                "source_report_sha256": "2" * 64,
                "results_commit": "1" * 40,
                "processed_at": "2026-07-16T23:42:47.562773+00:00",
                "status": "passed",
            }
        },
    )
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    gate = report.decision_evidence["health"]["checks"]["service_error_markers"]["services"]["gate"]
    assert gate["ok"] is False
    assert "predates" in gate["error"]


def test_empty_or_malformed_matching_ledger_entry_remains_blocking(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    _write_error_marker(
        config,
        "revision-loop-error.json",
        {
            "service": "revision",
            "recorded_at": "2026-07-16T06:31:32.999341+00:00",
            "associated": {"job_id": "job-1-revision-1"},
            "error_type": "RevisionLoopError",
            "message": "historical transport failure",
            "publication_enabled": False,
        },
    )
    _write_state_json(
        config,
        "revision-loop-seen.json",
        {"test-host/job-1": {"revision_job_id": "job-1-revision-1"}},
    )
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    revision = report.decision_evidence["health"]["checks"]["service_error_markers"]["services"][
        "revision"
    ]
    assert revision["ok"] is False
    assert "gate_report_sha256" in revision["error"]


def test_malformed_or_ambiguous_marker_blocks_refill(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    assert config.build_next.host_root is not None
    marker = config.build_next.host_root / "state" / "controlled-publisher-error.json"
    marker.write_text("{not-json", encoding="utf-8")
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    publisher = report.decision_evidence["health"]["checks"]["publisher_state"]
    assert publisher["ok"] is False
    assert "malformed" in publisher["error"]


def test_other_release_marker_cannot_clear_current_error(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    _write_error_marker(
        config,
        "controlled-publisher-error.json",
        {
            "service": "publisher",
            "release_commit": "b" * 40,
            "recorded_at": "2999-01-01T00:00:01+00:00",
            "error_type": "PublisherError",
            "message": "wrong release current failure",
            "draft_pr_publication_enabled": True,
            "merge_enabled": False,
            "main_write_enabled": False,
        },
    )
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    publisher = report.decision_evidence["health"]["checks"]["publisher_state"]
    assert publisher["ok"] is False
    assert "release contradicts" in publisher["error"]


def test_contradictory_witness_generation_metadata_blocks(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    _write_error_marker(
        config,
        "controlled-publisher-error.json",
        {
            "service": "publisher",
            "release_commit": EXACT_RELEASE,
            "witness_started_at": "2999-01-01T00:00:00+00:00",
            "witness_pid": 999,
            "recorded_at": "2999-01-01T00:00:01+00:00",
            "error_type": "PublisherError",
            "message": "contradictory generation",
            "draft_pr_publication_enabled": True,
            "merge_enabled": False,
            "main_write_enabled": False,
        },
    )
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    publisher = report.decision_evidence["health"]["checks"]["publisher_state"]
    assert publisher["ok"] is False
    assert "generation metadata contradicts" in publisher["error"]


def test_stale_marker_restart_recovery_is_deterministic(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    _write_error_marker(
        config,
        "controlled-publisher-error.json",
        {
            "recorded_at": "2026-07-16T23:46:31.078266+00:00",
            "error_type": "PublisherError",
            "message": "historical GitHub 503",
            "draft_pr_publication_enabled": True,
            "merge_enabled": False,
            "main_write_enabled": False,
        },
    )
    keep_one_running(config)

    first = reconcile_refill(config)
    second = RefillService(config, interval_seconds=0.01).run_once()

    assert first.status == "QUEUED"
    assert second.status == "QUEUED"
    assert first.decision_evidence["health"]["checks"]["publisher_state"]["marker_sha256"]
    assert (
        first.decision_evidence["health"]["checks"]["publisher_state"]["marker_sha256"]
        == second.decision_evidence["health"]["checks"]["publisher_state"]["marker_sha256"]
    )


def test_issue_50_observed_stale_marker_class_regression(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(
        config,
    )
    _write_error_marker(
        config,
        "controlled-publisher-error.json",
        {
            "recorded_at": "2026-07-16T23:46:31.078266+00:00",
            "error_type": "PublisherError",
            "message": (
                "GitHub API GET /repos/DanielTabakman/Probability-prediction-engine/pulls "
                "failed: 503"
            ),
            "draft_pr_publication_enabled": True,
            "merge_enabled": False,
            "main_write_enabled": False,
        },
    )
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "QUEUED"
    publisher = report.decision_evidence["health"]["checks"]["publisher_state"]
    assert publisher["ok"] is True
    assert publisher["preserved"] is True


def test_marker_bytes_and_sha_remain_unchanged_after_evaluation(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    marker = _write_error_marker(
        config,
        "controlled-publisher-error.json",
        {
            "recorded_at": "2026-07-16T23:46:31.078266+00:00",
            "error_type": "PublisherError",
            "message": "historical GitHub 503",
            "draft_pr_publication_enabled": True,
            "merge_enabled": False,
            "main_write_enabled": False,
        },
    )
    before = marker.read_bytes()
    before_sha = hashlib.sha256(before).hexdigest()
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "QUEUED"
    assert marker.read_bytes() == before
    assert hashlib.sha256(marker.read_bytes()).hexdigest() == before_sha
    assert (
        report.decision_evidence["health"]["checks"]["publisher_state"]["marker_sha256"]
        == before_sha
    )


def test_healthy_exact_release_witnesses_allow_refill(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    keep_one_running(config)

    report = reconcile_refill(config)

    assert report.status == "QUEUED"
    health = report.decision_evidence["health"]
    assert health["checks"]["active_release"]["ok"] is True
    assert health["checks"]["managed_services"]["ok"] is True


def test_pause_transaction_blocks_competing_reconcile_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    keep_one_running(config)
    entered_pause_snapshot = Event()
    release_pause_snapshot = Event()
    dispatches: list[str] = []
    original_snapshot = __import__(
        "msos_autobuilder.refill_controller", fromlist=["_capacity_snapshot"]
    )._capacity_snapshot

    def build_next_spy(*_args: object, **_kwargs: object) -> None:
        dispatches.append("dispatched")
        raise AssertionError("pause race should not dispatch")

    def snapshot_gate(*args: object, **kwargs: object) -> object:
        policy = load_refill_policy(config)
        if not policy.enabled and not entered_pause_snapshot.is_set():
            entered_pause_snapshot.set()
            assert release_pause_snapshot.wait(timeout=2)
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr("msos_autobuilder.refill_controller.build_next", build_next_spy)
    monkeypatch.setattr("msos_autobuilder.refill_controller._capacity_snapshot", snapshot_gate)
    pause_report: list[object] = []
    pause_thread = Thread(
        target=lambda: pause_report.append(pause_builds_and_reconcile(config))
    )
    pause_thread.start()
    assert entered_pause_snapshot.wait(timeout=2)
    reconcile_report: list[object] = []
    reconcile_thread = Thread(target=lambda: reconcile_report.append(reconcile_refill(config)))
    reconcile_thread.start()
    release_pause_snapshot.set()
    pause_thread.join(timeout=2)
    reconcile_thread.join(timeout=2)

    assert not pause_thread.is_alive()
    assert not reconcile_thread.is_alive()
    assert not dispatches
    assert pause_report[0].status == "PAUSED"
    assert reconcile_report[0].status == "PAUSED"
    policy = load_refill_policy(config)
    assert policy.enabled is False
    assert policy.last_decision_evidence is not None
    assert policy.last_decision_evidence["status"] == "PAUSED"


def test_submitted_before_import_occupies_capacity(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    keep_one_running(config)

    first = reconcile_refill(config)
    second = reconcile_refill(config)

    assert first.status == "QUEUED"
    assert second.status == "QUEUED"
    assert second.feed_awaiting_import == 1
    assert second.build_next_receipt is None


def test_published_and_failed_candidates_do_not_count_as_review_pressure(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    save_refill_policy(
        config,
        RefillPolicy(enabled=True, desired_capacity=1, review_cap_per_repository=1),
    )
    assert config.build_next.host_root is not None
    root = (
        config.build_next.host_root
        / "state"
        / "candidate-gate-results-repo"
        / "results"
        / "test-host"
    )
    cases = {
        "active": ("passed", "candidate_passed"),
        "failed": ("failed", "candidate_failed"),
        "published": ("passed", "candidate_passed"),
    }
    for job_id, (status, state) in cases.items():
        job_dir = root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "gate-report.json").write_text(
            json.dumps({"status": status, "state": state}) + "\n",
            encoding="utf-8",
        )
        (job_dir / "job.yaml").write_text(
            json.dumps(
                {
                    "version": 1,
                    "job_id": job_id,
                    "candidate_validation": {"target_repository": SOURCE_REPO},
                }
            )
            + "\n",
            encoding="utf-8",
        )
    publisher = config.build_next.host_root / "state" / "controlled-publisher-seen.json"
    publisher.write_text(json.dumps({"published": {"pr": 1}}) + "\n", encoding="utf-8")

    report = reconcile_refill(config)

    assert report.status == "BACKPRESSURE"
    assert report.awaiting_review == {SOURCE_REPO: 1}


def test_refill_service_reconciles_on_restart_without_founder_call(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    keep_one_running(config)
    service = RefillService(config, interval_seconds=0.01)

    report = service.run_once()
    status = service.read_status()

    assert report.status == "QUEUED"
    assert status.state == "running"
    assert status.last_reconcile is not None


def test_refill_service_graceful_stop_writes_stopped_status(tmp_path: Path) -> None:
    base = _refill_config(tmp_path)
    config = RefillConfig(build_next=replace(base.build_next, submit=False))
    _write_host_status(config)
    keep_one_running(config)
    service = RefillService(config, interval_seconds=0.01)
    thread = Thread(target=service.run_forever)
    thread.start()
    sleep(0.05)

    service.request_stop()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert service.read_status().state == "stopped"


def _ready_snapshot_with_two_items() -> dict[str, object]:
    snapshot = _snapshot()
    second = dict(snapshot["pipelines"][0]["ready_work"][0])
    second["work_item_id"] = "fixture_work_b"
    snapshot["pipelines"][0]["ready_work"].append(second)
    return snapshot


A_WORK_ITEM = "options_horizon_comparison_v1"
B_WORK_ITEM = "options_expression_fit_ranking_v1"


def _ready_snapshot_with_a_b() -> dict[str, object]:
    snapshot = _snapshot(work_item_id=A_WORK_ITEM)
    second = dict(snapshot["pipelines"][0]["ready_work"][0])
    second["work_item_id"] = B_WORK_ITEM
    snapshot["pipelines"][0]["ready_work"].append(second)
    return snapshot


def _write_gate_report(
    config: RefillConfig,
    job_id: str,
    *,
    status: str = "failed",
    state: str = "candidate_failed",
) -> None:
    assert config.build_next.host_root is not None
    root = (
        config.build_next.host_root
        / "state"
        / "candidate-gate-results-repo"
        / "results"
        / "test-host"
        / job_id
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "gate-report.json").write_text(
        json.dumps({"status": status, "state": state, "job_id": job_id}) + "\n",
        encoding="utf-8",
    )
    (root / "job.yaml").write_text(
        json.dumps({"version": 1, "job_id": job_id}) + "\n",
        encoding="utf-8",
    )


def _write_revision_seen(config: RefillConfig, source_job_id: str, revision_job_id: str) -> None:
    _write_state_json(
        config,
        "revision-loop-seen.json",
        {
            f"test-host/{source_job_id}": {
                "source_job_id": source_job_id,
                "revision_job_id": revision_job_id,
                "gate_report_sha256": "1" * 64,
                "jobs_commit": "2" * 40,
                "queued_at": "2026-07-20T00:00:00+00:00",
            }
        },
    )


def _seed_generation(
    config: RefillConfig,
    *,
    job_id: str = "attempt-a",
    work_item_id: str = "fixture_work",
    consumed: bool = False,
) -> dict[str, object]:
    generation = {
        "version": 1,
        "generation_id": "refill-generation-1",
        "created_at": "2026-07-20T00:00:00+00:00",
        "founder_intent": "refill-keep-one",
        "desired_capacity": 1,
        "source_ppe_identity": None,
        "attempt_sequence": [
            {
                "attempt_ordinal": 1,
                "retry_ordinal": 0,
                "reason": "initial",
                "job_id": job_id,
                "work_item_id": work_item_id,
                "pipeline_id": "ppe",
                "source_commit": "a" * 40,
            }
        ],
        "current_attempt": {
            "attempt_ordinal": 1,
            "retry_ordinal": 0,
            "reason": "initial",
            "job_id": job_id,
            "work_item_id": work_item_id,
            "pipeline_id": "ppe",
            "source_commit": "a" * 40,
        },
        "attempted_work_item_ids": [work_item_id],
        "item_scoped_terminal_exclusions": [],
        "provider_failure": None,
        "trustworthy_retry_at": None,
        "provider_retry_consumed": consumed,
        "state": "READY",
    }
    return save_refill_generation(config, generation)


def _archive_attempt(
    config: RefillConfig,
    job_id: str,
    *,
    failed: bool = False,
    message: str = "",
    error: dict[str, object] | None = None,
) -> None:
    assert config.build_next.host_root is not None
    root = config.build_next.host_root / "queue" / ("failed" if failed else "completed") / job_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "job.yaml").write_text(json.dumps({"version": 1, "job_id": job_id}) + "\n")
    if failed:
        (root / "error.json").write_text(
            json.dumps(error or {"message": message, "traceback": message}) + "\n",
            encoding="utf-8",
        )


def _submit_tracked_attempt(config: RefillConfig) -> str:
    keep_one_running(config)
    report = reconcile_refill(config)
    assert report.status == "QUEUED"
    assert report.build_next_receipt is not None
    generation = load_refill_generation(config)
    assert generation is not None
    assert generation["current_attempt"]["job_id"] == report.build_next_receipt.job_id
    assert config.build_next.checkout_root is not None
    assert (
        config.build_next.checkout_root
        / config.build_next.jobs_path
        / f"{report.build_next_receipt.job_id}.yaml"
    ).exists()
    return report.build_next_receipt.job_id


def _feed_job_path(config: RefillConfig, job_id: str) -> Path:
    assert config.build_next.checkout_root is not None
    return config.build_next.checkout_root / config.build_next.jobs_path / f"{job_id}.yaml"


def _policy_file(config: RefillConfig) -> Path:
    assert config.build_next.host_root is not None
    return config.build_next.host_root / "state" / "refill-policy.json"


def _generation_file(config: RefillConfig) -> Path:
    assert config.build_next.host_root is not None
    return config.build_next.host_root / "state" / "refill-generation.json"


def _generation_history_file(config: RefillConfig, generation: dict[str, object]) -> Path:
    assert config.build_next.host_root is not None
    return (
        config.build_next.host_root
        / "state"
        / "refill-generation-history"
        / f"{generation['generation_id']}.json"
    )


def _clear_generation_attempt_ledger(config: RefillConfig) -> dict[str, object]:
    generation = load_refill_generation(config)
    assert generation is not None
    generation["attempt_sequence"] = []
    generation["current_attempt"] = None
    generation["attempted_work_item_ids"] = []
    generation["state"] = "READY"
    return save_refill_generation(config, generation)


def _archive_job_yaml_from_feed(
    config: RefillConfig,
    job_id: str,
    *,
    failed: bool = False,
) -> None:
    assert config.build_next.host_root is not None
    source = _feed_job_path(config, job_id)
    root = config.build_next.host_root / "queue" / ("failed" if failed else "completed") / job_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "job.yaml").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    if failed:
        (root / "error.json").write_text(
            json.dumps({"message": "unexpected local crash", "traceback": ""}) + "\n",
            encoding="utf-8",
        )


def test_keep_one_creates_generation_and_refuses_ready_overwrite(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    keep_one_running(config)
    first = load_refill_generation(config)
    pause_builds(config)
    resume_builds(config)
    resumed = load_refill_generation(config)

    with pytest.raises(RefillControllerError, match="unresolved refill generation"):
        keep_one_running(config)
    second = load_refill_generation(config)

    assert first is not None and resumed is not None and second is not None
    assert first["generation_id"] == resumed["generation_id"] == second["generation_id"]
    assert second["attempt_sequence"] == []
    assert second["provider_retry_consumed"] is False


def test_legacy_paused_state_without_generation_cannot_dispatch_on_reconcile(
    tmp_path: Path,
) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    save_refill_policy(config, RefillPolicy(enabled=True, desired_capacity=1))

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    assert report.decision_evidence["reason"] == "missing_generation"


def test_host_completion_without_downstream_terminal_evidence_occupies_capacity(
    tmp_path: Path,
) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    save_refill_policy(config, RefillPolicy(enabled=True, desired_capacity=1))
    _seed_generation(config)
    _archive_attempt(config, "attempt-a")

    report = reconcile_refill(config)

    assert report.status == "RUNNING"
    assert report.decision_evidence["reason"] == "tracked_attempt_capacity_full"


def test_feed_submitted_job_recovery_after_pre_ledger_crash(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    _clear_generation_attempt_ledger(config)

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "QUEUED"
    assert generation is not None
    assert generation["current_attempt"]["job_id"] == job_id
    assert generation["attempt_sequence"][0]["job_id"] == job_id
    assert generation["last_attempt_recovery"]["stage"] == "feed"


def test_completed_and_failed_attempt_recovery(tmp_path: Path) -> None:
    completed = _refill_config(tmp_path / "completed")
    _write_host_status(completed)
    completed_job = _submit_tracked_attempt(completed)
    _archive_job_yaml_from_feed(completed, completed_job)
    _clear_generation_attempt_ledger(completed)

    failed = _refill_config(tmp_path / "failed")
    _write_host_status(failed)
    failed_job = _submit_tracked_attempt(failed)
    _archive_job_yaml_from_feed(failed, failed_job, failed=True)
    _clear_generation_attempt_ledger(failed)

    completed_report = reconcile_refill(completed)
    failed_report = reconcile_refill(failed)

    completed_generation = load_refill_generation(completed)
    failed_generation = load_refill_generation(failed)
    assert completed_report.status == "RUNNING"
    assert completed_generation is not None
    assert completed_generation["current_attempt"]["job_id"] == completed_job
    assert completed_generation["last_attempt_recovery"]["stage"] == "completed"
    assert failed_report.status == "BLOCKED"
    assert failed_generation is not None
    assert failed_generation["current_attempt"]["job_id"] == failed_job
    assert failed_generation["last_attempt_recovery"]["stage"] == "failed"


def test_conflicting_recovery_evidence_blocks(tmp_path: Path) -> None:
    config = _refill_config(
        tmp_path,
        ppe=_write_ppe(tmp_path / "ppe", snapshot=_ready_snapshot_with_two_items()),
    )
    _write_host_status(config)
    first_job = _submit_tracked_attempt(config)
    generation = _clear_generation_attempt_ledger(config)
    assert config.build_next.host_root is not None
    second_text = _feed_job_path(config, first_job).read_text(encoding="utf-8").replace(
        first_job,
        "different-refill-job",
    )
    pending = config.build_next.host_root / "queue" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / "different-refill-job.yaml").write_text(second_text, encoding="utf-8")
    save_refill_generation(config, generation)

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    assert report.decision_evidence["reason"] == "ambiguous_refill_attempt_recovery"


def test_restart_recovery_is_idempotent(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    _clear_generation_attempt_ledger(config)

    first = reconcile_refill(config)
    second = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert first.status == "QUEUED"
    assert second.status == "QUEUED"
    assert generation is not None
    assert [attempt["job_id"] for attempt in generation["attempt_sequence"]] == [job_id]


def test_missing_tracked_attempt_evidence_blocks(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    save_refill_policy(config, RefillPolicy(enabled=True, desired_capacity=1))
    _seed_generation(config)

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "BLOCKED"
    assert report.decision_evidence["reason"] == "ambiguous_attempt"
    assert generation is not None
    assert generation["last_attempt_classification"]["stage"] == "missing_attempt_evidence"


def test_item_terminal_attempt_excludes_a_and_dispatches_b(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe", snapshot=_ready_snapshot_with_two_items())
    feed = _feed_repo(tmp_path / "feed-work")
    config = _refill_config(tmp_path, ppe=ppe, feed=feed)
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    _archive_attempt(config, job_id)
    _write_state_json(
        config,
        "controlled-publisher-seen.json",
        {job_id: {"published_at": "2026-07-20T01:00:00+00:00", "status": "published-draft"}},
    )

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "QUEUED"
    assert report.build_next_receipt is not None
    assert report.build_next_receipt.work_item_id == "fixture_work_b"
    assert generation is not None
    assert generation["item_scoped_terminal_exclusions"] == ["fixture_work"]
    assert report.feed_awaiting_import == 1


def test_provider_retry_waits_until_retry_at_and_uses_fresh_same_item_identity(
    tmp_path: Path,
) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")
    early = RefillConfig(
        build_next=_config(tmp_path, ppe, feed, host_root=tmp_path / "host"),
        clock=lambda: datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
    )
    _write_host_status(early)
    job_id = _submit_tracked_attempt(early)
    _archive_attempt(
        early,
        job_id,
        failed=True,
        error={
            "provider_failure": {
                "version": 1,
                "scope": "provider",
                "temporary": True,
                "retryable": True,
                "retry_at": "2026-07-25T15:04:00Z",
            }
        },
    )

    before = reconcile_refill(early)
    due = RefillConfig(
        build_next=early.build_next,
        clock=lambda: datetime(2026, 7, 25, 15, 4, tzinfo=UTC),
    )
    at_retry = reconcile_refill(due)
    generation = load_refill_generation(due)

    assert before.status == "BACKPRESSURE"
    assert at_retry.status == "QUEUED"
    assert at_retry.build_next_receipt is not None
    assert at_retry.build_next_receipt.work_item_id == "fixture_work"
    assert at_retry.build_next_receipt.job_id != job_id
    assert generation is not None
    assert generation["provider_retry_consumed"] is True
    assert len(generation["attempt_sequence"]) == 2


def test_structured_provider_failure_without_retry_authorization_does_not_retry(
    tmp_path: Path,
) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    _archive_attempt(
        config,
        job_id,
        failed=True,
        error={
            "provider_failure": {
                "version": 1,
                "scope": "provider",
                "temporary": False,
                "retryable": False,
                "retry_at": "2026-07-25T15:04:00Z",
            }
        },
    )

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "BACKPRESSURE"
    assert report.build_next_receipt is None
    assert generation is not None
    assert len(generation["attempt_sequence"]) == 1


def test_prose_only_provider_failure_does_not_trigger_retry(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    _archive_attempt(
        config,
        job_id,
        failed=True,
        message="ERROR: usage limit; quota exhausted; try again at Jul 25th, 2026 3:04 PM.",
    )

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "BACKPRESSURE"
    assert report.build_next_receipt is None
    assert generation is not None
    assert generation["provider_retry_consumed"] is False


def test_unknown_failure_blocks_without_exclusion_or_b_dispatch(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe", snapshot=_ready_snapshot_with_two_items())
    feed = _feed_repo(tmp_path / "feed-work")
    config = _refill_config(tmp_path, ppe=ppe, feed=feed)
    _write_host_status(config)
    save_refill_policy(config, RefillPolicy(enabled=True, desired_capacity=1))
    _seed_generation(config)
    _archive_attempt(config, "attempt-a", failed=True, message="unexpected local crash")

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "BLOCKED"
    assert report.build_next_receipt is None
    assert generation is not None
    assert generation["item_scoped_terminal_exclusions"] == []


def test_ppe_source_movement_blocks_exclusion_rerank(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe", snapshot=_ready_snapshot_with_two_items())
    feed = _feed_repo(tmp_path / "feed-work")
    config = _refill_config(tmp_path, ppe=ppe, feed=feed)
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    pinned = load_refill_generation(config)["source_ppe_identity"]["commit"]
    (ppe / "movement.txt").write_text("moved\n", encoding="utf-8")
    moved = _commit_all(ppe, "move ppe main")
    _git(ppe, "push", "-q", "origin", "main")
    _archive_attempt(config, job_id)
    _write_state_json(config, "controlled-publisher-seen.json", {job_id: {"status": "published"}})

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert moved != pinned
    assert report.status == "BLOCKED"
    assert report.build_next_receipt is None
    assert report.decision_evidence["reason"] == "source_pin_mismatch"
    assert generation["item_scoped_terminal_exclusions"] == []


def test_ppe_source_movement_blocks_provider_retry(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")
    config = RefillConfig(
        build_next=_config(tmp_path, ppe, feed, host_root=tmp_path / "host"),
        clock=lambda: datetime(2026, 7, 25, 15, 4, tzinfo=UTC),
    )
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    (ppe / "movement.txt").write_text("moved\n", encoding="utf-8")
    _commit_all(ppe, "move ppe main")
    _git(ppe, "push", "-q", "origin", "main")
    _archive_attempt(
        config,
        job_id,
        failed=True,
        error={
            "provider_failure": {
                "version": 1,
                "scope": "provider",
                "temporary": True,
                "retryable": True,
                "retry_at": "2026-07-25T15:04:00Z",
            }
        },
    )

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    assert report.build_next_receipt is None
    assert report.decision_evidence["reason"] == "source_pin_mismatch"


def test_pause_resume_at_unchanged_pinned_source_proceeds(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    pause_builds(config)
    resume_builds(config)
    _archive_attempt(config, job_id)
    _write_state_json(config, "controlled-publisher-seen.json", {job_id: {"status": "published"}})

    report = reconcile_refill(config)

    assert report.status == "UNFILLED"
    assert report.build_next_receipt is not None


def test_fresh_generation_after_ppe_source_movement_pins_new_source(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")
    config = _refill_config(tmp_path, ppe=ppe, feed=feed)
    _write_host_status(config)
    first_job = _submit_tracked_attempt(config)
    first_generation = load_refill_generation(config)
    first_commit = first_generation["source_ppe_identity"]["commit"]
    _archive_attempt(config, first_job)
    _write_state_json(
        config,
        "controlled-publisher-seen.json",
        {first_job: {"status": "published"}},
    )
    pause_builds(config)
    (ppe / "movement.txt").write_text("moved\n", encoding="utf-8")
    second_commit = _commit_all(ppe, "move ppe main")
    _git(ppe, "push", "-q", "origin", "main")

    with pytest.raises(RefillControllerError, match="unresolved refill generation"):
        keep_one_running(config)
    generation = load_refill_generation(config)

    assert second_commit != first_commit
    assert generation is not None
    assert generation["source_ppe_identity"]["commit"] == first_commit


def test_unfilled_generation_archives_and_replaces_idempotently(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    keep_one_running(config)
    generation = load_refill_generation(config)
    assert generation is not None
    generation["state"] = "UNFILLED"
    save_refill_generation(config, generation)

    keep_one_running(config)
    replacement = load_refill_generation(config)
    history = (
        config.build_next.host_root
        / "state"
        / "refill-generation-history"
        / f"{generation['generation_id']}.json"
    )

    assert replacement is not None
    assert replacement["generation_id"] != generation["generation_id"]
    assert json.loads(history.read_text(encoding="utf-8")) == generation


def test_conflicting_generation_history_blocks_replacement(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    keep_one_running(config)
    generation = load_refill_generation(config)
    assert generation is not None
    generation["state"] = "UNFILLED"
    save_refill_generation(config, generation)
    history = (
        config.build_next.host_root
        / "state"
        / "refill-generation-history"
        / f"{generation['generation_id']}.json"
    )
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(json.dumps({"conflict": True}) + "\n", encoding="utf-8")

    with pytest.raises(RefillControllerError, match="history conflicts"):
        keep_one_running(config)


@pytest.mark.parametrize("state", ["PAUSED", "BLOCKED", "BACKPRESSURE"])
def test_keep_one_cannot_overwrite_unresolved_terminal_like_states(
    tmp_path: Path, state: str
) -> None:
    config = _refill_config(tmp_path)
    keep_one_running(config)
    generation = load_refill_generation(config)
    assert generation is not None
    generation["state"] = state
    save_refill_generation(config, generation)

    with pytest.raises(RefillControllerError, match="unresolved refill generation"):
        keep_one_running(config)


def test_prepared_dispatch_crash_before_feed_replays_same_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    keep_one_running(config)
    real_build_next = refill_controller.build_next

    def crash_after_prepare(build_config: object) -> object:
        if getattr(build_config, "submit", True):
            raise RuntimeError("crash before feed")
        return real_build_next(build_config)

    monkeypatch.setattr(refill_controller, "build_next", crash_after_prepare)
    with pytest.raises(RuntimeError, match="crash before feed"):
        reconcile_refill(config)
    prepared = load_refill_generation(config)["prepared_dispatch"]

    monkeypatch.setattr(refill_controller, "build_next", real_build_next)
    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "QUEUED"
    assert generation is not None
    assert "prepared_dispatch" not in generation
    assert generation["current_attempt"]["attempt_ordinal"] == prepared["attempt_ordinal"] == 1
    assert generation["current_attempt"]["job_id"] == prepared["job_id"]


def test_prepared_dispatch_crash_after_feed_recovers_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    keep_one_running(config)
    real_build_next = refill_controller.build_next

    def crash_after_feed(build_config: object) -> object:
        receipt = real_build_next(build_config)
        if getattr(build_config, "submit", True):
            raise RuntimeError("crash after feed")
        return receipt

    monkeypatch.setattr(refill_controller, "build_next", crash_after_feed)
    with pytest.raises(RuntimeError, match="crash after feed"):
        reconcile_refill(config)

    monkeypatch.setattr(refill_controller, "build_next", real_build_next)
    first = reconcile_refill(config)
    second = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert first.status == "QUEUED"
    assert second.status == "QUEUED"
    assert generation is not None
    assert len(generation["attempt_sequence"]) == 1
    assert "prepared_dispatch" not in generation


def test_revision_pending_owns_failed_source_item_and_blocks_b(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe", snapshot=_ready_snapshot_with_a_b())
    feed = _feed_repo(tmp_path / "feed-work")
    config = _refill_config(tmp_path, ppe=ppe, feed=feed)
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    _archive_job_yaml_from_feed(config, job_id)
    _write_gate_report(config, job_id, status="failed", state="candidate_failed")
    revision_id = "revision-for-a"
    _write_revision_seen(config, job_id, revision_id)
    pending = config.build_next.host_root / "queue" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / f"{revision_id}.yaml").write_text("version: 1\n", encoding="utf-8")

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "QUEUED"
    assert report.build_next_receipt is None
    assert generation is not None
    assert generation["item_scoped_terminal_exclusions"] == []


def test_revision_provider_failure_backpressures_a_without_b_dispatch(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe", snapshot=_ready_snapshot_with_a_b())
    feed = _feed_repo(tmp_path / "feed-work")
    config = _refill_config(tmp_path, ppe=ppe, feed=feed)
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    _archive_job_yaml_from_feed(config, job_id)
    _write_gate_report(config, job_id, status="failed", state="candidate_failed")
    revision_id = "revision-provider-failed"
    _write_revision_seen(config, job_id, revision_id)
    _archive_attempt(
        config,
        revision_id,
        failed=True,
        error={
            "provider_failure": {
                "version": 1,
                "scope": "provider",
                "temporary": True,
                "retryable": False,
                "retry_at": "2026-07-25T15:04:00Z",
            }
        },
    )

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "BACKPRESSURE"
    assert report.build_next_receipt is None
    assert generation is not None
    assert generation["item_scoped_terminal_exclusions"] == []


def test_terminal_revision_excludes_a_and_allows_b(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe", snapshot=_ready_snapshot_with_a_b())
    feed = _feed_repo(tmp_path / "feed-work")
    config = _refill_config(tmp_path, ppe=ppe, feed=feed)
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    _archive_job_yaml_from_feed(config, job_id)
    _write_gate_report(config, job_id, status="failed", state="candidate_failed")
    revision_id = "revision-terminal"
    _write_revision_seen(config, job_id, revision_id)
    _archive_attempt(config, revision_id)
    _write_gate_report(config, revision_id, status="failed", state="candidate_rejected")

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "QUEUED"
    assert report.build_next_receipt is not None
    assert report.build_next_receipt.work_item_id == B_WORK_ITEM
    assert generation is not None
    assert generation["item_scoped_terminal_exclusions"] == [A_WORK_ITEM]


def test_multiple_revision_descendants_block(tmp_path: Path) -> None:
    config = _refill_config(
        tmp_path,
        ppe=_write_ppe(tmp_path / "ppe", snapshot=_ready_snapshot_with_a_b()),
    )
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    _archive_job_yaml_from_feed(config, job_id)
    _write_gate_report(config, job_id, status="failed", state="candidate_failed")
    _write_state_json(
        config,
        "revision-loop-seen.json",
        {
            f"test-host/{job_id}": {"source_job_id": job_id, "revision_job_id": "r1"},
            f"other/{job_id}": {"source_job_id": job_id, "revision_job_id": "r2"},
        },
    )

    report = reconcile_refill(config)

    assert report.status == "BLOCKED"
    assert report.decision_evidence["reason"] == "ambiguous_attempt"


def test_generic_quota_text_backpressures_without_retry(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    _archive_attempt(config, job_id, failed=True, message="Codex quota exhausted; try again later")

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "BACKPRESSURE"
    assert report.build_next_receipt is None
    assert generation is not None
    assert generation["provider_retry_consumed"] is False


def test_timezone_free_structured_retry_at_does_not_authorize_retry(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    _archive_attempt(
        config,
        job_id,
        failed=True,
        error={
            "provider_failure": {
                "version": 1,
                "scope": "provider",
                "temporary": True,
                "retryable": True,
                "retry_at": "2026-07-25T15:04:00",
            }
        },
    )

    report = reconcile_refill(config)

    assert report.status == "BACKPRESSURE"
    assert report.build_next_receipt is None


def test_second_provider_failure_does_not_retry_or_dispatch_b(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe", snapshot=_ready_snapshot_with_a_b())
    feed = _feed_repo(tmp_path / "feed-work")
    config = RefillConfig(
        build_next=_config(tmp_path, ppe, feed, host_root=tmp_path / "host"),
        clock=lambda: datetime(2026, 7, 25, 15, 4, tzinfo=UTC),
    )
    _write_host_status(config)
    first_job = _submit_tracked_attempt(config)
    _archive_attempt(
        config,
        first_job,
        failed=True,
        error={
            "provider_failure": {
                "version": 1,
                "scope": "provider",
                "temporary": True,
                "retryable": True,
                "retry_at": "2026-07-25T15:04:00Z",
            }
        },
    )
    retry_report = reconcile_refill(config)
    retry_job = retry_report.build_next_receipt.job_id
    _archive_job_yaml_from_feed(config, retry_job, failed=True)

    second = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert second.status == "BLOCKED"
    assert second.build_next_receipt is None
    assert generation is not None
    assert generation["item_scoped_terminal_exclusions"] == []


def test_auth_text_is_nonretryable_but_structured_temporary_auth_can_retry(
    tmp_path: Path,
) -> None:
    auth_text = _refill_config(tmp_path / "auth-text")
    _write_host_status(auth_text)
    text_job = _submit_tracked_attempt(auth_text)
    _archive_attempt(auth_text, text_job, failed=True, message="authentication failed")

    structured = RefillConfig(
        build_next=_refill_config(tmp_path / "auth-structured").build_next,
        clock=lambda: datetime(2026, 7, 25, 15, 4, tzinfo=UTC),
    )
    _write_host_status(structured)
    structured_job = _submit_tracked_attempt(structured)
    _archive_attempt(
        structured,
        structured_job,
        failed=True,
        error={
            "provider_failure": {
                "version": 1,
                "scope": "provider",
                "temporary": True,
                "retryable": True,
                "retry_at": "2026-07-25T15:04:00+00:00",
                "reason": "temporary auth outage",
            }
        },
    )

    text_report = reconcile_refill(auth_text)
    structured_report = reconcile_refill(structured)

    assert text_report.status == "BACKPRESSURE"
    assert text_report.build_next_receipt is None
    assert structured_report.status == "QUEUED"
    assert structured_report.build_next_receipt is not None


def test_persistent_feed_copy_does_not_prevent_terminal_a_advancing_to_b(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe", snapshot=_ready_snapshot_with_a_b())
    feed = _feed_repo(tmp_path / "feed-work")
    config = _refill_config(tmp_path, ppe=ppe, feed=feed)
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    _archive_job_yaml_from_feed(config, job_id)
    _write_state_json(config, "controlled-publisher-seen.json", {job_id: {"status": "published"}})

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "QUEUED"
    assert report.build_next_receipt is not None
    assert report.build_next_receipt.work_item_id == B_WORK_ITEM
    assert generation is not None
    assert generation["item_scoped_terminal_exclusions"] == [A_WORK_ITEM]


def test_failed_keep_one_leaves_paused_policy_and_generation_bytes_unchanged(
    tmp_path: Path,
) -> None:
    config = _refill_config(tmp_path)
    keep_one_running(config)
    pause_builds(config)
    policy_before = _policy_file(config).read_bytes()
    generation_before = _generation_file(config).read_bytes()

    with pytest.raises(RefillControllerError, match="unresolved refill generation"):
        keep_one_running(config)

    assert _policy_file(config).read_bytes() == policy_before
    assert _generation_file(config).read_bytes() == generation_before
    assert load_refill_policy(config).enabled is False


def test_conflicting_history_leaves_policy_generation_and_history_bytes_unchanged(
    tmp_path: Path,
) -> None:
    config = _refill_config(tmp_path)
    keep_one_running(config)
    generation = load_refill_generation(config)
    assert generation is not None
    generation["state"] = "UNFILLED"
    save_refill_generation(config, generation)
    save_refill_policy(config, RefillPolicy(enabled=False, desired_capacity=0))
    history = _generation_history_file(config, generation)
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(json.dumps({"conflict": True}) + "\n", encoding="utf-8")
    policy_before = _policy_file(config).read_bytes()
    generation_before = _generation_file(config).read_bytes()
    history_before = history.read_bytes()

    with pytest.raises(RefillControllerError, match="history conflicts"):
        keep_one_running(config)

    assert _policy_file(config).read_bytes() == policy_before
    assert _generation_file(config).read_bytes() == generation_before
    assert history.read_bytes() == history_before


@pytest.mark.parametrize("history_text", ["{not-json", "[]"])
def test_malformed_history_blocks_without_overwriting_existing_bytes(
    tmp_path: Path, history_text: str
) -> None:
    config = _refill_config(tmp_path)
    keep_one_running(config)
    generation = load_refill_generation(config)
    assert generation is not None
    generation["state"] = "UNFILLED"
    save_refill_generation(config, generation)
    save_refill_policy(config, RefillPolicy(enabled=False, desired_capacity=0))
    history = _generation_history_file(config, generation)
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(history_text, encoding="utf-8")
    policy_before = _policy_file(config).read_bytes()
    generation_before = _generation_file(config).read_bytes()
    history_before = history.read_bytes()

    with pytest.raises(RefillControllerError, match="history"):
        keep_one_running(config)

    assert _policy_file(config).read_bytes() == policy_before
    assert _generation_file(config).read_bytes() == generation_before
    assert history.read_bytes() == history_before


def _provider_retry_ready_config(tmp_path: Path) -> tuple[RefillConfig, str]:
    config = RefillConfig(
        build_next=_refill_config(tmp_path).build_next,
        clock=lambda: datetime(2026, 7, 25, 15, 4, tzinfo=UTC),
    )
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    _archive_attempt(
        config,
        job_id,
        failed=True,
        error={
            "provider_failure": {
                "version": 1,
                "scope": "provider",
                "temporary": True,
                "retryable": True,
                "retry_at": "2026-07-25T15:04:00Z",
            }
        },
    )
    return config, job_id


def test_crash_during_retry_dry_prepare_leaves_retry_unconsumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _job_id = _provider_retry_ready_config(tmp_path)
    real_build_next = refill_controller.build_next

    def crash_retry_dry(build_config: object) -> object:
        attempt = getattr(build_config, "refill_attempt", None)
        if (
            getattr(build_config, "submit", True) is False
            and getattr(attempt, "reason", "") == "provider_retry"
        ):
            raise RuntimeError("dry retry crash")
        return real_build_next(build_config)

    monkeypatch.setattr(refill_controller, "build_next", crash_retry_dry)
    generation_before = load_refill_generation(config)
    with pytest.raises(RuntimeError, match="dry retry crash"):
        reconcile_refill(config)
    generation = load_refill_generation(config)

    assert generation == generation_before
    assert generation is not None
    assert generation["provider_retry_consumed"] is False
    assert "prepared_dispatch" not in generation

    monkeypatch.setattr(refill_controller, "build_next", real_build_next)
    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "QUEUED"
    assert generation is not None
    assert generation["provider_retry_consumed"] is True
    assert len(generation["attempt_sequence"]) == 2


def test_crash_after_prepared_retry_before_feed_recovers_same_retry_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _job_id = _provider_retry_ready_config(tmp_path)
    real_build_next = refill_controller.build_next

    def crash_retry_submit(build_config: object) -> object:
        attempt = getattr(build_config, "refill_attempt", None)
        if (
            getattr(build_config, "submit", True)
            and getattr(attempt, "reason", "") == "provider_retry"
        ):
            raise RuntimeError("retry feed crash")
        return real_build_next(build_config)

    monkeypatch.setattr(refill_controller, "build_next", crash_retry_submit)
    with pytest.raises(RuntimeError, match="retry feed crash"):
        reconcile_refill(config)
    prepared = load_refill_generation(config)["prepared_dispatch"]

    monkeypatch.setattr(refill_controller, "build_next", real_build_next)
    first = reconcile_refill(config)
    second = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert first.status == "QUEUED"
    assert second.status == "QUEUED"
    assert generation is not None
    assert generation["provider_retry_consumed"] is True
    assert generation["current_attempt"]["job_id"] == prepared["job_id"]
    retry_jobs = [
        item["job_id"]
        for item in generation["attempt_sequence"]
        if item["retry_ordinal"] == 1
    ]
    assert retry_jobs == [prepared["job_id"]]


@pytest.mark.parametrize("occupancy", ["pending", "running"])
def test_prepared_replay_waits_for_unrelated_capacity_then_submits_same_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, occupancy: str
) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    keep_one_running(config)
    real_build_next = refill_controller.build_next

    def crash_after_prepare(build_config: object) -> object:
        if getattr(build_config, "submit", True):
            raise RuntimeError("before feed")
        return real_build_next(build_config)

    monkeypatch.setattr(refill_controller, "build_next", crash_after_prepare)
    with pytest.raises(RuntimeError, match="before feed"):
        reconcile_refill(config)
    prepared = load_refill_generation(config)["prepared_dispatch"]
    queue_dir = config.build_next.host_root / "queue" / occupancy
    queue_dir.mkdir(parents=True, exist_ok=True)
    unrelated = queue_dir / "unrelated.yaml"
    unrelated.write_text("version: 1\n", encoding="utf-8")

    monkeypatch.setattr(refill_controller, "build_next", real_build_next)
    blocked = reconcile_refill(config)
    generation = load_refill_generation(config)
    unrelated.unlink()
    replayed = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert blocked.status == ("RUNNING" if occupancy == "running" else "QUEUED")
    assert blocked.build_next_receipt is None
    assert generation is not None
    assert replayed.status == "QUEUED"
    assert replayed.build_next_receipt is not None
    assert replayed.build_next_receipt.job_id == prepared["job_id"]
    assert generation["current_attempt"]["job_id"] == prepared["job_id"]


def _seed_failed_source_with_revision_ledger(
    tmp_path: Path, ledger: dict[str, object]
) -> tuple[RefillConfig, dict[str, object]]:
    ppe = _write_ppe(tmp_path / "ppe", snapshot=_ready_snapshot_with_a_b())
    config = _refill_config(tmp_path, ppe=ppe, feed=_feed_repo(tmp_path / "feed-work"))
    _write_host_status(config)
    job_id = _submit_tracked_attempt(config)
    _archive_job_yaml_from_feed(config, job_id)
    _write_gate_report(config, job_id, status="failed", state="candidate_failed")
    _write_state_json(config, "revision-loop-seen.json", ledger)
    return config, {"job_id": job_id}


@pytest.mark.parametrize(
    "case_name,ledger_factory",
    [
        (
            "missing_revision_job_id",
            lambda job_id: {
                f"test-host/{job_id}": {
                    "source_job_id": job_id,
                    "gate_report_sha256": "1" * 64,
                    "jobs_commit": "2" * 40,
                    "queued_at": "2026-07-20T00:00:00Z",
                }
            },
        ),
        (
            "missing_gate_hash",
            lambda job_id: {
                f"test-host/{job_id}": {
                    "source_job_id": job_id,
                    "revision_job_id": "revision-a",
                    "jobs_commit": "2" * 40,
                    "queued_at": "2026-07-20T00:00:00Z",
                }
            },
        ),
        (
            "invalid_jobs_commit",
            lambda job_id: {
                f"test-host/{job_id}": {
                    "source_job_id": job_id,
                    "revision_job_id": "revision-a",
                    "gate_report_sha256": "1" * 64,
                    "jobs_commit": "not-a-commit",
                    "queued_at": "2026-07-20T00:00:00Z",
                }
            },
        ),
        (
            "timezone_free_queued_at",
            lambda job_id: {
                f"test-host/{job_id}": {
                    "source_job_id": job_id,
                    "revision_job_id": "revision-a",
                    "gate_report_sha256": "1" * 64,
                    "jobs_commit": "2" * 40,
                    "queued_at": "2026-07-20T00:00:00",
                }
            },
        ),
        (
            "key_source_disagreement",
            lambda job_id: {
                f"test-host/{job_id}": {
                    "source_job_id": "other-source",
                    "revision_job_id": "revision-a",
                    "gate_report_sha256": "1" * 64,
                    "jobs_commit": "2" * 40,
                    "queued_at": "2026-07-20T00:00:00Z",
                }
            },
        ),
        (
            "source_points_to_self",
            lambda job_id: {
                f"test-host/{job_id}": {
                    "source_job_id": job_id,
                    "revision_job_id": job_id,
                    "gate_report_sha256": "1" * 64,
                    "jobs_commit": "2" * 40,
                    "queued_at": "2026-07-20T00:00:00Z",
                }
            },
        ),
        (
            "cycle_a_r_a",
            lambda job_id: {
                f"test-host/{job_id}": {
                    "source_job_id": job_id,
                    "revision_job_id": "revision-a",
                    "gate_report_sha256": "1" * 64,
                    "jobs_commit": "2" * 40,
                    "queued_at": "2026-07-20T00:00:00Z",
                },
                "test-host/revision-a": {
                    "source_job_id": "revision-a",
                    "revision_job_id": job_id,
                    "gate_report_sha256": "3" * 64,
                    "jobs_commit": "4" * 40,
                    "queued_at": "2026-07-20T00:01:00Z",
                },
            },
        ),
        (
            "descendant_revision",
            lambda job_id: {
                f"test-host/{job_id}": {
                    "source_job_id": job_id,
                    "revision_job_id": "revision-a",
                    "gate_report_sha256": "1" * 64,
                    "jobs_commit": "2" * 40,
                    "queued_at": "2026-07-20T00:00:00Z",
                },
                "test-host/revision-a": {
                    "source_job_id": "revision-a",
                    "revision_job_id": "revision-b",
                    "gate_report_sha256": "3" * 64,
                    "jobs_commit": "4" * 40,
                    "queued_at": "2026-07-20T00:01:00Z",
                },
            },
        ),
        (
            "multiple_direct_descendants",
            lambda job_id: {
                f"test-host/{job_id}": {
                    "source_job_id": job_id,
                    "revision_job_id": "revision-a",
                    "gate_report_sha256": "1" * 64,
                    "jobs_commit": "2" * 40,
                    "queued_at": "2026-07-20T00:00:00Z",
                },
                f"other/{job_id}": {
                    "source_job_id": job_id,
                    "revision_job_id": "revision-b",
                    "gate_report_sha256": "3" * 64,
                    "jobs_commit": "4" * 40,
                    "queued_at": "2026-07-20T00:01:00Z",
                },
            },
        ),
    ],
)
def test_malformed_revision_lineage_blocks_without_excluding_a_or_dispatching_b(
    tmp_path: Path, case_name: str, ledger_factory: object
) -> None:
    config, info = _seed_failed_source_with_revision_ledger(tmp_path / case_name, {})
    job_id = info["job_id"]
    _write_state_json(config, "revision-loop-seen.json", ledger_factory(job_id))

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "BLOCKED"
    assert report.build_next_receipt is None
    assert generation is not None
    assert generation["item_scoped_terminal_exclusions"] == []


@pytest.mark.parametrize("ledger_text", ["{not-json", "[]"])
def test_corrupt_revision_ledger_blocks_without_falling_through_to_failed_gate(
    tmp_path: Path, ledger_text: str
) -> None:
    config, _info = _seed_failed_source_with_revision_ledger(tmp_path, {})
    assert config.build_next.host_root is not None
    ledger = config.build_next.host_root / "state" / "revision-loop-seen.json"
    ledger.write_text(ledger_text, encoding="utf-8")

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "BLOCKED"
    assert report.build_next_receipt is None
    assert generation is not None
    assert generation["item_scoped_terminal_exclusions"] == []
    assert generation["last_attempt_classification"]["stage"] == "revision_lineage"


@pytest.mark.parametrize(
    "case_name,descendant_factory",
    [
        ("descendant_non_object", lambda _revision_id: []),
        (
            "descendant_incomplete",
            lambda revision_id: {
                "source_job_id": revision_id,
                "gate_report_sha256": "3" * 64,
                "jobs_commit": "4" * 40,
                "queued_at": "2026-07-20T00:01:00Z",
            },
        ),
        (
            "descendant_key_source_conflict",
            lambda _revision_id: {
                "source_job_id": "other-source",
                "revision_job_id": "revision-b",
                "gate_report_sha256": "3" * 64,
                "jobs_commit": "4" * 40,
                "queued_at": "2026-07-20T00:01:00Z",
            },
        ),
    ],
)
def test_malformed_targeted_revision_descendant_blocks_without_dispatching_b(
    tmp_path: Path, case_name: str, descendant_factory: object
) -> None:
    revision_id = "revision-a"
    config, info = _seed_failed_source_with_revision_ledger(tmp_path / case_name, {})
    job_id = info["job_id"]
    _write_state_json(
        config,
        "revision-loop-seen.json",
        {
            f"test-host/{job_id}": {
                "source_job_id": job_id,
                "revision_job_id": revision_id,
                "gate_report_sha256": "1" * 64,
                "jobs_commit": "2" * 40,
                "queued_at": "2026-07-20T00:00:00Z",
            },
            f"test-host/{revision_id}": descendant_factory(revision_id),
        },
    )

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "BLOCKED"
    assert report.build_next_receipt is None
    assert generation is not None
    assert generation["item_scoped_terminal_exclusions"] == []
    assert generation["last_attempt_classification"]["stage"] == "revision_lineage"
