"""Generation-aware service error marker evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_RECORDED_MARKER_ATTR = "_msos_autobuilder_error_marker"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dict(payload), indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_exact_sha(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 40 and all(char in "0123456789abcdef" for char in text)


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _generation_id(*, release_commit: Any, started_at: Any, pid: Any) -> str | None:
    release = str(release_commit or "")
    started = str(started_at or "")
    process = str(pid or "")
    if not release or not started or not process:
        return None
    return hashlib.sha256(f"{release}\n{started}\n{process}\n".encode()).hexdigest()


@dataclass(frozen=True)
class ServiceErrorSpec:
    service: str
    marker_name: str
    ledger_name: str


PUBLISHER_ERROR_SPEC = ServiceErrorSpec(
    service="publisher",
    marker_name="controlled-publisher-error.json",
    ledger_name="controlled-publisher-seen.json",
)
GATE_ERROR_SPEC = ServiceErrorSpec(
    service="gate",
    marker_name="candidate-gate-error.json",
    ledger_name="candidate-gate-seen.json",
)
REVISION_ERROR_SPEC = ServiceErrorSpec(
    service="revision",
    marker_name="revision-loop-error.json",
    ledger_name="revision-loop-seen.json",
)


def service_success_path(state_root: Path, service: str) -> Path:
    return state_root / f"{service}-service-success.json"


def _load_json_mapping(path: Path, label: str) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return {}, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(raw, dict):
        return None, f"{label} must be a JSON object"
    return dict(raw), None


def _current_witness(service_checks: Mapping[str, Any], service: str) -> Mapping[str, Any] | None:
    raw_services = service_checks.get("services")
    if not isinstance(raw_services, dict):
        return None
    raw = raw_services.get(service)
    if not isinstance(raw, dict) or raw.get("ok") is not True:
        return None
    return raw


def _associated_job(raw: Mapping[str, Any]) -> str | None:
    for key in ("job_id", "candidate_id", "attempt_id"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    associated = raw.get("associated")
    if isinstance(associated, dict):
        for key in ("job_id", "candidate_id", "attempt_id"):
            value = associated.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _associated_matches(raw: Mapping[str, Any], job_id: str) -> bool:
    associated = raw.get("associated")
    if isinstance(associated, dict) and associated.get("job_id") == job_id:
        return True
    associated_jobs = raw.get("associated_jobs")
    return isinstance(associated_jobs, list) and job_id in associated_jobs


def _witness_metadata(host_root: Path, service: str) -> dict[str, Any]:
    witness_path = (
        host_root.expanduser().resolve().parent
        / ".msos-autobuilder-supervisor"
        / "state"
        / "service-witnesses"
        / f"{service}.json"
    )
    try:
        witness = json.loads(witness_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(witness, dict):
        return {}
    pid = witness.get("child_pid") or witness.get("pid") or witness.get("wrapper_pid")
    generation = _generation_id(
        release_commit=witness.get("release_commit"),
        started_at=witness.get("started_at"),
        pid=pid,
    )
    payload: dict[str, Any] = {
        "release_commit": witness.get("release_commit"),
        "witness_started_at": witness.get("started_at"),
        "witness_pid": pid,
    }
    if generation:
        payload["generation_id"] = generation
    return payload


def service_generation_metadata(
    *,
    host_root: Path,
    service: str,
    recorded_at: str,
    associated: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return best-effort current generation metadata for a newly written marker."""

    payload: dict[str, Any] = {
        "service": service,
        "recorded_at": recorded_at,
        "pid": os.getpid(),
    }
    if associated:
        payload["associated"] = dict(associated)
    payload.update(_witness_metadata(host_root, service))
    return payload


def write_service_error_marker(
    *,
    state_root: Path,
    host_root: Path,
    service: str,
    marker_name: str,
    error_type: str,
    message: str,
    associated: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    exception: BaseException | None = None,
) -> None:
    recorded_at = utc_now()
    payload = {
        **service_generation_metadata(
            host_root=host_root,
            service=service,
            recorded_at=recorded_at,
            associated=associated,
        ),
        "error_type": error_type,
        "message": message,
    }
    if extra:
        payload.update(dict(extra))
    path = state_root / marker_name
    _atomic_write_json(path, payload)
    if exception is not None:
        setattr(
            exception,
            _RECORDED_MARKER_ATTR,
            {"path": str(path), "sha256": _sha256_file(path), "service": service},
        )


def exception_has_recorded_marker(exc: BaseException) -> bool:
    return isinstance(getattr(exc, _RECORDED_MARKER_ATTR, None), dict)


def record_service_cycle_success(
    *,
    state_root: Path,
    host_root: Path,
    service: str,
    cycle_started_at: str,
    associated_jobs: Sequence[str] = (),
    result: str = "success",
    terminal_evidence: Mapping[str, Any] | None = None,
) -> None:
    finished_at = utc_now()
    payload = {
        **service_generation_metadata(
            host_root=host_root,
            service=service,
            recorded_at=finished_at,
        ),
        "version": 1,
        "cycle_started_at": cycle_started_at,
        "finished_at": finished_at,
        "result": result,
        "associated_jobs": sorted(set(associated_jobs)),
        "terminal_evidence": dict(terminal_evidence or {}),
    }
    payload["cycle_id"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    _atomic_write_json(service_success_path(state_root, service), payload)


def _success_supersedes(
    *,
    path: Path,
    service: str,
    marker_recorded: datetime,
    current_generation_id: str | None,
    current_release: Any,
    job_id: str | None,
) -> tuple[bool, str | None]:
    raw, error = _load_json_mapping(path, "service success")
    if error:
        return False, f"service success evidence is invalid: {error}"
    if not raw:
        return False, None
    if raw.get("service") != service:
        return False, "service success evidence does not identify the same service"
    if raw.get("result") != "success":
        return False, "service success evidence did not record success"
    finished = _parse_utc(raw.get("finished_at"))
    if finished is None:
        return False, "service success evidence lacks valid finished_at"
    if finished <= marker_recorded:
        return False, "service success evidence is not later than marker"
    if raw.get("release_commit") != current_release:
        return False, "service success release does not match current release"
    if current_generation_id and raw.get("generation_id") != current_generation_id:
        return False, "service success generation does not match current service"
    if job_id is not None and not _associated_matches(raw, job_id):
        return False, "service success does not identify the associated job"
    return True, None


def _ledger_time(value: Any) -> datetime | None:
    return _parse_utc(value)


def _publisher_terminal_after(
    ledger: Mapping[str, Any],
    job_id: str,
    marker_recorded: datetime,
) -> tuple[bool, str | None]:
    entry = ledger.get(job_id)
    if not isinstance(entry, dict):
        return False, "publisher ledger lacks a matching job entry"
    required = {
        "gate_report_sha256": _is_sha256,
        "source_report_sha256": _is_sha256,
        "branch": lambda value: isinstance(value, str) and value.startswith("autobuilder/"),
        "commit_sha": _is_exact_sha,
        "pr_number": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "pr_url": lambda value: isinstance(value, str) and value.startswith("http"),
        "results_commit": _is_exact_sha,
    }
    for key, validator in required.items():
        if not validator(entry.get(key)):
            return False, f"publisher ledger entry is missing or invalid: {key}"
    terminal_at = _ledger_time(entry.get("published_at"))
    if terminal_at is None:
        return False, "publisher ledger entry lacks explicit published_at ordering"
    if terminal_at <= marker_recorded:
        return False, "publisher ledger entry predates or equals marker"
    return True, None


def _gate_terminal_after(
    ledger: Mapping[str, Any],
    job_id: str,
    marker_recorded: datetime,
) -> tuple[bool, str | None]:
    entry = ledger.get(job_id)
    if not isinstance(entry, dict):
        return False, "gate ledger lacks a matching job entry"
    if not _is_sha256(entry.get("source_report_sha256")):
        return False, "gate ledger source_report_sha256 is invalid"
    if not _is_exact_sha(entry.get("results_commit")):
        return False, "gate ledger results_commit is invalid"
    if entry.get("status") not in {"passed", "failed", "unvalidated"}:
        return False, "gate ledger status is not terminal"
    terminal_at = _ledger_time(entry.get("processed_at"))
    if terminal_at is None:
        return False, "gate ledger entry lacks explicit processed_at ordering"
    if terminal_at <= marker_recorded:
        return False, "gate ledger entry predates or equals marker"
    return True, None


def _revision_terminal_after(
    ledger: Mapping[str, Any],
    job_id: str,
    marker_recorded: datetime,
) -> tuple[bool, str | None]:
    entries = [
        value
        for key, value in ledger.items()
        if isinstance(key, str)
        and isinstance(value, dict)
        and (key.endswith(f"/{job_id}") or value.get("revision_job_id") == job_id)
    ]
    if len(entries) != 1:
        return False, "revision ledger lacks exactly one matching job entry"
    entry = entries[0]
    if not _is_sha256(entry.get("gate_report_sha256")):
        return False, "revision ledger gate_report_sha256 is invalid"
    if not isinstance(entry.get("revision_job_id"), str) or not entry.get("revision_job_id"):
        return False, "revision ledger revision_job_id is invalid"
    if not _is_exact_sha(entry.get("jobs_commit")):
        return False, "revision ledger jobs_commit is invalid"
    terminal_at = _ledger_time(entry.get("queued_at"))
    if terminal_at is None:
        return False, "revision ledger entry lacks explicit queued_at ordering"
    if terminal_at <= marker_recorded:
        return False, "revision ledger entry predates or equals marker"
    return True, None


def _terminal_ledger_after_marker(
    *,
    spec: ServiceErrorSpec,
    ledger: Mapping[str, Any],
    job_id: str,
    marker_recorded: datetime,
) -> tuple[bool, str | None]:
    if spec.service == "publisher":
        return _publisher_terminal_after(ledger, job_id, marker_recorded)
    if spec.service == "gate":
        return _gate_terminal_after(ledger, job_id, marker_recorded)
    if spec.service == "revision":
        return _revision_terminal_after(ledger, job_id, marker_recorded)
    return False, "unknown service terminal ledger schema"


def _validate_marker_generation(
    *,
    raw: Mapping[str, Any],
    spec: ServiceErrorSpec,
    witness: Mapping[str, Any],
    current_generation_id: str | None,
) -> str | None:
    marker_service = raw.get("service")
    if marker_service is not None and marker_service != spec.service:
        return "error marker service does not match evaluated service"
    marker_release = raw.get("release_commit")
    if marker_release is not None and not _is_exact_sha(marker_release):
        return "error marker release_commit is malformed"
    marker_started = raw.get("witness_started_at")
    if marker_started is not None and _parse_utc(marker_started) is None:
        return "error marker witness_started_at is malformed"
    marker_pid = raw.get("witness_pid")
    if marker_pid is not None and not isinstance(marker_pid, int):
        return "error marker witness_pid is malformed"
    if marker_release == witness.get("release_commit"):
        if marker_started is not None and marker_started != witness.get("started_at"):
            return "error marker generation metadata contradicts current witness"
        current_pid = witness.get("pid")
        if marker_pid is not None and marker_pid != current_pid:
            return "error marker generation metadata contradicts current witness"
    marker_generation = raw.get("generation_id")
    if marker_generation is not None:
        expected = _generation_id(
            release_commit=marker_release,
            started_at=marker_started,
            pid=marker_pid,
        )
        if expected is None or marker_generation != expected:
            return "error marker generation_id is malformed or contradictory"
        if marker_release == witness.get("release_commit") and current_generation_id:
            if marker_generation != current_generation_id:
                return "error marker generation metadata contradicts current witness"
    return None


def evaluate_service_error_marker(
    *,
    state_root: Path,
    service_checks: Mapping[str, Any],
    spec: ServiceErrorSpec,
) -> dict[str, Any]:
    marker_path = state_root / spec.marker_name
    ledger_path = state_root / spec.ledger_name
    success_path = service_success_path(state_root, spec.service)
    evidence: dict[str, Any] = {
        "ok": True,
        "service": spec.service,
        "marker": str(marker_path),
        "ledger": str(ledger_path),
        "success": str(success_path),
        "present": marker_path.exists(),
    }
    if not marker_path.exists():
        evidence["state"] = "absent"
        return evidence

    evidence["ok"] = False
    evidence["state"] = "active"
    try:
        marker_sha = _sha256_file(marker_path)
        raw = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        evidence.update({"error": f"error marker is missing or malformed: {exc}"})
        return evidence
    evidence["marker_sha256"] = marker_sha
    if not isinstance(raw, dict):
        evidence["error"] = "error marker must be a JSON object"
        return evidence

    recorded = _parse_utc(raw.get("recorded_at"))
    if recorded is None:
        evidence["error"] = "error marker lacks a valid recorded_at timestamp"
        return evidence
    evidence["recorded_at"] = raw.get("recorded_at")

    witness = _current_witness(service_checks, spec.service)
    if witness is None:
        evidence["error"] = "current healthy service witness is unavailable"
        return evidence
    witness_started = _parse_utc(witness.get("started_at"))
    if witness_started is None:
        evidence["error"] = "current service witness lacks a valid started_at timestamp"
        return evidence
    current_release = witness.get("release_commit")
    current_generation = _generation_id(
        release_commit=current_release,
        started_at=witness.get("started_at"),
        pid=witness.get("pid"),
    )
    evidence["current_release_commit"] = current_release
    evidence["current_witness_started_at"] = witness.get("started_at")
    evidence["current_generation_id"] = current_generation

    generation_error = _validate_marker_generation(
        raw=raw,
        spec=spec,
        witness=witness,
        current_generation_id=current_generation,
    )
    if generation_error:
        evidence["error"] = generation_error
        return evidence

    job_id = _associated_job(raw)
    if job_id is not None:
        evidence["associated_job_id"] = job_id

    success, success_error = _success_supersedes(
        path=success_path,
        service=spec.service,
        marker_recorded=recorded,
        current_generation_id=current_generation,
        current_release=current_release,
        job_id=job_id,
    )
    if success:
        evidence.update(
            {
                "ok": True,
                "state": "superseded",
                "superseded_by": "later_same_generation_service_success",
                "preserved": True,
            }
        )
        return evidence

    ledger, ledger_error = _load_json_mapping(ledger_path, "terminal ledger")
    if ledger_error:
        evidence["error"] = f"terminal ledger is invalid: {ledger_error}"
        return evidence
    assert ledger is not None
    if job_id is not None:
        terminal, terminal_error = _terminal_ledger_after_marker(
            spec=spec,
            ledger=ledger,
            job_id=job_id,
            marker_recorded=recorded,
        )
        if terminal:
            evidence.update(
                {
                    "ok": True,
                    "state": "superseded",
                    "superseded_by": "later_authoritative_terminal_job_evidence",
                    "preserved": True,
                }
            )
            return evidence
        evidence["error"] = terminal_error or "associated job has no terminal evidence"
        return evidence

    marker_release = raw.get("release_commit")
    if marker_release == current_release and recorded >= witness_started:
        evidence["error"] = success_error or "current-generation error marker remains unresolved"
        return evidence
    if (
        marker_release is not None
        and marker_release != current_release
        and recorded >= witness_started
    ):
        evidence["error"] = "error marker release contradicts current service generation"
        return evidence
    if recorded < witness_started:
        evidence.update(
            {
                "ok": True,
                "state": "superseded",
                "superseded_by": "later_healthy_exact_release_service_start",
                "preserved": True,
            }
        )
        return evidence

    evidence["error"] = success_error or "error marker cannot be proven superseded"
    return evidence
