"""Load the product-owned contract without importing product business code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ContractError(ValueError):
    """Raised when a product contract is missing required safe defaults."""


@dataclass(frozen=True)
class LayerContract:
    name: str
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductContract:
    version: int
    status: str
    project_id: str
    repository: str
    default_branch: str
    workspace_isolation: str
    shared_mutable_checkout: bool
    layers: dict[str, LayerContract]
    commands: dict[str, str]
    runtime_only_paths: tuple[str, ...]
    publication_enabled: bool
    direct_main_writes: bool
    max_publishers: int
    business_module_imports_allowed: bool


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be a mapping")
    return value


def _strings(value: Any, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ContractError(f"{field} must be a list of non-empty strings")
    if not value and not allow_empty:
        raise ContractError(f"{field} must not be empty")
    return tuple(value)


def _commands(root: dict[str, Any]) -> dict[str, str]:
    validation = _mapping(root.get("validation", {}), "validation")
    raw_commands = validation.get("commands", root.get("commands", {}))
    raw_commands = _mapping(raw_commands, "validation.commands")

    commands: dict[str, str] = {}
    for name, value in raw_commands.items():
        if not isinstance(name, str) or not name:
            raise ContractError("command names must be non-empty strings")
        command = value.get("command") if isinstance(value, dict) else value
        if not isinstance(command, str) or not command.strip():
            raise ContractError(
                "validation.commands must map names to strings or mappings with command"
            )
        commands[name] = command
    return commands


def load_product_contract(path: str | Path) -> ProductContract:
    contract_path = Path(path)
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    root = _mapping(raw, "contract")

    version = root.get("version")
    if version != 1:
        raise ContractError("only contract version 1 is supported")

    status = root.get("status", "draft")
    if not isinstance(status, str) or not status:
        raise ContractError("status must be a non-empty string")

    project = _mapping(root.get("project"), "project")
    project_id = project.get("id")
    repository = project.get("repository")
    default_branch = project.get("default_branch")
    if not all(isinstance(value, str) and value for value in (project_id, repository, default_branch)):
        raise ContractError("project.id, project.repository, and project.default_branch are required")

    workspace = _mapping(root.get("workspace", {}), "workspace")
    workspace_isolation = workspace.get("isolation", "worktree_or_clone")
    shared_mutable_checkout = workspace.get("shared_mutable_checkout", False)
    if not isinstance(workspace_isolation, str) or not workspace_isolation:
        raise ContractError("workspace.isolation must be a non-empty string")
    if not isinstance(shared_mutable_checkout, bool):
        raise ContractError("workspace.shared_mutable_checkout must be boolean")

    raw_layers = _mapping(root.get("layers"), "layers")
    if not raw_layers:
        raise ContractError("layers must not be empty")
    layers: dict[str, LayerContract] = {}
    for name, value in raw_layers.items():
        if not isinstance(name, str) or not name:
            raise ContractError("layer names must be non-empty strings")
        layer = _mapping(value, f"layers.{name}")
        layers[name] = LayerContract(
            name=name,
            allowed_paths=_strings(layer.get("allowed_paths"), f"layers.{name}.allowed_paths"),
            forbidden_paths=_strings(
                layer.get("forbidden_paths", []),
                f"layers.{name}.forbidden_paths",
                allow_empty=True,
            ),
        )

    runtime_only_paths = _strings(
        root.get("runtime_only_paths", []), "runtime_only_paths", allow_empty=True
    )

    publication = _mapping(root.get("publication", {}), "publication")
    publication_enabled = publication.get("enabled", False)
    direct_main_writes = publication.get("direct_main_writes", False)
    if not isinstance(publication_enabled, bool):
        raise ContractError("publication.enabled must be boolean")
    if not isinstance(direct_main_writes, bool):
        raise ContractError("publication.direct_main_writes must be boolean")

    lanes = _mapping(root.get("lanes", {}), "lanes")
    max_publishers = lanes.get("max_publishers", 1)
    if not isinstance(max_publishers, int) or max_publishers < 1:
        raise ContractError("lanes.max_publishers must be a positive integer")

    safety = _mapping(root.get("safety", {}), "safety")
    business_module_imports_allowed = safety.get(
        "product_business_module_imports_allowed", False
    )
    if not isinstance(business_module_imports_allowed, bool):
        raise ContractError("safety.product_business_module_imports_allowed must be boolean")

    return ProductContract(
        version=version,
        status=status,
        project_id=project_id,
        repository=repository,
        default_branch=default_branch,
        workspace_isolation=workspace_isolation,
        shared_mutable_checkout=shared_mutable_checkout,
        layers=layers,
        commands=_commands(root),
        runtime_only_paths=runtime_only_paths,
        publication_enabled=publication_enabled,
        direct_main_writes=direct_main_writes,
        max_publishers=max_publishers,
        business_module_imports_allowed=business_module_imports_allowed,
    )
