"""Fail unless a candidate enforces snapshot identity at the review boundary."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_frozen_evaluation_candidate.py <candidate-root>", file=sys.stderr)
        return 2

    candidate = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(candidate))

    fixture = (
        candidate
        / "tests"
        / "fixtures"
        / "frozen_evaluation"
        / "frozen_evaluation_v1_record.json"
    )
    if fixture.exists():
        record = json.loads(fixture.read_text(encoding="utf-8"))
    else:
        from src.viz.frozen_evaluation_record import build_frozen_evaluation_record

        record = build_frozen_evaluation_record(verification={}, expiry_str="1JAN26")

    mismatched_id = "11111111-1111-4111-8111-111111111111"
    if mismatched_id == record["snapshot_id"]:
        raise AssertionError("integrity witness requires distinct snapshot IDs")

    try:
        try:
            from src.viz.frozen_evaluation_contract import (
                FrozenEvaluationContractError as ContractError,
                build_snapshot_review_payload,
            )

            build_snapshot_review_payload(
                snapshot_id=mismatched_id,
                created_at=record.get("created_at_utc"),
                expiry=record.get("expiry"),
                summary_line="identity-integrity-witness",
                record=record,
                review=None,
            )
        except ModuleNotFoundError:
            from src.viz.frozen_evaluation_record import (
                build_snapshot_review_payload,
                validate_snapshot_review_payload,
            )

            payload = build_snapshot_review_payload(record=record, review=None)
            payload["snapshot_id"] = mismatched_id
            validate_snapshot_review_payload(payload)
    except (ValueError, ContractError if "ContractError" in locals() else ValueError):
        print("snapshot identity mismatch rejected")
        return 0

    print(
        "snapshot identity mismatch was accepted; outer snapshot_id must match record_header",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
