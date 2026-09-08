from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_build_next import (
    SOURCE_REPO,
    _catalog_root,
    _commit_all,
    _config,
    _feed_repo,
    _git,
    _write_ppe,
)

from msos_autobuilder.build_next import BuildNextConfig, build_next
from msos_autobuilder.job_packet import (
    JobPacketError,
    assert_identity_not_redirected,
    fetch_declared_target,
    freeze_admitted_identity,
    parse_approved_job_packet,
    prove_declared_commit_fetchable,
    select_next_packet,
)
from msos_autobuilder.validation_contract import canonical_dependency_source_sha256


def _symbolic_ref(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "-q", "--short", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.stdout.strip()


def _packet_template(ppe: Path, **overrides: object) -> dict[str, object]:
    catalog = _catalog_root(ppe)
    raw = json.loads(next(catalog.glob("*.json")).read_text(encoding="utf-8"))
    raw.update(overrides)
    return raw


def _parse_fixture_packet(ppe: Path, **overrides: object):
    return parse_approved_job_packet(
        _packet_template(ppe, **overrides),
        allow_test_local_source_remote=True,
    )


def test_admission_succeeds_without_ppe_checkout_or_founder_portfolio(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    catalog = _catalog_root(ppe)
    feed = _feed_repo(tmp_path / "feed-work")
    founder = ppe / "scripts" / "founder_portfolio.py"
    founder.unlink()
    ppe.rename(tmp_path / "ppe-removed")

    receipt = build_next(
        BuildNextConfig(
            ppe_repo=tmp_path / "missing-ppe",
            packet_root=catalog,
            feed_repo_url=str(feed),
            checkout_root=tmp_path / "checkout",
            allow_test_local_source_remote=True,
        )
    )

    assert receipt.status == "QUEUED", receipt.message
    assert receipt.work_item_id == "fixture_work"
    assert not founder.exists()


def test_unrelated_external_repo_movement_cannot_affect_admission(tmp_path: Path) -> None:
    target = _write_ppe(tmp_path / "target")
    other = _write_ppe(tmp_path / "other")
    feed = _feed_repo(tmp_path / "feed-work")
    frozen = _git(target, "rev-parse", "HEAD")
    (other / "unrelated.txt").write_text("moved\n", encoding="utf-8")
    _commit_all(other, "unrelated other-repo movement")
    _git(other, "push", "-q", "origin", "main")

    receipt = build_next(_config(tmp_path, target, feed))

    assert receipt.status == "QUEUED"
    assert receipt.repository == SOURCE_REPO
    assert receipt.source_commit == frozen
    assert receipt.source_commit != _git(other, "rev-parse", "HEAD")


def test_packet_targeting_frozen_commit_stays_valid_after_target_main_moves(
    tmp_path: Path,
) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")
    frozen = _git(ppe, "rev-parse", "HEAD")
    (ppe / "changelog.txt").write_text("unrelated main movement\n", encoding="utf-8")
    moved = _commit_all(ppe, "unrelated changelog")
    _git(ppe, "push", "-q", "origin", "main")

    receipt = build_next(_config(tmp_path, ppe, feed))
    dest = tmp_path / "fetched-frozen"
    fetched = fetch_declared_target(
        target_repository=SOURCE_REPO,
        target_source_commit=frozen,
        destination=dest,
        remote_url=_git(ppe, "remote", "get-url", "origin"),
        allow_test_local_source_remote=True,
    )

    assert moved != frozen
    assert receipt.status == "QUEUED", receipt.message
    assert receipt.source_commit == frozen
    assert (
        prove_declared_commit_fetchable(
            target_repository=SOURCE_REPO,
            target_source_commit=frozen,
            remote_url=_git(ppe, "remote", "get-url", "origin"),
            allow_test_local_source_remote=True,
        )
        == frozen
    )
    assert fetched == frozen
    assert _git(dest, "rev-parse", "HEAD") == frozen
    assert _symbolic_ref(dest) == ""


def test_missing_target_repository_fails_closed(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    packet = _packet_template(ppe)
    packet.pop("target_repository")
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "job.json").write_text(json.dumps(packet) + "\n", encoding="utf-8")

    receipt = build_next(
        BuildNextConfig(
            packet_root=catalog,
            feed_repo_url=str(_feed_repo(tmp_path / "feed-work")),
            checkout_root=tmp_path / "checkout",
            allow_test_local_source_remote=True,
        )
    )

    assert receipt.status == "BLOCKED"
    assert "target_repository" in receipt.message


def test_missing_or_non_commit_target_source_identity_fails_closed(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")
    for index, value in enumerate((None, "", "main", "abc", "g" * 40)):
        packet = _packet_template(ppe)
        if value is None:
            packet.pop("target_source_commit")
        else:
            packet["target_source_commit"] = value
        catalog = tmp_path / f"catalog-{index}"
        catalog.mkdir()
        (catalog / "job.json").write_text(json.dumps(packet) + "\n", encoding="utf-8")
        receipt = build_next(
            BuildNextConfig(
                packet_root=catalog,
                feed_repo_url=str(feed),
                checkout_root=tmp_path / f"checkout-{index}",
                allow_test_local_source_remote=True,
            )
        )
        assert receipt.status == "BLOCKED"
        assert "target_source_commit" in receipt.message


def test_target_identity_cannot_redirect_after_admission(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    original = _parse_fixture_packet(ppe)
    admitted = freeze_admitted_identity(original)
    redirected = _parse_fixture_packet(ppe, target_repository="SomeoneElse/other-product")

    with pytest.raises(JobPacketError, match="cannot redirect"):
        assert_identity_not_redirected(admitted, redirected)


def test_ab_ordering_is_represented_entirely_in_repo_local_state(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    base = _packet_template(ppe)
    for order, work_item_id in (
        (1, "options_horizon_comparison_v1"),
        (2, "options_expression_fit_ranking_v1"),
    ):
        packet = dict(base)
        packet["order"] = order
        packet["work_item_id"] = work_item_id
        (catalog / f"{order:02d}-{work_item_id}.json").write_text(
            json.dumps(packet) + "\n",
            encoding="utf-8",
        )
    first = build_next(
        BuildNextConfig(
            packet_root=catalog,
            feed_repo_url=str(feed),
            checkout_root=tmp_path / "first",
            allow_test_local_source_remote=True,
        )
    )
    second = build_next(
        BuildNextConfig(
            packet_root=catalog,
            feed_repo_url=str(feed),
            checkout_root=tmp_path / "second",
            allow_test_local_source_remote=True,
            exclude_work_item_ids=("options_horizon_comparison_v1",),
        )
    )
    packets = [
        parse_approved_job_packet(
            json.loads(path.read_text(encoding="utf-8")),
            allow_test_local_source_remote=True,
        )
        for path in sorted(catalog.glob("*.json"))
    ]

    assert first.status == "QUEUED"
    assert first.work_item_id == "options_horizon_comparison_v1"
    assert second.status == "QUEUED"
    assert second.work_item_id == "options_expression_fit_ranking_v1"
    assert select_next_packet(packets).work_item_id == "options_horizon_comparison_v1"
    assert (
        select_next_packet(
            packets,
            exclude_work_item_ids=("options_horizon_comparison_v1",),
        ).work_item_id
        == "options_expression_fit_ranking_v1"
    )


def test_selector_does_not_invoke_founder_portfolio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")
    real_run = subprocess.run

    def guarded_run(*args: object, **kwargs: object):
        argv = args[0] if args else kwargs.get("args")
        if argv and any("founder_portfolio.py" in str(part) for part in argv):
            raise AssertionError("founder_portfolio.py must not be invoked")
        return real_run(*args, **kwargs)

    monkeypatch.setattr("msos_autobuilder.build_next.subprocess.run", guarded_run)
    monkeypatch.setattr("msos_autobuilder.job_packet.subprocess.run", guarded_run)
    receipt = build_next(_config(tmp_path, ppe, feed))
    assert receipt.status == "QUEUED", receipt.message


def test_fetch_declared_target_uses_exact_commit_not_moving_main(tmp_path: Path) -> None:
    repo = _write_ppe(tmp_path / "target")
    frozen = _git(repo, "rev-parse", "HEAD")
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    later = _commit_all(repo, "later main")
    _git(repo, "push", "-q", "origin", "main")
    dest = tmp_path / "fetched"
    fetched = fetch_declared_target(
        target_repository=SOURCE_REPO,
        target_source_commit=frozen,
        destination=dest,
        remote_url=_git(repo, "remote", "get-url", "origin"),
        allow_test_local_source_remote=True,
    )
    assert later != frozen
    assert fetched == frozen
    assert _git(dest, "rev-parse", "HEAD") == frozen
    assert _symbolic_ref(dest) == ""


def test_malformed_merge_authority_is_rejected(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    with pytest.raises(JobPacketError, match="merge_authority class"):
        _parse_fixture_packet(ppe, merge_authority={"class": "SOMETIMES_MERGE"})
    with pytest.raises(JobPacketError, match="declared_at"):
        _parse_fixture_packet(
            ppe,
            merge_authority={"class": "AUTO_MERGE_WHEN_GREEN"},
        )


def test_authority_and_validation_remain_bound_to_immutable_job_identity(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    feed = _feed_repo(tmp_path / "feed-work")
    receipt = build_next(_config(tmp_path, ppe, feed))
    admitted = receipt.evidence["admitted_target"]
    validation = receipt.evidence["work_admission"]["evidence"]["objective"]

    assert receipt.status == "QUEUED"
    assert admitted["target_repository"] == receipt.repository
    assert admitted["target_source_commit"] == receipt.source_commit
    assert admitted["target_remote_url"] == _git(ppe, "remote", "get-url", "origin")
    assert admitted["allowed_paths"] == ("src/viz/panel.py", "tests/test_panel.py")
    assert admitted["dependency_source_sha256"] == canonical_dependency_source_sha256(
        b"# PPE fixture requirements\n"
    )
    assert validation["repository"] == receipt.repository
    assert validation["work_item_id"] == "fixture_work"


def test_packet_remote_url_for_another_github_repo_is_rejected(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    packet = _packet_template(
        ppe,
        target_repository=SOURCE_REPO,
        target_remote_url="https://github.com/SomeoneElse/Probability-prediction-engine.git",
    )

    with pytest.raises(JobPacketError, match="target_remote_url"):
        parse_approved_job_packet(packet)


def test_canonical_https_and_ssh_urls_for_same_repository_are_accepted(
    tmp_path: Path,
) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    urls = (
        f"https://github.com/{SOURCE_REPO}.git",
        f"git@github.com:{SOURCE_REPO}.git",
        f"ssh://git@github.com/{SOURCE_REPO}.git",
    )
    packets = [
        parse_approved_job_packet(_packet_template(ppe, target_remote_url=url))
        for url in urls
    ]

    assert {packet.target_repository for packet in packets} == {SOURCE_REPO}
    assert [packet.target_remote_url for packet in packets] == list(urls)
    assert len({packet.packet_sha256 for packet in packets}) == len(urls)


def test_changing_only_fetch_url_after_admission_cannot_redirect_target(
    tmp_path: Path,
) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    original = _parse_fixture_packet(ppe)
    admitted = freeze_admitted_identity(original)
    redirected = parse_approved_job_packet(
        _packet_template(
            ppe,
            target_remote_url=f"https://github.com/{SOURCE_REPO}.git",
        )
    )

    assert original.target_repository == redirected.target_repository
    assert original.target_source_commit == redirected.target_source_commit
    assert original.target_remote_url != redirected.target_remote_url
    assert original.packet_sha256 != redirected.packet_sha256
    assert admitted.target_remote_url == original.target_remote_url
    with pytest.raises(JobPacketError, match="cannot redirect"):
        assert_identity_not_redirected(admitted, redirected)


def test_fetch_cannot_clone_different_repo_even_when_commit_sha_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    frozen = _git(ppe, "rev-parse", "HEAD")
    wrong_dest = tmp_path / "fetched-wrong-repo"
    real_run = subprocess.run

    def guarded_run(*args: object, **kwargs: object):
        argv = args[0] if args else kwargs.get("args")
        if argv and "clone" in [str(part) for part in argv]:
            raise AssertionError("clone of a non-admitted repository must not run")
        return real_run(*args, **kwargs)

    monkeypatch.setattr("msos_autobuilder.job_packet.subprocess.run", guarded_run)
    with pytest.raises(JobPacketError, match="target_remote_url"):
        fetch_declared_target(
            target_repository=SOURCE_REPO,
            target_source_commit=frozen,
            destination=wrong_dest,
            remote_url="https://github.com/SomeoneElse/Probability-prediction-engine.git",
        )

    assert not wrong_dest.exists()


def test_production_packet_rejects_local_filesystem_remote(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    packet = _packet_template(ppe)
    dest = tmp_path / "fetched-local"

    with pytest.raises(JobPacketError, match="target_remote_url"):
        parse_approved_job_packet(packet)
    with pytest.raises(JobPacketError, match="target_remote_url"):
        fetch_declared_target(
            target_repository=SOURCE_REPO,
            target_source_commit=_git(ppe, "rev-parse", "HEAD"),
            destination=dest,
            remote_url=_git(ppe, "remote", "get-url", "origin"),
        )
    receipt = build_next(
        BuildNextConfig(
            packet_root=_catalog_root(ppe),
            feed_repo_url=str(_feed_repo(tmp_path / "feed-work")),
            checkout_root=tmp_path / "checkout",
        )
    )

    assert receipt.status == "BLOCKED"
    assert "target_remote_url" in receipt.message
    assert not dest.exists()


def test_production_packet_rejects_non_github_remote(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    packet = _packet_template(
        ppe,
        target_remote_url="https://example.com/not-github/repo.git",
    )

    with pytest.raises(JobPacketError, match="target_remote_url"):
        parse_approved_job_packet(packet)


def test_explicit_test_local_remote_escape_hatch_still_works(tmp_path: Path) -> None:
    ppe = _write_ppe(tmp_path / "ppe")
    local_url = _git(ppe, "remote", "get-url", "origin")
    frozen = _git(ppe, "rev-parse", "HEAD")
    packet = _parse_fixture_packet(ppe)
    dest = tmp_path / "fetched-hatch"
    fetched = fetch_declared_target(
        target_repository=SOURCE_REPO,
        target_source_commit=frozen,
        destination=dest,
        remote_url=local_url,
        allow_test_local_source_remote=True,
    )
    receipt = build_next(_config(tmp_path, ppe, _feed_repo(tmp_path / "feed-work")))

    assert packet.target_repository == SOURCE_REPO
    assert packet.target_remote_url == local_url
    assert fetched == frozen
    assert _git(dest, "rev-parse", "HEAD") == frozen
    assert receipt.status == "QUEUED", receipt.message
    assert receipt.evidence["admitted_target"]["target_remote_url"] == local_url
