from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from msos_autobuilder.lifecycle_evidence import (
    ENVELOPE_PATH_PARTS,
    HEAD_PATH_PARTS,
    LifecycleEvidenceError,
    attempt_identity,
    attempt_identity_from_job_yaml,
    canonical_json_bytes,
    emit_lifecycle_evidence,
    identity_digest,
    latest_evidence_set_sha256_v1,
    producer_envelope_path,
    producer_head_path,
    work_item_source_sha256_v1,
)


def _identity() -> dict[str, object]:
    return attempt_identity(
        pipeline_id="ppe",
        work_item_id="A",
        work_item_digest=work_item_source_sha256_v1(b"title: A\r\nstate: READY\r"),
        generation_id="refill-12345678",
        job_id="build-next-ppe-A",
        attempt_ordinal=1,
        retry_ordinal=0,
    )


def _source(tmp_path: Path, text: str = "source\n") -> Path:
    path = tmp_path / "source.json"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_canonical_json_and_work_item_digest_are_platform_stable() -> None:
    assert canonical_json_bytes({"b": True, "a": 1}) == b'{"a":1,"b":true}'
    assert work_item_source_sha256_v1(b"a\r\nb\rc\n") == work_item_source_sha256_v1(
        b"a\nb\nc\n"
    )


def test_exact_producer_paths_never_enter_attempt_lifecycle(tmp_path: Path) -> None:
    identity = _identity()
    digest = identity_digest(identity)
    expected_envelope_roots = {
        "dispatch.prepared": "state/refill-evidence/dispatch/prepared",
        "dispatch.submitted": "state/refill-evidence/dispatch/submitted",
        "host.execution": "state/host-evidence/execution",
        "relay.result": "state/relay-evidence/result",
        "gate.validation": "state/gate-evidence/validation",
        "revision.disposition": "state/revision-evidence/disposition",
        "publication_review.disposition": "state/publisher-evidence/publication-review",
    }
    expected_head_roots = {
        "dispatch.prepared": "state/refill-evidence/heads/dispatch/prepared",
        "dispatch.submitted": "state/refill-evidence/heads/dispatch/submitted",
        "host.execution": "state/host-evidence/heads/execution",
        "relay.result": "state/relay-evidence/heads/result",
        "gate.validation": "state/gate-evidence/heads/validation",
        "revision.disposition": "state/revision-evidence/heads/disposition",
        "publication_review.disposition": "state/publisher-evidence/heads/publication-review",
    }

    envelope_roots = {kind: "/".join(parts) for kind, parts in ENVELOPE_PATH_PARTS.items()}
    head_roots = {kind: "/".join(parts) for kind, parts in HEAD_PATH_PARTS.items()}
    assert envelope_roots == expected_envelope_roots
    assert head_roots == expected_head_roots

    for kind in expected_envelope_roots:
        envelope = producer_envelope_path(
            tmp_path,
            evidence_kind=kind,
            identity=identity,
            evidence_id="evidence",
        )
        head = producer_head_path(tmp_path, evidence_kind=kind, identity=identity)
        assert "state/attempt-lifecycle" not in envelope.as_posix()
        assert "state/attempt-lifecycle" not in head.as_posix()
        assert envelope.as_posix().endswith(f"{digest}.evidence.json")
        assert head.as_posix().endswith(f"{digest}.json")


def test_emit_lifecycle_evidence_replay_and_head_digest(tmp_path: Path) -> None:
    identity = _identity()
    source = _source(tmp_path)
    first = emit_lifecycle_evidence(
        tmp_path,
        evidence_kind="host.execution",
        identity=identity,
        source_path=source,
        payload={"execution_outcome": "completed"},
        final=True,
        closed_status="final",
        observed_at="2026-07-28T00:00:00Z",
    )
    replay = emit_lifecycle_evidence(
        tmp_path,
        evidence_kind="host.execution",
        identity=identity,
        source_path=source,
        payload={"execution_outcome": "completed"},
        final=True,
        closed_status="final",
        observed_at="2026-07-28T00:00:00Z",
    )

    assert replay == first
    head = json.loads(first.head_path.read_text(encoding="utf-8"))
    assert latest_evidence_set_sha256_v1([head]) == latest_evidence_set_sha256_v1([dict(head)])
    assert first.envelope_path.read_bytes()


def test_regression_gap_conflict_and_post_final_mutation_fail_closed(tmp_path: Path) -> None:
    identity = _identity()
    source = _source(tmp_path)
    emit_lifecycle_evidence(
        tmp_path,
        evidence_kind="gate.validation",
        identity=identity,
        source_path=source,
        payload={"validation_outcome": "failed"},
        producer_sequence=1,
        final=False,
        closed_status="open",
        observed_at="2026-07-28T00:00:00Z",
    )
    with pytest.raises(LifecycleEvidenceError, match="sequence gap"):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="gate.validation",
            identity=identity,
            source_path=source,
            payload={"validation_outcome": "passed"},
            producer_sequence=3,
            final=True,
            closed_status="final",
            observed_at="2026-07-28T00:00:01Z",
        )
    with pytest.raises(LifecycleEvidenceError, match="conflicting same-sequence"):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="gate.validation",
            identity=identity,
            source_path=source,
            payload={"validation_outcome": "passed"},
            producer_sequence=1,
            final=False,
            closed_status="open",
            observed_at="2026-07-28T00:00:02Z",
        )
    emit_lifecycle_evidence(
        tmp_path,
        evidence_kind="gate.validation",
        identity=identity,
        source_path=source,
        payload={"validation_outcome": "failed", "final": True},
        producer_sequence=2,
        final=True,
        closed_status="final",
        observed_at="2026-07-28T00:00:03Z",
    )
    with pytest.raises(LifecycleEvidenceError, match="closed producer stream mutated"):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="gate.validation",
            identity=identity,
            source_path=source,
            payload={"validation_outcome": "failed", "final": True, "extra": True},
            producer_sequence=2,
            final=True,
            closed_status="final",
            observed_at="2026-07-28T00:00:04Z",
        )
    with pytest.raises(LifecycleEvidenceError, match="closed producer stream mutated"):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="gate.validation",
            identity=identity,
            source_path=source,
            payload={"validation_outcome": "failed", "final": True},
            producer_sequence=1,
            final=True,
            closed_status="final",
            observed_at="2026-07-28T00:00:05Z",
        )


def test_attempt_identity_from_job_yaml_requires_refill_metadata(tmp_path: Path) -> None:
    identity = _identity()
    job = {
        "version": 1,
        "job_id": identity["job_id"],
        "founder_build_next": {
            "pipeline_id": identity["pipeline_id"],
            "work_item_id": identity["work_item_id"],
            "work_item_source_sha256_v1": identity["work_item_digest"],
            "refill_attempt": {
                "generation_id": identity["generation_id"],
                "attempt_ordinal": identity["attempt_ordinal"],
                "retry_ordinal": identity["retry_ordinal"],
            },
        },
    }
    path = tmp_path / "job.yaml"
    path.write_text(yaml.safe_dump(job, sort_keys=True), encoding="utf-8")

    assert attempt_identity_from_job_yaml(path) == identity

    path.write_text(yaml.safe_dump({"job_id": "ordinary"}, sort_keys=True), encoding="utf-8")
    assert attempt_identity_from_job_yaml(path) is None
