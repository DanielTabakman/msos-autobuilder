import json
import subprocess
import sys
from pathlib import Path

import pytest

from msos_autobuilder.backends.process import LocalProcessBackend, ProcessExecutionError
from msos_autobuilder.lanes import LaneScopeError
from msos_autobuilder.models import BuildLane, BuildTask, CostClass


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def make_source(root: Path) -> Path:
    source = root / "source"
    (source / "apps/msos-web").mkdir(parents=True)
    (source / "src/engine").mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    git(source, "config", "user.email", "fixture@example.com")
    git(source, "config", "user.name", "Fixture")
    (source / "apps/msos-web/.keep").write_text("\n", encoding="utf-8")
    (source / "src/engine/.keep").write_text("\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "fixture")
    return source


def lane() -> BuildLane:
    return BuildLane(
        lane_id="web",
        chapter_id="web",
        branch="chapter/web",
        layer="msos-shell",
        allowed_paths=("apps/msos-web/**",),
        required_capabilities=frozenset({"process", "code", "git-clone"}),
    )


def worker(source: Path, *, timeout: int = 10) -> LocalProcessBackend:
    return LocalProcessBackend(
        source,
        (sys.executable, "-m", "msos_autobuilder.witness_worker"),
        cost_class=CostClass.FREE,
        max_concurrency=2,
        timeout_seconds=timeout,
        env_allowlist=("PYTHONPATH",),
    )


def instruction(target: str, barrier: Path) -> str:
    return json.dumps(
        {
            "lane_id": "web",
            "target": target,
            "content": "done\n",
            "barrier_dir": str(barrier),
            "parties": 1,
        }
    )


def test_process_backend_captures_allowed_changes(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    backend = worker(source)
    task = BuildTask(
        task_id="web",
        lane=lane(),
        instruction=instruction("apps/msos-web/result.txt", tmp_path / "barrier"),
    )
    workspace = backend.prepare_workspace(task, tmp_path / "workspace")

    evidence = backend.execute(task, workspace)

    assert evidence.status == "completed"
    assert evidence.changed_paths == ("apps/msos-web/result.txt",)
    assert git(source, "status", "--porcelain") == ""


def test_process_backend_rejects_out_of_scope_changes(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    backend = worker(source)
    task = BuildTask(
        task_id="web",
        lane=lane(),
        instruction=instruction("src/engine/escape.txt", tmp_path / "barrier"),
    )
    workspace = backend.prepare_workspace(task, tmp_path / "workspace")

    with pytest.raises(LaneScopeError, match="outside lane"):
        backend.execute(task, workspace)


def test_process_backend_times_out(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    backend = LocalProcessBackend(
        source,
        (sys.executable, "-c", "import time; time.sleep(2)"),
        timeout_seconds=1,
    )
    task = BuildTask(task_id="web", lane=lane(), instruction="ignored")
    workspace = backend.prepare_workspace(task, tmp_path / "workspace")

    with pytest.raises(ProcessExecutionError, match="timed out"):
        backend.execute(task, workspace)
