"""Focused Windows-safe results checkout contracts for Issue #128."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

from msos_autobuilder.candidate_gate import CandidateGateConfig, ResultsBranch
from msos_autobuilder.revision_loop import BranchCheckout, RevisionLoop, RevisionLoopConfig
from msos_autobuilder.windows_git_checkout import (
    CANDIDATE_RESULTS_LEGACY,
    CANDIDATE_RESULTS_SHORT,
    REVISION_RESULTS_LEGACY,
    REVISION_RESULTS_SHORT,
    candidate_results_checkout,
    git_environment,
    prefer_checkout,
    remove_git_tree,
    revision_results_checkout,
)


def _git(path: Path | None, *args: str) -> str:
    command = ["git"]
    if path is not None:
        command.extend(["-C", str(path)])
    command.extend(args)
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        env=git_environment(),
    )
    return proc.stdout.strip()


def _init_bare_results_remote(path: Path, *, branch: str = "results") -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("results fixture\n", encoding="utf-8")
    nested = (
        path
        / "results"
        / "DESKTOP-GE39O15"
        / (
            "build-next-ppe-issue55_generic_gate_witness_v2-"
            "Issue55-GenericGate-WitnessV2-ec1789cd9540"
        )
        / "patches"
    )
    nested.mkdir(parents=True)
    (nested / "Issue55-GenericGate-WitnessV2.patch").write_text(
        "diff --git a/x b/x\n",
        encoding="utf-8",
    )
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "seed results")
    current = _git(path, "branch", "--show-current") or "master"
    if current != branch:
        _git(path, "branch", "-M", branch)
    return path


def _long_isolated_host_root(tmp_path: Path) -> Path:
    return (
        tmp_path
        / "Users"
        / "USER"
        / ".msos-autobuilder-pilot-issue119-4336d74-20260806T0514Z-a"
    ).resolve()


def _nested_patch(checkout: Path) -> Path:
    return (
        checkout
        / "results"
        / "DESKTOP-GE39O15"
        / (
            "build-next-ppe-issue55_generic_gate_witness_v2-"
            "Issue55-GenericGate-WitnessV2-ec1789cd9540"
        )
        / "patches"
        / "Issue55-GenericGate-WitnessV2.patch"
    )


def test_remove_git_tree_deletes_read_only_git_objects(tmp_path: Path) -> None:
    """A clone holds read-only pack files, which defeat a plain shutil.rmtree.

    On Windows a disposable workspace then survives its own cleanup, and every
    later run fails with PermissionError instead of recloning from scratch.
    """
    source = _init_bare_results_remote(tmp_path / "source")
    _git(source, "gc", "-q")
    workspace = tmp_path / "workspace"
    _git(None, "clone", "-q", str(source), str(workspace))

    assert list((workspace / ".git" / "objects" / "pack").glob("*.idx")), (
        "expected a packed clone so the read-only pack case is exercised"
    )
    locked = workspace / "locked.txt"
    locked.write_text("read only\n", encoding="utf-8")
    locked.chmod(stat.S_IREAD)

    remove_git_tree(workspace)

    assert not workspace.exists()


def test_remove_git_tree_tolerates_a_missing_path(tmp_path: Path) -> None:
    absent = tmp_path / "absent"
    remove_git_tree(absent)
    remove_git_tree(absent, ignore_errors=True)
    assert not absent.exists()


def test_prefer_checkout_defaults_to_short_and_reuses_legacy(tmp_path: Path) -> None:
    state = tmp_path / "state"
    short = prefer_checkout(state, "rl-repo", "revision-loop-results-repo")
    assert short == state / "rl-repo"

    legacy = state / "revision-loop-results-repo"
    legacy.mkdir(parents=True)
    (legacy / ".git").mkdir()
    assert prefer_checkout(state, "rl-repo", "revision-loop-results-repo") == legacy


def test_git_environment_enables_longpaths() -> None:
    env = git_environment()
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "core.autocrlf"
    assert env["GIT_CONFIG_VALUE_0"] == "false"
    assert env["GIT_CONFIG_KEY_1"] == "core.longpaths"
    assert env["GIT_CONFIG_VALUE_1"] == "true"


def test_git_environment_preserves_inherited_foreground_prompt(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    env = git_environment()
    assert env["GIT_TERMINAL_PROMPT"] == "1"
    assert env["GIT_CONFIG_KEY_1"] == "core.longpaths"
    assert env["GIT_CONFIG_VALUE_1"] == "true"


def test_revision_loop_prefers_short_results_checkout_on_long_host_root(
    tmp_path: Path,
) -> None:
    host_root = _long_isolated_host_root(tmp_path)
    remote = _init_bare_results_remote(tmp_path / "remote-results")
    _git(remote, "branch", "jobs")
    config = RevisionLoopConfig(
        host_root=host_root,
        repo_url=str(remote),
        results_branch="results",
        jobs_branch="jobs",
        machine_id="DESKTOP-GE39O15",
        poll_seconds=1,
        plans={},
    )
    loop = RevisionLoop(config)
    assert loop.results.root == host_root / "state" / REVISION_RESULTS_SHORT
    assert revision_results_checkout(host_root / "state") == loop.results.root

    legacy_nested = _nested_patch(host_root / "state" / REVISION_RESULTS_LEGACY)
    short_nested = _nested_patch(loop.results.root)
    assert len(str(short_nested)) < len(str(legacy_nested))
    assert len(str(legacy_nested)) - len(str(short_nested)) == len(
        REVISION_RESULTS_LEGACY
    ) - len(REVISION_RESULTS_SHORT)

    loop.results.prepare()
    assert (loop.results.root / ".git").exists()
    assert not (host_root / "state" / REVISION_RESULTS_LEGACY).exists()
    assert short_nested.exists()


def test_revision_loop_reuses_legacy_results_checkout(tmp_path: Path) -> None:
    host_root = _long_isolated_host_root(tmp_path)
    remote = _init_bare_results_remote(tmp_path / "remote-results")
    _git(remote, "branch", "jobs")
    legacy = host_root / "state" / REVISION_RESULTS_LEGACY
    legacy.parent.mkdir(parents=True)
    _git(None, "clone", "--branch", "results", str(remote), str(legacy))

    config = RevisionLoopConfig(
        host_root=host_root,
        repo_url=str(remote),
        results_branch="results",
        jobs_branch="jobs",
        machine_id="DESKTOP-GE39O15",
        poll_seconds=1,
        plans={},
    )
    loop = RevisionLoop(config)
    assert loop.results.root == legacy
    loop.results.prepare()
    assert (legacy / ".git").exists()
    assert not (host_root / "state" / REVISION_RESULTS_SHORT / ".git").exists()


def test_candidate_gate_prefers_short_results_checkout_on_long_host_root(
    tmp_path: Path,
) -> None:
    host_root = _long_isolated_host_root(tmp_path)
    remote = _init_bare_results_remote(tmp_path / "remote-results")
    config = CandidateGateConfig(
        host_root=host_root,
        results_repo_url=str(remote),
        results_branch="results",
        source_repo=tmp_path / "product-source",
        machine_id="DESKTOP-GE39O15",
        poll_seconds=1.0,
        plans={},
    )
    branch = ResultsBranch(config)
    assert branch.checkout == host_root / "state" / CANDIDATE_RESULTS_SHORT
    assert candidate_results_checkout(host_root / "state") == branch.checkout

    legacy_nested = _nested_patch(host_root / "state" / CANDIDATE_RESULTS_LEGACY)
    short_nested = _nested_patch(branch.checkout)
    assert len(str(short_nested)) < len(str(legacy_nested))
    assert len(str(legacy_nested)) - len(str(short_nested)) == len(
        CANDIDATE_RESULTS_LEGACY
    ) - len(CANDIDATE_RESULTS_SHORT)

    branch.prepare()
    assert (branch.checkout / ".git").exists()
    assert short_nested.exists()
    assert not (host_root / "state" / CANDIDATE_RESULTS_LEGACY).exists()


def test_candidate_gate_reuses_legacy_results_checkout(tmp_path: Path) -> None:
    host_root = _long_isolated_host_root(tmp_path)
    remote = _init_bare_results_remote(tmp_path / "remote-results")
    legacy = host_root / "state" / CANDIDATE_RESULTS_LEGACY
    legacy.parent.mkdir(parents=True)
    _git(None, "clone", "--branch", "results", str(remote), str(legacy))
    config = CandidateGateConfig(
        host_root=host_root,
        results_repo_url=str(remote),
        results_branch="results",
        source_repo=tmp_path / "product-source",
        machine_id="DESKTOP-GE39O15",
        poll_seconds=1.0,
        plans={},
    )
    branch = ResultsBranch(config)
    assert branch.checkout == legacy
    branch.prepare()
    assert not (host_root / "state" / CANDIDATE_RESULTS_SHORT / ".git").exists()


def test_branch_checkout_prepare_uses_longpaths_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    remote = _init_bare_results_remote(tmp_path / "remote")
    captured: dict[str, object] = {}
    real_run = subprocess.run

    def _capture_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["env"] = kwargs.get("env")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _capture_run)
    checkout = BranchCheckout(
        tmp_path / "state" / "rl-repo",
        str(remote),
        "results",
        writable=False,
    )
    checkout.prepare()
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["GIT_CONFIG_KEY_1"] == "core.longpaths"
    assert env["GIT_CONFIG_VALUE_1"] == "true"
