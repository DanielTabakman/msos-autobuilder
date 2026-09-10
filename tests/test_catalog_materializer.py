"""Tests for Stage 5 JIT approved-backlog → catalog materializer."""

from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_build_next import (
    SOURCE_REPO,
    _catalog_root,
    _commit_all,
    _feed_repo,
    _git,
    _write_ppe,
)

from msos_autobuilder.catalog_materializer import (
    BACKLOG_RELPATH,
    GUIDED_SHELL_ALLOWED_PATHS,
    GUIDED_SHELL_CATALOG_FILENAME,
    GUIDED_SHELL_ORDER,
    GUIDED_SHELL_WORK_ITEM_ID,
    MARKET_COMPARE_ORDER,
    MARKET_COMPARE_RELATED_PR,
    MARKET_COMPARE_WORK_ITEM_ID,
    PREDECESSOR_WORK_ITEM_ID,
    RISK_EXPRESSION_ORDER,
    CatalogMaterializerError,
    author_guided_shell_packet,
    ensure_jit_catalog_for_refill,
    evaluate_jit_eligibility,
    is_predecessor_terminal_proof,
    load_phase_chapter_backlog,
    materialize_guided_shell_packet,
    order_is_draft_gated_blocked,
    resolve_predecessor_terminal_proof,
    write_packet_to_catalog_dir,
)
from msos_autobuilder.job_packet import (
    load_packet_dir,
    parse_approved_job_packet,
    select_next_packet,
)
from msos_autobuilder.refill_controller import _maybe_materialize_jit_catalog
from msos_autobuilder.validation_contract import canonical_dependency_source_sha256


def _backlog_payload() -> dict[str, object]:
    return {
        "version": 1,
        "notes": "test backlog",
        "items": [
            {
                "chapterId": PREDECESSOR_WORK_ITEM_ID,
                "status": "blocked",
                "autobuilderCatalogOrder": 6,
                "eligibility": "buildable_via_autobuilder_catalog",
                "dependsOn": ["ppe_equity_universe_tier1c_v1"],
                "packetization": "materialize_now",
                "reason": "contract",
            },
            {
                "chapterId": GUIDED_SHELL_WORK_ITEM_ID,
                "status": "blocked",
                "autobuilderCatalogOrder": 7,
                "eligibility": "blocked_until_predecessor_terminal",
                "dependsOn": [PREDECESSOR_WORK_ITEM_ID],
                "packetization": "just_in_time",
                "reason": "guided shell JIT",
            },
            {
                "chapterId": MARKET_COMPARE_WORK_ITEM_ID,
                "status": "blocked",
                "autobuilderCatalogOrder": 8,
                "eligibility": "blocked_until_predecessor_and_draft_decision",
                "dependsOn": [GUIDED_SHELL_WORK_ITEM_ID],
                "relatedPullRequests": [MARKET_COMPARE_RELATED_PR],
                "packetization": "just_in_time",
                "reason": "market compare blocked on #5427",
            },
            {
                "chapterId": "region_bet_risk_expression_bridge_v1",
                "status": "blocked",
                "autobuilderCatalogOrder": 9,
                "eligibility": "blocked_until_predecessor_and_draft_decision",
                "dependsOn": [MARKET_COMPARE_WORK_ITEM_ID],
                "relatedPullRequests": [
                    "DanielTabakman/Probability-prediction-engine#5428"
                ],
                "packetization": "just_in_time",
                "reason": "risk expression blocked on #5428",
            },
        ],
    }


def _stub_guided_shell_primitives(ppe: Path) -> None:
    for rel in GUIDED_SHELL_ALLOWED_PATHS:
        if rel.startswith("tests/"):
            continue
        path = ppe / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(f"// fixture {rel}\n", encoding="utf-8")


def _write_backlog(ppe: Path) -> None:
    _stub_guided_shell_primitives(ppe)
    path = ppe / BACKLOG_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_backlog_payload(), indent=2) + "\n", encoding="utf-8")
    _commit_all(ppe, "add chapter backlog and guided-shell primitives")
    _git(ppe, "push", "-q", "origin", "main")


def _seed_contract_packet(catalog: Path, ppe: Path) -> Path:
    commit = _git(ppe, "rev-parse", "HEAD")
    remote = _git(ppe, "remote", "get-url", "origin")
    from msos_autobuilder.validation_contract import canonical_dependency_source_sha256

    requirements = (ppe / "requirements.txt").read_bytes()
    paths = [
        "apps/msos-web/src/lib/regionBet.ts",
        "apps/msos-web/src/lib/msosWorkflowStore.ts",
        "apps/msos-web/src/app/api/theses/region-bet/route.ts",
        "tests/test_msos_web_region_bet_contract.py",
    ]
    packet = {
        "version": 1,
        "pipeline_id": "ppe",
        "work_item_id": PREDECESSOR_WORK_ITEM_ID,
        "order": 6,
        "eligible": True,
        "target_repository": SOURCE_REPO,
        "target_source_commit": commit,
        "target_remote_url": remote,
        "adapter": "ppe_operator",
        "allowed_paths": paths,
        "native_slice": {
            "slice_id": "RegionBet-Contract-Product-Slice002",
            "build_branch": "build/auto/RegionBet-Contract-Product-Slice002",
            "layer_preset": "MSOS_UI",
            "worker_mode": "local-agent",
            "declared_plane": "PRODUCT-PLANE",
            "touch_set": paths,
            "sequence_index": 1,
            "total_slices": 4,
            "previous_slices": ["RegionBet-Contract-Control-Slice001"],
            "following_slices": [
                "RegionBet-Contract-Witness-Slice003",
                "RegionBet-Contract-Closeout-Slice004",
            ],
            "raw_slice": {
                "sliceId": "RegionBet-Contract-Product-Slice002",
                "layerPreset": "MSOS_UI",
                "buildBranch": "build/auto/RegionBet-Contract-Product-Slice002",
                "declaredPlane": "PRODUCT-PLANE",
                "touchSet": paths,
                "workerMode": "local-agent",
            },
        },
        "prerequisites": {"version": 1, "read_only": True, "source": "ppe_native_read_only"},
        "dependency_source_sha256": canonical_dependency_source_sha256(requirements),
        "authority": {
            "publication_enabled": False,
            "merge_enabled": False,
            "product_main_write_enabled": False,
        },
        "validation": {"profile_id": "ppe-ci-pytest-v1"},
    }
    catalog.mkdir(parents=True, exist_ok=True)
    for path in catalog.glob("*.json"):
        path.unlink()
    dest = catalog / "06-region_bet_contract_v1.json"
    dest.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    return dest


def _merged_proof() -> dict[str, object]:
    return {
        "work_item_id": PREDECESSOR_WORK_ITEM_ID,
        "status": "merged",
        "pr_number": 5436,
        "merge_commit": "a" * 40,
    }


def test_predecessor_not_terminal_does_not_materialize(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    _write_backlog(ppe)
    catalog = _catalog_root(ppe)
    _seed_contract_packet(catalog, ppe)

    result = materialize_guided_shell_packet(
        backlog=ppe / BACKLOG_RELPATH,
        ppe_repo=ppe,
        predecessor_terminal=False,
        catalog_dir=catalog,
        allow_test_local_source_remote=True,
        fetch_remote=False,
    )

    assert result.status == "skipped"
    assert result.reason == "predecessor_not_terminal"
    assert not (catalog / GUIDED_SHELL_CATALOG_FILENAME).exists()
    packets = load_packet_dir(catalog, allow_test_local_source_remote=True)
    assert (
        select_next_packet(packets, exclude_work_item_ids=(PREDECESSOR_WORK_ITEM_ID,))
        is None
    )


def test_predecessor_terminal_creates_parseable_07_and_select_returns_it(
    tmp_path: Path,
) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    _write_backlog(ppe)
    catalog = _catalog_root(ppe)
    _seed_contract_packet(catalog, ppe)

    result = materialize_guided_shell_packet(
        backlog=ppe / BACKLOG_RELPATH,
        ppe_repo=ppe,
        predecessor_terminal=_merged_proof(),
        catalog_dir=catalog,
        allow_test_local_source_remote=True,
        fetch_remote=False,
    )

    assert result.status == "created"
    assert result.work_item_id == GUIDED_SHELL_WORK_ITEM_ID
    assert result.order == GUIDED_SHELL_ORDER
    path = catalog / GUIDED_SHELL_CATALOG_FILENAME
    assert path.is_file()
    raw = json.loads(path.read_text(encoding="utf-8"))
    packet = parse_approved_job_packet(raw, allow_test_local_source_remote=True)
    assert packet.work_item_id == GUIDED_SHELL_WORK_ITEM_ID
    assert packet.order == 7
    assert packet.eligible is True
    assert packet.merge_authority is None
    assert "merge_authority" not in raw
    assert packet.authority == {
        "publication_enabled": False,
        "merge_enabled": False,
        "product_main_write_enabled": False,
    }
    assert packet.allowed_paths == GUIDED_SHELL_ALLOWED_PATHS
    for marker in ("comparison", "expression_fit", "5427", "5428", "market_compare"):
        assert all(marker not in item.lower() for item in packet.allowed_paths)

    packets = load_packet_dir(catalog, allow_test_local_source_remote=True)
    nxt = select_next_packet(
        packets, exclude_work_item_ids=(PREDECESSOR_WORK_ITEM_ID,)
    )
    assert nxt is not None
    assert nxt.work_item_id == GUIDED_SHELL_WORK_ITEM_ID
    assert nxt.order == 7


def test_duplicate_identical_07_is_idempotent(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    _write_backlog(ppe)
    catalog = _catalog_root(ppe)
    _seed_contract_packet(catalog, ppe)

    first = materialize_guided_shell_packet(
        backlog=ppe / BACKLOG_RELPATH,
        ppe_repo=ppe,
        predecessor_terminal=_merged_proof(),
        catalog_dir=catalog,
        allow_test_local_source_remote=True,
        fetch_remote=False,
    )
    second = materialize_guided_shell_packet(
        backlog=ppe / BACKLOG_RELPATH,
        ppe_repo=ppe,
        predecessor_terminal=_merged_proof(),
        catalog_dir=catalog,
        allow_test_local_source_remote=True,
        fetch_remote=False,
        frozen_commit=first.frozen_commit,
        target_remote_url=_git(ppe, "remote", "get-url", "origin"),
    )

    assert first.status == "created"
    assert second.status == "unchanged"
    assert second.packet_sha256 == first.packet_sha256
    assert len(list(catalog.glob("07-*.json"))) == 1


def test_conflicting_07_fails_closed(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    _write_backlog(ppe)
    catalog = _catalog_root(ppe)
    _seed_contract_packet(catalog, ppe)
    first = materialize_guided_shell_packet(
        backlog=ppe / BACKLOG_RELPATH,
        ppe_repo=ppe,
        predecessor_terminal=_merged_proof(),
        catalog_dir=catalog,
        allow_test_local_source_remote=True,
        fetch_remote=False,
    )
    assert first.status == "created"
    conflicting = json.loads(
        (catalog / GUIDED_SHELL_CATALOG_FILENAME).read_text(encoding="utf-8")
    )
    conflicting["target_source_commit"] = "b" * 40
    (catalog / GUIDED_SHELL_CATALOG_FILENAME).write_text(
        json.dumps(conflicting, indent=2) + "\n", encoding="utf-8"
    )

    with pytest.raises(CatalogMaterializerError, match="conflicting_catalog_packet"):
        materialize_guided_shell_packet(
            backlog=ppe / BACKLOG_RELPATH,
            ppe_repo=ppe,
            predecessor_terminal=_merged_proof(),
            catalog_dir=catalog,
            allow_test_local_source_remote=True,
            fetch_remote=False,
            frozen_commit=first.frozen_commit,
            target_remote_url=_git(ppe, "remote", "get-url", "origin"),
        )


def test_order_08_blocked_while_5427_unresolved(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    _write_backlog(ppe)
    items = load_phase_chapter_backlog(ppe / BACKLOG_RELPATH)
    eligibility = evaluate_jit_eligibility(items, predecessor_terminal=True)
    assert eligibility.status == "eligible"
    blocked = (eligibility.evidence or {})["blocked_orders"]
    assert blocked[str(MARKET_COMPARE_ORDER)]["resolved"] is False
    assert blocked[str(RISK_EXPRESSION_ORDER)]["resolved"] is False
    assert order_is_draft_gated_blocked(items, MARKET_COMPARE_ORDER) is True
    assert order_is_draft_gated_blocked(items, RISK_EXPRESSION_ORDER) is True

    catalog = _catalog_root(ppe)
    _seed_contract_packet(catalog, ppe)
    materialize_guided_shell_packet(
        backlog=items,
        ppe_repo=ppe,
        predecessor_terminal=_merged_proof(),
        catalog_dir=catalog,
        allow_test_local_source_remote=True,
        fetch_remote=False,
    )
    assert (catalog / GUIDED_SHELL_CATALOG_FILENAME).is_file()
    assert not list(catalog.glob("08-*.json"))
    assert not list(catalog.glob("09-*.json"))
    assert not any(
        p.work_item_id
        in {MARKET_COMPARE_WORK_ITEM_ID, "region_bet_risk_expression_bridge_v1"}
        for p in load_packet_dir(catalog, allow_test_local_source_remote=True)
    )


def test_concurrent_passes_do_not_create_two_different_07_packets(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    _write_backlog(ppe)
    catalog = _catalog_root(ppe)
    _seed_contract_packet(catalog, ppe)
    commit = _git(ppe, "rev-parse", "HEAD")
    remote = _git(ppe, "remote", "get-url", "origin")
    from msos_autobuilder.validation_contract import canonical_dependency_source_sha256

    dependency = canonical_dependency_source_sha256((ppe / "requirements.txt").read_bytes())
    packet = author_guided_shell_packet(
        frozen_commit=commit,
        target_remote_url=remote,
        dependency_source_sha256=dependency,
        allow_test_local_source_remote=True,
    )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def _once() -> None:
        barrier.wait()
        try:
            result = write_packet_to_catalog_dir(
                catalog,
                packet,
                allow_test_local_source_remote=True,
            )
            outcomes.append(result.status)
        except CatalogMaterializerError as exc:
            outcomes.append(f"error:{exc}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_once), pool.submit(_once)]
        for future in futures:
            future.result()

    assert sorted(outcomes) in (
        ["created", "unchanged"],
        ["unchanged", "created"],
    )
    files = list(catalog.glob("07-*.json"))
    assert len(files) == 1
    loaded = parse_approved_job_packet(
        json.loads(files[0].read_text(encoding="utf-8")),
        allow_test_local_source_remote=True,
    )
    assert loaded.packet_sha256 == parse_approved_job_packet(
        packet, allow_test_local_source_remote=True
    ).packet_sha256


def test_queue_emptiness_alone_is_not_predecessor_terminal() -> None:
    assert is_predecessor_terminal_proof({"status": "queued"}) is False
    assert is_predecessor_terminal_proof(_merged_proof()) is True
    assert is_predecessor_terminal_proof(False) is False


def test_missing_jobs_publish_config_fails_closed(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    _write_backlog(ppe)

    with pytest.raises(CatalogMaterializerError, match="feed_repo_url"):
        materialize_guided_shell_packet(
            backlog=ppe / BACKLOG_RELPATH,
            ppe_repo=ppe,
            predecessor_terminal=_merged_proof(),
            publish_to_jobs=True,
            feed_repo_url="",
            allow_test_local_source_remote=True,
            fetch_remote=False,
        )


def test_refill_hook_materializes_when_select_empty_and_predecessor_merged(
    tmp_path: Path,
) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    _write_backlog(ppe)
    catalog = _catalog_root(ppe)
    _seed_contract_packet(catalog, ppe)
    feed = _feed_repo(tmp_path / "feed-work")

    from msos_autobuilder.build_next import BuildNextConfig
    from msos_autobuilder.persistent_host import HostPaths
    from msos_autobuilder.refill_controller import RefillConfig

    host_root = tmp_path / "host"
    host_root.mkdir()
    (host_root / "state").mkdir()
    config = RefillConfig(
        build_next=BuildNextConfig(
            ppe_repo=ppe,
            packet_root=catalog,
            feed_repo_url=str(feed),
            checkout_root=tmp_path / "checkout",
            host_root=host_root,
            allow_test_local_source_remote=True,
            submit=False,
        )
    )
    generation = {
        "last_attempt_classification": {
            "category": "item_terminal",
            "stage": "canonical_lifecycle",
            "evidence": {
                "reason": "item_terminal_success_merged",
                "attempt_identity": {"work_item_id": PREDECESSOR_WORK_ITEM_ID},
            },
        },
        "item_scoped_terminal_exclusions": [PREDECESSOR_WORK_ITEM_ID],
    }
    evidence = _maybe_materialize_jit_catalog(
        config,
        HostPaths.from_root(host_root),
        exclusions=(PREDECESSOR_WORK_ITEM_ID,),
        generation=generation,
    )
    assert evidence is not None
    assert evidence["blocked"] is False
    assert evidence["status"] == "created"
    assert evidence["work_item_id"] == GUIDED_SHELL_WORK_ITEM_ID
    packets = load_packet_dir(catalog, allow_test_local_source_remote=True)
    nxt = select_next_packet(
        packets, exclude_work_item_ids=(PREDECESSOR_WORK_ITEM_ID,)
    )
    assert nxt is not None
    assert nxt.work_item_id == GUIDED_SHELL_WORK_ITEM_ID


def test_refill_hook_skips_when_catalog_still_has_next(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    _write_backlog(ppe)
    catalog = _catalog_root(ppe)
    _seed_contract_packet(catalog, ppe)
    feed = _feed_repo(tmp_path / "feed-work")

    from msos_autobuilder.build_next import BuildNextConfig
    from msos_autobuilder.persistent_host import HostPaths
    from msos_autobuilder.refill_controller import RefillConfig

    host_root = tmp_path / "host"
    host_root.mkdir()
    (host_root / "state").mkdir()
    config = RefillConfig(
        build_next=BuildNextConfig(
            ppe_repo=ppe,
            packet_root=catalog,
            feed_repo_url=str(feed),
            checkout_root=tmp_path / "checkout",
            host_root=host_root,
            allow_test_local_source_remote=True,
            submit=False,
        )
    )
    evidence = _maybe_materialize_jit_catalog(
        config,
        HostPaths.from_root(host_root),
        exclusions=(),
        generation={
            "last_attempt_classification": {
                "category": "item_terminal",
                "evidence": {
                    "reason": "item_terminal_success_merged",
                    "attempt_identity": {"work_item_id": PREDECESSOR_WORK_ITEM_ID},
                },
            }
        },
    )
    assert evidence is None
    assert not (catalog / GUIDED_SHELL_CATALOG_FILENAME).exists()


def test_ensure_jit_probe_skips_when_catalog_has_next(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    _write_backlog(ppe)
    catalog = _catalog_root(ppe)
    _seed_contract_packet(catalog, ppe)

    result = ensure_jit_catalog_for_refill(
        ppe_repo=ppe,
        feed_repo_url="unused",
        packet_root=catalog,
        exclude_work_item_ids=(),
        allow_test_local_source_remote=True,
        publish_to_jobs=False,
        fetch_remote=False,
    )
    assert result.status == "skipped"
    assert result.reason == "catalog_next_not_empty"


def test_empty_attempt_identity_does_not_prove_predecessor_terminal() -> None:
    proof = resolve_predecessor_terminal_proof(
        generation={
            "last_attempt_classification": {
                "category": "item_terminal",
                "evidence": {
                    "reason": "item_terminal_success_merged",
                    "attempt_identity": {"work_item_id": ""},
                },
            }
        }
    )
    assert proof is False
    assert is_predecessor_terminal_proof(proof) is False

    missing_identity = resolve_predecessor_terminal_proof(
        generation={
            "last_attempt_classification": {
                "category": "item_terminal",
                "evidence": {"reason": "item_terminal_success_merged"},
            }
        }
    )
    assert missing_identity is False


def test_author_guided_shell_omits_merge_authority(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    remote = _git(ppe, "remote", "get-url", "origin")
    commit = _git(ppe, "rev-parse", "HEAD")
    packet = author_guided_shell_packet(
        frozen_commit=commit,
        target_remote_url=remote,
        dependency_source_sha256="0" * 64,
        allow_test_local_source_remote=True,
    )
    assert "merge_authority" not in packet
    parsed = parse_approved_job_packet(packet, allow_test_local_source_remote=True)
    assert parsed.merge_authority is None


def test_completed_guided_shell_is_not_materialized_again_after_main_advances(
    tmp_path: Path,
) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    _write_backlog(ppe)
    catalog = _catalog_root(ppe)
    _seed_contract_packet(catalog, ppe)
    first = ensure_jit_catalog_for_refill(
        ppe_repo=ppe,
        packet_root=catalog,
        exclude_work_item_ids=(PREDECESSOR_WORK_ITEM_ID,),
        predecessor_proof=_merged_proof(),
        allow_test_local_source_remote=True,
        fetch_remote=False,
    )
    assert first.status == "created"
    original = (catalog / GUIDED_SHELL_CATALOG_FILENAME).read_bytes()
    (ppe / "README.md").write_text("product main advanced after 07 merged\n")
    _commit_all(ppe, "merge completed guided shell")
    _git(ppe, "push", "-q", "origin", "main")

    for _ in range(2):
        result = ensure_jit_catalog_for_refill(
            ppe_repo=ppe,
            packet_root=catalog,
            exclude_work_item_ids=(PREDECESSOR_WORK_ITEM_ID, GUIDED_SHELL_WORK_ITEM_ID),
            predecessor_proof=_merged_proof(),
            allow_test_local_source_remote=True,
            fetch_remote=False,
        )
        assert result.status == "skipped"
        assert result.reason == "work_item_excluded"
        assert (catalog / GUIDED_SHELL_CATALOG_FILENAME).read_bytes() == original


def test_refill_materializes_current_remote_main_with_matching_dependency_digest(
    tmp_path: Path,
) -> None:
    import yaml

    from msos_autobuilder.build_next import BuildNextConfig, build_next
    from msos_autobuilder.persistent_host import HostPaths
    from msos_autobuilder.refill_controller import RefillConfig

    ppe = _write_ppe(tmp_path / "ppe")
    _write_backlog(ppe)
    old_head = _git(ppe, "rev-parse", "HEAD")
    catalog = _catalog_root(ppe)
    _seed_contract_packet(catalog, ppe)
    writer = tmp_path / "other-writer"
    _git(None, "clone", "-q", _git(ppe, "remote", "get-url", "origin"), str(writer))
    _git(writer, "config", "user.email", "test@example.com")
    _git(writer, "config", "user.name", "Test")
    requirements = b"# Newly approved dependencies on product main\n"
    (writer / "requirements.txt").write_bytes(requirements)
    current_backlog = _backlog_payload()
    current_backlog["items"][1]["reason"] = "Retain asset, expiry and region on forward/back."
    (writer / BACKLOG_RELPATH).write_text(json.dumps(current_backlog))
    new_head = _commit_all(writer, "advance product main")
    _git(writer, "push", "-q", "origin", "main")
    host_root = tmp_path / "host"
    feed = _feed_repo(tmp_path / "feed-work")
    build_config = BuildNextConfig(
        ppe_repo=ppe,
        packet_root=catalog,
        feed_repo_url=str(feed),
        checkout_root=tmp_path / "feed-checkout",
        host_root=host_root,
        allow_test_local_source_remote=True,
        submit=True,
        exclude_work_item_ids=(PREDECESSOR_WORK_ITEM_ID,),
    )
    generation = {
        "last_attempt_classification": {
            "category": "item_terminal",
            "evidence": {
                "reason": "item_terminal_success_merged",
                "attempt_identity": {"work_item_id": PREDECESSOR_WORK_ITEM_ID},
            },
        },
    }
    result = _maybe_materialize_jit_catalog(
        RefillConfig(build_next=build_config),
        HostPaths.from_root(host_root),
        exclusions=(PREDECESSOR_WORK_ITEM_ID,),
        generation=generation,
    )
    assert result is not None and result["status"] == "created"
    raw = json.loads((catalog / GUIDED_SHELL_CATALOG_FILENAME).read_text())
    assert raw["target_source_commit"] == new_head
    assert raw["dependency_source_sha256"] == canonical_dependency_source_sha256(requirements)
    assert _git(ppe, "rev-parse", "HEAD") == old_head
    receipt = build_next(build_config)
    assert receipt.status == "QUEUED", receipt.message
    assert receipt.submitted is True
    assert receipt.work_item_id == GUIDED_SHELL_WORK_ITEM_ID
    assert receipt.source_commit == new_head
    job = yaml.safe_load(_git(feed, "show", f"jobs:{receipt.feed_path}"))
    assert "Retain asset, expiry and region on forward/back." in (
        job["manifest"]["lanes"][0]["instruction"]
    )


def test_failed_main_fetch_cannot_fall_back_to_stale_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import msos_autobuilder.catalog_materializer as materializer

    ppe = _write_ppe(tmp_path / "ppe")
    _write_backlog(ppe)
    catalog = _catalog_root(ppe)
    run = subprocess.run

    def failed_fetch(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if "fetch" in argv and "main" in argv:
            return subprocess.CompletedProcess(argv, 128, "", "network unavailable")
        return run(argv, **kwargs)

    monkeypatch.setattr(materializer.subprocess, "run", failed_fetch)
    with pytest.raises(CatalogMaterializerError, match="network unavailable"):
        materialize_guided_shell_packet(
            backlog=ppe / BACKLOG_RELPATH,
            ppe_repo=ppe,
            predecessor_terminal=_merged_proof(),
            catalog_dir=catalog,
            allow_test_local_source_remote=True,
        )
    assert not (catalog / GUIDED_SHELL_CATALOG_FILENAME).exists()


@pytest.mark.parametrize("reported_work", ["", PREDECESSOR_WORK_ITEM_ID + "_other"])
def test_unrelated_completion_does_not_unlock_predecessor(
    tmp_path: Path, reported_work: str,
) -> None:
    report = tmp_path / f"build-next-ppe-{PREDECESSOR_WORK_ITEM_ID}-other"
    report.mkdir()
    (report / "completion-report.json").write_text(json.dumps({
        "status": "merged", "work_item_id": reported_work, "job_id": report.name,
    }))
    assert resolve_predecessor_terminal_proof(results_root=tmp_path) is False


def test_missing_frozen_requirements_cannot_create_unusable_packet(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    _write_backlog(ppe)
    _git(ppe, "rm", "requirements.txt")
    _commit_all(ppe, "remove dependency contract")
    _git(ppe, "push", "-q", "origin", "main")
    with pytest.raises(CatalogMaterializerError, match="requirements.txt"):
        materialize_guided_shell_packet(
            backlog=ppe / BACKLOG_RELPATH,
            ppe_repo=ppe,
            predecessor_terminal=_merged_proof(),
            catalog_dir=_catalog_root(ppe),
            allow_test_local_source_remote=True,
            fetch_remote=False,
        )


def test_git_timeout_returns_a_bounded_materializer_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import msos_autobuilder.catalog_materializer as materializer

    def timeout(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        assert kwargs["timeout"] == 180
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(materializer.subprocess, "run", timeout)
    with pytest.raises(CatalogMaterializerError, match="did not complete"):
        materializer.freeze_ppe_main_sha(ppe_repo=Path("fixture"))


def test_source_remote_cannot_redirect_packet_to_another_product(tmp_path: Path) -> None:
    from msos_autobuilder.catalog_materializer import freeze_ppe_main_sha

    ppe = _write_ppe(tmp_path / "ppe")
    _git(ppe, "remote", "set-url", "origin", "https://github.com/example/other-product.git")
    with pytest.raises(CatalogMaterializerError, match="target remote does not match"):
        freeze_ppe_main_sha(ppe_repo=ppe, fetch_remote=False)


def test_explicit_completion_identity_unlocks_predecessor(tmp_path: Path) -> None:
    report = tmp_path / "completion-report.json"
    proof = _merged_proof()
    report.write_text(json.dumps(proof))
    assert resolve_predecessor_terminal_proof(results_root=tmp_path) == proof
