"""Trusted build-next validation contract helpers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class ValidationContractError(ValueError):
    """Raised when a build-next validation contract is unsafe or inconsistent."""


@dataclass(frozen=True)
class ValidationCommand:
    name: str
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: int = 600
    required: bool = True
    phase: str = "check"


@dataclass(frozen=True)
class CandidateValidationContract:
    version: int
    contract_sha256: str
    pipeline_id: str
    adapter: str
    target_repository: str
    source_commit: str
    job_id: str
    work_item_id: str
    native_slice_id: str
    allowed_changed_paths: tuple[str, ...]
    bootstrap: tuple[ValidationCommand, ...]
    checks: tuple[ValidationCommand, ...]
    publication_enabled: bool
    merge_enabled: bool
    product_main_write_enabled: bool


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def stable_contract_sha256(payload: Mapping[str, Any]) -> str:
    stable = dict(payload)
    stable.pop("contract_sha256", None)
    stable.pop("validation_contract_sha256", None)
    return hashlib.sha256(_canonical_json(stable).encode("utf-8")).hexdigest()


def _safe_relative(value: Any, label: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise ValidationContractError(f"{label} must be a safe relative path")
    return path.as_posix()


def _command(raw: Mapping[str, Any], *, phase: str, label: str) -> ValidationCommand:
    name = str(raw.get("name") or "").strip()
    argv_raw = raw.get("argv")
    if not name or not isinstance(argv_raw, list) or not argv_raw:
        raise ValidationContractError(f"{label} requires name and argv")
    argv = tuple(str(item) for item in argv_raw)
    if not all(argv):
        raise ValidationContractError(f"{label} argv contains an empty value")
    if any(item in {"&&", "||", ";", "|"} for item in argv):
        raise ValidationContractError(f"{label} argv contains shell control syntax")
    timeout = int(raw.get("timeout_seconds", 600))
    if timeout <= 0:
        raise ValidationContractError(f"{label} timeout_seconds must be positive")
    required = bool(raw.get("required", True))
    return ValidationCommand(
        name=name,
        argv=argv,
        cwd=_safe_relative(raw.get("cwd", "."), f"{label} cwd"),
        timeout_seconds=timeout,
        required=required,
        phase=phase,
    )


def _commands(raw: Any, *, phase: str) -> tuple[ValidationCommand, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValidationContractError(f"{phase} must be a list")
    commands: list[ValidationCommand] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationContractError(f"{phase}[{index}] must be a mapping")
        commands.append(_command(item, phase=phase, label=f"{phase}[{index}]"))
    return tuple(commands)


def load_validation_contract(raw: Any) -> CandidateValidationContract:
    if not isinstance(raw, dict):
        raise ValidationContractError("candidate_validation must be a mapping")
    if raw.get("version") != 1:
        raise ValidationContractError("candidate_validation version must be 1")
    declared = str(raw.get("contract_sha256") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", declared):
        raise ValidationContractError("candidate_validation contract_sha256 is invalid")
    actual = stable_contract_sha256(raw)
    if declared != actual:
        raise ValidationContractError("candidate_validation contract_sha256 mismatch")
    source_commit = str(raw.get("source_commit") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", source_commit):
        raise ValidationContractError("candidate_validation source_commit must be a full SHA")
    allowed_raw = raw.get("allowed_changed_paths")
    if not isinstance(allowed_raw, list) or not allowed_raw:
        raise ValidationContractError("candidate_validation allowed_changed_paths is required")
    allowed = tuple(sorted(_safe_relative(item, "allowed_changed_paths") for item in allowed_raw))
    checks = _commands(raw.get("checks"), phase="check")
    if not checks:
        raise ValidationContractError("candidate_validation requires at least one check")
    publication_enabled = bool(raw.get("publication_enabled", False))
    merge_enabled = bool(raw.get("merge_enabled", False))
    product_main_write_enabled = bool(raw.get("product_main_write_enabled", False))
    if publication_enabled or merge_enabled or product_main_write_enabled:
        raise ValidationContractError("candidate_validation grants forbidden publication authority")
    return CandidateValidationContract(
        version=1,
        contract_sha256=declared,
        pipeline_id=str(raw.get("pipeline_id") or "").strip(),
        adapter=str(raw.get("adapter") or "").strip(),
        target_repository=str(raw.get("target_repository") or "").strip(),
        source_commit=source_commit,
        job_id=str(raw.get("job_id") or "").strip(),
        work_item_id=str(raw.get("work_item_id") or "").strip(),
        native_slice_id=str(raw.get("native_slice_id") or "").strip(),
        allowed_changed_paths=allowed,
        bootstrap=_commands(raw.get("bootstrap", []), phase="bootstrap"),
        checks=checks,
        publication_enabled=False,
        merge_enabled=False,
        product_main_write_enabled=False,
    )


def contract_to_plan_commands(
    contract: CandidateValidationContract,
) -> tuple[ValidationCommand, ...]:
    return (*contract.bootstrap, *contract.checks)


def command_asdict(command: ValidationCommand) -> dict[str, Any]:
    return asdict(command)


def build_ppe_validation_contract(
    *,
    pipeline_id: str,
    job_id: str,
    work_item_id: str,
    native_slice_id: str,
    source_commit: str,
    allowed_changed_paths: Sequence[str],
    target_repository: str,
    adapter: str = "ppe_operator",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 1,
        "pipeline_id": pipeline_id,
        "adapter": adapter,
        "target_repository": target_repository,
        "source_commit": source_commit,
        "job_id": job_id,
        "work_item_id": work_item_id,
        "native_slice_id": native_slice_id,
        "allowed_changed_paths": sorted(path.replace("\\", "/") for path in allowed_changed_paths),
        "dependency_policy": {
            "version": 1,
            "source": "target_repository_package_metadata",
            "network_allowed": True,
            "strategy": "python_editable_install_from_candidate",
        },
        "bootstrap": [
            {
                "name": "install-candidate-package",
                "argv": ["python", "-m", "pip", "install", "-e", "."],
                "cwd": ".",
                "timeout_seconds": 1200,
                "required": True,
            }
        ],
        "checks": [
            {
                "name": "repository-pytest",
                "argv": ["python", "-m", "pytest", "-q"],
                "cwd": ".",
                "timeout_seconds": 1800,
                "required": True,
            }
        ],
        "working_directory": ".",
        "publication_enabled": False,
        "merge_enabled": False,
        "product_main_write_enabled": False,
        "required_semantics": "all required bootstrap and check commands must pass",
    }
    payload["contract_sha256"] = stable_contract_sha256(payload)
    return payload
