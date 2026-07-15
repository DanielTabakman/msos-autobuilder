from __future__ import annotations

import hashlib

import pytest

from msos_autobuilder.validation_contract import (
    ValidationContractError,
    build_ppe_validation_contract,
    load_validation_contract,
    stable_contract_sha256,
)

SOURCE = "0123456789abcdef0123456789abcdef01234567"
REQ_SHA = hashlib.sha256(b"").hexdigest()


def _contract() -> dict:
    return build_ppe_validation_contract(
        pipeline_id="ppe",
        job_id="build-next-ppe-work-Slice002-0123456789ab",
        work_item_id="work",
        native_slice_id="Slice002",
        source_commit=SOURCE,
        allowed_changed_paths=["src/app.py", "tests/test_app.py"],
        target_repository="DanielTabakman/Probability-prediction-engine",
        dependency_source_sha256=REQ_SHA,
    )


def _rehash(contract: dict) -> dict:
    contract["contract_sha256"] = stable_contract_sha256(contract)
    return contract


def test_validation_contract_parses_required_fields() -> None:
    contract = load_validation_contract(_contract())

    assert contract.pipeline_id == "ppe"
    assert contract.adapter == "ppe_operator"
    assert contract.target_repository == "DanielTabakman/Probability-prediction-engine"
    assert contract.source_commit == SOURCE
    assert contract.allowed_changed_paths == ("src/app.py", "tests/test_app.py")
    assert (
        contract.dependency_policy["strategy"]
        == "candidate_local_venv_requirements_test_tooling_editable_install"
    )
    assert contract.dependency_policy["dependency_source_path"] == "requirements.txt"
    assert contract.dependency_policy["dependency_source_sha256"] == REQ_SHA
    assert [command.name for command in contract.bootstrap] == [
        "upgrade-pip",
        "install-requirements",
        "install-test-tooling",
        "install-candidate-package",
    ]
    assert all(command.required is True for command in contract.bootstrap)
    assert contract.checks[0].required is True
    assert contract.publication_enabled is False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("pipeline_id", "", "pipeline_id"),
        ("adapter", "", "adapter"),
        ("target_repository", "not-a-repo", "target_repository"),
        ("source_commit", "1234", "source_commit"),
        ("job_id", "", "job_id"),
        ("work_item_id", "", "work_item_id"),
        ("native_slice_id", "", "native_slice_id"),
        ("publication_enabled", True, "publication authority"),
        ("merge_enabled", True, "publication authority"),
        ("product_main_write_enabled", True, "publication authority"),
    ],
)
def test_validation_contract_rejects_missing_or_unsafe_identity(
    field: str,
    value: object,
    message: str,
) -> None:
    contract = _contract()
    contract[field] = value
    _rehash(contract)

    with pytest.raises(ValidationContractError, match=message):
        load_validation_contract(contract)


@pytest.mark.parametrize(
    "path",
    ["../x.py", "/tmp/x.py", "C:/tmp/x.py", "//server/share/x.py", "src/../x.py"],
)
def test_validation_contract_rejects_unsafe_paths(path: str) -> None:
    contract = _contract()
    contract["allowed_changed_paths"] = [path]
    _rehash(contract)

    with pytest.raises(ValidationContractError, match="allowed_changed_paths"):
        load_validation_contract(contract)


def test_validation_contract_rejects_duplicate_paths() -> None:
    contract = _contract()
    contract["allowed_changed_paths"] = ["src/app.py", "src/app.py"]
    _rehash(contract)

    with pytest.raises(ValidationContractError, match="duplicates"):
        load_validation_contract(contract)


@pytest.mark.parametrize(
    "argv",
    [
        ["sh", "-c", "echo nope"],
        ["bash", "-c", "echo nope"],
        ["cmd", "/c", "echo nope"],
        ["cmd.exe", "/k", "echo nope"],
        ["powershell", "-Command", "Write-Host nope"],
        ["pwsh.exe", "-EncodedCommand", "QQ=="],
        ["python", "-c", "print('nope')"],
    ],
)
def test_validation_contract_rejects_shell_or_string_commands(argv: list[str]) -> None:
    contract = _contract()
    contract["checks"][0]["argv"] = argv
    _rehash(contract)

    with pytest.raises(ValidationContractError):
        load_validation_contract(contract)


def test_validation_contract_requires_supported_dependency_policy_and_required_steps() -> None:
    contract = _contract()
    contract["dependency_policy"]["strategy"] = "global-python"
    _rehash(contract)
    with pytest.raises(ValidationContractError, match="dependency_policy"):
        load_validation_contract(contract)

    contract = _contract()
    contract["bootstrap"][0]["required"] = False
    _rehash(contract)
    with pytest.raises(ValidationContractError, match="bootstrap"):
        load_validation_contract(contract)

    contract = _contract()
    contract["checks"][0]["required"] = False
    _rehash(contract)
    with pytest.raises(ValidationContractError, match="required check"):
        load_validation_contract(contract)
