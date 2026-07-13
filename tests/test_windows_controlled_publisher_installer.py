from pathlib import Path


def test_controlled_publisher_installer_is_draft_only_single_writer() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "install_windows_controlled_publisher.ps1"
    ).read_text(encoding="utf-8")

    assert "MSOS Autobuilder Controlled Publisher" in script
    assert "draft_pr_publication_enabled: true" in script
    assert "merge_enabled: false" in script
    assert "main_write_enabled: false" in script
    assert "product-writer-owner.json" in script
    assert "PPE_GIT_AUTONOMOUS_WRITES" in script
    assert "PPE_ALLOW_LEGACY_GIT_PUBLISH" in script
    assert "Disable-ScheduledTask" in script
    assert "Stop-Process" in script
    assert "MSOS Autobuilder Host" in script
    assert "MSOS Autobuilder Result Relay" in script
    assert "MSOS Autobuilder Candidate Gate" in script
    assert "MSOS Autobuilder Revision Loop" in script
    assert "autobuilder/$WitnessJobId" in script
    assert "git push --force" not in script.lower()
    assert "ppe-automerge: true" not in script
