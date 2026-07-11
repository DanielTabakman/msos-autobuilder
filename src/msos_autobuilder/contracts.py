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
    project_id: str
    repository: str
    default_branch: str
    layers: dict[str, LayerContract]
    commands: dict[str, str]
    runtime_only_paths: tuple[str, ...]
    publication_enabled: bool


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


def load_product_contract(path: str | Path) -> ProductContract:
    contract_path = Path(path)
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    root = _mapping(raw, "contract")

    version = root.get("version")
    if version != 1:
        raise ContractError("only contract version 1 is supported")

    project = _mapping(root.get("project"), "project")
    project_id = project.get("id")
    repository = project.get("repository")
    default_branch = project.get("default_branch")
    if not all(isinstance(value, str) and value for value in (project_id, repository, default_branch)):
        raise ContractError("project.id, project.repository, and project.default_branch are required")

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

    raw_commands = _mapping(root.get("commands", {}), "commands")
    commands: dict[str, str] = {}
    for name, command in raw_commands.items():
        if not isinstance(name, str) or not isinstance(command, str) or not command.strip():
            raise ContractError("commands must map non-empty names to non-empty strings")
        commands[name] = command

    runtime_only_paths = _strings(
        root.get("runtime_only_paths", []), "runtime_only_paths", allow_empty=True
    )
    publication = _mapping(root.get("publication", {}), "publication")
    publication_enabled = publication.get("enabled", False)
    if not isinstance(publication_enabled, bool):
        raise ContractError("publication.enabled must be boolean")

    return ProductContract(
        version=version,
        project_id=project_id,
        repository=repository,
        default_branch=default_branch,
        layers=layers,
        commands=commands,
        runtime_only_paths=runtime_only_paths,
        publication_enabled=publication_enabled,
    )
