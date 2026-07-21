from __future__ import annotations

import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from msos_autobuilder.build_next import (
    BuildNextConfig,
    RefillAttemptContext,
    _job_id,
    _normalize_github_repository,
    _select_native_slice,
    build_next,
)
from msos_autobuilder.validation_contract import stable_contract_sha256

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
    _git(path, "checkout", "-qb", "main")
    return path


def _commit_all(repo: Path, message: str = "fixture") -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _registry(
    *,
    adapter_ready: bool = True,
    dispatch_enabled: bool | None = True,
    canonical_repo: str = SOURCE_REPO,
) -> dict[str, Any]:
    adapter: dict[str, Any] = {
        "adapter": "ppe_operator",
        "readiness": (
            "READY_FOR_MANUAL_OR_SINGLE_DISPATCH"
            if adapter_ready
            else "NOT_READY"
        ),
    }
    if dispatch_enabled is not None:
        adapter["dispatch_commands_enabled"] = dispatch_enabled
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
                "build_adapter": adapter,
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
                "sliceId": "Fixture-Control-Slice001",
                "layerPreset": "CONTROL",
                "declaredPlane": "EVIDENCE-PLANE",
                "buildBranch": "build/auto/control",
            },
            {
                "sliceId": "Fixture-Product-Slice002",
                "layerPreset": "PPE_UI",
                "workerMode": "local-agent",
                "declaredPlane": "PRODUCT-PLANE",
                "buildBranch": "build/auto/product-slice",
                "touchSet": paths,
            },
            {
                "sliceId": "Fixture-Smoke-Slice003",
                "layerPreset": "CONTROL",
                "declaredPlane": "EVIDENCE-PLANE",
                "buildBranch": "build/auto/smoke",
            },
            {
                "sliceId": "Fixture-Closeout-Slice002",
                "layerPreset": "CONTROL",
                "declaredPlane": "EVIDENCE-PLANE",
                "buildBranch": "build/auto/closeout",
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
    prerequisite_status: str = "complete",
    include_prerequisites: bool = True,
    work_item_id: str = "fixture_work",
) -> dict[str, Any]:
    work = {
        "work_item_id": work_item_id,
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
    if include_prerequisites:
        work["native_prerequisites"] = {
            "version": 1,
            "read_only": True,
            "source": "ppe_native_read_only",
            "evidence": "native_runtime",
            "statuses": [
                {
                    "slice_id": "Fixture-Control-Slice001",
                    "status": prerequisite_status,
                    "non_blocking": False,
                }
            ],
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
                "work_item_id": work_item_id,
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
        "import json, pathlib, sys\n"
        "root = pathlib.Path(__file__).resolve().parents[1]\n"
        "excluded = []\n"
        "args = sys.argv[1:]\n"
        "for index, arg in enumerate(args):\n"
        "    if arg == '--exclude-work-item-id' and index + 1 < len(args):\n"
        "        excluded.append(args[index + 1])\n"
        "payload = json.loads((root / 'snapshot.json').read_text())\n"
        "ready = payload['pipelines'][0].get('ready_work') or []\n"
        "ready_ids = {item.get('work_item_id') for item in ready}\n"
        "matched = [item for item in excluded if item in ready_ids]\n"
        "unmatched = [item for item in excluded if item not in ready_ids]\n"
        "payload['selection_context'] = {\n"
        "    'scope': 'request',\n"
        "    'excluded_work_item_ids': excluded,\n"
        "    'matched_exclusions': matched,\n"
        "    'unmatched_exclusions': unmatched,\n"
        "}\n"
        "rec = payload.get('recommended_next_action') or {}\n"
        "remaining = [item for item in ready if item.get('work_item_id') not in excluded]\n"
        "if rec.get('work_item_id') in excluded and remaining:\n"
        "    next_item = remaining[0]\n"
        "    rec['work_item_id'] = next_item['work_item_id']\n"
        "    payload['recommended_next_action'] = rec\n"
        "elif rec.get('work_item_id') in excluded:\n"
        "    payload['recommended_next_action'] = {\n"
        "        'pipeline_id': rec.get('pipeline_id'),\n"
        "        'state': 'UNFILLED',\n"
        "        'action_type': 'wait',\n"
        "    }\n"
        "print(json.dumps(payload))\n",
        encoding="utf-8",
    )
    (repo / "config").mkdir()
    (repo / "config" / "founder_pipeline_registry.json").write_text(
        json.dumps(registry or _registry(), indent=2) + "\n",
        encoding="utf-8",
    )
    (repo / "requirements.txt").write_bytes(b"# PPE fixture requirements\n")
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
    origin = root.parent / f"{root.name}-origin.git"
    _git(None, "clone", "-q", "--bare", str(repo), str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
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
        allow_test_local_source_remote=True,
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
    assert job["founder_build_next"]["native_slice"]["slice_id"] == "Fixture-Product-Slice002"
    assert job["founder_build_next"]["native_slice"]["previous_slices"] == [
        "Fixture-Control-Slice001"
    ]
    assert job["founder_build_next"]["native_slice"]["following_slices"] == [
        "Fixture-Smoke-Slice003",
        "Fixture-Closeout-Slice002",
    ]
    validation = job["candidate_validation"]
    assert validation["version"] == 1
    assert validation["pipeline_id"] == "ppe"
    assert validation["adapter"] == "ppe_operator"
    assert validation["target_repository"] == SOURCE_REPO
    assert validation["source_commit"] == receipt.source_commit
    assert validation["job_id"] == receipt.job_id
    assert validation["work_item_id"] == "fixture_work"
    assert validation["native_slice_id"] == "Fixture-Product-Slice002"
    assert validation["allowed_changed_paths"] == ["src/viz/panel.py", "tests/test_panel.py"]
    assert validation["dependency_policy"]["profile_id"] == "ppe-ci-pytest-v1"
    assert validation["dependency_policy"]["dependency_source_path"] == "requirements.txt"
    assert validation["dependency_policy"]["dependency_source_sha256"] == hashlib.sha256(
        b"# PPE fixture requirements\n"
    ).hexdigest()
    assert [step["name"] for step in validation["bootstrap"]] == [
        "upgrade-pip",
        "install-requirements",
        "install-test-tooling",
        "install-candidate-package",
    ]
    assert validation["contract_sha256"] == stable_contract_sha256(validation)
    assert validation["publication_enabled"] is False
    assert validation["merge_enabled"] is False
    assert validation["product_main_write_enabled"] is False
    assert lane["task_id"] == "Fixture-Product-Slice002"
    assert lane["branch"] == "build/auto/product-slice"
    assert lane["layer"] == "PPE_UI"
    assert lane["worker_mode"] == "local-agent"
    assert "src/viz/panel.py" in lane["allowed_paths"]
    assert ".github/workflows/**" in lane["forbidden_paths"]
    assert "docs/SOP/SPRINT_FIXTURE.md" not in lane["allowed_paths"]
    assert "docs/SOP/POST_FIXTURE_SELECTION.md" not in lane["allowed_paths"]


def test_control_smoke_and_closeout_slices_are_not_folded_into_product_lane(
    tmp_path: Path,
) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")

    receipt = build_next(_config(tmp_path, ppe, feed))

    review = tmp_path / "review"
    _git(None, "clone", "-q", "--branch", "jobs", str(feed), str(review))
    job_path = next((review / "jobs" / "approved").glob(f"{receipt.job_id}.yaml"))
    job = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    lanes = job["manifest"]["lanes"]
    assert len(lanes) == 1
    instruction = lanes[0]["instruction"]
    assert (
        "Do not perform smoke, closeout, selection, queue, or control-plane updates."
        in instruction
    )
    assert "Fixture-Smoke-Slice003" in instruction
    assert "Fixture-Closeout-Slice002" in instruction


def test_duplicate_invocation_is_idempotent(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")
    config = _config(tmp_path, ppe, feed)

    first = build_next(config)
    second = build_next(config)

    assert first.status == "QUEUED"
    assert second.status == "QUEUED"
    assert first.submitted is True
    assert second.submitted is False
    assert first.job_id == second.job_id
    assert first.feed_commit == second.feed_commit
    assert first.feed_path == second.feed_path
    assert "Submitted one immutable" in first.message
    assert "already exists" in second.message
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
        {
            "plan": {"name": "bad", "slices": [{"sliceId": "x"}]},
            "message": "no bounded native implementation slice",
        },
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


def test_dispatch_commands_enabled_must_be_explicitly_true(tmp_path: Path) -> None:
    cases = [
        (_registry(dispatch_enabled=False), "dispatch_commands_enabled"),
        (_registry(dispatch_enabled=None), "dispatch_commands_enabled"),
        (_registry(dispatch_enabled=True), None),
    ]
    for index, (registry, message) in enumerate(cases):
        ppe = _write_ppe(tmp_path / f"ppe-dispatch-{index}", registry=registry)
        feed = _feed_repo(tmp_path / f"feed-dispatch-{index}")

        receipt = build_next(_config(tmp_path / f"dispatch-{index}", ppe, feed))

        if message is None:
            assert receipt.status == "QUEUED"
        else:
            assert receipt.status == "BLOCKED"
            assert message in receipt.message
            assert "#5366" in receipt.message


def test_native_prerequisite_evidence_blocks_incomplete_product_slice(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(prerequisite_status="in_progress")
    ppe = _write_ppe(tmp_path / "ppe", snapshot=snapshot)
    feed = _feed_repo(tmp_path / "feed-work")

    receipt = build_next(_config(tmp_path, ppe, feed))

    assert receipt.status == "BLOCKED"
    assert "Control-Slice001" in receipt.message


def test_missing_native_prerequisite_evidence_fails_closed(tmp_path: Path) -> None:
    snapshot = _snapshot(include_prerequisites=False)
    ppe = _write_ppe(tmp_path / "ppe", snapshot=snapshot)
    feed = _feed_repo(tmp_path / "feed-work")

    receipt = build_next(_config(tmp_path, ppe, feed))

    assert receipt.status == "BLOCKED"
    assert "prerequisite evidence" in receipt.message


def test_native_prerequisite_completion_permits_product_slice(tmp_path: Path) -> None:
    snapshot = _snapshot(prerequisite_status="completed")
    ppe = _write_ppe(tmp_path / "ppe", snapshot=snapshot)
    feed = _feed_repo(tmp_path / "feed-work")

    receipt = build_next(_config(tmp_path, ppe, feed))

    assert receipt.status == "QUEUED"
    assert receipt.evidence["prerequisites"]["satisfied_slices"] == [
        "Fixture-Control-Slice001"
    ]


def test_forbidden_control_plane_paths_and_broad_grants_fail_closed(tmp_path: Path) -> None:
    forbidden_cases = [
        ["docs/SOP/SPRINT_FIXTURE.md"],
        ["docs/SOP/POST_FIXTURE_SELECTION.md"],
        ["config/founder_pipeline_registry.json"],
        ["docs/SOP/PHASE_QUEUE.json"],
        ["docs/SOP/ACTIVE_PHASE_MANIFEST.json"],
        ["docs/SOP/FOUNDER_PIPELINE_COMMANDS_V1.md"],
        ["docs/SOP/PIPELINE_CREATION_SOP_V1.md"],
        ["docs/SOP/SCHEDULED_AUTOBUILDER_LANE_POLICY_V1.md"],
        ["docs/SOP/CHATGPT_GITHUB_CODEX_CONTROL_PLANE_V1.md"],
        ["docs"],
        ["docs/SOP"],
        ["."],
    ]
    for index, touch_set in enumerate(forbidden_cases):
        ppe = _write_ppe(tmp_path / f"ppe-{index}", plan=_plan(touch_set=touch_set))
        feed = _feed_repo(tmp_path / f"feed-{index}")

        receipt = build_next(_config(tmp_path / f"case-{index}", ppe, feed))

        assert receipt.status == "BLOCKED"
        assert "forbidden authority" in receipt.message or "broad writable path" in receipt.message


def test_wildcard_touch_sets_are_rejected_in_v1(tmp_path: Path) -> None:
    wildcard_cases = ["docs/**", "docs/SOP/**", "config/**", "artifacts/**", "src/**"]
    for index, touch_set in enumerate(wildcard_cases):
        ppe = _write_ppe(tmp_path / f"ppe-wildcard-{index}", plan=_plan(touch_set=[touch_set]))
        feed = _feed_repo(tmp_path / f"feed-wildcard-{index}")

        receipt = build_next(_config(tmp_path / f"wildcard-{index}", ppe, feed))

        assert receipt.status == "BLOCKED"
        assert "wildcard writable path" in receipt.message


def test_no_ready_work_returns_unfilled(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe", snapshot=_snapshot(state="UNFILLED"))
    feed = _feed_repo(tmp_path / "feed-work")

    receipt = build_next(_config(tmp_path, ppe, feed))

    assert receipt.status == "UNFILLED"


def test_stale_clean_branch_and_clean_feature_branch_fail_closed(tmp_path: Path) -> None:
    ppe_stale = _write_ppe(tmp_path / "ppe-stale")
    (ppe_stale / "new-main.txt").write_text("new main\n", encoding="utf-8")
    _commit_all(ppe_stale, "advance main")
    _git(ppe_stale, "push", "-q", "origin", "main")
    _git(ppe_stale, "reset", "--hard", "HEAD~1")
    assert not _git(ppe_stale, "status", "--porcelain")
    stale_receipt = build_next(
        _config(tmp_path / "stale", ppe_stale, _feed_repo(tmp_path / "feed-stale"))
    )

    ppe_feature = _write_ppe(tmp_path / "ppe-feature")
    _git(ppe_feature, "checkout", "-qb", "feature/off-main")
    (ppe_feature / "feature.txt").write_text("feature\n", encoding="utf-8")
    _commit_all(ppe_feature, "feature")
    assert not _git(ppe_feature, "status", "--porcelain")
    feature_receipt = build_next(
        _config(tmp_path / "feature", ppe_feature, _feed_repo(tmp_path / "feed-feature"))
    )

    assert stale_receipt.status == "BLOCKED"
    assert "does not match freshly fetched origin/main" in stale_receipt.message
    assert feature_receipt.status == "BLOCKED"
    assert "does not match freshly fetched origin/main" in feature_receipt.message


def test_exact_origin_main_succeeds_and_records_source_evidence(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")
    origin_main = _git(ppe, "rev-parse", "origin/main")

    receipt = build_next(_config(tmp_path, ppe, feed))

    assert receipt.status == "QUEUED"
    assert receipt.source_commit == origin_main
    assert receipt.evidence["source"]["remote"] == "origin"
    assert receipt.evidence["source"]["remote_ref"] == "origin/main"
    assert receipt.evidence["source"]["repository"] == SOURCE_REPO
    review = tmp_path / "review"
    _git(None, "clone", "-q", "--branch", "jobs", str(feed), str(review))
    job = yaml.safe_load(next((review / "jobs" / "approved").glob("build-next-*.yaml")).read_text())
    assert job["founder_build_next"]["source"]["commit"] == origin_main
    assert job["founder_build_next"]["source"]["repository"] == SOURCE_REPO


def test_source_remote_identity_accepts_canonical_https_and_ssh() -> None:
    assert (
        _normalize_github_repository(
            "https://github.com/DanielTabakman/Probability-prediction-engine.git"
        )
        == SOURCE_REPO
    )
    assert (
        _normalize_github_repository(
            "git@github.com:DanielTabakman/Probability-prediction-engine.git"
        )
        == SOURCE_REPO
    )
    assert (
        _normalize_github_repository(
            "ssh://git@github.com/DanielTabakman/Probability-prediction-engine.git"
        )
        == SOURCE_REPO
    )


def test_source_remote_identity_rejects_forks_malformed_and_local_in_production(
    tmp_path: Path,
) -> None:
    cases = [
        "https://github.com/SomeoneElse/Probability-prediction-engine.git",
        "https://example.com/not-github/repo.git",
        str(tmp_path / "ppe-origin.git"),
    ]
    for index, remote_url in enumerate(cases):
        ppe = _write_ppe(tmp_path / f"ppe-bad-remote-{index}")
        feed = _feed_repo(tmp_path / f"feed-bad-remote-{index}")
        if index < 2:
            _git(ppe, "remote", "set-url", "origin", remote_url)

        receipt = build_next(
            BuildNextConfig(
                ppe_repo=ppe,
                feed_repo_url=str(feed),
                checkout_root=tmp_path / f"checkout-bad-remote-{index}",
            )
        )

        assert receipt.status == "BLOCKED"
        assert "source remote" in receipt.message


def test_dry_run_is_not_reported_as_queued(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")

    receipt = build_next(_config(tmp_path, ppe, feed).__class__(
        ppe_repo=ppe,
        feed_repo_url=str(feed),
        checkout_root=tmp_path / "checkout",
        submit=False,
        allow_test_local_source_remote=True,
    ))

    assert receipt.status == "UNFILLED"
    assert receipt.submitted is False
    assert receipt.projected_status == "QUEUED"
    review = tmp_path / "review"
    _git(None, "clone", "-q", "--branch", "jobs", str(feed), str(review))
    assert not list((review / "jobs" / "approved").glob("build-next-*.yaml"))


def test_concurrent_identical_invocations_create_one_immutable_job(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")
    config = _config(tmp_path, ppe, feed)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: build_next(config), range(2)))

    assert {receipt.status for receipt in receipts} == {"QUEUED"}
    assert {receipt.job_id for receipt in receipts} == {receipts[0].job_id}
    assert sum(1 for receipt in receipts if receipt.submitted) == 1
    assert sum(1 for receipt in receipts if not receipt.submitted) == 1
    review = tmp_path / "review"
    _git(None, "clone", "-q", "--branch", "jobs", str(feed), str(review))
    assert len(list((review / "jobs" / "approved").glob("build-next-*.yaml"))) == 1


def test_production_config_is_derived_from_installed_service_config(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")
    host_root = tmp_path / "host"
    codex_config = tmp_path / "host.yaml"
    codex_config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "publication_enabled": False,
                "source_repo": str(ppe),
                "workspace_root": str(tmp_path / "workspaces"),
                "runtime_root": str(tmp_path / "runtime"),
                "owner_id": "test-host",
                "codex": {"sandbox_mode": "workspace-write", "max_concurrency": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    service_config = tmp_path / "service.yaml"
    service_config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "publication_enabled": False,
                "host_root": str(host_root),
                "codex_host_config": str(codex_config),
                "job_feed": {
                    "enabled": True,
                    "repo_url": str(feed),
                    "branch": "jobs",
                    "path": "jobs/approved",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = BuildNextConfig.from_service_config(
        service_config,
        checkout_root=tmp_path / "checkout",
        allow_test_local_source_remote=True,
    )
    receipt = build_next(config)

    assert config.ppe_repo == ppe.resolve()
    assert config.host_root == host_root.resolve()
    assert config.feed_repo_url == str(feed)
    assert receipt.status == "QUEUED"


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


def test_manual_build_next_without_attempt_context_keeps_deterministic_id(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")

    manual = build_next(_config(tmp_path / "manual", ppe, feed))
    refill = build_next(
        BuildNextConfig(
            ppe_repo=ppe,
            feed_repo_url=str(feed),
            checkout_root=tmp_path / "refill-checkout",
            host_root=tmp_path / "host",
            allow_test_local_source_remote=True,
            refill_attempt=RefillAttemptContext(
                generation_id="refill-generation-1",
                attempt_ordinal=1,
                selected_work_item_id="fixture_work",
            ),
        )
    )

    expected = (
        "build-next-ppe-fixture_work-Fixture-Product-Slice002-"
        f"{manual.source_commit[:12]}"
    )
    assert manual.job_id == expected
    assert refill.job_id != manual.job_id


def test_manual_job_id_keeps_legacy_96_character_truncation() -> None:
    native_slice = _select_native_slice(
        _plan(touch_set=["src/options.py"])
        | {
            "slices": [
                {
                    "sliceId": "Options-HorizonComparison-Product-Slice002",
                    "layerPreset": "PPE_UI",
                    "declaredPlane": "PRODUCT-PLANE",
                    "buildBranch": "build/options",
                    "touchSet": ["src/options.py"],
                }
            ]
        }
    )
    source_commit = "a25f26d06b067e39047f1d825203a96810ae4a8c"

    job_id = _job_id(
        "ppe",
        "options_horizon_comparison_v1",
        native_slice,
        source_commit,
    )

    old = (
        "build-next-ppe-options_horizon_comparison_v1-"
        "Options-HorizonComparison-Product-Slice002-a25f26d06b067"
    )[:96]
    assert job_id == old
    assert len(job_id) == 96


def test_refill_job_id_keeps_digest_suffix_distinct_after_length_limit() -> None:
    native_slice = _select_native_slice(
        {
            "slices": [
                {
                    "sliceId": "Options-HorizonComparison-Product-Slice002",
                    "layerPreset": "PPE_UI",
                    "declaredPlane": "PRODUCT-PLANE",
                    "buildBranch": "build/options",
                    "touchSet": ["src/options.py"],
                }
            ]
        }
    )
    source_commit = "a25f26d06b067e39047f1d825203a96810ae4a8c"

    first = _job_id(
        "ppe",
        "options_horizon_comparison_v1",
        native_slice,
        source_commit,
        refill_attempt=RefillAttemptContext(
            generation_id="refill-generation-1",
            attempt_ordinal=1,
            selected_work_item_id="options_horizon_comparison_v1",
        ).evidence("options_horizon_comparison_v1"),
    )
    second_generation = _job_id(
        "ppe",
        "options_horizon_comparison_v1",
        native_slice,
        source_commit,
        refill_attempt=RefillAttemptContext(
            generation_id="refill-generation-2",
            attempt_ordinal=1,
            selected_work_item_id="options_horizon_comparison_v1",
        ).evidence("options_horizon_comparison_v1"),
    )
    retry = _job_id(
        "ppe",
        "options_horizon_comparison_v1",
        native_slice,
        source_commit,
        refill_attempt=RefillAttemptContext(
            generation_id="refill-generation-1",
            attempt_ordinal=2,
            retry_ordinal=1,
            selected_work_item_id="options_horizon_comparison_v1",
        ).evidence("options_horizon_comparison_v1"),
    )

    assert first != second_generation
    assert retry != first
    assert len(first.rsplit("-", 1)[-1]) == 16
    assert first.endswith(first.rsplit("-", 1)[-1])
    assert len(first) <= 120


def test_exclusions_are_passed_to_ppe_and_echoed_in_selection_context(tmp_path: Path) -> None:
    snapshot = _snapshot()
    second = dict(snapshot["pipelines"][0]["ready_work"][0])
    second["work_item_id"] = "fixture_work_b"
    snapshot["pipelines"][0]["ready_work"].append(second)
    ppe = _write_ppe(tmp_path / "ppe", snapshot=snapshot)
    feed = _feed_repo(tmp_path / "feed-work")

    receipt = build_next(
        BuildNextConfig(
            ppe_repo=ppe,
            feed_repo_url=str(feed),
            checkout_root=tmp_path / "checkout",
            allow_test_local_source_remote=True,
            exclude_work_item_ids=("fixture_work",),
        )
    )

    assert receipt.status == "QUEUED"
    assert receipt.work_item_id == "fixture_work_b"
    assert receipt.evidence["requested_exclusions"] == ["fixture_work"]


def test_malformed_or_mismatched_selection_context_fails_closed(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    script = ppe / "scripts" / "founder_portfolio.py"
    script.write_text(
        "import json, pathlib\n"
        "root = pathlib.Path(__file__).resolve().parents[1]\n"
        "payload = json.loads((root / 'snapshot.json').read_text())\n"
        "payload['selection_context'] = {'scope': 'global', 'excluded_work_item_ids': []}\n"
        "print(json.dumps(payload))\n",
        encoding="utf-8",
    )
    _commit_all(ppe, "bad selector context")
    _git(ppe, "push", "-q", "origin", "main")
    feed = _feed_repo(tmp_path / "feed-work")

    receipt = build_next(
        BuildNextConfig(
            ppe_repo=ppe,
            feed_repo_url=str(feed),
            checkout_root=tmp_path / "checkout",
            allow_test_local_source_remote=True,
            exclude_work_item_ids=("fixture_work",),
        )
    )

    assert receipt.status == "BLOCKED"
    assert "selection_context" in receipt.message
