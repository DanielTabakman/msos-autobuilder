from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_windows_self_update_supervisor.ps1"
RUNNER = ROOT / "scripts" / "run_windows_managed_service.ps1"
TASK_CONTROL = ROOT / "scripts" / "windows_self_update_task_control.ps1"
INVOKER = ROOT / "scripts" / "invoke_windows_self_update.ps1"
ROLLBACK = ROOT / "scripts" / "rollback_windows_self_update.ps1"
PROBE = ROOT / "scripts" / "managed_release_health_probe.py"
SCRIPTS = (INSTALLER, RUNNER, TASK_CONTROL, INVOKER, ROLLBACK)


def test_installer_preserves_external_supervisor_and_atomic_release_boundary() -> None:
    script = INSTALLER.read_text(encoding="utf-8")

    assert ".msos-autobuilder-supervisor" in script
    assert "bootstrap-venv" in script
    assert "versions" in script
    assert "active-release.json" in script
    assert "Move-Item -Force -Path $Temporary -Destination $Path" in script
    assert "MSOS Autobuilder Update Supervisor" in script
    assert "MSOS Autobuilder Host" in script
    assert "MSOS Autobuilder Result Relay" in script
    assert "MSOS Autobuilder Candidate Gate" in script
    assert "MSOS Autobuilder Revision Loop" in script
    assert "MSOS Autobuilder Controlled Publisher" in script
    assert "run_windows_managed_service.ps1" in script
    assert "windows_self_update_task_control.ps1" in script
    assert "managed_release_health_probe.py" in script
    assert "A different managed release is already active" in script
    assert "The active release directory is incomplete" in script
    assert "Move-Item -Path $StagingPath -Destination $VersionPath" not in script
    assert "RepoUrl must not embed credentials" in script
    assert "service-witnesses" in script
    assert (
        "Initial managed release did not produce five fresh exact-commit service witnesses"
        in script
    )
    assert "service_witnesses = $ServiceWitnesses" in script
    assert "git pull" not in script.lower()
    assert "push --force" not in script.lower()
    assert "merge_pull_request" not in script


def test_managed_runner_resolves_only_the_active_version_and_writes_witnesses() -> None:
    script = RUNNER.read_text(encoding="utf-8")

    assert "active-release.json" in script
    assert "release.json" in script
    assert ".venv\\Scripts\\python.exe" in script
    assert "managed_release_health_probe.py" in script
    assert "msos_autobuilder.self_update_supervisor release-smoke" not in script
    assert "service-witnesses" in script
    assert "release_commit = $ReleaseCommit" in script
    assert 'state = "running"' in script
    assert 'state = "stopped"' in script
    assert "{managed_python}" in script
    assert "{managed_release_root}" in script
    assert "{runtime_config}" in script


def test_stable_probe_requires_modules_to_resolve_inside_selected_release(
    tmp_path: Path,
) -> None:
    spec = importlib.util.spec_from_file_location("managed_release_health_probe", PROBE)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)

    root = tmp_path / "release"
    (root / "pyproject.toml").parent.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")

    def importer(name: str) -> ModuleType:
        path = root / "src" / Path(*name.split("."))
        path = path.with_suffix(".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")
        module = ModuleType(name)
        module.__file__ = str(path)
        return module

    imported = probe.probe_release(root, importer=importer)
    assert set(imported) == set(probe.MANAGED_MODULES)

    outside = tmp_path / "outside.py"
    outside.write_text("# outside\n", encoding="utf-8")

    def outside_importer(name: str) -> ModuleType:
        module = ModuleType(name)
        module.__file__ = str(outside)
        return module

    with pytest.raises(RuntimeError, match="outside the selected release"):
        probe.probe_release(root, importer=outside_importer)


def test_task_controller_can_touch_only_explicit_task_names() -> None:
    script = TASK_CONTROL.read_text(encoding="utf-8")

    assert '[ValidateSet("stop", "start", "states")]' in script
    assert "$TaskNamesJson | ConvertFrom-Json" in script
    assert "Get-ScheduledTask -TaskName $Name" in script
    assert "Get-ScheduledTask |" not in script
    assert "Unregister-ScheduledTask" not in script
    assert "Register-ScheduledTask" not in script


def test_update_invoker_downloads_one_manifest_then_calls_stable_python() -> None:
    script = INVOKER.read_text(encoding="utf-8")

    assert "Invoke-WebRequest" in script
    assert "approved-update.yaml" in script
    assert "bootstrap-venv\\Scripts\\python.exe" in script
    assert "bootstrap\\self_update_supervisor.py" in script
    assert " apply --config " in script
    assert "git pull" not in script.lower()


def test_one_command_manual_rollback_uses_the_stable_supervisor() -> None:
    script = ROLLBACK.read_text(encoding="utf-8")

    assert " rollback --config " in script
    assert "bootstrap-venv\\Scripts\\python.exe" in script
    assert "bootstrap\\self_update_supervisor.py" in script


@pytest.mark.parametrize("path", SCRIPTS)
def test_windows_self_update_scripts_parse_in_powershell(path: Path) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")
    command = (
        "$tokens=$null; $errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{path.as_posix()}', "
        "[ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_installer_has_no_unsafe_colon_interpolation() -> None:
    script = INSTALLER.read_text(encoding="utf-8")
    here_strings = re.findall(r'@"(.*?)"@', script, flags=re.DOTALL)
    for here_string in here_strings:
        assert re.search(r"(?m)^\s*\$[A-Za-z_][A-Za-z0-9_]*:", here_string) is None


def test_self_update_document_keeps_issue_33_blocked_until_rollback_witness() -> None:
    document = (ROOT / "docs" / "FAIL_SAFE_SELF_UPDATE_SUPERVISOR_V1.md").read_text(
        encoding="utf-8"
    )
    assert "Issue #33 remains blocked" in document
    assert "deliberately broken" in document
    assert "automatically restores the previous release" in document
    assert "two-stage handoff" in document
