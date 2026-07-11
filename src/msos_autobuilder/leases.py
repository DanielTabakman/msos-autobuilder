"""File-backed runtime leases kept outside product repositories."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class LeaseConflictError(RuntimeError):
    """Raised when an active lane lease is owned by someone else."""


class LeaseStateError(RuntimeError):
    """Raised when lease state is invalid or cannot be safely changed."""


@dataclass(frozen=True)
class LeaseRecord:
    lane_id: str
    owner_id: str
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime

    def active_at(self, now: datetime) -> bool:
        return self.expires_at > now


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LeaseStateError("lease timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _validate_id(value: str, field: str) -> str:
    if not _ID_PATTERN.fullmatch(value):
        raise LeaseStateError(
            f"{field} must contain only letters, numbers, dot, dash, or underscore"
        )
    return value


class FileLeaseStore:
    """Atomic local lease store using one JSON file and lock per lane."""

    def __init__(self, runtime_root: str | Path) -> None:
        self.root = Path(runtime_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _lease_path(self, lane_id: str) -> Path:
        return self.root / f"{_validate_id(lane_id, 'lane_id')}.json"

    def _lock_path(self, lane_id: str) -> Path:
        return self.root / f"{_validate_id(lane_id, 'lane_id')}.lock"

    @contextmanager
    def _lane_lock(self, lane_id: str) -> Iterator[None]:
        lock_path = self._lock_path(lane_id)
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise LeaseStateError(f"lease operation already in progress for {lane_id}") from exc
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(descriptor)
            lock_path.unlink(missing_ok=True)

    def _read_unlocked(self, lane_id: str) -> LeaseRecord | None:
        path = self._lease_path(lane_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return LeaseRecord(
                lane_id=str(raw["lane_id"]),
                owner_id=str(raw["owner_id"]),
                acquired_at=datetime.fromisoformat(raw["acquired_at"]),
                heartbeat_at=datetime.fromisoformat(raw["heartbeat_at"]),
                expires_at=datetime.fromisoformat(raw["expires_at"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LeaseStateError(f"invalid lease file: {path}") from exc

    def _write_unlocked(self, record: LeaseRecord) -> None:
        payload = {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in asdict(record).items()
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.root,
            prefix=f".{record.lane_id}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, self._lease_path(record.lane_id))

    def get(self, lane_id: str) -> LeaseRecord | None:
        return self._read_unlocked(lane_id)

    def acquire(
        self,
        lane_id: str,
        owner_id: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> LeaseRecord:
        _validate_id(owner_id, "owner_id")
        if ttl_seconds < 1:
            raise LeaseStateError("ttl_seconds must be positive")
        instant = _require_aware(now or _utc_now())

        with self._lane_lock(lane_id):
            existing = self._read_unlocked(lane_id)
            if existing is not None and existing.active_at(instant):
                raise LeaseConflictError(
                    f"lane {lane_id!r} is leased by {existing.owner_id!r} "
                    f"until {existing.expires_at.isoformat()}"
                )
            record = LeaseRecord(
                lane_id=lane_id,
                owner_id=owner_id,
                acquired_at=instant,
                heartbeat_at=instant,
                expires_at=instant + timedelta(seconds=ttl_seconds),
            )
            self._write_unlocked(record)
            return record

    def renew(
        self,
        lane_id: str,
        owner_id: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> LeaseRecord:
        if ttl_seconds < 1:
            raise LeaseStateError("ttl_seconds must be positive")
        instant = _require_aware(now or _utc_now())

        with self._lane_lock(lane_id):
            existing = self._read_unlocked(lane_id)
            if existing is None:
                raise LeaseStateError(f"lane {lane_id!r} has no lease")
            if existing.owner_id != owner_id:
                raise LeaseConflictError(f"lane {lane_id!r} is owned by {existing.owner_id!r}")
            if not existing.active_at(instant):
                raise LeaseConflictError(f"lane {lane_id!r} lease has expired")
            record = LeaseRecord(
                lane_id=lane_id,
                owner_id=owner_id,
                acquired_at=existing.acquired_at,
                heartbeat_at=instant,
                expires_at=instant + timedelta(seconds=ttl_seconds),
            )
            self._write_unlocked(record)
            return record

    def release(self, lane_id: str, owner_id: str) -> bool:
        with self._lane_lock(lane_id):
            existing = self._read_unlocked(lane_id)
            if existing is None:
                return False
            if existing.owner_id != owner_id:
                raise LeaseConflictError(f"lane {lane_id!r} is owned by {existing.owner_id!r}")
            self._lease_path(lane_id).unlink(missing_ok=True)
            return True
