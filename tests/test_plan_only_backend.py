from pathlib import Path

from msos_autobuilder.backends import PlanOnlyBackend
from msos_autobuilder.models import BuildLane, BuildTask


def test_plan_only_backend_produces_no_changed_paths(tmp_path: Path) -> None:
    lane = BuildLane(
        lane_id="web",
        chapter_id="web-chapter",
        branch="chapter/web",
        layer="msos-shell",
        allowed_paths=("apps/msos-web/**",),
        required_capabilities=frozenset({"plan"}),
    )
    task = BuildTask(task_id="task-1", lane=lane, instruction="Plan a UI change")
    backend = PlanOnlyBackend(tmp_path)

    assert backend.claim(task)
    workspace = backend.prepare_workspace(task)
    evidence = backend.execute(task, workspace)

    assert evidence.status == "planned"
    assert evidence.changed_paths == ()
    assert not workspace.exists()
