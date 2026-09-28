"""Check the interpreter against the governed Windows release policy before installation."""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
from pathlib import Path


def check(policy_path: Path) -> dict[str, object]:
    raw = policy_path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[1-9][0-9]*\.[0-9]+", raw):
        raise ValueError(f"invalid Python runtime policy in {policy_path}")
    actual = f"{sys.version_info.major}.{sys.version_info.minor}"
    return {
        "status": "PASS" if raw == actual else "BLOCKED",
        "expected_python": raw,
        "actual_python": platform.python_version(),
        "architecture": platform.machine(),
        "platform": sys.platform,
        "policy": str(policy_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("policy", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = check(args.policy)
    except (OSError, UnicodeError, ValueError) as exc:
        result = {"status": "BLOCKED", "error": str(exc)}
    payload = json.dumps(result, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
