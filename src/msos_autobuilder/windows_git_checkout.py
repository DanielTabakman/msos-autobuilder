"""Windows-safe git checkout path selection and environment helpers.

Prefer short ``state/<short>`` checkout roots under long isolated host paths and
enable ``core.longpaths`` on git invocations, matching the Issue #126/PR #127
evidence-relay contract for revision-loop and candidate-gate results checkouts.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

REVISION_RESULTS_SHORT = "rl-repo"
REVISION_RESULTS_LEGACY = "revision-loop-results-repo"
CANDIDATE_RESULTS_SHORT = "cg-repo"
CANDIDATE_RESULTS_LEGACY = "candidate-gate-results-repo"


def prefer_checkout(state_root: Path, short_name: str, legacy_name: str) -> Path:
    """Return the preferred checkout directory under ``state_root``.

    New checkouts use the short name. An existing legacy checkout with a
    ``.git`` directory continues to be reused so production hosts are not
    forcibly migrated.
    """

    state = Path(state_root)
    short = state / short_name
    legacy = state / legacy_name
    if (short / ".git").exists():
        return short
    if (legacy / ".git").exists():
        return legacy
    return short


def revision_results_checkout(state_root: Path) -> Path:
    return prefer_checkout(state_root, REVISION_RESULTS_SHORT, REVISION_RESULTS_LEGACY)


def candidate_results_checkout(state_root: Path) -> Path:
    return prefer_checkout(state_root, CANDIDATE_RESULTS_SHORT, CANDIDATE_RESULTS_LEGACY)


def remove_git_tree(path: Path, *, ignore_errors: bool = False) -> None:
    """Remove a directory tree that may hold read-only git objects.

    Git marks pack files read-only, and on Windows ``shutil.rmtree`` cannot
    unlink a read-only file. A leftover checkout is therefore undeletable by a
    plain rmtree, so a disposable workspace survives its own cleanup and the
    next run fails with ``PermissionError`` instead of recloning from scratch.
    """

    target = Path(path)
    if not target.exists():
        return
    for child in target.rglob("*"):
        try:
            child.chmod(child.stat().st_mode | stat.S_IWRITE)
        except OSError:
            # Leave it to rmtree, which reports or ignores per ignore_errors.
            continue
    shutil.rmtree(target, ignore_errors=ignore_errors)


def git_environment() -> dict[str, str]:
    """Process environment for git with autocrlf disabled and longpaths enabled.

    Inherited ``GIT_TERMINAL_PROMPT`` is preserved so foreground installers can
    keep prompting enabled while scheduled wrappers keep prompting disabled.
    """

    environment = dict(os.environ)
    environment["GIT_CONFIG_COUNT"] = "2"
    environment["GIT_CONFIG_KEY_0"] = "core.autocrlf"
    environment["GIT_CONFIG_VALUE_0"] = "false"
    environment["GIT_CONFIG_KEY_1"] = "core.longpaths"
    environment["GIT_CONFIG_VALUE_1"] = "true"
    return environment
