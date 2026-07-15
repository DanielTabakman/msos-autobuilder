from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from threading import Thread
from time import sleep

import pytest
from test_build_next import _config, _feed_repo, _snapshot, _write_ppe

from msos_autobuilder.refill_controller import (
    RefillConfig,
    RefillControllerError,
    RefillPolicy,
    RefillService,
    keep_one_running,
    load_refill_policy,
    pause_builds,
    reconcile_refill,
    resume_builds,
    save_refill_policy,
)

SOURCE_REPO = "DanielTabakman/Probability-prediction-engine"


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
    running.mkdir(parents=True)
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
