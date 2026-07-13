"""Fail unless a candidate preserves the established frozen-evaluation write version."""

from __future__ import annotations

import sys
from pathlib import Path

EXPECTED_VERSION = "ppe_frozen_eval_v1"
REJECTED_UNAPPROVED_VERSION = "frozen_evaluation_v1"


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "usage: check_frozen_evaluation_schema_compatibility.py <candidate-root>",
            file=sys.stderr,
        )
        return 2

    candidate = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(candidate))

    from src.viz import frozen_evaluation_record as record_module

    actual = str(getattr(record_module, "PAYLOAD_SCHEMA_VERSION", ""))
    if actual != EXPECTED_VERSION:
        print(
            "canonical frozen-evaluation write version changed: "
            f"{actual!r} != {EXPECTED_VERSION!r}",
            file=sys.stderr,
        )
        return 1

    record = record_module.build_frozen_evaluation_record(
        verification={},
        expiry_str="1JAN26",
    )
    if record.get("payload_schema_version") != EXPECTED_VERSION:
        print("newly built record does not preserve ppe_frozen_eval_v1", file=sys.stderr)
        return 1

    validator = getattr(record_module, "validate_frozen_evaluation_record", None)
    if validator is None:
        try:
            from src.viz.frozen_evaluation_contract import validate_frozen_evaluation_record

            validator = validate_frozen_evaluation_record
        except ModuleNotFoundError:
            print("candidate has no frozen-evaluation boundary validator", file=sys.stderr)
            return 1

    unsupported = dict(record)
    unsupported["payload_schema_version"] = REJECTED_UNAPPROVED_VERSION
    try:
        validator(unsupported)
    except ValueError:
        print("frozen-evaluation schema compatibility preserved")
        return 0

    print("unapproved frozen_evaluation_v1 write version was accepted", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
