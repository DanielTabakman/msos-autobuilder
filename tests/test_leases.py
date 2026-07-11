from datetime import UTC, datetime, timedelta

import pytest

from msos_autobuilder.leases import FileLeaseStore, LeaseConflictError


def test_active_lease_blocks_second_owner_and_expired_lease_is_reclaimed(tmp_path) -> None:
    store = FileLeaseStore(tmp_path / "runtime")
    start = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)

    first = store.acquire("web", "worker-a", 60, now=start)
    assert first.owner_id == "worker-a"

    with pytest.raises(LeaseConflictError, match="worker-a"):
        store.acquire("web", "worker-b", 60, now=start + timedelta(seconds=30))

    reclaimed = store.acquire(
        "web",
        "worker-b",
        60,
        now=start + timedelta(seconds=61),
    )
    assert reclaimed.owner_id == "worker-b"


def test_renew_and_release_require_current_owner(tmp_path) -> None:
    store = FileLeaseStore(tmp_path / "runtime")
    start = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
    store.acquire("core", "worker-a", 60, now=start)

    renewed = store.renew(
        "core",
        "worker-a",
        120,
        now=start + timedelta(seconds=30),
    )
    assert renewed.expires_at == start + timedelta(seconds=150)

    with pytest.raises(LeaseConflictError, match="worker-a"):
        store.release("core", "worker-b")

    assert store.release("core", "worker-a") is True
    assert store.get("core") is None
