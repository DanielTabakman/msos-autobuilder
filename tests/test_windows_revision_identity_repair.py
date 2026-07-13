from pathlib import Path


def test_revision_identity_repair_is_narrow_and_publication_disabled() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "repair_windows_revision_identity_witness.ps1"
    ).read_text(encoding="utf-8")

    assert "MSOS Autobuilder Candidate Gate" in script
    assert "MSOS Autobuilder Revision Loop" in script
    assert "frozen-evaluation-snapshot-identity" in script
    assert "ImportError*frozen_evaluation_contract" in script
    assert "Refusing repair" in script
    assert "candidate-gate-seen.json" in script
    assert "product publication" in script.lower()
    assert "git push" not in script.lower()
    assert "product repo" not in script.lower()
