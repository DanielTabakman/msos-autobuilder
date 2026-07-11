"""Synthetic stdin worker used only to prove isolated concurrent process execution."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def main() -> int:
    payload = json.load(sys.stdin)
    target = Path(str(payload["target"]))
    if target.is_absolute() or ".." in target.parts:
        raise ValueError("target must be a safe relative path")

    barrier_dir = Path(str(payload["barrier_dir"]))
    lane_id = str(payload["lane_id"])
    parties = int(payload.get("parties", 1))
    timeout_seconds = float(payload.get("barrier_timeout_seconds", 10))
    barrier_dir.mkdir(parents=True, exist_ok=True)
    (barrier_dir / f"{lane_id}.ready").write_text("ready\n", encoding="utf-8")

    deadline = time.monotonic() + timeout_seconds
    while len(list(barrier_dir.glob("*.ready"))) < parties:
        if time.monotonic() >= deadline:
            raise TimeoutError("parallel worker barrier timed out")
        time.sleep(0.02)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(payload["content"]), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
