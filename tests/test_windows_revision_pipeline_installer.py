from __future__ import annotations

from pathlib import Path


def test_revision_pipeline_installer_is_publication_disabled() -> None:
    root = Path(__file__).resolve().parents[1]
    pipeline = (root / "scripts" / "install_windows_revision_pipeline.ps1").read_text(
        encoding="utf-8"
    )
    gate = (root / "scripts" / "configure_windows_revision_candidate_gate.ps1").read_text(
        encoding="utf-8"
    )
    revision = (root / "scripts" / "install_windows_revision_loop.ps1").read_text(
        encoding="utf-8"
    )

    assert "candidate_gate_revisions" in gate
    assert "revision_plans:" in gate
    assert "check_frozen_evaluation_schema_compatibility.py" in gate
    assert "MSOS Autobuilder Revision Loop" in revision
    assert "jobs_branch: $JobsBranchYaml" in revision
    assert "max_revision_depth: $MaxRevisionDepth" in revision
    assert "Product publication remains disabled." in pipeline
    assert "main" in gate and "master" in gate
    assert "main" in revision and "master" in revision
