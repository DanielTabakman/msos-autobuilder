from __future__ import annotations

from msos_autobuilder.candidate_gate_revisions import _matches_revision, _safe_revision_prefix


def test_revision_job_matching_is_strict() -> None:
    prefix = _safe_revision_prefix("ppe-frozen-evaluation-contract-v1")
    assert _matches_revision("ppe-frozen-evaluation-contract-v1-revision-1", prefix)
    assert _matches_revision("ppe-frozen-evaluation-contract-v1-revision-12", prefix)
    assert not _matches_revision("ppe-frozen-evaluation-contract-v1", prefix)
    assert not _matches_revision("ppe-frozen-evaluation-contract-v1-revision-0", prefix)
    assert not _matches_revision("other-revision-1", prefix)
