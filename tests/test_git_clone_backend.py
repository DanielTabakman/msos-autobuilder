import subprocess
from pathlib import Path

from msos_autobuilder.backends.git_clone import LocalGitCloneBackend
from msos_autobuilder.leases import FileLeaseStore
from msos_autobuilder.models import BuildLane, BuildTask
from msos_autobuilder.routing import BackendRouter
from msos_autobuilder.scheduler import ParallelScheduler
from msos_autobuilder.workspaces import WorkspacePolicy


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def source_repo(root: Path) -> Path:
    repo = root / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.email", "fixture@example.com")
    git(repo, "config", "user.name", "Fixture")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "fixture")
    return repo


def lane(lane_id: str, path: str) -> BuildLane:
    return BuildLane(
        lane_id=lane_id,
        chapter_id=f"chapter-{lane_id}",
        branch=f"chapter/{lane_id}",
        layer=lane_id,
        allowed_paths=(path,),
        required_capabilities=frozenset({"git-clone", "read-only"}),
    )


def test_two_clean_isolated_git_clones_match_source_head(tmp_path: Path) -> None:
    source = source_repo(tmp_path)
    workspace_root = tmp_path / "workspaces"
    runtime_root = tmp_path / "runtime"
    backend = LocalGitCloneBackend(source)
    scheduler = ParallelScheduler(
        router=BackendRouter([backend]),
        lease_store=FileLeaseStore(runtime_root),
        workspace_policy=WorkspacePolicy(
            product_root=source,
            workspace_root=workspace_root,
            runtime_root=runtime_root,
        ),
    )
    tasks = [
        BuildTask(
            task_id="web",
            lane=lane("web", "apps/msos-web/**"),
            instruction="Prepare web workspace",
        ),
        BuildTask(
            task_id="core",
            lane=lane("core", "src/engine/**"),
            instruction="Prepare core workspace",
        ),
    ]

    evidence = scheduler.run(tasks, owner_id="workspace-witness")

    source_head = git(source, "rev-parse", "HEAD")
    assert len(evidence) == 2
    for lane_id in ("web", "core"):
        workspace = workspace_root / lane_id
        assert workspace.is_dir()
        assert git(workspace, "rev-parse", "HEAD") == source_head
        assert git(workspace, "status", "--porcelain") == ""
    assert git(source, "status", "--porcelain") == ""
    assert (workspace_root / "web").resolve() != (workspace_root / "core").resolve()
