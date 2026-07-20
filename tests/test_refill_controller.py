from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from time import sleep

import pytest
from test_build_next import _config, _feed_repo, _snapshot, _write_ppe

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
) -> None:
    assert config.build_next.host_root is not None
    root = config.build_next.host_root / "queue" / ("failed" if failed else "completed") / job_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "job.yaml").write_text(json.dumps({"version": 1, "job_id": job_id}) + "\n")
    if failed:
        (root / "error.json").write_text(
            json.dumps({"message": message, "traceback": message}) + "\n",
            encoding="utf-8",
        )


def test_keep_one_creates_fresh_generation_and_resume_preserves_it(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    keep_one_running(config)
    first = load_refill_generation(config)
    pause_builds(config)
    resume_builds(config)
    resumed = load_refill_generation(config)
    keep_one_running(config)
    second = load_refill_generation(config)

    assert first is not None and resumed is not None and second is not None
    assert first["generation_id"] == resumed["generation_id"]
    assert first["generation_id"] != second["generation_id"]
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


def test_item_terminal_attempt_excludes_a_and_dispatches_b(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe", snapshot=_ready_snapshot_with_two_items())
    feed = _feed_repo(tmp_path / "feed-work")
    config = _refill_config(tmp_path, ppe=ppe, feed=feed)
    _write_host_status(config)
    save_refill_policy(config, RefillPolicy(enabled=True, desired_capacity=1))
    _seed_generation(config)
    _archive_attempt(config, "attempt-a")
    _write_state_json(
        config,
        "controlled-publisher-seen.json",
        {"attempt-a": {"published_at": "2026-07-20T01:00:00+00:00", "status": "published-draft"}},
    )

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "QUEUED"
    assert report.build_next_receipt is not None
    assert report.build_next_receipt.work_item_id == "fixture_work_b"
    assert generation is not None
    assert generation["item_scoped_terminal_exclusions"] == ["fixture_work"]


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
    save_refill_policy(early, RefillPolicy(enabled=True, desired_capacity=1))
    _seed_generation(early)
    _archive_attempt(
        early,
        "attempt-a",
        failed=True,
        message="ERROR: usage limit; try again at Jul 25th, 2026 3:04 PM.",
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
    assert at_retry.build_next_receipt.job_id != "attempt-a"
    assert generation is not None
    assert generation["provider_retry_consumed"] is True
    assert len(generation["attempt_sequence"]) == 2


def test_second_systemic_failure_remains_backpressure_and_no_third_attempt(tmp_path: Path) -> None:
    config = _refill_config(tmp_path)
    _write_host_status(config)
    save_refill_policy(config, RefillPolicy(enabled=True, desired_capacity=1))
    _seed_generation(config, job_id="attempt-b", consumed=True)
    _archive_attempt(
        config,
        "attempt-b",
        failed=True,
        message="ERROR: usage limit; try again at Jul 25th, 2026 3:04 PM.",
    )

    report = reconcile_refill(config)
    generation = load_refill_generation(config)

    assert report.status == "BACKPRESSURE"
    assert report.build_next_receipt is None
    assert generation is not None
    assert len(generation["attempt_sequence"]) == 1


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
