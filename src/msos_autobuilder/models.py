"""Provider-neutral Autobuilder data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class CostClass(StrEnum):
    """Relative worker cost used for routing without naming a provider."""

    FREE = "free"
    LOW = "low"
    STANDARD = "standard"
    PREMIUM = "premium"


@dataclass(frozen=True)
class WorkerCapabilities:
    backend_id: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    max_concurrency: int = 1
    cost_class: CostClass = CostClass.STANDARD
    timeout_seconds: int = 3600

    def __post_init__(self) -> None:
        if not self.backend_id.strip():
            raise ValueError("backend_id is required")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class BuildLane:
    lane_id: str
    chapter_id: str
    branch: str
    layer: str
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...] = ()
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    preferred_cost_class: CostClass = CostClass.LOW

    def __post_init__(self) -> None:
        for name, value in (
            ("lane_id", self.lane_id),
            ("chapter_id", self.chapter_id),
            ("branch", self.branch),
            ("layer", self.layer),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        if not self.allowed_paths:
            raise ValueError("allowed_paths must not be empty")


@dataclass(frozen=True)
class BuildTask:
    task_id: str
    lane: BuildLane
    instruction: str

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task_id is required")
        if not self.instruction.strip():
            raise ValueError("instruction is required")
