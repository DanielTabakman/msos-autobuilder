from __future__ import annotations

from dataclasses import replace

import pytest

from msos_autobuilder.self_update_supervisor import (
    ExpectedFile,
    SupervisorError,
    UpdateManifest,
)
from msos_autobuilder.update_check_waiter import (
    UpdateCheckTimeout,
    wait_for_update_checks,
)


class SequenceVerifier:
    def __init__(self, outcomes: list[Exception | None]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def verify(self, repository: str, commit: str, contexts: tuple[str, ...]) -> None:
        assert repository == "DanielTabakman/msos-autobuilder"
        assert commit == "a" * 40
        assert contexts == ("test",)
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if outcome is not None:
            raise outcome


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _manifest() -> UpdateManifest:
    return UpdateManifest(
        version=1,
        release_id="witness-v1",
        approved=True,
        repository="DanielTabakman/msos-autobuilder",
        repo_url="https://github.com/DanielTabakman/msos-autobuilder.git",
        commit="a" * 40,
        required_status_contexts=("test",),
        expected_files=(ExpectedFile(path="pyproject.toml", sha256="b" * 64),),
        manifest_sha256="c" * 64,
        supervisor_update=False,
    )


def test_waiter_retries_until_exact_required_check_is_successful() -> None:
    verifier = SequenceVerifier(
        [
            SupervisorError("required GitHub commit checks are not successful: test=missing"),
            SupervisorError("required GitHub commit checks are not successful: test=in_progress"),
            None,
        ]
    )
    clock = Clock()

    wait_for_update_checks(
        _manifest(),
        verifier=verifier,
        timeout_seconds=30,
        poll_seconds=5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert verifier.calls == 3
    assert clock.value == 10


def test_waiter_times_out_without_publishing_unverified_manifest() -> None:
    verifier = SequenceVerifier([SupervisorError("test=failure")])
    clock = Clock()

    with pytest.raises(UpdateCheckTimeout, match="test=failure"):
        wait_for_update_checks(
            replace(_manifest()),
            verifier=verifier,
            timeout_seconds=12,
            poll_seconds=5,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert verifier.calls == 4
    assert clock.value == 12
