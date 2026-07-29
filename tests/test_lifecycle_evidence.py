from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import msos_autobuilder.lifecycle_evidence as lifecycle
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
        payload={
            "execution_outcome": "completed",
            "host_archive_path": "queue/completed/build-next-ppe-A",
            "error_class": None,
        },
        final=True,
        closed_status="final",
        observed_at="2026-07-28T00:00:00Z",
    )
    replay = emit_lifecycle_evidence(
        tmp_path,
        evidence_kind="host.execution",
        identity=identity,
        source_path=source,
        payload={
            "execution_outcome": "completed",
            "host_archive_path": "queue/completed/build-next-ppe-A",
            "error_class": None,
        },
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
        payload={
            "execution_outcome": "completed",
            "host_archive_path": "queue/completed/build-next-ppe-A",
            "error_class": None,
        },
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
        evidence_kind="host.execution",
        identity=identity,
        source_path=source,
        payload={"execution_outcome": "imported"},
        producer_sequence=1,
        final=False,
        closed_status="open",
        observed_at="2026-07-28T00:00:00Z",
    )
    with pytest.raises(LifecycleEvidenceError, match="sequence gap"):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="host.execution",
            identity=identity,
            source_path=source,
            payload={"execution_outcome": "pending"},
            producer_sequence=3,
            final=False,
            closed_status="open",
            observed_at="2026-07-28T00:00:01Z",
        )
    with pytest.raises(LifecycleEvidenceError, match="conflicting same-sequence"):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="host.execution",
            identity=identity,
            source_path=source,
            payload={"execution_outcome": "pending"},
            producer_sequence=1,
            final=False,
            closed_status="open",
            observed_at="2026-07-28T00:00:02Z",
        )
    emit_lifecycle_evidence(
        tmp_path,
        evidence_kind="host.execution",
        identity=identity,
        source_path=source,
        payload={
            "execution_outcome": "completed",
            "host_archive_path": "queue/completed/build-next-ppe-A",
            "error_class": None,
        },
        producer_sequence=2,
        final=True,
        closed_status="final",
        observed_at="2026-07-28T00:00:03Z",
    )
    with pytest.raises(LifecycleEvidenceError, match="closed producer stream mutated"):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="host.execution",
            identity=identity,
            source_path=source,
            payload={
                "execution_outcome": "completed",
                "host_archive_path": "queue/completed/other",
                "error_class": None,
            },
            producer_sequence=2,
            final=True,
            closed_status="final",
            observed_at="2026-07-28T00:00:04Z",
        )
    with pytest.raises(LifecycleEvidenceError, match="closed producer stream mutated"):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="host.execution",
            identity=identity,
            source_path=source,
            payload={
                "execution_outcome": "completed",
                "host_archive_path": "queue/completed/build-next-ppe-A",
                "error_class": None,
            },
            producer_sequence=1,
            final=True,
            closed_status="final",
            observed_at="2026-07-28T00:00:05Z",
        )


def test_initial_producer_sequence_must_start_at_one(tmp_path: Path) -> None:
    identity = _identity()
    source = _source(tmp_path)

    with pytest.raises(LifecycleEvidenceError, match="producer sequence gap"):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="gate.validation",
            identity=identity,
            source_path=source,
            payload={
                "validation_outcome": "failed",
                "validation_state": "candidate_failed",
                "validation_contract_sha256": "5" * 64,
                "gate_report_sha256": "0" * 64,
                "results_commit": "a" * 40,
            },
            producer_sequence=2,
            final=True,
            closed_status="final",
            observed_at="2026-07-28T00:00:00Z",
        )

    assert not producer_head_path(
        tmp_path,
        evidence_kind="gate.validation",
        identity=identity,
    ).exists()

    with pytest.raises(LifecycleEvidenceError, match="producer_sequence must be an integer"):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="gate.validation",
            identity=identity,
            source_path=source,
            payload={
                "validation_outcome": "failed",
                "validation_state": "candidate_failed",
                "validation_contract_sha256": "5" * 64,
                "gate_report_sha256": "0" * 64,
                "results_commit": "a" * 40,
            },
            producer_sequence=True,
            final=True,
            closed_status="final",
            observed_at="2026-07-28T00:00:00Z",
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

    path.write_text("[", encoding="utf-8")
    with pytest.raises(LifecycleEvidenceError, match="job YAML is invalid"):
        attempt_identity_from_job_yaml(path)

    path.write_text("- ordinary\n", encoding="utf-8")
    with pytest.raises(LifecycleEvidenceError, match="job YAML must be a mapping"):
        attempt_identity_from_job_yaml(path)

    job["founder_build_next"]["refill_attempt"]["attempt_ordinal"] = "not-an-int"
    path.write_text(yaml.safe_dump(job, sort_keys=True), encoding="utf-8")
    with pytest.raises(LifecycleEvidenceError, match="refill attempt identity is malformed"):
        attempt_identity_from_job_yaml(path)


def test_observational_failures_are_normalized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identity = _identity()
    source = _source(tmp_path)
    original_read_bytes = Path.read_bytes

    def deny_source_hash(path: Path) -> bytes:
        if path == source:
            raise PermissionError("hash denied")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", deny_source_hash)
    with pytest.raises(LifecycleEvidenceError, match="lifecycle evidence operation failed"):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="host.execution",
            identity=identity,
            source_path=source,
            payload={"execution_outcome": "running"},
            final=False,
            closed_status="open",
            observed_at="2026-07-28T00:00:00Z",
        )

    monkeypatch.setattr(Path, "read_bytes", original_read_bytes)
    monkeypatch.setattr(
        lifecycle,
        "_write_immutable",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("write denied")),
    )
    with pytest.raises(LifecycleEvidenceError, match="lifecycle evidence operation failed"):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="host.execution",
            identity=identity,
            source_path=source,
            payload={"execution_outcome": "running"},
            final=False,
            closed_status="open",
            observed_at="2026-07-28T00:00:00Z",
        )


def test_head_failures_are_normalized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    identity = _identity()
    source = _source(tmp_path)
    head_path = producer_head_path(tmp_path, evidence_kind="host.execution", identity=identity)
    head_path.parent.mkdir(parents=True)
    head_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(LifecycleEvidenceError, match="lifecycle evidence operation failed"):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="host.execution",
            identity=identity,
            source_path=source,
            payload={"execution_outcome": "running"},
            final=False,
            closed_status="open",
            observed_at="2026-07-28T00:00:00Z",
        )

    head_path.unlink()
    monkeypatch.setattr(
        lifecycle,
        "_replace_head",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("replace denied")),
    )
    with pytest.raises(LifecycleEvidenceError, match="lifecycle evidence operation failed"):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="host.execution",
            identity=identity,
            source_path=source,
            payload={"execution_outcome": "running"},
            final=False,
            closed_status="open",
            observed_at="2026-07-28T00:00:00Z",
        )


def test_diagnostic_write_failure_does_not_escape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        lifecycle,
        "_atomic_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("diag denied")),
    )

    assert (
        lifecycle.record_producer_evidence_error(
            tmp_path,
            producer="persistent_host",
            evidence_kind="host.execution",
            error=PermissionError("primary evidence failed"),
            identity=_identity(),
            primary_outcome={"outcome": "completed"},
        )
        is None
    )


def test_strict_v1_schema_rejects_duplicate_missing_path_and_bad_formats(tmp_path: Path) -> None:
    identity = _identity()
    source = _source(tmp_path)
    result = emit_lifecycle_evidence(
        tmp_path,
        evidence_kind="host.execution",
        identity=identity,
        source_path=source,
        payload={"execution_outcome": "running"},
        final=False,
        closed_status="open",
        observed_at="2026-07-28T00:00:00Z",
    )
    head = json.loads(result.head_path.read_text(encoding="utf-8"))

    with pytest.raises(LifecycleEvidenceError, match="duplicate producer head"):
        latest_evidence_set_sha256_v1([head, dict(head)])

    result.envelope_path.unlink()
    with pytest.raises(LifecycleEvidenceError, match="envelope file is missing"):
        validate_producer_head(head, host_root=tmp_path)

    bad = dict(head)
    bad["evidence_id"] = "G" * 32
    with pytest.raises(LifecycleEvidenceError, match="evidence_id"):
        validate_producer_head(bad)

    with pytest.raises(LifecycleEvidenceError, match="observed_at"):
        emit_lifecycle_evidence(
            tmp_path,
            evidence_kind="host.execution",
            identity=identity,
            source_path=source,
            payload={"execution_outcome": "running"},
            final=False,
            closed_status="open",
            observed_at="not-a-timestamp",
        )


@pytest.mark.parametrize(
    ("kind", "payload", "final", "closed_status"),
    [
        (
            "dispatch.prepared",
            {
                "selected_work_item": {
                    "pipeline_id": "ppe",
                    "work_item_id": "A",
                    "work_item_source_sha256_v1": "1" * 64,
                },
                "generation_id": "refill-12345678",
                "dispatch_intent_sha256": "2" * 64,
                "capacity_slot": {
                    "slot_id": "capacity-one",
                    "desired_capacity": 1,
                    "active_running": 0,
                    "active_queued": 0,
                },
            },
            True,
            "final",
        ),
        ("host.execution", {"execution_outcome": "imported"}, False, "open"),
        ("host.execution", {"execution_outcome": "pending"}, False, "open"),
        ("host.execution", {"execution_outcome": "running"}, False, "open"),
        (
            "host.execution",
            {
                "execution_outcome": "completed",
                "host_archive_path": "queue/completed/job",
                "error_class": None,
            },
            True,
            "final",
        ),
        (
            "host.execution",
            {
                "execution_outcome": "failed",
                "host_archive_path": "queue/failed/job",
                "error_class": "RuntimeError",
            },
            True,
            "final",
        ),
        (
            "relay.result",
            {
                "relay_disposition": "relayed",
                "relayed_commit": "a" * 40,
                "canonical_report_sha256": "3" * 64,
                "source_report_sha256": "4" * 64,
                "complete_patch_reconstruction": True,
            },
            True,
            "final",
        ),
        (
            "relay.result",
            {
                "relay_disposition": "not_applicable",
                "relayed_commit": None,
                "canonical_report_sha256": None,
                "source_report_sha256": None,
                "complete_patch_reconstruction": False,
            },
            True,
            "not_applicable",
        ),
        (
            "gate.validation",
            {
                "validation_outcome": "passed",
                "validation_state": "candidate_passed",
                "validation_contract_sha256": "5" * 64,
                "gate_report_sha256": "6" * 64,
                "results_commit": "b" * 40,
            },
            True,
            "final",
        ),
        (
            "gate.validation",
            {
                "validation_outcome": "failed",
                "validation_state": "candidate_failed",
                "validation_contract_sha256": "7" * 64,
                "gate_report_sha256": "7" * 64,
                "results_commit": "c" * 40,
            },
            True,
            "final",
        ),
        (
            "revision.disposition",
            {
                "revision_disposition": "queued",
                "descendant_job_id": "job-revision-1",
                "gate_report_sha256": "8" * 64,
                "jobs_commit": "d" * 40,
            },
            True,
            "final",
        ),
        (
            "revision.disposition",
            {
                "revision_disposition": "not_applicable",
                "descendant_job_id": None,
                "gate_report_sha256": "9" * 64,
                "jobs_commit": None,
            },
            True,
            "not_applicable",
        ),
        (
            "publication_review.disposition",
            {
                "publication_review_disposition": "drafted",
                "reason_code": "publication_review.drafted.v1",
                "draft_pr": "https://github.example/pull/1",
                "product_branch": "autobuilder/job",
                "product_commit": "e" * 40,
                "results_commit": "f" * 40,
            },
            True,
            "final",
        ),
        (
            "publication_review.disposition",
            {
                "publication_review_disposition": "not_applicable",
                "draft_pr": None,
                "product_branch": None,
                "product_commit": None,
                "results_commit": None,
            },
            True,
            "not_applicable",
        ),
    ],
)
def test_required_producer_outcomes_emit_verifiable_bytes_and_replay(
    tmp_path: Path,
    kind: str,
    payload: dict[str, object],
    final: bool,
    closed_status: str,
) -> None:
    identity = _identity()
    if kind == "dispatch.prepared":
        payload = {
            **payload,
            "selected_work_item": {
                **payload["selected_work_item"],  # type: ignore[index]
                "work_item_source_sha256_v1": identity["work_item_digest"],
            },
        }
    source = _source(tmp_path, text=f"{kind}\n{closed_status}\n")

    first = emit_lifecycle_evidence(
        tmp_path,
        evidence_kind=kind,
        identity=identity,
        source_path=source,
        payload=payload,
        final=final,
        closed_status=closed_status,
        observed_at="2026-07-28T00:00:00Z",
    )
    replay = emit_lifecycle_evidence(
        tmp_path,
        evidence_kind=kind,
        identity=identity,
        source_path=source,
        payload=payload,
        final=final,
        closed_status=closed_status,
        observed_at="2026-07-28T00:00:00Z",
    )

    assert replay == first
    envelope = json.loads(first.envelope_path.read_text(encoding="utf-8"))
    head = json.loads(first.head_path.read_text(encoding="utf-8"))
    assert head["evidence_kind"] == kind
    assert envelope["payload"] == payload
    assert head["envelope_sha256"] == lifecycle.sha256_file(first.envelope_path)
    validate_producer_head(head, envelope=envelope, host_root=tmp_path)


def test_canonical_semantic_payload_validation_rejects_impossible_combinations(
    tmp_path: Path,
) -> None:
    identity = _identity()
    source = _source(tmp_path)
    source_ref = SourceRef(
        repository="git@example.invalid/repo.git",
        ref="jobs",
        commit="a" * 40,
        path="jobs/approved/job.yaml",
        sha256="b" * 64,
    )
    prepared = {
        "selected_work_item": {
            "pipeline_id": identity["pipeline_id"],
            "work_item_id": identity["work_item_id"],
            "work_item_source_sha256_v1": identity["work_item_digest"],
        },
        "generation_id": identity["generation_id"],
        "dispatch_intent_sha256": "2" * 64,
        "capacity_slot": {
            "slot_id": "capacity-one",
            "desired_capacity": 1,
            "active_running": 0,
            "active_queued": 0,
        },
    }
    submitted = {
        "feed_commit": source_ref.commit,
        "feed_path": source_ref.path,
        "submitted_job_sha256": source_ref.sha256,
    }

    cases = [
        (
            "dispatch.prepared",
            {**prepared, "generation_id": "other-generation"},
            True,
            "final",
            "attempt identity",
            None,
        ),
        (
            "dispatch.prepared",
            {
                **prepared,
                "capacity_slot": {**prepared["capacity_slot"], "active_running": -1},
            },
            True,
            "final",
            "non-negative",
            None,
        ),
        (
            "dispatch.prepared",
            {
                **prepared,
                "capacity_slot": {**prepared["capacity_slot"], "desired_capacity": 2},
            },
            True,
            "final",
            "desired_capacity",
            None,
        ),
        (
            "dispatch.submitted",
            {**submitted, "feed_commit": "c" * 40},
            True,
            "final",
            "source_ref",
            source_ref,
        ),
        (
            "host.execution",
            {"execution_outcome": "imported"},
            True,
            "final",
            "finality",
            None,
        ),
        (
            "host.execution",
            {"execution_outcome": "completed"},
            True,
            "final",
            "archive path",
            None,
        ),
        (
            "host.execution",
            {"execution_outcome": "failed", "host_archive_path": "queue/failed/job"},
            True,
            "final",
            "error_class",
            None,
        ),
        (
            "relay.result",
            {
                "relay_disposition": "relayed",
                "relayed_commit": None,
                "canonical_report_sha256": None,
                "source_report_sha256": None,
                "complete_patch_reconstruction": True,
            },
            True,
            "final",
            "relay.result requires",
            None,
        ),
        (
            "relay.result",
            {
                "relay_disposition": "not_applicable",
                "relayed_commit": "a" * 40,
                "canonical_report_sha256": "3" * 64,
                "source_report_sha256": "4" * 64,
                "complete_patch_reconstruction": True,
            },
            True,
            "not_applicable",
            "prohibits relay proof",
            None,
        ),
        (
            "gate.validation",
            {
                "validation_outcome": "passed",
                "validation_state": "candidate_failed",
                "validation_contract_sha256": "5" * 64,
                "gate_report_sha256": "6" * 64,
                "results_commit": "b" * 40,
            },
            True,
            "final",
            "state is incompatible",
            None,
        ),
        (
            "gate.validation",
            {
                "validation_outcome": "failed",
                "validation_state": "candidate_failed",
                "validation_contract_sha256": "7" * 64,
                "gate_report_sha256": "7" * 64,
                "results_commit": "c" * 40,
            },
            False,
            "open",
            "finality",
            None,
        ),
        (
            "gate.validation",
            {
                "validation_outcome": "passed",
                "validation_state": "candidate_passed",
                "gate_report_sha256": "6" * 64,
                "results_commit": "b" * 40,
            },
            True,
            "final",
            "report and results proof",
            None,
        ),
        (
            "gate.validation",
            {
                "validation_outcome": "failed",
                "validation_state": "candidate_failed",
                "validation_contract_sha256": None,
                "gate_report_sha256": "7" * 64,
                "results_commit": "c" * 40,
            },
            True,
            "final",
            "report and results proof",
            None,
        ),
        (
            "gate.validation",
            {
                "validation_outcome": "passed",
                "validation_contract_sha256": "5" * 64,
                "gate_report_sha256": "6" * 64,
                "results_commit": "b" * 40,
            },
            True,
            "final",
            "state is incompatible",
            None,
        ),
        (
            "revision.disposition",
            {"revision_disposition": "queued", "gate_report_sha256": "8" * 64},
            True,
            "final",
            "descendant proof",
            None,
        ),
        (
            "revision.disposition",
            {
                "revision_disposition": "not_applicable",
                "descendant_job_id": "child",
                "gate_report_sha256": "9" * 64,
                "jobs_commit": None,
            },
            True,
            "not_applicable",
            "prohibits job proof",
            None,
        ),
        (
            "revision.disposition",
            {"revision_disposition": "exhausted", "gate_report_sha256": "9" * 64},
            True,
            "final",
            "terminal reason",
            None,
        ),
        (
            "publication_review.disposition",
            {
                "publication_review_disposition": "drafted",
                "draft_pr": "https://github.invalid/pull/1",
                "product_branch": "autobuilder/job",
                "product_commit": "e" * 40,
                "results_commit": "f" * 40,
            },
            True,
            "final",
            "drafted reason",
            None,
        ),
        (
            "publication_review.disposition",
            {
                "publication_review_disposition": "rejected",
                "reason_code": "publication_review.no_publication.not_required.v1",
            },
            True,
            "final",
            "incompatible",
            None,
        ),
        (
            "publication_review.disposition",
            {"publication_review_disposition": "terminal_no_publication"},
            True,
            "final",
            "accepted reason",
            None,
        ),
        (
            "publication_review.disposition",
            {
                "publication_review_disposition": "not_applicable",
                "reason_code": "publication_review.drafted.v1",
                "draft_pr": "https://github.invalid/pull/1",
                "product_branch": "autobuilder/job",
                "product_commit": "e" * 40,
                "results_commit": "f" * 40,
            },
            True,
            "not_applicable",
            "prohibits terminal proof",
            None,
        ),
        (
            "publication_review.disposition",
            {
                "publication_review_disposition": "awaiting_review",
                "draft_pr": "https://github.invalid/pull/1",
            },
            False,
            "open",
            "terminal",
            None,
        ),
    ]

    for index, (kind, payload, final, closed_status, match, ref) in enumerate(cases, start=1):
        with pytest.raises(LifecycleEvidenceError, match=match):
            emit_lifecycle_evidence(
                tmp_path,
                evidence_kind=kind,
                identity=identity,
                source_ref=ref,
                source_path=None if ref is not None else source,
                payload=payload,
                producer_sequence=1,
                final=final,
                closed_status=closed_status,
                observed_at=f"2026-07-28T00:00:{index:02d}Z",
            )


def _emit_prepared_and_submitted(tmp_path: Path, identity: dict[str, object]) -> None:
    source = _source(tmp_path, "dispatch\n")
    emit_lifecycle_evidence(
        tmp_path,
        evidence_kind="dispatch.prepared",
        identity=identity,
        source_path=source,
        payload={
            "selected_work_item": {
                "pipeline_id": identity["pipeline_id"],
                "work_item_id": identity["work_item_id"],
                "work_item_source_sha256_v1": identity["work_item_digest"],
            },
            "generation_id": identity["generation_id"],
            "dispatch_intent_sha256": "1" * 64,
            "capacity_slot": {
                "slot_id": "capacity-one",
                "desired_capacity": 1,
                "active_running": 0,
                "active_queued": 0,
            },
        },
        final=True,
        closed_status="final",
        observed_at="2026-07-29T12:00:00Z",
    )
    emit_lifecycle_evidence(
        tmp_path,
        evidence_kind="dispatch.submitted",
        identity=identity,
        source_ref=SourceRef(
            repository="git@example.invalid/repo.git",
            ref="jobs",
            commit="a" * 40,
            path="jobs/approved/job.yaml",
            sha256="b" * 64,
        ),
        payload={
            "feed_commit": "a" * 40,
            "feed_path": "jobs/approved/job.yaml",
            "submitted_job_sha256": "b" * 64,
        },
        final=True,
        closed_status="final",
        observed_at="2026-07-29T12:01:00Z",
    )


def test_lifecycle_transition_journal_is_ordered_hash_linked_and_replayable(
    tmp_path: Path,
) -> None:
    identity = _identity()
    digest = identity_digest(identity)
    _emit_prepared_and_submitted(tmp_path, identity)
    first = lifecycle.reduce_attempt_lifecycle(tmp_path)
    emit_lifecycle_evidence(
        tmp_path,
        evidence_kind="host.execution",
        identity=identity,
        source_path=_source(tmp_path, "host\n"),
        payload={"execution_outcome": "running"},
        final=False,
        closed_status="open",
        observed_at="2026-07-29T12:02:00Z",
    )
    second = lifecycle.reduce_attempt_lifecycle(tmp_path)

    transitions = sorted(
        (tmp_path / "state" / "attempt-lifecycle" / "transitions" / digest).glob("*.json")
    )
    assert [path.name for path in transitions] == [
        "00000000000000000001.json",
        "00000000000000000002.json",
    ]
    first_transition = json.loads(transitions[0].read_text(encoding="utf-8"))
    second_transition = json.loads(transitions[1].read_text(encoding="utf-8"))
    assert first_transition["previous_transition_sha256"] is None
    assert second_transition["previous_transition_sha256"] == first_transition[
        "transition_sha256"
    ]
    assert first["reduced"][0]["lifecycle_phase"] == "host_awaiting_import"
    assert second["reduced"][0]["lifecycle_phase"] == "host_running"

    snapshot_path = tmp_path / "state" / "attempt-lifecycle" / "attempts" / f"{digest}.json"
    watermark_path = tmp_path / "state" / "attempt-lifecycle" / "reduced-through" / f"{digest}.json"
    snapshot_path.unlink()
    watermark_path.unlink()
    classification = lifecycle.canonical_refill_classification(
        tmp_path,
        job_id=str(identity["job_id"]),
        generation_id=str(identity["generation_id"]),
    )

    assert classification is not None
    assert classification["category"] == "in_flight"
    assert snapshot_path.exists()
    assert watermark_path.exists()


def test_lifecycle_recovery_then_unchanged_reduction_does_not_append_transition(
    tmp_path: Path,
) -> None:
    identity = _identity()
    digest = identity_digest(identity)
    _emit_prepared_and_submitted(tmp_path, identity)
    lifecycle.reduce_attempt_lifecycle(tmp_path)
    transition_root = tmp_path / "state" / "attempt-lifecycle" / "transitions" / digest
    snapshot_path = tmp_path / "state" / "attempt-lifecycle" / "attempts" / f"{digest}.json"
    watermark_path = tmp_path / "state" / "attempt-lifecycle" / "reduced-through" / f"{digest}.json"
    snapshot_path.unlink()
    watermark_path.unlink()

    lifecycle.reduce_attempt_lifecycle(tmp_path)

    transitions = sorted(transition_root.glob("*.json"))
    assert [path.name for path in transitions] == ["00000000000000000001.json"]
    assert snapshot_path.exists()
    assert watermark_path.exists()


def test_lifecycle_replay_rejects_transition_digest_mismatch(tmp_path: Path) -> None:
    identity = _identity()
    digest = identity_digest(identity)
    _emit_prepared_and_submitted(tmp_path, identity)
    lifecycle.reduce_attempt_lifecycle(tmp_path)
    transition_path = (
        tmp_path
        / "state"
        / "attempt-lifecycle"
        / "transitions"
        / digest
        / "00000000000000000001.json"
    )
    transition = json.loads(transition_path.read_text(encoding="utf-8"))
    transition["snapshot"]["item_disposition"] = "corrupted"
    transition_path.write_text(json.dumps(transition, sort_keys=True) + "\n", encoding="utf-8")
    (tmp_path / "state" / "attempt-lifecycle" / "attempts" / f"{digest}.json").unlink()

    with pytest.raises(LifecycleEvidenceError, match="snapshot hash mismatch"):
        lifecycle.canonical_refill_classification(
            tmp_path,
            job_id=str(identity["job_id"]),
            generation_id=str(identity["generation_id"]),
        )
