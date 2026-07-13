from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_windows_self_update_supervisor.ps1"
INVOKER = ROOT / "scripts" / "invoke_windows_self_update.ps1"


def test_installer_copies_and_configures_external_evidence_relay() -> None:
    script = INSTALLER.read_text(encoding="utf-8")
    assert "self_update_evidence_relay.py" in script
    assert "evidence_repo_url:" in script
    assert "evidence_branch:" in script
    assert "machine_id:" in script
    assert "BootstrapNotification" in script
    assert "requires_founder_attention = $false" in script
    assert "The scheduled updater will retry" in script
    assert 'EvidenceBranch -in @("main", "master")' in script


def test_update_invoker_relays_before_dedupe_and_after_every_attempt() -> None:
    script = INVOKER.read_text(encoding="utf-8")
    assert "function Invoke-EvidenceRelay" in script
    assert script.index("$InitialRelayExitCode = Invoke-EvidenceRelay") < script.index(
        "$SeenManifestHash"
    )
    assert script.count("Invoke-EvidenceRelay") >= 3
    assert "$ApplyExitCode -eq 0 -and $RelayExitCode -eq 0" in script
    assert "exit $InitialRelayExitCode" in script
