from pathlib import Path

from msos_autobuilder.contracts import load_product_contract


FIXTURE = Path("fixtures/product_repo/config/autobuilder_project_contract.v1.yaml")


def test_fixture_contract_loads_read_only() -> None:
    contract = load_product_contract(FIXTURE)

    assert contract.project_id == "synthetic-msos"
    assert contract.default_branch == "main"
    assert contract.workspace_isolation == "worktree_or_clone"
    assert contract.shared_mutable_checkout is False
    assert set(contract.layers) == {"msos-shell", "ppe-core"}
    assert contract.commands["fast"] == "python -m pytest -q"
    assert contract.publication_enabled is False
    assert contract.direct_main_writes is False
    assert contract.max_publishers == 1
    assert contract.business_module_imports_allowed is False
    assert "artifacts/**" in contract.runtime_only_paths
