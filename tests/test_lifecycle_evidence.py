from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from msos_autobuilder.lifecycle_evidence import (
    ENVELOPE_PATH_PARTS,
    HEAD_PATH_PARTS,
    LifecycleEvidenceError,
    SourceRef,
    attempt_identity,
    attempt_identity_from_job_yaml,
    canonical_json_bytes,
    emit_lifecycle_evidence,
    identity_digest,
    latest_evidence_set_sha256_v1,
    producer_envelope_path,
    producer_head_path,
    validate_producer_head,
    work_item_source_bytes_from_snapshot_json,
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


def test_work_item_digest_uses_exact_source_bytes_boundary() -> None:
    pretty = (
        '{\n  "pipelines": [\n    {"ready_work": [\n'
        '      {"work_item_id": "A", "title": "Alpha", "state": "READY_TO_BUILD",'
        ' "trace": "plan.json", "evidence": "manual", "spaces": "  kept  "}\n'
        "    ]}\n  ]\n}\n"
    )
    source = work_item_source_bytes_from_snapshot_json(pretty, "A")

    assert source == (
        b'{"work_item_id": "A", "title": "Alpha", "state": "READY_TO_BUILD",'
        b' "trace": "plan.json", "evidence": "manual", "spaces": "  kept  "}'
    )
    assert work_item_source_sha256_v1(source) == work_item_source_sha256_v1(
        b'{"work_item_id": "A", "title": "Alpha", "state": "READY_TO_BUILD",'
        b' "trace": "plan.json", "evidence": "manual", "spaces": "  kept  "}'
    )
    assert pretty.encode("utf-8").find(source) > 0


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
    assert not Path(head["envelope_path"]).is_absolute()
    assert first.envelope_path.read_bytes()


def test_replay_rejects_default_wall_clock_and_validates_source_ref(tmp_path: Path) -> None:
    identity = _identity()
    source_ref = SourceRef(
        repository="git@example.invalid/repo.git",
        ref="jobs",
        commit="a" * 40,
        path="jobs/approved/job.yaml",
        sha256="b" * 64,
    )
    first = emit_lifecycle_evidence(
        tmp_path,
        evidence_kind="dispatch.submitted",
        identity=identity,
        source_ref=source_ref,
        payload={
            "feed_commit": "a" * 40,
            "feed_path": "jobs/approved/job.yaml",
            "submitted_job_sha256": "b" * 64,
        },
        final=True,
        closed_status="final",
        observed_at="2026-07-28T00:00:00+00:00",
    )
    replay = emit_lifecycle_evidence(
        tmp_path,
        evidence_kind="dispatch.submitted",
        identity=identity,
        source_ref=source_ref,
        payload={
            "feed_commit": "a" * 40,
            "feed_path": "jobs/approved/job.yaml",
            "submitted_job_sha256": "b" * 64,
        },
        final=True,
        closed_status="final",
        observed_at="2026-07-28T00:00:00+00:00",
    )
    with pytest.raises(TypeError):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="dispatch.submitted",
            identity=identity,
            source_ref=source_ref,
            payload={
                "feed_commit": "a" * 40,
                "feed_path": "jobs/approved/job.yaml",
                "submitted_job_sha256": "b" * 64,
            },
            final=True,
            closed_status="final",
        )

    assert replay == first


def test_latest_evidence_set_rejects_malformed_heads(tmp_path: Path) -> None:
    identity = _identity()
    source = _source(tmp_path)
    result = emit_lifecycle_evidence(
        tmp_path,
        evidence_kind="host.execution",
        identity=identity,
        source_path=source,
        payload={"execution_outcome": "completed"},
        final=True,
        closed_status="final",
        observed_at="2026-07-28T00:00:00Z",
    )
    head = json.loads(result.head_path.read_text(encoding="utf-8"))
    validate_producer_head(head, host_root=tmp_path)
    malformed = dict(head)
    malformed["envelope_path"] = str(result.envelope_path.resolve())

    with pytest.raises(LifecycleEvidenceError, match="host-root-relative"):
        latest_evidence_set_sha256_v1([malformed])


def test_regression_gap_conflict_and_post_final_mutation_fail_closed(tmp_path: Path) -> None:
    identity = _identity()
    source = _source(tmp_path)
    emit_lifecycle_evidence(
        tmp_path,
        evidence_kind="gate.validation",
        identity=identity,
        source_path=source,
        payload={
            "validation_outcome": "failed",
            "gate_report_sha256": "0" * 64,
            "results_commit": "a" * 40,
        },
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
            payload={
                "validation_outcome": "passed",
                "gate_report_sha256": "1" * 64,
                "results_commit": "b" * 40,
            },
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
            payload={
                "validation_outcome": "passed",
                "gate_report_sha256": "1" * 64,
                "results_commit": "b" * 40,
            },
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
        payload={
            "validation_outcome": "failed",
            "gate_report_sha256": "2" * 64,
            "results_commit": "c" * 40,
        },
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
            payload={
                "validation_outcome": "failed",
                "gate_report_sha256": "2" * 64,
                "results_commit": "d" * 40,
            },
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
            payload={
                "validation_outcome": "failed",
                "gate_report_sha256": "2" * 64,
                "results_commit": "c" * 40,
            },
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
