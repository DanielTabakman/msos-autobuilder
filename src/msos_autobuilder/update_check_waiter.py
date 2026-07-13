"""Wait for required checks on an approved exact-commit update manifest."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from .self_update_supervisor import (
    GitHubCommitStatusVerifier,
    SupervisorError,
    UpdateManifest,
    parse_update_manifest,
)


class CommitStatusVerifier(Protocol):
    def verify(self, repository: str, commit: str, contexts: Sequence[str]) -> None: ...


class UpdateCheckTimeout(SupervisorError):
    """Raised when required exact-commit checks do not become successful in time."""


def wait_for_update_checks(
    manifest: UpdateManifest,
    *,
    verifier: CommitStatusVerifier,
    timeout_seconds: float = 900.0,
    poll_seconds: float = 10.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("timeout_seconds and poll_seconds must be positive")
    deadline = monotonic() + timeout_seconds
    last_error: SupervisorError | None = None
    while True:
        try:
            verifier.verify(
                manifest.repository,
                manifest.commit,
                manifest.required_status_contexts,
            )
            return
        except SupervisorError as exc:
            last_error = exc
        remaining = deadline - monotonic()
        if remaining <= 0:
            detail = str(last_error or "required checks did not become successful")
            raise UpdateCheckTimeout(
                f"timed out waiting for exact-commit update checks: {detail}"
            ) from last_error
        sleep(min(poll_seconds, remaining))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    manifest = parse_update_manifest(Path(args.manifest).read_text(encoding="utf-8"))
    token = os.environ.get(args.token_env, "") or None
    wait_for_update_checks(
        manifest,
        verifier=GitHubCommitStatusVerifier(token=token),
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )
    print(
        json.dumps(
            {
                "status": "successful",
                "repository": manifest.repository,
                "commit": manifest.commit,
                "required_status_contexts": list(manifest.required_status_contexts),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
