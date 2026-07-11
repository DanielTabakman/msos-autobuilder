from pathlib import Path

from msos_autobuilder.inventory import (
    InventoryDecision,
    build_inventory,
    render_json,
    render_markdown,
)


def _write_contract(root: Path) -> Path:
    contract = root / "config" / "autobuilder_project_contract.v1.yaml"
    contract.parent.mkdir(parents=True)
    contract.write_text(
        """
version: 1
status: draft
project:
  id: synthetic-msos
  repository: example/synthetic-msos
  default_branch: main
workspace:
  isolation: worktree_or_clone
  shared_mutable_checkout: false
layers:
  msos-shell:
    allowed_paths:
      - apps/msos-web/**
validation:
  commands:
    fast:
      command: python -m pytest -q
publication:
  enabled: false
  direct_main_writes: false
runtime_only_paths:
  - artifacts/**
lanes:
  max_publishers: 1
safety:
  product_business_module_imports_allowed: false
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return contract


def _write_candidate(root: Path, relative: str, text: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_inventory_classifies_boundary_and_excludes_product_code(tmp_path: Path) -> None:
    contract = _write_contract(tmp_path)
    _write_candidate(tmp_path, "scripts/run_pushable_gate.py")
    _write_candidate(tmp_path, "scripts/ppe_chapter_publisher.py")
    _write_candidate(
        tmp_path,
        "scripts/relay_runtime_v0.py",
        "from src.engine.distributions import build\n",
    )
    _write_candidate(tmp_path, "scripts/ppe_vm_phase_mirror.py")
    _write_candidate(tmp_path, "docs/SOP/VM_OPERATOR_PHASE.json", "{}\n")
    _write_candidate(tmp_path, "src/engine/math.py", "VALUE = 1\n")

    rules = Path("config/inventory_rules.v1.yaml")
    report = build_inventory(tmp_path, contract, rules)
    decisions = {item.path: item.decision for item in report.items}

    assert decisions["scripts/run_pushable_gate.py"] is InventoryDecision.KEEP_IN_PRODUCT
    assert decisions["scripts/ppe_chapter_publisher.py"] is InventoryDecision.MOVE
    assert (
        decisions["scripts/relay_runtime_v0.py"]
        is InventoryDecision.REFACTOR_BEFORE_MOVE
    )
    assert (
        decisions["scripts/ppe_vm_phase_mirror.py"]
        is InventoryDecision.TEMPORARY_COMPATIBILITY
    )
    assert (
        decisions["docs/SOP/VM_OPERATOR_PHASE.json"]
        is InventoryDecision.DELETE_AS_LEGACY
    )
    assert "src/engine/math.py" not in decisions


def test_inventory_is_deterministic_and_reports_unclassified(tmp_path: Path) -> None:
    contract = _write_contract(tmp_path)
    _write_candidate(tmp_path, "scripts/ppe_chapter_unknown.py", "VALUE = 1\n")

    rules = Path("config/inventory_rules.v1.yaml")
    first = build_inventory(tmp_path, contract, rules)
    second = build_inventory(tmp_path, contract, rules)

    assert first == second
    assert first.items[0].decision is InventoryDecision.UNCLASSIFIED
    assert render_json(first) == render_json(second)


def test_markdown_and_json_include_summary(tmp_path: Path) -> None:
    contract = _write_contract(tmp_path)
    _write_candidate(tmp_path, "ppe_chapter_publish.cmd", "@echo off\n")

    report = build_inventory(
        tmp_path,
        contract,
        Path("config/inventory_rules.v1.yaml"),
    )
    json_text = render_json(report)
    markdown = render_markdown(report)

    assert '"MOVE": 1' in json_text
    assert "# Autobuilder extraction inventory" in markdown
    assert "`ppe_chapter_publish.cmd`" in markdown
