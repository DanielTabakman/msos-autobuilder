from pathlib import Path

from msos_autobuilder.contracts import load_product_contract


FIXTURE = Path("fixtures/product_repo/config/autobuilder_project_contract.v1.yaml")


def test_fixture_contract_loads_read_only() -> None:
    contract = load_product_contract(FIXTURE)

    assert contract.project_id == "synthetic-msos"
    assert contract.default_branch == "main"
    assert set(contract.layers) == {"msos-shell", "ppe-core"}
    assert contract.publication_enabled is False
    assert "artifacts/**" in contract.runtime_only_paths
