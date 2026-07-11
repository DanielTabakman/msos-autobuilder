import pytest

from msos_autobuilder.lanes import LaneConflictError, assert_lanes_compatible
from msos_autobuilder.models import BuildLane


def lane(lane_id: str, branch: str, *paths: str) -> BuildLane:
    return BuildLane(
        lane_id=lane_id,
        chapter_id=f"chapter-{lane_id}",
        branch=branch,
        layer=lane_id,
        allowed_paths=paths,
    )


def test_disjoint_msos_lanes_can_run_together() -> None:
    lanes = [
        lane("web", "chapter/web", "apps/msos-web/**"),
        lane("core", "chapter/core", "src/engine/**", "src/data/**", "src/models/**"),
    ]

    assert_lanes_compatible(lanes)


def test_overlapping_lanes_fail_closed() -> None:
    lanes = [
        lane("engine", "chapter/engine", "src/engine/**"),
        lane("engine-child", "chapter/engine-child", "src/engine/distributions/**"),
    ]

    with pytest.raises(LaneConflictError, match="overlapping ownership"):
        assert_lanes_compatible(lanes)


def test_shared_branch_fails_closed_even_with_disjoint_paths() -> None:
    lanes = [
        lane("web", "chapter/shared", "apps/msos-web/**"),
        lane("core", "chapter/shared", "src/engine/**"),
    ]

    with pytest.raises(LaneConflictError, match="isolated branch"):
        assert_lanes_compatible(lanes)
