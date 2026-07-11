from pathlib import Path

import pytest

from msos_autobuilder.backends.base import ExecutionEvidence
from msos_autobuilder.models import BuildLane, BuildTask, CostClass, WorkerCapabilities
from msos_autobuilder.routing import BackendRouter, BackendRoutingError


class StubBackend:
    def __init__(
        self,
        backend_id: str,
        *,
        capabilities: frozenset[str],
        cost_class: CostClass,
    ) -> None:
        self._capabilities = WorkerCapabilities(
            backend_id=backend_id,
            capabilities=capabilities,
            max_concurrency=1,
            cost_class=cost_class,
        )

    @property
    def capabilities(self) -> WorkerCapabilities:
        return self._capabilities

    def claim(self, task: BuildTask) -> bool:
        return task.lane.required_capabilities <= self.capabilities.capabilities

    def prepare_workspace(self, task: BuildTask, workspace: Path) -> Path:
        return workspace

    def execute(self, task: BuildTask, workspace: Path) -> ExecutionEvidence:
        raise AssertionError("not used")

    def collect_evidence(self, task: BuildTask, workspace: Path) -> ExecutionEvidence:
        raise AssertionError("not used")

    def cancel(self, task: BuildTask) -> None:
        return None


def task(*capabilities: str, preferred: CostClass = CostClass.LOW) -> BuildTask:
    lane = BuildLane(
        lane_id="web",
        chapter_id="web-chapter",
        branch="chapter/web",
        layer="msos-shell",
        allowed_paths=("apps/msos-web/**",),
        required_capabilities=frozenset(capabilities),
        preferred_cost_class=preferred,
    )
    return BuildTask(task_id="task-web", lane=lane, instruction="Plan web work")


def test_router_selects_cheapest_compatible_backend() -> None:
    router = BackendRouter(
        [
            StubBackend(
                "premium",
                capabilities=frozenset({"plan", "code"}),
                cost_class=CostClass.PREMIUM,
            ),
            StubBackend(
                "free",
                capabilities=frozenset({"plan"}),
                cost_class=CostClass.FREE,
            ),
            StubBackend(
                "low",
                capabilities=frozenset({"plan", "code"}),
                cost_class=CostClass.LOW,
            ),
        ]
    )

    assert router.select(task("plan")).capabilities.backend_id == "free"
    assert router.select(task("code")).capabilities.backend_id == "low"


def test_router_fails_closed_without_compatible_backend() -> None:
    router = BackendRouter(
        [
            StubBackend(
                "free",
                capabilities=frozenset({"plan"}),
                cost_class=CostClass.FREE,
            )
        ]
    )

    with pytest.raises(BackendRoutingError, match="no backend supports"):
        router.select(task("code"))
