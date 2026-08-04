from pathlib import Path

from msos_autobuilder import candidate_gate_revisions


def test_deliberate_witness_passes_staging_but_exits_managed_service() -> None:
    marker = candidate_gate_revisions._ROLLBACK_WITNESS_MARKER
    assert marker == Path(__file__).resolve().parents[1] / "config" / "rollback_witness.enabled"
    assert marker.is_file()
    assert candidate_gate_revisions.main(["--config", "unused.yaml"]) == 73


def test_deliberate_witness_does_not_change_publication_authority() -> None:
    source = Path(candidate_gate_revisions.__file__).read_text(encoding="utf-8")
    assert '"publication_enabled": False' in source
    assert "push --force" not in source
    assert "merge" not in source.lower()
