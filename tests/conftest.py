from __future__ import annotations

from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def hermetic_repository_test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run tests from the repository root with deterministic Git line endings."""

    monkeypatch.chdir(REPOSITORY_ROOT)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
