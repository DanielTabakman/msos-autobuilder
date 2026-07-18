"""Windows-safe candidate-gate entry point with dynamic revision-job plans.

The ordinary candidate gate intentionally accepts only explicitly configured job IDs. This
entry point adds a narrow second namespace: a revision plan keyed by a safe job prefix may
apply only to ``<prefix>-revision-<n>`` jobs already present on the review-only results
branch. Product publication remains disabled.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from . import candidate_gate as gate
from . import candidate_gate_windows as windows
from .service_error_lifecycle import exception_has_recorded_marker, write_service_error_marker


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise gate.CandidateGateError(f"{label} must be a mapping")
    return value


def _safe_revision_prefix(value: Any) -> str:
    text = str(value or "").strip()
    safe = gate._safe_segment(text, fallback="revision")
    if safe != text:
        raise gate.CandidateGateError(f"unsafe revision plan prefix: {value!r}")
    return safe


def _matches_revision(job_id: str, prefix: str) -> bool:
    return re.fullmatch(re.escape(prefix) + r"-revision-[1-9][0-9]*", job_id) is not None


def _expanded_config(config_path: Path) -> gate.CandidateGateConfig:
    raw = _mapping(yaml.safe_load(config_path.read_text(encoding="utf-8")), "candidate gate config")
    base = gate.load_candidate_gate_config(config_path)
    revision_plans = raw.get("revision_plans", {})
    if revision_plans is None:
        revision_plans = {}
    revision_plans = _mapping(revision_plans, "revision_plans")

    results = gate.ResultsBranch(base)
    results.prepare()
    plans = dict(_mapping(raw.get("plans"), "plans"))
    for job_dir in results.job_dirs():
        job_id = gate._safe_segment(job_dir.name, fallback="job")
        for raw_prefix, raw_plan in revision_plans.items():
            prefix = _safe_revision_prefix(raw_prefix)
            if not _matches_revision(job_id, prefix):
                continue
            if job_id in plans and plans[job_id] != raw_plan:
                raise gate.CandidateGateError(f"conflicting exact and revision plans for {job_id}")
            plans[job_id] = raw_plan

    expanded = dict(raw)
    expanded["plans"] = plans
    expanded.pop("revision_plans", None)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=config_path.parent,
        prefix=".candidate-gate-expanded-",
        suffix=".yaml",
        delete=False,
    ) as handle:
        yaml.safe_dump(expanded, handle, sort_keys=False, allow_unicode=True)
        temporary = Path(handle.name)
    try:
        return gate.load_candidate_gate_config(temporary)
    finally:
        temporary.unlink(missing_ok=True)


def _install_windows_patch_hooks() -> None:
    gate._sha256_file = windows._sha256_file
    gate._run_git = windows._run_git


def run_once(config_path: Path) -> tuple[str, ...]:
    _install_windows_patch_hooks()
    return gate.CandidateGate(_expanded_config(config_path)).run_once()


def _record_global_error_if_needed(config_path: Path, exc: BaseException) -> bool:
    if exception_has_recorded_marker(exc):
        return False
    host_root = gate.load_candidate_gate_config(config_path).host_root
    write_service_error_marker(
        state_root=host_root / "state",
        host_root=host_root,
        service="gate",
        marker_name="candidate-gate-error.json",
        error_type=type(exc).__name__,
        message=str(exc),
        associated={"scope": "global"},
        extra={"publication_enabled": False},
        exception=exc,
    )
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="msos-autobuilder-candidate-gate-revisions")
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()
    if args.once:
        processed = run_once(config_path)
        print(
            json.dumps(
                {
                    "status": "completed",
                    "processed_jobs": list(processed),
                    "publication_enabled": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    while True:
        try:
            config = gate.load_candidate_gate_config(config_path)
            run_once(config_path)
            delay = config.poll_seconds
        except (gate.CandidateGateError, OSError, ValueError, yaml.YAMLError) as exc:
            delay = 30.0
            try:
                _record_global_error_if_needed(config_path, exc)
            except Exception:
                pass
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
