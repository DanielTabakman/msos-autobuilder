"""Read-only inventory of factory-related surfaces in a product repository."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

from .contracts import ProductContract, load_product_contract


class InventoryError(ValueError):
    """Raised when inventory rules or repository inputs are invalid."""


class InventoryDecision(StrEnum):
    MOVE = "MOVE"
    KEEP_IN_PRODUCT = "KEEP_IN_PRODUCT"
    REFACTOR_BEFORE_MOVE = "REFACTOR_BEFORE_MOVE"
    TEMPORARY_COMPATIBILITY = "TEMPORARY_COMPATIBILITY"
    DELETE_AS_LEGACY = "DELETE_AS_LEGACY"
    UNCLASSIFIED = "UNCLASSIFIED"


@dataclass(frozen=True)
class InventoryRule:
    rule_id: str
    decision: InventoryDecision
    patterns: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class InventoryRules:
    version: int
    candidate_patterns: tuple[str, ...]
    product_import_markers: tuple[str, ...]
    rules: tuple[InventoryRule, ...]


@dataclass(frozen=True)
class InventoryItem:
    path: str
    decision: InventoryDecision
    reason: str
    matched_rule: str | None
    product_imports: tuple[str, ...]
    sha256: str


@dataclass(frozen=True)
class InventoryReport:
    rules_version: int
    project_id: str
    repository: str
    default_branch: str
    contract_status: str
    scanned_candidates: int
    counts: dict[str, int]
    items: tuple[InventoryItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules_version": self.rules_version,
            "project_id": self.project_id,
            "repository": self.repository,
            "default_branch": self.default_branch,
            "contract_status": self.contract_status,
            "scanned_candidates": self.scanned_candidates,
            "counts": self.counts,
            "items": [
                {
                    **asdict(item),
                    "decision": item.decision.value,
                }
                for item in self.items
            ],
        }


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InventoryError(f"{field} must be a mapping")
    return value


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise InventoryError(f"{field} must be a non-empty list")
    if not all(isinstance(item, str) and item for item in value):
        raise InventoryError(f"{field} must contain non-empty strings")
    return tuple(value)


def load_inventory_rules(path: str | Path) -> InventoryRules:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    root = _mapping(raw, "rules")
    version = root.get("version")
    if version != 1:
        raise InventoryError("only inventory rules version 1 is supported")

    raw_rules = root.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise InventoryError("rules must be a non-empty list")

    parsed_rules: list[InventoryRule] = []
    seen_ids: set[str] = set()
    for index, value in enumerate(raw_rules):
        rule = _mapping(value, f"rules[{index}]")
        rule_id = rule.get("id")
        reason = rule.get("reason")
        decision_raw = rule.get("decision")
        if not isinstance(rule_id, str) or not rule_id:
            raise InventoryError(f"rules[{index}].id must be a non-empty string")
        if rule_id in seen_ids:
            raise InventoryError(f"duplicate inventory rule id: {rule_id}")
        if not isinstance(reason, str) or not reason:
            raise InventoryError(f"rules[{index}].reason must be a non-empty string")
        try:
            decision = InventoryDecision(decision_raw)
        except (TypeError, ValueError) as exc:
            raise InventoryError(f"rules[{index}].decision is invalid") from exc
        parsed_rules.append(
            InventoryRule(
                rule_id=rule_id,
                decision=decision,
                patterns=_strings(rule.get("patterns"), f"rules[{index}].patterns"),
                reason=reason,
            )
        )
        seen_ids.add(rule_id)

    return InventoryRules(
        version=version,
        candidate_patterns=_strings(
            root.get("candidate_patterns"),
            "candidate_patterns",
        ),
        product_import_markers=_strings(
            root.get("product_import_markers"),
            "product_import_markers",
        ),
        rules=tuple(parsed_rules),
    )


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def _find_product_imports(text: str, markers: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(marker for marker in markers if marker in text)


def _classify(
    relative_path: str,
    text: str,
    rules: InventoryRules,
) -> tuple[InventoryDecision, str, str | None, tuple[str, ...]]:
    product_imports = _find_product_imports(text, rules.product_import_markers)
    for rule in rules.rules:
        if not _matches(relative_path, rule.patterns):
            continue
        decision = rule.decision
        reason = rule.reason
        if decision is InventoryDecision.MOVE and product_imports:
            decision = InventoryDecision.REFACTOR_BEFORE_MOVE
            reason = f"{reason} Product-module imports must be removed before extraction."
        return decision, reason, rule.rule_id, product_imports
    return (
        InventoryDecision.UNCLASSIFIED,
        "Candidate factory surface has no approved extraction rule.",
        None,
        product_imports,
    )


def _candidate_paths(repo_root: Path, rules: InventoryRules) -> list[Path]:
    candidates: list[Path] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(repo_root).as_posix()
        if relative.startswith(".git/"):
            continue
        if _matches(relative, rules.candidate_patterns):
            candidates.append(path)
    return sorted(candidates, key=lambda path: path.relative_to(repo_root).as_posix())


def build_inventory(
    repo_root: str | Path,
    contract_path: str | Path,
    rules_path: str | Path,
) -> InventoryReport:
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise InventoryError(f"repository root does not exist: {root}")

    contract: ProductContract = load_product_contract(contract_path)
    rules = load_inventory_rules(rules_path)

    items: list[InventoryItem] = []
    for path in _candidate_paths(root, rules):
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        text = _read_text(path)
        decision, reason, matched_rule, product_imports = _classify(
            relative,
            text,
            rules,
        )
        items.append(
            InventoryItem(
                path=relative,
                decision=decision,
                reason=reason,
                matched_rule=matched_rule,
                product_imports=product_imports,
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )

    counts = Counter(item.decision.value for item in items)
    normalized_counts = {
        decision.value: counts.get(decision.value, 0)
        for decision in InventoryDecision
    }
    return InventoryReport(
        rules_version=rules.version,
        project_id=contract.project_id,
        repository=contract.repository,
        default_branch=contract.default_branch,
        contract_status=contract.status,
        scanned_candidates=len(items),
        counts=normalized_counts,
        items=tuple(items),
    )


def render_json(report: InventoryReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n"


def render_markdown(report: InventoryReport) -> str:
    lines = [
        "# Autobuilder extraction inventory",
        "",
        f"- Project: `{report.project_id}`",
        f"- Repository: `{report.repository}`",
        f"- Contract status: `{report.contract_status}`",
        f"- Rules version: `{report.rules_version}`",
        f"- Candidate files scanned: `{report.scanned_candidates}`",
        "",
        "## Decision counts",
        "",
        "| Decision | Count |",
        "|---|---:|",
    ]
    for decision in InventoryDecision:
        lines.append(f"| `{decision.value}` | {report.counts[decision.value]} |")

    lines.extend(["", "## Files", ""])
    for item in report.items:
        imports = ", ".join(f"`{marker}`" for marker in item.product_imports)
        import_suffix = f" Product imports: {imports}." if imports else ""
        rule = item.matched_rule or "none"
        lines.append(
            f"- **{item.decision.value}** `{item.path}` "
            f"(rule: `{rule}`) — {item.reason}{import_suffix}"
        )
    return "\n".join(lines) + "\n"
