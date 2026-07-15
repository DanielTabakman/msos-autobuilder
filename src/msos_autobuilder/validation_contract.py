"""Trusted build-next validation contract helpers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
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
    dependency_policy: dict[str, Any]
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
    parts = tuple(part for part in text.split("/") if part)
    if (
        not text
        or text.startswith(("/", "//"))
        or re.match(r"^[A-Za-z]:", text)
        or any(part == ".." for part in parts)
    ):
        raise ValidationContractError(f"{label} must be a safe relative path")
    return text


def _identity(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,191}", text):
        raise ValidationContractError(f"{label} must be a non-empty safe identifier")
    return text


def _repository(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text):
        raise ValidationContractError("candidate_validation target_repository is invalid")
    return text


def _dependency_policy(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationContractError("candidate_validation dependency_policy must be a mapping")
    expected_scalars = {
        "version": 1,
        "adapter": "ppe_operator",
        "profile_id": "ppe-ci-pytest-v1",
        "source": "accepted_ppe_ci_bootstrap",
        "dependency_source_path": "requirements.txt",
        "network_allowed": True,
        "candidate_environment_required": True,
        "strategy": "candidate_local_venv_requirements_test_tooling_editable_install",
    }
    for key, expected in expected_scalars.items():
        if raw.get(key) != expected:
            raise ValidationContractError("candidate_validation dependency_policy is unsupported")
    digest = str(raw.get("dependency_source_sha256") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValidationContractError(
            "candidate_validation dependency_policy dependency_source_sha256 is invalid"
        )
    source_commit = str(raw.get("source_commit") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", source_commit):
        raise ValidationContractError(
            "candidate_validation dependency_policy source_commit is invalid"
        )
    tooling = raw.get("test_tooling")
    if tooling != ["pytest", "pytest-xdist", "ruff"]:
        raise ValidationContractError(
            "candidate_validation dependency_policy test_tooling is unsupported"
        )
    return dict(raw)


def _reject_shell_argv(argv: tuple[str, ...], label: str) -> None:
    executable = argv[0].strip().lower().replace("\\", "/").rsplit("/", 1)[-1]
    if executable.endswith(".exe"):
        executable = executable[:-4]
    if executable in {"sh", "bash"} and len(argv) > 1 and argv[1].lower() == "-c":
        raise ValidationContractError(f"{label} may not invoke a shell command string")
    if executable == "cmd" and len(argv) > 1 and argv[1].lower() in {"/c", "/k"}:
        raise ValidationContractError(f"{label} may not invoke a shell command string")
    if executable in {"powershell", "pwsh"} and any(
        arg.lower() in {"-command", "-encodedcommand"} for arg in argv[1:]
    ):
        raise ValidationContractError(f"{label} may not invoke a shell command string")
    if executable not in {"python", "python3", "py"}:
        raise ValidationContractError(f"{label} must run through the candidate Python interpreter")
    if "-c" in argv:
        raise ValidationContractError(f"{label} may not use Python command strings")


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
    _reject_shell_argv(argv, label)
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
    if phase == "bootstrap" and any(not command.required for command in commands):
        raise ValidationContractError("candidate_validation bootstrap steps must be required")
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
    allowed = tuple(_safe_relative(item, "allowed_changed_paths") for item in allowed_raw)
    if len(set(allowed)) != len(allowed):
        raise ValidationContractError(
            "candidate_validation allowed_changed_paths contains duplicates"
        )
    allowed = tuple(sorted(allowed))
    checks = _commands(raw.get("checks"), phase="check")
    if not checks or not any(check.required for check in checks):
        raise ValidationContractError("candidate_validation requires at least one required check")
    bootstrap = _commands(raw.get("bootstrap", []), phase="bootstrap")
    if not bootstrap:
        raise ValidationContractError("candidate_validation requires required bootstrap")
    dependency_policy = _dependency_policy(raw.get("dependency_policy"))
    publication_enabled = bool(raw.get("publication_enabled", False))
    merge_enabled = bool(raw.get("merge_enabled", False))
    product_main_write_enabled = bool(raw.get("product_main_write_enabled", False))
    if publication_enabled or merge_enabled or product_main_write_enabled:
        raise ValidationContractError("candidate_validation grants forbidden publication authority")
    return CandidateValidationContract(
        version=1,
        contract_sha256=declared,
        pipeline_id=_identity(raw.get("pipeline_id"), "candidate_validation pipeline_id"),
        adapter=_identity(raw.get("adapter"), "candidate_validation adapter"),
        target_repository=_repository(raw.get("target_repository")),
        source_commit=source_commit,
        job_id=_identity(raw.get("job_id"), "candidate_validation job_id"),
        work_item_id=_identity(raw.get("work_item_id"), "candidate_validation work_item_id"),
        native_slice_id=_identity(
            raw.get("native_slice_id"),
            "candidate_validation native_slice_id",
        ),
        allowed_changed_paths=allowed,
        dependency_policy=dependency_policy,
        bootstrap=bootstrap,
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
    dependency_source_sha256: str,
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
            "adapter": adapter,
            "profile_id": "ppe-ci-pytest-v1",
            "source": "accepted_ppe_ci_bootstrap",
            "source_commit": source_commit,
            "dependency_source_path": "requirements.txt",
            "dependency_source_sha256": dependency_source_sha256,
            "network_allowed": True,
            "candidate_environment_required": True,
            "strategy": "candidate_local_venv_requirements_test_tooling_editable_install",
            "test_tooling": ["pytest", "pytest-xdist", "ruff"],
        },
        "bootstrap": [
            {
                "name": "upgrade-pip",
                "argv": ["python", "-m", "pip", "install", "--upgrade", "pip"],
                "cwd": ".",
                "timeout_seconds": 600,
                "required": True,
            },
            {
                "name": "install-requirements",
                "argv": ["python", "-m", "pip", "install", "-r", "requirements.txt"],
                "cwd": ".",
                "timeout_seconds": 1200,
                "required": True,
            },
            {
                "name": "install-test-tooling",
                "argv": ["python", "-m", "pip", "install", "pytest", "pytest-xdist", "ruff"],
                "cwd": ".",
                "timeout_seconds": 1200,
                "required": True,
            },
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
        "publication_enabled": False,
        "merge_enabled": False,
        "product_main_write_enabled": False,
    }
    payload["contract_sha256"] = stable_contract_sha256(payload)
    return payload
