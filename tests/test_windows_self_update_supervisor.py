from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_windows_self_update_supervisor.ps1"
RUNNER = ROOT / "scripts" / "run_windows_managed_service.ps1"
TASK_CONTROL = ROOT / "scripts" / "windows_self_update_task_control.ps1"
INVOKER = ROOT / "scripts" / "invoke_windows_self_update.ps1"
ROLLBACK = ROOT / "scripts" / "rollback_windows_self_update.ps1"
BOOTSTRAP_HANDOFF = ROOT / "scripts" / "update_windows_stable_supervisor_bootstrap.ps1"
PROBE = ROOT / "scripts" / "managed_release_health_probe.py"
SCRIPTS = (INSTALLER, RUNNER, TASK_CONTROL, INVOKER, ROLLBACK, BOOTSTRAP_HANDOFF)
MANAGED_TASK_NAMES = [
    "MSOS Autobuilder Host",
    "MSOS Autobuilder Result Relay",
    "MSOS Autobuilder Candidate Gate",
    "MSOS Autobuilder Revision Loop",
    "MSOS Autobuilder Controlled Publisher",
    "MSOS Autobuilder Capacity-One Refill",
]


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
    assert "MSOS Autobuilder Capacity-One Refill" in script
    assert '"refill-run"' in script
    assert "run_windows_managed_service.ps1" in script
    assert "windows_self_update_task_control.ps1" in script
    assert "managed_release_health_probe.py" in script
    assert "bootstrap checkout must be clean" in script
    assert "health_stability_seconds: 10" in script
    assert "TotalSeconds -ge 10" in script
    assert "$StableHealthy = $true" in script
    assert "if (-not $StableHealthy)" in script
    assert "A different managed release is already active" in script
    assert "The active release directory is incomplete" in script
    assert "Move-Item -Path $StagingPath -Destination $VersionPath" not in script
    assert "RepoUrl must not embed credentials" in script
    assert "service-witnesses" in script
    assert "Initial managed release did not remain healthy" in script
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
    assert "[Console]::In.ReadToEnd()" in script
    assert "$TaskNamesJson | ConvertFrom-Json" in script
    assert "Get-ScheduledTask -TaskName $Name" in script
    assert "Get-ScheduledTask |" not in script
    assert "Unregister-ScheduledTask" not in script
    assert "Register-ScheduledTask" not in script


def test_stable_bootstrap_handoff_is_exact_commit_reversible_and_non_mutating() -> None:
    script = BOOTSTRAP_HANDOFF.read_text(encoding="utf-8")

    assert "ExpectedOldBootstrapCommit" in script
    assert "rev-parse HEAD" in script
    assert "status --porcelain --untracked-files=all" in script
    assert "Get-BootstrapHashEvidence" in script
    assert "Get-GitBlobBytes" in script
    assert "cat-file blob" in script
    assert "CRLF-to-LF byte pairs only" in script
    assert "new_checkout_canonical_sha256" in script
    assert "staged_sha256" in script
    assert "bootstrap-updates" in script
    assert "bootstrap-rollbacks" in script
    assert "Disable-ScheduledTask -TaskName $UpdateTaskName" in script
    assert "Enable-ScheduledTask -TaskName $UpdateTaskName" in script
    assert "Assert-NoActiveUpdateAttempt" in script
    assert "PowerShellTaskController" in script
    assert "controller.states(task_names)" in script
    assert "Add-StagedServiceConfiguration" in script
    assert "Register-DisabledRefillTask" in script
    assert "Restore-RefillTask" in script
    assert "Disable-ScheduledTask -TaskName $RefillTaskName" in script
    assert "Refill task must remain disabled" in script
    assert "previous = sys.modules.get(module_name)" in script
    assert "sys.modules[module_name] = module" in script
    assert "sys.modules.pop(module_name, None)" in script
    transport_script = script[script.index("function Test-StagedTaskTransport") :]
    assert "stdout_path = pathlib.Path(sys.argv[3])" in transport_script
    assert "stderr_path = pathlib.Path(sys.argv[4])" in transport_script
    assert 'stdout_path.open("w", encoding="utf-8")' in transport_script
    assert 'stderr_path.open(' in transport_script
    assert "traceback.print_exc(file=stderr_file)" in transport_script
    assert "& $BootstrapPython `" in transport_script
    assert "$StdoutPath `" in transport_script
    assert "$StderrPath" in transport_script
    assert "$ExitCode = $LASTEXITCODE" in transport_script
    assert "& $Cmd /d /c $CommandLine" not in transport_script
    assert '1> "{4}" 2> "{5}"' not in transport_script
    assert '"/bin/sh"' not in transport_script
    assert "Quote-PosixShell" not in transport_script
    assert "Get-Content -Raw -Encoding UTF8 $StdoutPath" in transport_script
    assert "Get-Content -Raw -Encoding UTF8 $StderrPath" in transport_script
    assert "Move-Item -Path $BootstrapRoot -Destination $ActivationBackup" in script
    assert "Move-Item -Path $StagedBootstrap -Destination $BootstrapRoot" in script
    assert "stable-bootstrap-update-handoff" in script
    assert "active-release.json" in script
    assert "update-ledger.json" not in script
    assert "Unregister-ScheduledTask" in script
    assert "Register-ScheduledTask" in script
    assert "MSOS_TASK_CONTROLLER_POWERSHELL" not in (
        ROOT / "src" / "msos_autobuilder" / "self_update_supervisor.py"
    ).read_text(encoding="utf-8")


def test_old_unregistered_stable_supervisor_import_fails_in_subprocess() -> None:
    supervisor_path = ROOT / "src" / "msos_autobuilder" / "self_update_supervisor.py"
    code = "\n".join(
        [
            "import importlib.util",
            f"module_path = {str(supervisor_path)!r}",
            "spec = importlib.util.spec_from_file_location(",
            "    'staged_self_update_supervisor', module_path",
            ")",
            "module = importlib.util.module_from_spec(spec)",
            "assert spec.loader is not None",
            "spec.loader.exec_module(module)",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode != 0
    assert "Traceback (most recent call last)" in result.stderr
    assert "dataclasses.py" in result.stderr


def _build_stable_bootstrap_handoff_fixture(
    tmp_path: Path,
    *,
    task_control_stderr_failure: str | None = None,
    task_control_stderr_repeat: int = 0,
) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)

    source_paths = {
        "src/msos_autobuilder/self_update_supervisor.py": (
            ROOT / "src" / "msos_autobuilder" / "self_update_supervisor.py"
        ),
        "scripts/managed_release_health_probe.py": (
            ROOT / "scripts" / "managed_release_health_probe.py"
        ),
        "scripts/windows_self_update_task_control.ps1": TASK_CONTROL,
        "scripts/run_windows_managed_service.ps1": RUNNER,
        "scripts/invoke_windows_self_update.ps1": INVOKER,
        "scripts/rollback_windows_self_update.ps1": ROLLBACK,
    }
    for relative, source in source_paths.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == "scripts/windows_self_update_task_control.ps1":
            path.write_text(
                _stubbed_task_control_script(
                    stderr_failure=task_control_stderr_failure,
                    stderr_repeat=task_control_stderr_repeat,
                ),
                encoding="utf-8",
            )
        else:
            shutil.copy2(source, path)
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    source_payloads = {
        "src/msos_autobuilder/self_update_evidence_relay.py": "print('relay fixture')\n",
    }
    for relative, text in source_payloads.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((text.strip() + "\n").encode("utf-8"))
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "old bootstrap"], cwd=repo, check=True)
    old_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    (repo / "src/msos_autobuilder/self_update_evidence_relay.py").write_bytes(
        b"print('relay fixture v2')\n"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "new bootstrap"], cwd=repo, check=True)
    new_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    supervisor = tmp_path / "supervisor"
    live_bootstrap = supervisor / "bootstrap"
    live_bootstrap.mkdir(parents=True)
    subprocess.run(["git", "checkout", "-q", old_commit], cwd=repo, check=True)
    target_names = {
        "src/msos_autobuilder/self_update_supervisor.py": "self_update_supervisor.py",
        "src/msos_autobuilder/self_update_evidence_relay.py": "self_update_evidence_relay.py",
        "scripts/managed_release_health_probe.py": "managed_release_health_probe.py",
        "scripts/windows_self_update_task_control.ps1": "windows_self_update_task_control.ps1",
        "scripts/run_windows_managed_service.ps1": "run_windows_managed_service.ps1",
        "scripts/invoke_windows_self_update.ps1": "invoke_windows_self_update.ps1",
        "scripts/rollback_windows_self_update.ps1": "rollback_windows_self_update.ps1",
    }
    for source, target in target_names.items():
        shutil.copy2(repo / source, live_bootstrap / target)
    (live_bootstrap / "supervisor.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                f"supervisor_root: '{supervisor.as_posix()}'",
                f"host_root: '{(tmp_path / 'host').as_posix()}'",
                "repo_url: 'https://github.com/DanielTabakman/msos-autobuilder.git'",
                "repository: 'DanielTabakman/msos-autobuilder'",
                "task_controller_script: "
                f"'{(live_bootstrap / 'windows_self_update_task_control.ps1').as_posix()}'",
                "release_probe_script: "
                f"'{(live_bootstrap / 'managed_release_health_probe.py').as_posix()}'",
                "managed_tasks:",
                "  - service: host",
                "    task_name: 'MSOS Autobuilder Host'",
                "  - service: relay",
                "    task_name: 'MSOS Autobuilder Result Relay'",
                "  - service: gate",
                "    task_name: 'MSOS Autobuilder Candidate Gate'",
                "  - service: revision",
                "    task_name: 'MSOS Autobuilder Revision Loop'",
                "  - service: publisher",
                "    task_name: 'MSOS Autobuilder Controlled Publisher'",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (live_bootstrap / "managed-services.json").write_text(
        json.dumps(
            {
                "version": 1,
                "services": {
                    "host": {"argv": ["host"], "log_file": "{host_root}/logs/host.log"},
                    "relay": {"argv": ["relay"], "log_file": "{host_root}/logs/relay.log"},
                    "gate": {"argv": ["gate"], "log_file": "{host_root}/logs/gate.log"},
                    "revision": {
                        "argv": ["revision"],
                        "log_file": "{host_root}/logs/revision.log",
                    },
                    "publisher": {
                        "argv": ["publisher"],
                        "log_file": "{host_root}/logs/publisher.log",
                    },
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "checkout", "-q", new_commit], cwd=repo, check=True)

    return {
        "repo": repo,
        "supervisor": supervisor,
        "live_bootstrap": live_bootstrap,
        "old_commit": old_commit,
        "new_commit": new_commit,
        "target_names": target_names,
    }


def _convert_installed_bootstrap_to_crlf(fixture: dict[str, object]) -> None:
    live_bootstrap = fixture["live_bootstrap"]
    target_names = fixture["target_names"]
    assert isinstance(live_bootstrap, Path)
    assert isinstance(target_names, dict)
    for target in target_names.values():
        path = live_bootstrap / target
        original = path.read_bytes()
        assert b"\r\n" not in original
        path.write_bytes(original.replace(b"\n", b"\r\n"))


def _read_handoff_report(fixture: dict[str, object]) -> dict[str, object]:
    supervisor = fixture["supervisor"]
    assert isinstance(supervisor, Path)
    reports = list((supervisor / "reports").glob("stable-bootstrap-update-*.json"))
    assert len(reports) == 1
    return json.loads(reports[0].read_text(encoding="utf-8-sig"))


def _stubbed_task_control_script(
    *,
    state: str = "Running",
    stderr_failure: str | None = None,
    stderr_repeat: int = 0,
) -> str:
    script = TASK_CONTROL.read_text(encoding="utf-8")
    if stderr_failure is None:
        get_body = (
            "    Add-Call -Action 'get' -Name $TaskName\n"
            f"    [pscustomobject]@{{ TaskName = $TaskName; State = '{state}' }}\n"
        )
    else:
        escaped = stderr_failure.replace("'", "''")
        if stderr_repeat > 0:
            get_body = (
                "    Add-Call -Action 'get' -Name $TaskName\n"
                f"    $Payload = '{escaped}' -f ('x' * {stderr_repeat})\n"
                "    [Console]::Error.WriteLine($Payload)\n"
                "    throw 'stubbed scheduled task failure'\n"
            )
        else:
            get_body = (
                "    Add-Call -Action 'get' -Name $TaskName\n"
                f"    [Console]::Error.WriteLine('{escaped}')\n"
                "    throw 'stubbed scheduled task failure'\n"
            )
    stubs = "\n".join(
        [
            "$CallsPath = $env:MSOS_STUBBED_TASK_CALLS_PATH",
            "if (-not $CallsPath) { throw 'MSOS_STUBBED_TASK_CALLS_PATH is required.' }",
            "function Add-Call([string]$Action, [string]$Name) {",
            "    [pscustomobject]@{ action = $Action; name = $Name } |",
            "        ConvertTo-Json -Compress |",
            "        Add-Content -Path $CallsPath -Encoding UTF8",
            "}",
            "function Get-ScheduledTask {",
            "    param([string]$TaskName, [object]$ErrorAction)",
            get_body.rstrip(),
            "}",
            "function Stop-ScheduledTask {",
            "    param([string]$TaskName, [object]$ErrorAction)",
            "    Add-Call -Action 'stop' -Name $TaskName",
            "}",
            "function Enable-ScheduledTask {",
            "    param([string]$TaskName, [object]$ErrorAction)",
            "    Add-Call -Action 'enable' -Name $TaskName",
            "}",
            "function Start-ScheduledTask {",
            "    param([string]$TaskName, [object]$ErrorAction)",
            "    Add-Call -Action 'start' -Name $TaskName",
            "}",
            "",
        ]
    )
    return script.replace(
        '$ErrorActionPreference = "Stop"',
        f'$ErrorActionPreference = "Stop"\n{stubs}',
    )


def _run_handoff_fixture(
    fixture: dict[str, object],
    powershell: str,
    tmp_path: Path,
    *,
    before_script: str = "",
    capture_output: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = fixture["repo"]
    supervisor = fixture["supervisor"]
    old_commit = fixture["old_commit"]
    new_commit = fixture["new_commit"]
    assert isinstance(repo, Path)
    assert isinstance(supervisor, Path)
    assert isinstance(old_commit, str)
    assert isinstance(new_commit, str)

    calls_path = tmp_path / "scheduled-task-calls.jsonl"
    nested_calls_path = tmp_path / "nested-scheduled-task-calls.jsonl"
    command = f"""
$CallsPath = '{calls_path.as_posix()}'
function Add-Call([string]$Action, [string]$Name) {{
    [pscustomobject]@{{ action = $Action; name = $Name }} |
        ConvertTo-Json -Compress |
        Add-Content -Path $CallsPath -Encoding UTF8
}}
function Get-ScheduledTask {{
    param([string]$TaskName, [object]$ErrorAction)
    Add-Call -Action 'get' -Name $TaskName
    if ($TaskName -eq 'MSOS Autobuilder Capacity-One Refill' -and -not $global:RefillRegistered) {{
        return $null
    }}
    if ($TaskName -eq 'MSOS Autobuilder Capacity-One Refill') {{
        $State = $global:RefillState
    }} else {{
        $State = 'Ready'
    }}
    [pscustomobject]@{{
        TaskName = $TaskName
        State = $State
        TaskPath = '\\'
        Actions = @([pscustomobject]@{{
            Execute = 'powershell.exe'
            Arguments = '-stub'
            WorkingDirectory = $null
        }})
        Triggers = @([pscustomobject]@{{ Enabled = $true }})
        Principal = [pscustomobject]@{{
            UserId = 'USER'
            LogonType = 'Interactive'
            RunLevel = 'Limited'
        }}
        Settings = [pscustomobject]@{{
            MultipleInstances = 'IgnoreNew'
            RestartCount = 3
            RestartInterval = 'PT1M'
        }}
        Description = "stub task"
    }}
}}
function Stop-ScheduledTask {{
    param([string]$TaskName, [object]$ErrorAction)
    Add-Call -Action 'stop' -Name $TaskName
}}
function Disable-ScheduledTask {{
    param([string]$TaskName, [object]$ErrorAction)
    Add-Call -Action 'disable' -Name $TaskName
    if ($TaskName -eq 'MSOS Autobuilder Capacity-One Refill') {{ $global:RefillState = 'Disabled' }}
    [pscustomobject]@{{ TaskName = $TaskName; State = 'Disabled' }}
}}
function Enable-ScheduledTask {{
    param([string]$TaskName, [object]$ErrorAction)
    Add-Call -Action 'enable' -Name $TaskName
    [pscustomobject]@{{ TaskName = $TaskName; State = 'Ready' }}
}}
$global:RefillRegistered = $false
$global:RefillState = 'Ready'
function Export-ScheduledTask {{
    param([string]$TaskName)
    Add-Call -Action 'export' -Name $TaskName
    '<Task></Task>'
}}
function Unregister-ScheduledTask {{
    param([string]$TaskName, [switch]$Confirm)
    Add-Call -Action 'unregister' -Name $TaskName
    if ($TaskName -eq 'MSOS Autobuilder Capacity-One Refill') {{
        $global:RefillRegistered = $false
    }}
}}
function New-ScheduledTaskAction {{
    param([string]$Execute, [string]$Argument)
    [pscustomobject]@{{ Execute = $Execute; Arguments = $Argument }}
}}
function New-ScheduledTaskTrigger {{
    param([string]$User, [switch]$AtLogOn)
    [pscustomobject]@{{ UserId = $User; AtLogOn = [bool]$AtLogOn }}
}}
function New-ScheduledTaskPrincipal {{
    param([string]$UserId, [string]$LogonType, [string]$RunLevel)
    [pscustomobject]@{{ UserId = $UserId; LogonType = $LogonType; RunLevel = $RunLevel }}
}}
function New-ScheduledTaskSettingsSet {{
    param(
        [switch]$AllowStartIfOnBatteries,
        [switch]$DontStopIfGoingOnBatteries,
        [string]$MultipleInstances,
        [int]$RestartCount,
        [timespan]$RestartInterval
    )
    [pscustomobject]@{{
        MultipleInstances = $MultipleInstances
        RestartCount = $RestartCount
        RestartInterval = $RestartInterval
    }}
}}
function Register-ScheduledTask {{
    param(
        [string]$TaskName,
        [object]$Action,
        [object]$Trigger,
        [object]$Principal,
        [object]$Settings,
        [string]$Description,
        [string]$Xml,
        [switch]$Force
    )
    Add-Call -Action 'register' -Name $TaskName
    if ($TaskName -eq 'MSOS Autobuilder Capacity-One Refill') {{
        $global:RefillRegistered = $true
    }}
}}
$env:MSOS_STUBBED_TASK_CALLS_PATH = '{nested_calls_path.as_posix()}'
{before_script}
& '{BOOTSTRAP_HANDOFF.as_posix()}' `
    -Commit '{new_commit}' `
    -ExpectedOldBootstrapCommit '{old_commit}' `
    -RepoRoot '{repo.as_posix()}' `
    -HostRoot '{(tmp_path / "host").as_posix()}' `
    -SupervisorRoot '{supervisor.as_posix()}' `
    -BootstrapPython '{Path(sys.executable).as_posix()}'
"""
    argv = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
    if capture_output:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
    else:
        stdout_path = tmp_path / "outer-handoff-stdout.txt"
        stderr_path = tmp_path / "outer-handoff-stderr.txt"
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w",
            encoding="utf-8",
        ) as stderr:
            completed = subprocess.run(
                argv,
                stdout=stdout,
                stderr=stderr,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=60,
            )
        result = subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            stdout_path.read_text(encoding="utf-8"),
            stderr_path.read_text(encoding="utf-8"),
        )
    return result, calls_path


def test_stable_bootstrap_handoff_runs_with_temp_root_and_stubbed_tasks(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    _convert_installed_bootstrap_to_crlf(fixture)
    live_bootstrap = fixture["live_bootstrap"]
    assert isinstance(live_bootstrap, Path)
    result, calls_path = _run_handoff_fixture(fixture, powershell, tmp_path)

    assert result.returncode == 0, result.stderr or result.stdout
    assert (live_bootstrap / "self_update_evidence_relay.py").read_text(encoding="utf-8") == (
        "print('relay fixture v2')\n"
    )
    report = _read_handoff_report(fixture)
    assert report["outcome"] == "success"
    assert report["old_bootstrap_commit"] == fixture["old_commit"]
    assert report["new_bootstrap_commit"] == fixture["new_commit"]
    assert report["rollback"]["performed"] is False
    assert report["update_task"]["restored"] is True
    assert report["service_configuration"]["preflight"]["active_release"]["exists"] is False
    assert report["service_configuration"]["supervisor.yaml"]["activated"]["exists"] is True
    assert report["service_configuration"]["managed-services.json"]["activated"]["exists"] is True
    assert report["scheduled_tasks"]["preflight"]["MSOS Autobuilder Capacity-One Refill"][
        "exists"
    ] is False
    assert report["scheduled_tasks"]["staged_refill"]["state"] == "Disabled"
    assert "self_update_evidence_relay.py" in report["file_hashes"]
    relay_hashes = report["file_hashes"]["self_update_evidence_relay.py"]
    assert relay_hashes["installed_sha256"] != relay_hashes["expected_old_commit_sha256"]
    assert (
        relay_hashes["installed_canonical_sha256"]
        == relay_hashes["expected_old_commit_canonical_sha256"]
    )
    assert (
        relay_hashes["new_checkout_canonical_sha256"]
        == relay_hashes["new_commit_canonical_sha256"]
    )
    assert relay_hashes["staged_sha256"] == relay_hashes["new_checkout_sha256"]
    assert relay_hashes["activated_sha256"] == relay_hashes["staged_sha256"]
    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8-sig").splitlines()
    ]
    update_task_actions = [
        call["action"]
        for call in calls
        if call["name"] == "MSOS Autobuilder Update Supervisor"
    ]
    assert update_task_actions == [
        "get",
        "stop",
        "get",
        "get",
        "disable",
        "get",
        "enable",
        "get",
    ]
    nested_calls = [
        json.loads(line)
        for line in (tmp_path / "nested-scheduled-task-calls.jsonl")
        .read_text(encoding="utf-8-sig")
        .splitlines()
    ]
    assert [call["name"] for call in nested_calls if call["action"] == "get"] == MANAGED_TASK_NAMES
    assert any(
        call["action"] == "register" and call["name"] == "MSOS Autobuilder Capacity-One Refill"
        for call in calls
    )
    assert any(
        call["action"] == "disable" and call["name"] == "MSOS Autobuilder Capacity-One Refill"
        for call in calls
    )


def test_stable_bootstrap_handoff_runs_with_spaced_fixture_and_probe_paths(
    tmp_path: Path,
) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows path quoting regression requires Windows")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture_root = tmp_path / "fixture root with spaces"
    run_root = fixture_root / "run artifacts with spaces"
    probe_temp = fixture_root / "probe output temp with spaces"
    probe_temp.mkdir(parents=True)
    fixture = _build_stable_bootstrap_handoff_fixture(fixture_root)
    before_script = f"""
$SpacedTemp = '{probe_temp.as_posix()}'
$env:TEMP = $SpacedTemp
$env:TMP = $SpacedTemp
"""

    result, _calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        run_root,
        before_script=before_script,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    report = _read_handoff_report(fixture)
    assert report["outcome"] == "success"
    assert report["activation"]["performed"] is True
    assert not list(probe_temp.glob("msos-bootstrap-task-transport-*"))


def test_stable_bootstrap_handoff_rejects_substantive_installed_mutation_with_partial_evidence(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    live_bootstrap = fixture["live_bootstrap"]
    assert isinstance(live_bootstrap, Path)
    mutated = live_bootstrap / "self_update_evidence_relay.py"
    original = mutated.read_bytes()
    mutated.write_bytes(original[:8] + bytes([original[8] ^ 1]) + original[9:])

    result, calls_path = _run_handoff_fixture(fixture, powershell, tmp_path)

    assert result.returncode != 0
    assert mutated.read_bytes() != original
    assert (
        (live_bootstrap / "self_update_evidence_relay.py").read_bytes()
        != b"print('relay fixture v2')\n"
    )
    report = _read_handoff_report(fixture)
    assert report["outcome"] == "failed"
    assert report["activation"]["performed"] is False
    assert report["rollback"]["performed"] is False
    assert report["update_task"]["restored"] is True
    assert report["update_task"]["final_state"] == "Ready"
    hashes = report["file_hashes"]
    assert "self_update_supervisor.py" in hashes
    assert "self_update_evidence_relay.py" in hashes
    mutation_hashes = hashes["self_update_evidence_relay.py"]
    assert mutation_hashes["installed_sha256"] != mutation_hashes["expected_old_commit_sha256"]
    assert (
        mutation_hashes["installed_canonical_sha256"]
        != mutation_hashes["expected_old_commit_canonical_sha256"]
    )
    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8-sig").splitlines()
    ]
    update_task_actions = [
        call["action"]
        for call in calls
        if call["name"] == "MSOS Autobuilder Update Supervisor"
    ]
    assert update_task_actions == ["get", "enable", "get"]


def test_stable_bootstrap_handoff_retains_complete_python_stderr_on_probe_failure(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    stderr_begin = "BEGIN-LARGE-STUBBED-STDERR"
    stderr_end = "END-LARGE-STUBBED-STDERR"
    fixture = _build_stable_bootstrap_handoff_fixture(
        tmp_path,
        task_control_stderr_failure=stderr_begin + "{0}" + stderr_end,
        task_control_stderr_repeat=80_000,
    )
    result, calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        capture_output=False,
    )

    assert result.returncode != 0
    report = _read_handoff_report(fixture)
    report_text = json.dumps(report)
    assert "Staged Python to PowerShell task-name transport failed" in report_text
    assert stderr_begin in report_text
    assert stderr_end in report_text
    assert "Traceback (most recent call last)" in report_text
    assert "SupervisorError" in report_text
    assert report["activation"]["performed"] is False
    assert report["update_task"]["restored"] is True
    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8-sig").splitlines()
    ]
    assert any(
        call["action"] == "enable"
        and call["name"] == "MSOS Autobuilder Update Supervisor"
        for call in calls
    )


def test_stable_bootstrap_handoff_restores_old_bootstrap_after_activation_failure(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    live_bootstrap = fixture["live_bootstrap"]
    target_names = fixture["target_names"]
    assert isinstance(live_bootstrap, Path)
    assert isinstance(target_names, dict)
    old_bytes = {
        target: (live_bootstrap / target).read_bytes()
        for target in target_names.values()
    }

    result, calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script="$env:MSOS_STABLE_BOOTSTRAP_HANDOFF_TEST_CORRUPT_ACTIVATED_FILE = '1'",
    )

    assert result.returncode != 0
    assert all(
        (live_bootstrap / target).read_bytes() == content
        for target, content in old_bytes.items()
    )
    relay_bytes = (live_bootstrap / "self_update_evidence_relay.py").read_bytes()
    assert b"relay fixture v2" not in relay_bytes
    reports = list((fixture["supervisor"] / "reports").glob("stable-bootstrap-update-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8-sig"))
    assert report["outcome"] == "failed"
    assert report["rollback"]["performed"] is True
    assert report["update_task"]["restored"] is True
    assert "activated_sha256" in report["file_hashes"]["self_update_supervisor.py"]
    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8-sig").splitlines()
    ]
    assert any(
        call["action"] == "enable"
        and call["name"] == "MSOS Autobuilder Update Supervisor"
        for call in calls
    )


def test_stable_bootstrap_handoff_report_write_failure_is_not_nominal_success(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    result, _ = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script="$env:MSOS_STABLE_BOOTSTRAP_HANDOFF_TEST_REPORT_WRITE_FAILURE = '1'",
    )

    assert result.returncode != 0
    assert "Stable supervisor bootstrap updated" not in result.stdout
    assert "Could not write stable bootstrap update report" in (result.stderr + result.stdout)
    assert not list((fixture["supervisor"] / "reports").glob("stable-bootstrap-update-*.json"))


def test_task_controller_round_trips_task_names_through_powershell_stdin(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    calls_path = tmp_path / "task-calls.jsonl"
    stubbed_task_control = tmp_path / "windows-self-update-task-control-stubbed.ps1"
    stubbed_task_control.write_text(_stubbed_task_control_script(), encoding="utf-8")

    from msos_autobuilder.self_update_supervisor import PowerShellTaskController

    task_names = MANAGED_TASK_NAMES
    controller = PowerShellTaskController(stubbed_task_control, executable=powershell)

    previous_calls_path = os.environ.get("MSOS_STUBBED_TASK_CALLS_PATH")
    os.environ["MSOS_STUBBED_TASK_CALLS_PATH"] = calls_path.as_posix()
    try:
        controller.stop(task_names)
        controller.start(task_names)
        states = controller.states(task_names)
    finally:
        if previous_calls_path is None:
            os.environ.pop("MSOS_STUBBED_TASK_CALLS_PATH", None)
        else:
            os.environ["MSOS_STUBBED_TASK_CALLS_PATH"] = previous_calls_path

    assert states == {name: "Running" for name in task_names}
    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8-sig").splitlines()
    ]
    assert [call["name"] for call in calls if call["action"] == "stop"] == task_names
    assert [call["name"] for call in calls if call["action"] == "enable"] == task_names
    assert [call["name"] for call in calls if call["action"] == "start"] == task_names
    assert [call["name"] for call in calls if call["action"] == "get"] == [
        *task_names,
        *task_names,
        *task_names,
    ]


def test_update_invoker_downloads_one_manifest_then_calls_stable_python() -> None:
    script = INVOKER.read_text(encoding="utf-8")

    assert "Invoke-WebRequest" in script
    assert "approved-update.yaml" in script
    assert "bootstrap-venv\\Scripts\\python.exe" in script
    assert "bootstrap\\self_update_supervisor.py" in script
    assert " apply --config " in script
    assert "last-successful-manifest.sha256" in script
    assert "Get-FileHash" in script
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
