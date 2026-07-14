from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from msos_autobuilder.build_next import BuildNextConfig, build_next

SOURCE_REPO = "DanielTabakman/Probability-prediction-engine"


def _git(path: Path | None, *args: str) -> str:
    argv = ["git"]
    if path is not None:
        argv.extend(["-C", str(path)])
    argv.extend(args)
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return proc.stdout.strip()


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    return path


def _commit_all(repo: Path, message: str = "fixture") -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _registry(*, adapter_ready: bool = True, canonical_repo: str = SOURCE_REPO) -> dict[str, Any]:
    return {
        "version": 1,
        "canon": [
            "docs/SOP/CHATGPT_GITHUB_CODEX_CONTROL_PLANE_V1.md",
            "docs/SOP/FOUNDER_PIPELINE_COMMANDS_V1.md",
            "docs/SOP/PIPELINE_CREATION_SOP_V1.md",
            "docs/SOP/SCHEDULED_AUTOBUILDER_LANE_POLICY_V1.md",
        ],
        "pipelines": [
            {
                "pipeline_id": "ppe",
                "display_name": "PPE",
                "canonical_repo": canonical_repo,
                "registration_stage": "EXECUTION_READY",
                "build_adapter": {
                    "adapter": "ppe_operator",
                    "readiness": (
                        "READY_FOR_MANUAL_OR_SINGLE_DISPATCH"
                        if adapter_ready
                        else "NOT_READY"
                    ),
                },
                "authority": {
                    "publication_authority": "controlled publisher only; draft PR by default"
                },
                "scheduling": {"build_next_eligible": True},
            }
        ],
    }


def _plan(*, touch_set: list[str] | None = None) -> dict[str, Any]:
    paths = ["src/viz/panel.py", "tests/test_panel.py"] if touch_set is None else touch_set
    return {
        "name": "fixture plan",
        "sprintSpecPath": "docs/SOP/SPRINT_FIXTURE.md",
        "selectionRecord": "docs/SOP/POST_FIXTURE_SELECTION.md",
        "slices": [
            {
                "sliceId": "Fixture-Slice001",
                "layerPreset": "PPE_UI",
                "buildBranch": "build/auto/fixture",
                "touchSet": paths,
            },
            {
                "sliceId": "Fixture-Closeout-Slice002",
                "layerPreset": "CONTROL",
                "closeout": {
                    "evidenceDoc": "docs/SOP/FIXTURE_EVIDENCE.md",
                    "sprintSpec": "docs/SOP/SPRINT_FIXTURE.md",
                    "selectionOutcomeDoc": "docs/SOP/POST_FIXTURE_SELECTION.md",
                },
            },
        ],
    }


def _snapshot(
    *,
    state: str = "READY_TO_BUILD",
    pipeline_id: str = "ppe",
    work_state: str = "READY_TO_BUILD",
    running: list[dict[str, Any]] | None = None,
    queued: list[dict[str, Any]] | None = None,
    stale: list[dict[str, Any]] | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    work = {
        "work_item_id": "fixture_work",
        "title": "[HIGH] Fixture work",
        "native_state": "READY",
        "state": work_state,
        "trace": "docs/SOP/PHASE_PLANS/fixture_relay.json",
        "evidence": "manual",
        "selection": {
            "founder_priority": "high",
            "founder_priority_rank": 1,
            "deadline_rank": "9999-12-31T00:00:00+00:00",
            "dependency_unblock_value": 0,
            "age_index": 1,
        },
    }
    ready = [work] if work_state == "READY_TO_BUILD" else []
    return {
        "version": 1,
        "as_of": as_of or _now(),
        "read_only": True,
        "registry_errors": [],
        "capacity": {"running": len(running or []), "queued": len(queued or [])},
        "pipelines": [
            {
                "pipeline_id": pipeline_id,
                "display_name": "PPE",
                "registration_stage": "EXECUTION_READY",
                "canonical_repo": SOURCE_REPO,
                "state": state,
                "evidence": [
                    {
                        "kind": "manual",
                        "source": "docs/SOP/ACTIVE_PHASE_MANIFEST.json",
                        "fresh": True,
                    }
                ],
                "running_work": running or [],
                "ready_work": ready,
                "queued_work": queued or [],
                "awaiting_review_work": [],
                "backpressure": [],
                "stale_evidence": stale or [],
            }
        ],
        "recommended_next_action": (
            {
                "pipeline_id": pipeline_id,
                "state": "READY_TO_BUILD",
                "action_type": "build",
                "summary": "[HIGH] Fixture work",
                "work_item_id": "fixture_work",
                "selection_explanation": {"rank_tuple": [1, "ppe", "fixture_work"]},
            }
            if state == "READY_TO_BUILD"
            else {"pipeline_id": pipeline_id, "state": state, "action_type": "wait"}
        ),
    }


def _write_ppe(
    root: Path,
    *,
    snapshot: dict[str, Any] | None = None,
    registry: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
) -> Path:
    repo = _init_repo(root)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "founder_portfolio.py").write_text(
        "import json, pathlib\n"
        "root = pathlib.Path(__file__).resolve().parents[1]\n"
        "print(json.dumps(json.loads((root / 'snapshot.json').read_text())))\n",
        encoding="utf-8",
    )
    (repo / "config").mkdir()
    (repo / "config" / "founder_pipeline_registry.json").write_text(
        json.dumps(registry or _registry(), indent=2) + "\n",
        encoding="utf-8",
    )
    sop = repo / "docs" / "SOP"
    (sop / "PHASE_PLANS").mkdir(parents=True)
    for rel in [
        "CHATGPT_GITHUB_CODEX_CONTROL_PLANE_V1.md",
        "FOUNDER_PIPELINE_COMMANDS_V1.md",
        "PIPELINE_CREATION_SOP_V1.md",
        "SCHEDULED_AUTOBUILDER_LANE_POLICY_V1.md",
        "ACTIVE_PHASE_MANIFEST.json",
        "PHASE_QUEUE.json",
        "SPRINT_FIXTURE.md",
        "POST_FIXTURE_SELECTION.md",
    ]:
        path = sop / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n" if rel.endswith(".json") else f"# {rel}\n", encoding="utf-8")
    (sop / "PHASE_PLANS" / "fixture_relay.json").write_text(
        json.dumps(plan or _plan(), indent=2) + "\n",
        encoding="utf-8",
    )
    (repo / "snapshot.json").write_text(
        json.dumps(snapshot or _snapshot(), indent=2) + "\n",
        encoding="utf-8",
    )
    _commit_all(repo)
    return repo


def _feed_repo(root: Path) -> Path:
    repo = _init_repo(root)
    (repo / "README.md").write_text("feed\n", encoding="utf-8")
    _commit_all(repo, "init")
    _git(repo, "checkout", "-qb", "jobs")
    (repo / "jobs" / "approved").mkdir(parents=True)
    (repo / "jobs" / "approved" / "README.md").write_text("approved\n", encoding="utf-8")
    _commit_all(repo, "jobs")
    bare = root.parent / f"{root.name}.git"
    _git(None, "clone", "-q", "--bare", str(repo), str(bare))
    return bare


def _config(
    tmp_path: Path,
    ppe: Path,
    feed: Path,
    *,
    host_root: Path | None = None,
) -> BuildNextConfig:
    return BuildNextConfig(
        ppe_repo=ppe,
        feed_repo_url=str(feed),
        checkout_root=tmp_path / "checkout",
        host_root=host_root,
    )


def test_build_next_submits_exactly_one_selected_item(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")

    receipt = build_next(_config(tmp_path, ppe, feed))

    assert receipt.status == "QUEUED"
    assert receipt.pipeline_id == "ppe"
    assert receipt.work_item_id == "fixture_work"
    review = tmp_path / "review"
    _git(None, "clone", "-q", "--branch", "jobs", str(feed), str(review))
    jobs = list((review / "jobs" / "approved").glob("build-next-*.yaml"))
    assert len(jobs) == 1
    job = yaml.safe_load(jobs[0].read_text(encoding="utf-8"))
    lane = job["manifest"]["lanes"][0]
    assert job["approved"] is True
    assert job["publication_enabled"] is False
    assert job["founder_build_next"]["work_item_id"] == "fixture_work"
    assert "src/viz/panel.py" in lane["allowed_paths"]
    assert ".github/workflows/**" in lane["forbidden_paths"]


def test_duplicate_invocation_is_idempotent(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")
    config = _config(tmp_path, ppe, feed)

    first = build_next(config)
    second = build_next(config)

    assert first.status == "QUEUED"
    assert second.status == "QUEUED"
    assert first.job_id == second.job_id
    review = tmp_path / "review"
    _git(None, "clone", "-q", "--branch", "jobs", str(feed), str(review))
    assert len(list((review / "jobs" / "approved").glob("build-next-*.yaml"))) == 1


def test_running_and_queued_host_items_are_not_duplicated(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")
    first = build_next(_config(tmp_path, ppe, feed))
    host_root = tmp_path / "host"
    running = host_root / "queue" / "running"
    running.mkdir(parents=True)
    (running / f"{first.job_id}.yaml").write_text("version: 1\n", encoding="utf-8")

    receipt = build_next(_config(tmp_path, ppe, feed, host_root=host_root))

    assert receipt.status == "RUNNING"
    assert "no duplicate" in receipt.message


def test_stale_selection_evidence_fails_closed(tmp_path: Path) -> None:
    stale = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0).isoformat()
    ppe = _write_ppe(tmp_path / "ppe", snapshot=_snapshot(as_of=stale))
    feed = _feed_repo(tmp_path / "feed-work")

    receipt = build_next(_config(tmp_path, ppe, feed))

    assert receipt.status == "BLOCKED"
    assert "stale" in receipt.message


def test_blocked_and_awaiting_founder_fail_closed(tmp_path: Path) -> None:
    for state in ("BLOCKED", "AWAITING_FOUNDER"):
        ppe = _write_ppe(tmp_path / f"ppe-{state}", snapshot=_snapshot(state=state))
        feed = _feed_repo(tmp_path / f"feed-{state}")

        receipt = build_next(_config(tmp_path / state, ppe, feed))

        assert receipt.status == "BLOCKED"
        assert "non-dispatchable" in receipt.message


def test_missing_adapter_path_source_or_authority_fails_closed(tmp_path: Path) -> None:
    cases = [
        {"registry": _registry(adapter_ready=False), "message": "adapter"},
        {"plan": {"name": "bad", "slices": [{"sliceId": "x"}]}, "message": "path ownership"},
        {"registry": _registry(canonical_repo="DanielTabakman/other"), "message": "supports only"},
        {
            "registry": {
                **_registry(),
                "pipelines": [
                    {
                        **_registry()["pipelines"][0],
                        "authority": {"publication_authority": "merge to main"},
                    }
                ],
            },
            "message": "authority",
        },
    ]
    for index, case in enumerate(cases):
        ppe = _write_ppe(
            tmp_path / f"ppe-{index}",
            registry=case.get("registry"),
            plan=case.get("plan"),
        )
        feed = _feed_repo(tmp_path / f"feed-{index}")

        receipt = build_next(_config(tmp_path / f"case-{index}", ppe, feed))

        assert receipt.status == "BLOCKED"
        assert case["message"] in receipt.message


def test_no_ready_work_returns_unfilled(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe", snapshot=_snapshot(state="UNFILLED"))
    feed = _feed_repo(tmp_path / "feed-work")

    receipt = build_next(_config(tmp_path, ppe, feed))

    assert receipt.status == "UNFILLED"


def test_receipts_distinguish_running_queued_blocked_and_unfilled(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")
    queued = build_next(_config(tmp_path / "queued", ppe, feed))
    host_root = tmp_path / "host"
    running_dir = host_root / "queue" / "running"
    running_dir.mkdir(parents=True)
    (running_dir / f"{queued.job_id}.yaml").write_text("version: 1\n", encoding="utf-8")
    running = build_next(_config(tmp_path / "running", ppe, feed, host_root=host_root))
    blocked = build_next(
        _config(
            tmp_path / "blocked",
            _write_ppe(tmp_path / "ppe-blocked", snapshot=_snapshot(state="BLOCKED")),
            _feed_repo(tmp_path / "feed-blocked"),
        )
    )
    unfilled = build_next(
        _config(
            tmp_path / "unfilled",
            _write_ppe(tmp_path / "ppe-unfilled", snapshot=_snapshot(state="UNFILLED")),
            _feed_repo(tmp_path / "feed-unfilled"),
        )
    )

    assert {queued.status, running.status, blocked.status, unfilled.status} == {
        "QUEUED",
        "RUNNING",
        "BLOCKED",
        "UNFILLED",
    }


def test_no_product_main_write_or_merge_authority_is_introduced(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")

    receipt = build_next(_config(tmp_path, ppe, feed))

    assert receipt.publication_enabled is False
    assert receipt.merge_enabled is False
    assert receipt.product_main_write_enabled is False
    review = tmp_path / "review"
    _git(None, "clone", "-q", "--branch", "jobs", str(feed), str(review))
    job_path = next((review / "jobs" / "approved").glob("build-next-*.yaml"))
    job = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    assert job["publication_enabled"] is False
    assert job["founder_build_next"]["authority"] == {
        "publication_enabled": False,
        "merge_enabled": False,
        "product_main_write_enabled": False,
    }
