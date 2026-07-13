from pathlib import Path

import yaml


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "self-update-evidence-notify.yml"
)


def test_notifier_workflow_reads_results_as_data_and_keeps_code_on_main() -> None:
    raw = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert raw["permissions"] == {"contents": "read", "issues": "write"}
    steps = raw["jobs"]["notify"]["steps"]
    assert steps[0]["with"]["path"] == "control"
    assert "ref" not in steps[0]["with"]
    assert steps[1]["with"]["ref"] == "results"
    assert steps[1]["with"]["path"] == "evidence"
    command = steps[2]["run"]
    assert "PYTHONPATH" in steps[2]["env"]
    assert "msos_autobuilder.self_update_evidence_notify" in command
    assert "--issue-number 32" in command
    assert "pull-requests: write" not in WORKFLOW.read_text(encoding="utf-8")
