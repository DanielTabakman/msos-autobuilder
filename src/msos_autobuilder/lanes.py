"""Fail-closed ownership checks for parallel build lanes."""

from __future__ import annotations

from fnmatch import fnmatchcase
from itertools import combinations

from .models import BuildLane


class LaneConflictError(ValueError):
    """Raised when lanes could modify the same ownership surface."""


class LaneScopeError(ValueError):
    """Raised when worker changes escape a lane's declared ownership."""


def _ownership_root(pattern: str) -> str:
    normalized = pattern.replace("\\", "/").strip().lstrip("./")
    wildcard_positions = [
        position for token in ("*", "?", "[") if (position := normalized.find(token)) >= 0
    ]
    if wildcard_positions:
        normalized = normalized[: min(wildcard_positions)]
    return normalized.rstrip("/")


def _roots_overlap(left: str, right: str) -> bool:
    if not left or not right:
        return True
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


def lanes_conflict(left: BuildLane, right: BuildLane) -> bool:
    left_roots = {_ownership_root(path) for path in left.allowed_paths}
    right_roots = {_ownership_root(path) for path in right.allowed_paths}
    return any(_roots_overlap(a, b) for a in left_roots for b in right_roots)


def assert_lanes_compatible(lanes: list[BuildLane] | tuple[BuildLane, ...]) -> None:
    lane_ids = [lane.lane_id for lane in lanes]
    if len(lane_ids) != len(set(lane_ids)):
        raise LaneConflictError("lane IDs must be unique")

    branches = [lane.branch for lane in lanes]
    if len(branches) != len(set(branches)):
        raise LaneConflictError("each lane must use an isolated branch")

    for left, right in combinations(lanes, 2):
        if lanes_conflict(left, right):
            raise LaneConflictError(
                f"lanes {left.lane_id!r} and {right.lane_id!r} have overlapping ownership"
            )


def _path_matches(pattern: str, path: str) -> bool:
    normalized_pattern = pattern.replace("\\", "/").lstrip("./")
    normalized_path = path.replace("\\", "/").lstrip("./")
    if normalized_pattern.endswith("/**"):
        root = normalized_pattern[:-3].rstrip("/")
        return normalized_path == root or normalized_path.startswith(f"{root}/")
    return fnmatchcase(normalized_path, normalized_pattern)


def assert_changed_paths_allowed(lane: BuildLane, changed_paths: tuple[str, ...]) -> None:
    for path in changed_paths:
        if any(_path_matches(pattern, path) for pattern in lane.forbidden_paths):
            raise LaneScopeError(f"changed path is forbidden for lane {lane.lane_id!r}: {path}")
        if not any(_path_matches(pattern, path) for pattern in lane.allowed_paths):
            raise LaneScopeError(f"changed path is outside lane {lane.lane_id!r}: {path}")
