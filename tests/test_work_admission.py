import json
from pathlib import Path

import pytest

from msos_autobuilder.work_admission import (
    AdmissionError,
    AdmissionRequest,
    AdmissionStatus,
    WorkClassification,
    admit_work,
    candidate_from_pr,
    objective_identity_from_work,
    release_claim,
)


def _objective(issue: int = 110):
    return objective_identity_from_work(
        repository="DanielTabakman/msos-autobuilder",
        linked_issue=issue,
        work_item_id="staged-pytest-timeout",
        stable_parts={
            "issue": issue,
            "error_signature": "python -m pytest -q timed out after 1800s",
            "acceptance": "raise only staged pytest timeout",
        },
        acceptance_contract_sha256="1" * 64,
    )


def test_pr_111_113_fixture_selects_111_and_rejects_second_writer(tmp_path: Path) -> None:
    objective = _objective()
    canonical = candidate_from_pr(
        number=111,
        title="Raise staged pytest timeout for Windows suite",
        state="closed",
        branch="codex/issue-110-staged-pytest-timeout",
        linked_issue=110,
        objective_sha256=objective.objective_sha256,
        acceptance_contract_sha256="1" * 64,
        changed_paths=(
            "src/msos_autobuilder/self_update_supervisor.py",
            "tests/test_self_update_supervisor.py",
        ),
        canonical=True,
        merged=True,
        url="https://github.com/DanielTabakman/msos-autobuilder/pull/111",
    )
    duplicate = candidate_from_pr(
        number=113,
        title="Raise self-update pytest staging timeout",
        state="closed",
        branch="codex/issue50-self-update-pytest-timeout",
        linked_issue=110,
        objective_sha256=objective.objective_sha256,
        acceptance_contract_sha256="1" * 64,
        changed_paths=(
            "src/msos_autobuilder/self_update_supervisor.py",
            "tests/test_self_update_supervisor.py",
        ),
        canonical=False,
        merged=False,
        url="https://github.com/DanielTabakman/msos-autobuilder/pull/113",
    )

    decision = admit_work(
        AdmissionRequest(
            objective=objective,
            writer_id="codex:second-writer",
            branch="codex/second-timeout",
            authorized_paths=(
                "src/msos_autobuilder/self_update_supervisor.py",
                "tests/test_self_update_supervisor.py",
            ),
            claim_root=tmp_path,
            candidates=(duplicate, canonical),
            evidence={"fixture": "PR #111/#113"},
        )
    )

    assert decision.status == AdmissionStatus.CONTINUE_EXISTING_WORK
    assert decision.canonical is not None
    assert decision.canonical.number == 111
    assert not (tmp_path / "work-admission" / "claims").exists()


def test_durable_claim_survives_restart_and_blocks_second_writer(tmp_path: Path) -> None:
    objective = _objective(115)
    first = admit_work(
        AdmissionRequest(
            objective=objective,
            writer_id="writer-one",
            branch="codex/issue-115-work-admission",
            authorized_paths=("src/msos_autobuilder/work_admission.py",),
            claim_root=tmp_path,
        )
    )

    assert first.status == AdmissionStatus.NEW_WORK_ADMITTED
    assert first.claim is not None
    second = admit_work(
        AdmissionRequest(
            objective=objective,
            writer_id="writer-two",
            branch="codex/issue-115-duplicate",
            authorized_paths=("src/msos_autobuilder/work_admission.py",),
            claim_root=tmp_path,
        )
    )

    assert second.status == AdmissionStatus.BLOCKED_BY_OWNERSHIP_CONFLICT
    assert second.claim is not None
    assert second.claim.writer_id == "writer-one"
    assert second.claim.generation == first.claim.generation

    released = release_claim(
        tmp_path,
        objective.objective_sha256,
        writer_id="writer-one",
        terminal_state="failed",
        evidence={"bounded_failure_disposition": "test"},
    )
    assert released.state == "failed"
    third = admit_work(
        AdmissionRequest(
            objective=objective,
            writer_id="writer-two",
            branch="codex/issue-115-duplicate",
            authorized_paths=("src/msos_autobuilder/work_admission.py",),
            claim_root=tmp_path,
        )
    )
    assert third.status == AdmissionStatus.NEW_WORK_ADMITTED
    assert third.claim is not None
    assert third.claim.generation == first.claim.generation + 1
    index = json.loads(
        (tmp_path / "work-admission" / "active-claims.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert [claim["writer_id"] for claim in index["active_claims"]] == ["writer-two"]


def test_wrong_claim_generation_cannot_be_released(tmp_path: Path) -> None:
    objective = _objective(115)
    decision = admit_work(
        AdmissionRequest(
            objective=objective,
            writer_id="writer-one",
            branch="codex/issue-115-work-admission",
            authorized_paths=("src/msos_autobuilder/work_admission.py",),
            claim_root=tmp_path,
        )
    )

    assert decision.claim is not None
    with pytest.raises(AdmissionError, match="claim generation mismatch"):
        release_claim(
            tmp_path,
            objective.objective_sha256,
            writer_id="writer-one",
            terminal_state="merged",
            expected_generation=decision.claim.generation + 1,
        )

    stored = json.loads(
        next((tmp_path / "work-admission" / "claims").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    assert stored["state"] == "active"


def test_same_objective_disjoint_paths_never_overwrites_active_writer(
    tmp_path: Path,
) -> None:
    objective = _objective(115)
    first = admit_work(
        AdmissionRequest(
            objective=objective,
            writer_id="writer-one",
            branch="codex/issue-115-a",
            authorized_paths=("src/msos_autobuilder/work_admission.py",),
            claim_root=tmp_path,
        )
    )

    second = admit_work(
        AdmissionRequest(
            objective=objective,
            writer_id="writer-two",
            branch="codex/issue-115-b",
            authorized_paths=("tests/test_work_admission.py",),
            claim_root=tmp_path,
        )
    )

    assert first.claim is not None
    assert second.status == AdmissionStatus.BLOCKED_BY_OWNERSHIP_CONFLICT
    assert second.claim == first.claim
    stored = json.loads(
        next((tmp_path / "work-admission" / "claims").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    assert stored["writer_id"] == "writer-one"
    assert stored["generation"] == 1


def test_active_claim_index_blocks_overlapping_paths_across_objectives(
    tmp_path: Path,
) -> None:
    first = admit_work(
        AdmissionRequest(
            objective=_objective(115),
            writer_id="writer-one",
            branch="codex/issue-115-a",
            authorized_paths=("src/msos_autobuilder",),
            claim_root=tmp_path,
        )
    )
    second = admit_work(
        AdmissionRequest(
            objective=_objective(116),
            writer_id="writer-two",
            branch="codex/issue-116-b",
            authorized_paths=("src/msos_autobuilder/work_admission.py",),
            claim_root=tmp_path,
        )
    )

    assert first.claim is not None
    assert second.status == AdmissionStatus.BLOCKED_BY_OWNERSHIP_CONFLICT
    assert second.claim == first.claim
    assert second.evidence["conflict_reason"] == "overlapping_paths"


def test_ambiguous_unique_work_is_preserved_not_deleted(tmp_path: Path) -> None:
    objective = _objective(115)
    ambiguous = candidate_from_pr(
        number=200,
        title="Add related admission docs",
        state="open",
        branch="codex/related-docs",
        linked_issue=115,
        objective_sha256=objective.objective_sha256,
        acceptance_contract_sha256="1" * 64,
        changed_paths=("src/msos_autobuilder/work_admission.py", "docs/ADMISSION.md"),
        unique_required_change=True,
    )

    decision = admit_work(
        AdmissionRequest(
            objective=objective,
            writer_id="writer-one",
            branch="codex/issue-115-work-admission",
            authorized_paths=("src/msos_autobuilder/work_admission.py",),
            claim_root=tmp_path,
            candidates=(ambiguous,),
        )
    )

    assert decision.status == AdmissionStatus.NEW_WORK_ADMITTED
    assert decision.classifications[0]["classification"] == (
        WorkClassification.PRESERVE_UNIQUE_WORK.value
    )
    assert decision.evidence["classifications"][0]["unique_required_change"] is True
