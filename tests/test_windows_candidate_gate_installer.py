from __future__ import annotations

from pathlib import Path


INSTALLER = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "install_windows_candidate_gate.ps1"
)


def test_windows_candidate_gate_forces_canonical_patch_line_endings() -> None:
    script = INSTALLER.read_text(encoding="utf-8")

    assert '$env:GIT_CONFIG_KEY_0 = "core.autocrlf"' in script
    assert '$env:GIT_CONFIG_VALUE_0 = "false"' in script
    assert '"checkout-index", "-a", "-f"' in script
    assert "patch hash mismatch*" in script


def test_windows_candidate_gate_retries_only_the_named_witness() -> None:
    script = INSTALLER.read_text(encoding="utf-8")

    assert '[string]$WitnessJobId = "mcd-boundary-and-frozen-contract-v1"' in script
    assert "$Ledger.PSObject.Properties.Remove($WitnessJobId)" in script
    assert "Other processed jobs remain untouched." in script
