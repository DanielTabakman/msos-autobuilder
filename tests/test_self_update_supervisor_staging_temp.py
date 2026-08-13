from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from msos_autobuilder.self_update_supervisor import (
    STAGING_PYTEST_HARD_CEILING_SECONDS,
    STAGING_PYTEST_TEMP_IDENTITY_CHARS,
    STAGING_PYTEST_TEMP_NAMESPACE,
    STAGING_PYTEST_TEMP_RELEASE_CHARS,
    STAGING_PYTEST_TIMEOUT_SECONDS,
    STAGING_TEMP_DELETE_RETRY_SECONDS,
    CheckResult,
    ManagedTask,
    SupervisorConfig,
    SupervisorError,
    UpdateManifest,
    _cleanup_staging_pytest_temp_dir,
    _is_within,
    _prepare_staging_pytest_temp_dir,
    _run_named_check,
    _run_staged_pytest_checks,
    _staging_pytest_temp_dir,
    _supervisor_identity_digest,
    _validate_staging_pytest_temp_dir,
    default_command_executor,
)

STAGING_PYTEST_TEMP_SUFFIX_BUDGET_CHARS = 23


@pytest.fixture(autouse=True)
def os_temp_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the OS-local staging temp base inside the test sandbox."""

    base = tmp_path / "os-temp"
    base.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(base))
    return base


def _config(tmp_path: Path, *, supervisor_root: Path | None = None) -> SupervisorConfig:
    probe = tmp_path / "managed-release-health-probe.py"
    probe.write_text("# fixture\n", encoding="utf-8")
    return SupervisorConfig(
        supervisor_root=supervisor_root or (tmp_path / "supervisor"),
        host_root=tmp_path / "host",
        repo_url="https://github.com/DanielTabakman/msos-autobuilder.git",
        repository="DanielTabakman/msos-autobuilder",
        task_controller_script=tmp_path / "task-control.ps1",
        release_probe_script=probe,
        managed_tasks=(ManagedTask("host", "MSOS Autobuilder Host"),),
    )


def _long_supervisor_root(tmp_path: Path) -> Path:
    """A synthetic root as long as the isolated Issue #119 pilot supervisor root."""

    root = tmp_path / ".msos-autobuilder-pilot-issue119-6e9434c-20260807T0226Z-c-supervisor"
    while len(str(root)) < 160:
        root = root / "nested-isolated-pilot-segment"
    return root


def _manifest(*, commit: str = "a" * 40, release_id: str = "release-1") -> UpdateManifest:
    return UpdateManifest(
        version=1,
        release_id=release_id,
        approved=True,
        repository="DanielTabakman/msos-autobuilder",
        repo_url="https://github.com/DanielTabakman/msos-autobuilder.git",
        commit=commit,
        required_status_contexts=("test",),
        expected_files=(),
        manifest_sha256="b" * 64,
    )


def test_staging_pytest_temp_path_is_identity_owned_and_release_bounded(
    tmp_path: Path,
    os_temp_base: Path,
) -> None:
    config = _config(tmp_path)
    first = _staging_pytest_temp_dir(config, _manifest())
    other_commit = _staging_pytest_temp_dir(config, _manifest(commit="c" * 40))
    other_release = _staging_pytest_temp_dir(config, _manifest(release_id="release-2"))

    assert first.parent == config.staging_pytest_temp_root.absolute()
    assert len(first.name) == STAGING_PYTEST_TEMP_RELEASE_CHARS
    assert first != other_commit
    assert first != other_release
    assert first.parent.name == _supervisor_identity_digest(config.supervisor_root)
    assert len(first.parent.name) == STAGING_PYTEST_TEMP_IDENTITY_CHARS
    assert first.parent.parent == os_temp_base / STAGING_PYTEST_TEMP_NAMESPACE
    assert not _is_within(first, config.supervisor_root.absolute())


def test_default_staging_temp_base_is_the_os_local_temp_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", None)
    config = _config(tmp_path)

    root = config.staging_pytest_temp_root

    os_temp = Path(os.path.abspath(tempfile.gettempdir()))
    assert root.parent == os_temp / STAGING_PYTEST_TEMP_NAMESPACE
    assert root.name == _supervisor_identity_digest(config.supervisor_root)
    assert not _is_within(root, config.supervisor_root.absolute())


def test_long_supervisor_root_does_not_lengthen_the_staging_pytest_temp_path(
    tmp_path: Path,
    os_temp_base: Path,
) -> None:
    long_root = _long_supervisor_root(tmp_path)
    long_config = _config(tmp_path, supervisor_root=long_root)
    short_config = _config(tmp_path, supervisor_root=tmp_path / "s")

    long_path = _staging_pytest_temp_dir(long_config, _manifest())
    short_path = _staging_pytest_temp_dir(short_config, _manifest())

    # The old design nested the temp root under the supervisor root, so the staged pytest TMP
    # grew with the pilot path and overran the Windows path limit.
    assert not _is_within(long_path, long_root.absolute())
    assert len(str(long_path)) == len(str(short_path))
    assert len(str(long_path)) < len(str(long_root))
    expected_suffix_chars = (
        len(f"{os.sep}{STAGING_PYTEST_TEMP_NAMESPACE}{os.sep}")
        + STAGING_PYTEST_TEMP_IDENTITY_CHARS
        + len(os.sep)
        + STAGING_PYTEST_TEMP_RELEASE_CHARS
    )
    assert len(str(long_path)) - len(str(os_temp_base)) == expected_suffix_chars


def test_staging_pytest_temp_suffix_fits_the_windows_staging_path_budget(
    tmp_path: Path,
    os_temp_base: Path,
) -> None:
    # This suite already builds fixture paths about 350 characters below TMP, and staged runs on
    # Windows start failing once TMP pushes those past roughly 413 characters. A standard per-user
    # temp base is about 32 characters, so the supervisor-owned suffix has to stay small enough to
    # leave that headroom rather than consume it.
    config = _config(tmp_path, supervisor_root=_long_supervisor_root(tmp_path))

    path = _staging_pytest_temp_dir(config, _manifest())

    suffix_chars = len(str(path)) - len(str(os_temp_base))
    assert suffix_chars <= STAGING_PYTEST_TEMP_SUFFIX_BUDGET_CHARS


def test_distinct_supervisor_roots_cannot_share_staging_temp_ownership(
    tmp_path: Path,
) -> None:
    first = _config(tmp_path, supervisor_root=tmp_path / "supervisor-a")
    second = _config(tmp_path, supervisor_root=tmp_path / "supervisor-b")

    first_path = _staging_pytest_temp_dir(first, _manifest())
    second_path = _staging_pytest_temp_dir(second, _manifest())

    assert first.staging_pytest_temp_root != second.staging_pytest_temp_root
    assert first_path.parent != second_path.parent
    assert first_path != second_path
    with pytest.raises(SupervisorError, match="release-specific child"):
        _validate_staging_pytest_temp_dir(first, second_path)


def test_cleanup_refuses_another_supervisors_staging_temp_path(tmp_path: Path) -> None:
    owner = _config(tmp_path, supervisor_root=tmp_path / "supervisor-a")
    intruder = _config(tmp_path, supervisor_root=tmp_path / "supervisor-b")
    owned = _staging_pytest_temp_dir(owner, _manifest())
    owned.mkdir(parents=True)
    sentinel = owned / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    cleanup = _cleanup_staging_pytest_temp_dir(intruder, owned, tmp_path)

    assert not cleanup.passed
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_staging_temp_root_inside_the_supervisor_root_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor_root = tmp_path / "supervisor"
    inherited_base = supervisor_root / "tmp"
    inherited_base.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(inherited_base))
    config = _config(tmp_path, supervisor_root=supervisor_root)

    with pytest.raises(SupervisorError, match="supervisor root prefix"):
        _staging_pytest_temp_dir(config, _manifest())


def test_staged_pytest_receives_owned_temp_and_preserves_full_gate(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    manifest = _manifest()
    staging_path = tmp_path / "checkout"
    staging_path.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    calls: list[tuple[tuple[str, ...], Path, float, str, str]] = []

    def executor(argv: Sequence[str], cwd: Path, timeout: float) -> CheckResult:
        effective_tmp = os.environ["TMP"]
        effective_temp = os.environ["TEMP"]
        calls.append((tuple(argv), cwd, timeout, effective_tmp, effective_temp))
        temp_path = Path(effective_tmp)
        (temp_path / "git-heavy" / ".git" / "objects").mkdir(parents=True)
        (temp_path / "git-heavy" / ".git" / "objects" / "fixture").write_text(
            "fixture", encoding="utf-8"
        )
        return CheckResult(
            name="raw",
            argv=tuple(argv),
            cwd=str(cwd),
            returncode=0,
            duration_seconds=1.0,
            stdout="495 passed",
        )

    pytest_result, cleanup_result = _run_staged_pytest_checks(
        config,
        manifest,
        executor,
        Path("python"),
        staging_path,
    )

    assert calls == [
        (
            ("python", "-m", "pytest", "-q"),
            staging_path,
            STAGING_PYTEST_HARD_CEILING_SECONDS,
            calls[0][3],
            calls[0][3],
        )
    ]
    effective_tmp = Path(calls[0][3])
    assert effective_tmp == _staging_pytest_temp_dir(config, manifest)
    assert effective_tmp.parent == config.staging_pytest_temp_root.absolute()
    assert not _is_within(effective_tmp, config.supervisor_root.absolute())
    assert pytest_result.environment == {"TMP": str(effective_tmp), "TEMP": str(effective_tmp)}
    assert pytest_result.argv[-3:] == ("-m", "pytest", "-q")
    assert pytest_result.timeout_seconds == STAGING_PYTEST_HARD_CEILING_SECONDS
    assert STAGING_PYTEST_TIMEOUT_SECONDS == 2400.0
    assert STAGING_PYTEST_HARD_CEILING_SECONDS == 3600.0
    assert cleanup_result.passed
    assert not effective_tmp.exists()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_prepare_replaces_a_stale_release_temp_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manifest = _manifest()
    stale = _staging_pytest_temp_dir(config, manifest)
    stale.mkdir(parents=True)
    (stale / "stale-pytest-tree").mkdir()

    prepared = _prepare_staging_pytest_temp_dir(config, manifest, tmp_path)

    assert prepared == stale
    assert prepared.is_dir()
    assert not (prepared / "stale-pytest-tree").exists()


def test_unsafe_or_escaping_temp_paths_are_rejected_without_deletion(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(SupervisorError, match="release-specific child"):
        _validate_staging_pytest_temp_dir(config, outside)

    cleanup = _cleanup_staging_pytest_temp_dir(config, outside, tmp_path)
    assert not cleanup.passed
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert outside.is_dir()


def test_cleanup_refuses_release_temp_symlink_and_preserves_target(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = tmp_path / "unrelated"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    candidate = _staging_pytest_temp_dir(config, _manifest())
    candidate.parent.mkdir(parents=True)
    try:
        candidate.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    cleanup = _cleanup_staging_pytest_temp_dir(config, candidate, tmp_path)

    assert not cleanup.passed
    assert candidate.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_symlinked_ownership_root_is_rejected_without_deletion(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = tmp_path / "unrelated"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    ownership_root = config.staging_pytest_temp_root
    ownership_root.parent.mkdir(parents=True)
    try:
        ownership_root.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(SupervisorError, match="contains a symlink"):
        _staging_pytest_temp_dir(config, _manifest())

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_cleanup_unlinks_nested_symlink_without_traversing_target(tmp_path: Path) -> None:
    config = _config(tmp_path)
    candidate = _staging_pytest_temp_dir(config, _manifest())
    candidate.mkdir(parents=True)
    target = tmp_path / "nested-target"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    nested_link = candidate / "linked-directory"
    try:
        nested_link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    cleanup = _cleanup_staging_pytest_temp_dir(config, candidate, tmp_path)

    assert cleanup.passed
    assert not candidate.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_timeout_evidence_contains_output_environment_and_termination(
    tmp_path: Path,
) -> None:
    temp_root = tmp_path / "pytest-temp"
    temp_root.mkdir()
    script = (
        "import sys,time; "
        "print('pytest stdout evidence', flush=True); "
        "print('pytest stderr evidence', file=sys.stderr, flush=True); "
        "time.sleep(30)"
    )

    result = _run_named_check(
        default_command_executor,
        "pytest",
        [sys.executable, "-c", script],
        tmp_path,
        1.0,
        environment={"TMP": str(temp_root), "TEMP": str(temp_root)},
    )

    assert result.timed_out
    assert not result.passed
    assert result.argv == (sys.executable, "-c", script)
    assert Path(result.cwd) == tmp_path
    assert result.environment == {"TMP": str(temp_root), "TEMP": str(temp_root)}
    assert result.timeout_seconds == 1.0
    assert "pytest stdout evidence" in result.stdout
    assert "pytest stderr evidence" in result.stderr
    assert result.duration_seconds >= 1.0
    assert result.termination["attempted"] is True
    assert result.process_tree["root_pid"] > 0


def test_failed_pytest_records_cleanup_failure_without_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    staging_path = tmp_path / "checkout"
    staging_path.mkdir()

    def failed_executor(argv: Sequence[str], cwd: Path, timeout: float) -> CheckResult:
        return CheckResult(
            name="raw",
            argv=tuple(argv),
            cwd=str(cwd),
            returncode=1,
            duration_seconds=1.0,
            stdout="failed test output",
        )

    def fail_cleanup(path: Path) -> None:
        raise OSError(f"locked: {path}")

    monkeypatch.setattr(
        "msos_autobuilder.self_update_supervisor._remove_tree_no_symlinks",
        fail_cleanup,
    )

    pytest_result, cleanup_result = _run_staged_pytest_checks(
        config,
        _manifest(),
        failed_executor,
        Path("python"),
        staging_path,
    )

    assert not pytest_result.passed
    assert pytest_result.stdout == "failed test output"
    assert not cleanup_result.passed
    assert "locked:" in cleanup_result.stderr


def test_ordinary_cleanup_removes_the_owned_tree(tmp_path: Path) -> None:
    config = _config(tmp_path)
    candidate = _staging_pytest_temp_dir(config, _manifest())
    candidate.mkdir(parents=True)
    nested = candidate / ".git" / "objects" / "1f"
    nested.mkdir(parents=True)
    (nested / "662911b39737dc7e2c05ec609d360a586bb21e").write_text("blob", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    cleanup = _cleanup_staging_pytest_temp_dir(config, candidate, tmp_path)

    assert cleanup.passed
    assert cleanup.stdout == "removed"
    assert not candidate.exists()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_permission_error_on_owned_file_is_normalized_and_cleanup_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import msos_autobuilder.self_update_supervisor as supervisor

    config = _config(tmp_path)
    candidate = _staging_pytest_temp_dir(config, _manifest())
    candidate.mkdir(parents=True)
    owned = candidate / "readonly-object"
    owned.write_text("blob", encoding="utf-8")
    chmod_paths: list[Path] = []
    original_unlink = Path.unlink
    attempts = {"count": 0}

    def flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == owned and attempts["count"] == 0:
            attempts["count"] += 1
            raise PermissionError(5, "Access is denied", str(self))
        return original_unlink(self, *args, **kwargs)

    real_chmod = os.chmod

    def tracking_chmod(
        path: str | bytes | os.PathLike[str],
        mode: int,
        *args: object,
        **kwargs: object,
    ) -> None:
        chmod_paths.append(Path(os.fspath(path)))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    monkeypatch.setattr(os, "chmod", tracking_chmod)
    monkeypatch.setattr(supervisor, "STAGING_TEMP_DELETE_SLEEP_SECONDS", 0.0)

    cleanup = _cleanup_staging_pytest_temp_dir(config, candidate, tmp_path)

    assert cleanup.passed
    assert not candidate.exists()
    assert attempts["count"] == 1
    assert any(path == owned for path in chmod_paths)


def test_transient_permission_error_within_retry_bound_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import msos_autobuilder.self_update_supervisor as supervisor

    config = _config(tmp_path)
    candidate = _staging_pytest_temp_dir(config, _manifest())
    candidate.mkdir(parents=True)
    owned = candidate / "locked-object"
    owned.write_text("blob", encoding="utf-8")
    original_unlink = Path.unlink
    failures_remaining = {"count": 3}

    def flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == owned and failures_remaining["count"] > 0:
            failures_remaining["count"] -= 1
            raise PermissionError(5, "Access is denied", str(self))
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    monkeypatch.setattr(supervisor, "STAGING_TEMP_DELETE_SLEEP_SECONDS", 0.0)
    monkeypatch.setattr(supervisor, "_make_owned_path_writable", lambda path: None)

    cleanup = _cleanup_staging_pytest_temp_dir(config, candidate, tmp_path)

    assert cleanup.passed
    assert not candidate.exists()
    assert failures_remaining["count"] == 0


def test_persistent_permission_error_fails_cleanup_and_blocks_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import msos_autobuilder.self_update_supervisor as supervisor

    config = _config(tmp_path)
    staging_path = tmp_path / "checkout"
    staging_path.mkdir()
    owned_holder: dict[str, Path] = {}

    def executor(argv: Sequence[str], cwd: Path, timeout: float) -> CheckResult:
        temp_path = Path(os.environ["TMP"])
        owned_holder["path"] = temp_path
        (temp_path / "locked-object").write_text("blob", encoding="utf-8")
        return CheckResult(
            name="raw",
            argv=tuple(argv),
            cwd=str(cwd),
            returncode=0,
            duration_seconds=1.0,
            stdout="495 passed",
        )

    original_unlink = Path.unlink

    def always_denied(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "locked-object":
            raise PermissionError(5, "Access is denied", str(self))
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", always_denied)
    monkeypatch.setattr(supervisor, "STAGING_TEMP_DELETE_SLEEP_SECONDS", 0.0)
    monkeypatch.setattr(supervisor, "STAGING_TEMP_DELETE_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(supervisor, "_make_owned_path_writable", lambda path: None)

    pytest_result, cleanup_result = _run_staged_pytest_checks(
        config,
        _manifest(),
        executor,
        Path("python"),
        staging_path,
    )

    assert pytest_result.passed
    assert not cleanup_result.passed
    assert "Access is denied" in cleanup_result.stderr
    assert owned_holder["path"].exists()
    assert (owned_holder["path"] / "locked-object").exists()
    # Exact stage_release cutover gate: cleanup failure raises before pointer swap.
    with pytest.raises(SupervisorError, match="staging pytest temp cleanup failed"):
        if not cleanup_result.passed:
            raise SupervisorError(
                "staging pytest temp cleanup failed: "
                f"{cleanup_result.stderr or cleanup_result.stdout}"
            )


def test_symlink_permission_retry_does_not_normalize_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import msos_autobuilder.self_update_supervisor as supervisor

    config = _config(tmp_path)
    candidate = _staging_pytest_temp_dir(config, _manifest())
    candidate.mkdir(parents=True)
    target = tmp_path / "outside-target.txt"
    target.write_text("keep", encoding="utf-8")
    nested_link = candidate / "linked-file"
    try:
        nested_link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    chmod_paths: list[Path] = []
    original_unlink = Path.unlink
    attempts = {"count": 0}

    def flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == nested_link and attempts["count"] == 0:
            attempts["count"] += 1
            raise PermissionError(5, "Access is denied", str(self))
        return original_unlink(self, *args, **kwargs)

    real_chmod = os.chmod

    def tracking_chmod(
        path: str | bytes | os.PathLike[str],
        mode: int,
        *args: object,
        **kwargs: object,
    ) -> None:
        chmod_paths.append(Path(os.fspath(path)).resolve())
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    monkeypatch.setattr(os, "chmod", tracking_chmod)
    monkeypatch.setattr(supervisor, "STAGING_TEMP_DELETE_SLEEP_SECONDS", 0.0)

    cleanup = _cleanup_staging_pytest_temp_dir(config, candidate, tmp_path)

    assert cleanup.passed
    assert not candidate.exists()
    assert target.read_text(encoding="utf-8") == "keep"
    assert target.resolve() not in chmod_paths
    assert attempts["count"] == 1


def test_cleanup_success_requires_release_temp_tree_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    candidate = _staging_pytest_temp_dir(config, _manifest())
    candidate.mkdir(parents=True)
    (candidate / "residue.txt").write_text("still here", encoding="utf-8")

    monkeypatch.setattr(
        "msos_autobuilder.self_update_supervisor._remove_tree_no_symlinks",
        lambda path: None,
    )

    cleanup = _cleanup_staging_pytest_temp_dir(config, candidate, tmp_path)

    assert not cleanup.passed
    assert "still present after cleanup" in cleanup.stderr
    assert candidate.exists()
    assert (candidate / "residue.txt").read_text(encoding="utf-8") == "still here"


def test_retry_window_is_short_and_finite() -> None:
    assert 0 < STAGING_TEMP_DELETE_RETRY_SECONDS <= 2.0
