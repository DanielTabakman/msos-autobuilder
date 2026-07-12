"""Windows-safe entry point for the candidate gate.

Git on Windows may materialize text patches with CRLF line endings even though the
relay hashes canonical LF bytes. This wrapper keeps the strict hash contract while
normalizing only patch-file line endings for hashing and `git apply`.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Sequence
from pathlib import Path

from . import candidate_gate as gate

_ORIGINAL_RUN_GIT = gate._run_git


def _canonical_patch_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def _sha256_file(path: Path) -> str:
    if path.suffix.lower() == ".patch":
        return hashlib.sha256(_canonical_patch_bytes(path)).hexdigest()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_git(
    repo: Path | None,
    *args: str,
    accepted: tuple[int, ...] = (0,),
):
    if args and args[0] == "apply" and args[-1].lower().endswith(".patch"):
        source = Path(args[-1])
        canonical = _canonical_patch_bytes(source)
        with tempfile.NamedTemporaryFile(suffix=".patch", delete=False) as handle:
            handle.write(canonical)
            temporary = Path(handle.name)
        try:
            return _ORIGINAL_RUN_GIT(repo, *args[:-1], str(temporary), accepted=accepted)
        finally:
            temporary.unlink(missing_ok=True)
    return _ORIGINAL_RUN_GIT(repo, *args, accepted=accepted)


def main(argv: Sequence[str] | None = None) -> int:
    gate._sha256_file = _sha256_file
    gate._run_git = _run_git
    return gate.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
