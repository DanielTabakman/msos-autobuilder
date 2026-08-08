from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from msos_autobuilder.controlled_publisher import load_publisher_config

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_windows_self_update_supervisor.ps1"
RUNNER = ROOT / "scripts" / "run_windows_managed_service.ps1"
TASK_CONTROL = ROOT / "scripts" / "windows_self_update_task_control.ps1"
INVOKER = ROOT / "scripts" / "invoke_windows_self_update.ps1"
ROLLBACK = ROOT / "scripts" / "rollback_windows_self_update.ps1"
BOOTSTRAP_HANDOFF = ROOT / "scripts" / "update_windows_stable_supervisor_bootstrap.ps1"
PROBE = ROOT / "scripts" / "managed_release_health_probe.py"
LEGACY_BOOTSTRAP_COMMIT = "cf1b9e7c0f9429a53ad66ca043fef27a89a49474"
SCRIPTS = (INSTALLER, RUNNER, TASK_CONTROL, INVOKER, ROLLBACK, BOOTSTRAP_HANDOFF)
MANAGED_TASK_NAMES = [
    "MSOS Autobuilder Host",
    "MSOS Autobuilder Result Relay",
    "MSOS Autobuilder Candidate Gate",
    "MSOS Autobuilder Revision Loop",
    "MSOS Autobuilder Controlled Publisher",
    "MSOS Autobuilder Capacity-One Refill",
]


def _legacy_supervisor_source() -> str:
    source = (ROOT / "src" / "msos_autobuilder" / "self_update_supervisor.py").read_text(
        encoding="utf-8"
    )
    source = re.sub(
        r"\n    def disable\(self, task_names: Sequence\[str\]\) -> None:\n"
        r"        if task_names:\n"
        r"            self\._invoke\(\"disable\", task_names\)\n",
        "\n",
        source,
        count=1,
    )
    source = re.sub(
        r"\n    def wait_for\(\n"
        r".*?"
        r"\n\ndef _read_active_pointer",
        r"""
    def wait_for(self, commit: str, not_before: datetime) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.health_timeout_seconds
        task_names = [task.task_name for task in self.config.managed_tasks]
        last_detail: dict[str, Any] = {}
        healthy_since: float | None = None
        while time.monotonic() < deadline:
            observed_at = time.monotonic()
            states = dict(self.task_controller.states(task_names))
            witnesses: dict[str, Any] = {}
            healthy = True
            for task in self.config.managed_tasks:
                state = states.get(task.task_name, "Missing")
                if state.lower() != "running":
                    healthy = False
                witness_path = self.config.witnesses_root / f"{task.service}.json"
                witness = _load_json(witness_path, {})
                witnesses[task.service] = witness
                try:
                    started_at = datetime.fromisoformat(str(witness.get("started_at")))
                except (TypeError, ValueError):
                    started_at = datetime.min.replace(tzinfo=UTC)
                if (
                    witness.get("release_commit") != commit
                    or witness.get("state") != "running"
                    or started_at < not_before
                    or not isinstance(witness.get("child_pid"), int)
                ):
                    healthy = False
            last_detail = {"task_states": states, "witnesses": witnesses}
            if healthy:
                if healthy_since is None:
                    healthy_since = observed_at
                if observed_at - healthy_since >= self.config.health_stability_seconds:
                    return {
                        **last_detail,
                        "stability_seconds": self.config.health_stability_seconds,
                    }
            else:
                healthy_since = None
            time.sleep(self.config.health_poll_seconds)
        raise SupervisorError(
            "managed tasks did not produce a complete post-cutover health witness: "
            + json.dumps(last_detail, sort_keys=True)
        )


def _read_active_pointer""",
        source,
        count=1,
        flags=re.DOTALL,
    )
    source = re.sub(
        r"\n\ndef _release_supports_refill\(release_path: Path\) -> bool:\n"
        r".*?"
        r"\n\ndef _write_active_pointer",
        "\n\ndef _write_active_pointer",
        source,
        count=1,
        flags=re.DOTALL,
    )
    assert "def _release_managed_tasks" not in source
    controller_source = source.split("class PowerShellTaskController:", 1)[1].split(
        "class FileHealthVerifier:",
        1,
    )[0]
    assert "def disable(self, task_names" not in controller_source
    assert "def wait_for(self, commit: str, not_before: datetime)" in source
    return source


def _legacy_task_control_source() -> str:
    script = TASK_CONTROL.read_text(encoding="utf-8")
    script = script.replace(
        '[ValidateSet("stop", "start", "disable", "states")]',
        '[ValidateSet("stop", "start", "states")]',
    )
    script = re.sub(
        r'\n    "disable" \{\n'
        r"        foreach \(\$Name in \$TaskNames\) \{\n"
        r".*?"
        r"\n    \}",
        "",
        script,
        count=1,
        flags=re.DOTALL,
    )
    assert '"disable"' not in script
    return script


def test_installer_preserves_external_supervisor_and_atomic_release_boundary() -> None:
    script = INSTALLER.read_text(encoding="utf-8")

    assert ".msos-autobuilder-supervisor" in script
    assert "bootstrap-venv" in script
    assert "versions" in script
    assert "active-release.json" in script
    assert "Move-Item -Force -Path $Temporary -Destination $Path" in script
    assert '[string]$TaskNamespace = ""' in script
    assert '$PSBoundParameters.ContainsKey("TaskNamespace")' in script
    assert "TaskNamespace was explicitly supplied but is blank" in script
    assert "$EffectiveTaskNamespace" in script
    assert "MSOS_INSTALLER_TASK_NAMESPACE_PROBE" in script
    assert "Resolve-InstallerTaskNames" in script
    assert "Assert-InstallerTaskNamespaceReady" in script
    assert "GetInvalidFileNameChars" in script
    assert "letters, digits, spaces, '.', '_', or '-'" in script
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
    assert script.index(
        "Assert-InstallerTaskNamespaceReady -ResolvedNames $ResolvedTaskNames"
    ) < script.index("New-ManagedTask -TaskName $Managed.task")
    assert script.index(
        "Assert-InstallerTaskNamespaceReady -ResolvedNames $ResolvedTaskNames"
    ) < script.index("Register-ScheduledTask -TaskName $UpdateTaskName")
    assert "Isolated task name collides with protected production task name" in script
    assert "overlaps protected Issue #50" in script
    assert "resolves through a reparse point" in script
    assert "Duplicate scheduled task name resolved" in script
    assert "Isolated pilot refill remains paused" in script
    assert "plans: {}" in script


PRODUCTION_TASK_NAMES = [
    *MANAGED_TASK_NAMES,
    "MSOS Autobuilder Update Supervisor",
]


def _powershell_test_env(*, userprofile_root: Path | None = None) -> dict[str, str]:
    """Ensure PowerShell subprocesses have a non-null USERPROFILE.

    Windows keeps the real USERPROFILE. Linux CI gets a deterministic test-owned path
    because Ubuntu runners leave USERPROFILE unset while the installer reads it for
    protected Issue #50 roots.
    """
    env = os.environ.copy()
    existing = (env.get("USERPROFILE") or "").strip()
    if os.name == "nt" and existing:
        return env
    if existing:
        return env
    root = userprofile_root or (
        Path(tempfile.gettempdir()) / "msos-autobuilder-ci-userprofile"
    )
    root.mkdir(parents=True, exist_ok=True)
    env["USERPROFILE"] = str(root)
    return env


def _installer_helper_prelude() -> str:
    return f"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$InstallerPath = '{INSTALLER.as_posix()}'
$Tokens = $null
$Errors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $InstallerPath,
    [ref]$Tokens,
    [ref]$Errors
)
if ($Errors -and $Errors.Count -gt 0) {{
    throw ("Installer parse failed: " + ($Errors | ForEach-Object {{ $_.ToString() }} | Out-String))
}}
$Wanted = @(
    'Get-NormalizedTaskNamespace',
    'Get-NamespacedTaskName',
    'Get-ProductionTaskNames',
    'Resolve-InstallerTaskNames',
    'Test-RootPathOverlap',
    'Assert-PathHasNoReparsePoints',
    'Assert-InstallerTaskNamespaceReady'
)
foreach ($Name in $Wanted) {{
    $FunctionAst = $Ast.Find({{
            param($Node)
            $Node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $Node.Name -eq $Name
        }}, $true) | Select-Object -First 1
    if (-not $FunctionAst) {{ throw "Missing installer helper: $Name" }}
    Invoke-Expression $FunctionAst.Extent.Text
}}
$script:MaxScheduledTaskNameLength = 238
$script:ProductionManagedTaskRoles = @(
    @{{ service = 'host'; role = 'Host' }},
    @{{ service = 'relay'; role = 'Result Relay' }},
    @{{ service = 'gate'; role = 'Candidate Gate' }},
    @{{ service = 'revision'; role = 'Revision Loop' }},
    @{{ service = 'publisher'; role = 'Controlled Publisher' }},
    @{{ service = 'refill'; role = 'Capacity-One Refill' }}
)
$script:UpdateSupervisorRole = 'Update Supervisor'
$script:ProtectedProductionTaskNames = @(
    'MSOS Autobuilder Host',
    'MSOS Autobuilder Result Relay',
    'MSOS Autobuilder Candidate Gate',
    'MSOS Autobuilder Revision Loop',
    'MSOS Autobuilder Controlled Publisher',
    'MSOS Autobuilder Capacity-One Refill',
    'MSOS Autobuilder Update Supervisor'
)
$script:ProtectedProductionHostRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $env:USERPROFILE '.msos-autobuilder')
)
$script:ProtectedProductionSupervisorRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $env:USERPROFILE '.msos-autobuilder-supervisor')
)
"""


def _run_installer_helper_script(script: str) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")
    command = _installer_helper_prelude() + "\n" + script
    return subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=_powershell_test_env(),
    )


def _run_installer_binding_probe(
    tmp_path: Path,
    *,
    task_namespace: str | None = None,
    bind_task_namespace: bool = False,
    host_root: Path | None = None,
    supervisor_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real installer -File path; probe exits after binding validation."""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    host = host_root or (tmp_path / "binding-host")
    supervisor = supervisor_root or (tmp_path / "binding-supervisor")
    marker = tmp_path / "mutation-marker.txt"
    argv = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(INSTALLER),
        "-HostRoot",
        str(host),
        "-SupervisorRoot",
        str(supervisor),
    ]
    if bind_task_namespace:
        argv.extend(
            ["-TaskNamespace", task_namespace if task_namespace is not None else ""]
        )

    env = _powershell_test_env(userprofile_root=tmp_path / "ci-userprofile")
    env["MSOS_INSTALLER_TASK_NAMESPACE_PROBE"] = "1"
    # If validation somehow continued past the probe, later paths must not look like success.
    env["MSOS_BINDING_MUTATION_MARKER"] = str(marker)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
        cwd=str(ROOT),
    )


def _run_installer_config_generation_probe(
    tmp_path: Path,
    *,
    task_namespace: str | None,
    seed_production_configs: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real installer -File path through isolated config generation."""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    host = tmp_path / "isolated-host"
    supervisor = tmp_path / "isolated-supervisor"
    if seed_production_configs:
        host.mkdir(parents=True)
        for name in ("service.yaml", "host.yaml", "revision-loop.yaml"):
            (host / name).write_text(f"sentinel {name}\n", encoding="utf-8")
        (host / "candidate-gate.yaml").write_text(
            "version: 1\npublication_enabled: false\nplans: {}\n",
            encoding="utf-8",
        )
        (host / "controlled-publisher.yaml").write_text(
            "version: 1\n"
            "draft_pr_publication_enabled: false\n"
            "merge_enabled: false\n"
            "main_write_enabled: false\n"
            "plans: {}\n",
            encoding="utf-8",
        )
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    ).strip()
    release = supervisor / "versions" / commit
    release.mkdir(parents=True)
    (release / "release.json").write_text(
        json.dumps({"version": 1, "commit": commit, "release_id": f"bootstrap-{commit}"})
        + "\n",
        encoding="utf-8",
    )

    fake_codex = tmp_path / ("codex.cmd" if os.name == "nt" else "codex")
    fake_codex.write_text(
        "@echo off\r\nexit /b 0\r\n" if os.name == "nt" else "#!/bin/sh\nexit 0\n"
    )
    if os.name != "nt":
        fake_codex.chmod(0o755)

    env = _powershell_test_env(userprofile_root=tmp_path / "ci-userprofile")
    env["MSOS_INSTALLER_CONFIG_GENERATION_PROBE"] = "1"
    env["MSOS_INSTALLER_PROBE_PYTHON"] = sys.executable
    env["MSOS_AUTOBUILDER_SOURCE_PYTHON"] = sys.executable
    env["MSOS_AUTOBUILDER_CODEX_EXE"] = str(fake_codex)
    env["PYTHONPATH"] = str(ROOT / "src")

    argv = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(INSTALLER),
        "-HostRoot",
        str(host),
        "-SupervisorRoot",
        str(supervisor),
        "-MachineId",
        "ci-probe",
    ]
    if task_namespace is not None:
        argv.extend(["-TaskNamespace", task_namespace])

    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
        cwd=str(ROOT),
    )


def test_installer_default_task_namespace_preserves_production_names() -> None:
    result = _run_installer_helper_script(
        r"""
$Resolved = Resolve-InstallerTaskNames -Namespace ''
Assert-InstallerTaskNamespaceReady `
  -ResolvedNames $Resolved `
  -HostRootPath (Join-Path $env:USERPROFILE 'msos-test-host') `
  -SupervisorRootPath (Join-Path $env:USERPROFILE 'msos-test-supervisor')
$Names = @($Resolved.managed_tasks | ForEach-Object { $_.task }) + @($Resolved.update_task_name)
$Names | ConvertTo-Json -Compress
Write-Output ("isolated=" + $Resolved.isolated)
"""
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = result.stdout.strip().splitlines()
    names = json.loads(payload[0])
    assert names == PRODUCTION_TASK_NAMES
    assert payload[-1].strip() == "isolated=False"


def test_installer_binding_omitted_task_namespace_preserves_production_names(
    tmp_path: Path,
) -> None:
    host = tmp_path / "omitted-host"
    supervisor = tmp_path / "omitted-supervisor"
    result = _run_installer_binding_probe(
        tmp_path,
        bind_task_namespace=False,
        host_root=host,
        supervisor_root=supervisor,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["task_namespace_was_bound"] is False
    assert payload["effective_namespace"] == ""
    assert payload["isolated"] is False
    names = [entry["task"] for entry in payload["managed_tasks"]]
    names.append(payload["update_task_name"])
    assert names == PRODUCTION_TASK_NAMES
    assert not host.exists()
    assert not supervisor.exists()


@pytest.mark.parametrize(
    ("bound_value", "expected_fragment"),
    [
        ("", "explicitly supplied but is blank"),
        ("   ", "explicitly supplied but is blank"),
        ("Pilot:Issue119", "TaskNamespace is malformed"),
    ],
)
def test_installer_binding_rejects_blank_or_colon_task_namespace(
    tmp_path: Path,
    bound_value: str,
    expected_fragment: str,
) -> None:
    host = tmp_path / "reject-host"
    supervisor = tmp_path / "reject-supervisor"
    result = _run_installer_binding_probe(
        tmp_path,
        bind_task_namespace=True,
        task_namespace=bound_value,
        host_root=host,
        supervisor_root=supervisor,
    )
    assert result.returncode != 0
    combined = (result.stdout or "") + (result.stderr or "")
    assert expected_fragment in combined
    assert not host.exists()
    assert not supervisor.exists()


def test_isolated_installer_generates_all_managed_configs_before_task_mutation(
    tmp_path: Path,
) -> None:
    result = _run_installer_config_generation_probe(
        tmp_path,
        task_namespace="Pilot Issue119",
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["isolated"] is True

    host_root = Path(payload["host_root"])
    supervisor_root = Path(payload["supervisor_root"])
    generated = {key: Path(value) for key, value in payload["generated"].items()}
    for path in generated.values():
        assert path.exists(), path

    service = yaml.safe_load(generated["service"].read_text(encoding="utf-8"))
    host = yaml.safe_load(generated["host"].read_text(encoding="utf-8"))
    revision = yaml.safe_load(generated["revision"].read_text(encoding="utf-8"))
    gate = yaml.safe_load(generated["gate"].read_text(encoding="utf-8"))
    publisher = yaml.safe_load(generated["publisher"].read_text(encoding="utf-8"))
    refill_policy = json.loads(generated["refill_policy"].read_text(encoding="utf-8"))
    managed_services = json.loads(generated["managed_services"].read_text(encoding="utf-8"))

    assert service["version"] == 1
    assert service["publication_enabled"] is False
    assert Path(service["host_root"]) == host_root
    assert Path(service["codex_host_config"]) == host_root / "host.yaml"
    assert Path(service["supervisor_root"]) == supervisor_root
    assert service["job_feed"] == {
        "enabled": False,
        "repo_url": "https://github.com/DanielTabakman/msos-autobuilder.git",
        "branch": "jobs",
        "path": "jobs/approved",
        "refresh_seconds": 30,
    }
    assert not any(
        (host_root / relative).exists() for relative in ["queue/pending", "queue/running"]
    )

    assert host["version"] == 1
    assert host["publication_enabled"] is False
    assert Path(host["source_repo"]).is_relative_to(host_root)
    assert Path(host["workspace_root"]) == host_root / "workspaces"
    assert Path(host["runtime_root"]) == host_root / "runtime"
    assert host["codex"]["sandbox_mode"] == "workspace-write"
    assert host["codex"]["max_concurrency"] == 2

    assert revision["version"] == 1
    assert revision["publication_enabled"] is False
    assert Path(revision["host_root"]) == host_root
    assert revision["results_branch"] == "results"
    assert revision["jobs_branch"] == "jobs"
    assert revision["jobs_path"] == "jobs/approved"
    assert revision["max_revision_depth"] == 3
    assert revision["plans"] == {}

    assert gate["publication_enabled"] is False
    assert gate["plans"] == {}
    assert publisher["draft_pr_publication_enabled"] is False
    assert publisher["merge_enabled"] is False
    assert publisher["main_write_enabled"] is False
    assert publisher["plans"] == {}
    loaded_publisher = load_publisher_config(generated["publisher"])
    assert loaded_publisher.draft_pr_publication_enabled is False
    assert loaded_publisher.merge_enabled is False
    assert loaded_publisher.main_write_enabled is False
    assert loaded_publisher.plans == {}
    publisher_entry = subprocess.run(
        [
            sys.executable,
            "-m",
            "msos_autobuilder.controlled_publisher",
            "--config",
            str(generated["publisher"]),
            "--once",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert publisher_entry.returncode == 0, publisher_entry.stderr or publisher_entry.stdout
    publisher_entry_payload = json.loads(publisher_entry.stdout)
    assert publisher_entry_payload == {
        "draft_pr_publication_enabled": False,
        "main_write_enabled": False,
        "merge_enabled": False,
        "processed_jobs": [],
        "status": "completed",
    }
    publisher_success = json.loads(
        (host_root / "state" / "publisher-service-success.json").read_text(
            encoding="utf-8"
        )
    )
    assert publisher_success["associated_jobs"] == []
    assert publisher_success["terminal_evidence"] == {
        "draft_pr_publication_enabled": False,
        "main_write_enabled": False,
        "merge_enabled": False,
        "mode": "publication-disabled-idle",
        "processed_jobs": [],
        "verified_jobs": [],
    }
    assert not (host_root / "state" / "controlled-publisher-error.json").exists()
    assert refill_policy["enabled"] is False
    assert refill_policy["desired_capacity"] == 0
    assert refill_policy["status"] == "PAUSED"
    update_policy = json.loads(
        generated["update_supervisor_policy"].read_text(encoding="utf-8")
    )
    assert update_policy["autonomous_installation_enabled"] is False
    assert update_policy["mode"] == "disabled-idle"

    from msos_autobuilder.persistent_host import (
        HostPaths,
        PersistentHost,
        load_persistent_host_config,
    )
    from msos_autobuilder.refill_controller import RefillConfig, _supervisor_root

    loaded_service = load_persistent_host_config(generated["service"])
    assert loaded_service.feed is None
    host_paths = HostPaths.from_root(host_root)
    host_paths.ensure()
    assert not host_paths.feed_ledger.exists()
    assert list(host_paths.pending.iterdir()) == []
    assert list(host_paths.running.iterdir()) == []
    assert list(host_paths.failed.iterdir()) == []
    once = PersistentHost(loaded_service).run_once(sync_feed=True)
    assert once.processed is False
    assert not host_paths.feed_ledger.exists()
    assert list(host_paths.pending.iterdir()) == []
    assert list(host_paths.running.iterdir()) == []
    assert list(host_paths.failed.iterdir()) == []

    refill = RefillConfig.from_service_config(generated["service"])
    assert refill.supervisor_root == supervisor_root.resolve()
    assert refill.build_next.submit is False
    assert _supervisor_root(refill, host_paths) == supervisor_root.resolve()
    production_default = Path.home() / ".msos-autobuilder-supervisor"
    assert _supervisor_root(refill, host_paths) != production_default.resolve()
    assert str(production_default) not in str(_supervisor_root(refill, host_paths))

    for service_config in managed_services["services"].values():
        for arg in service_config["argv"]:
            if arg.startswith("{host_root}/"):
                assert (host_root / arg.removeprefix("{host_root}/")).exists()
        if "config_template" in service_config:
            assert Path(service_config["config_template"]).exists()

    assert not (supervisor_root / "state" / "active-release.json").exists()


def _run_bootstrap_evidence_helper_script(script: str) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")
    prelude = f"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$InstallerPath = '{INSTALLER.as_posix()}'
$Tokens = $null
$Errors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $InstallerPath,
    [ref]$Tokens,
    [ref]$Errors
)
if ($Errors -and $Errors.Count -gt 0) {{
    throw ("Installer parse failed: " + ($Errors | ForEach-Object {{ $_.ToString() }} | Out-String))
}}
$Wanted = @('Write-Utf8NoBom', 'Write-IsolatedBootstrapRelayFailureEvidence')
foreach ($Name in $Wanted) {{
    $FunctionAst = $Ast.Find({{
            param($Node)
            $Node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $Node.Name -eq $Name
        }}, $true) | Select-Object -First 1
    if (-not $FunctionAst) {{ throw "Missing installer helper: $Name" }}
    Invoke-Expression $FunctionAst.Extent.Text
}}
"""
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            prelude + "\n" + script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=_powershell_test_env(),
    )


def test_isolated_pilot_bootstrap_boundaries_are_empty_self_contained_and_update_idle() -> None:
    script = INSTALLER.read_text(encoding="utf-8")
    invoker = INVOKER.read_text(encoding="utf-8")
    assert "job_feed:\n  enabled: false" in script.replace("\r\n", "\n")
    assert "supervisor_root: $SupervisorRootYamlForConfig" in script
    assert "update-supervisor-policy.json" in script
    assert 'mode = "disabled-idle"' in script
    assert "autonomous_installation_enabled = $false" in script
    assert "Disable-ScheduledTask -TaskName $UpdateTaskName" in script
    assert "Write-IsolatedBootstrapRelayFailureEvidence" in script
    assert 'outcome = "blocked"' in script
    assert "requires_founder_attention = $true" in script
    assert "Isolated bootstrap evidence relay failed" in script
    assert script.index("Write-IsolatedBootstrapRelayFailureEvidence") < script.index(
        "throw $RelayFailureMessage"
    )
    assert script.index("throw $RelayFailureMessage") < script.index(
        'Write-Host "Fail-safe Autobuilder self-update supervisor installed."'
    )
    assert (
        "The scheduled updater will retry the durable local evidence automatically."
        in script
    )
    assert "disabled-idle" in invoker
    assert "update_attempted = $false" in invoker
    assert "installation_attempted = $false" in invoker
    assert script.index("Disable-ScheduledTask -TaskName $UpdateTaskName") > script.index(
        "Register-ScheduledTask -TaskName $UpdateTaskName"
    )
    assert "$script:ProductionManagedTaskRoles" in script
    assert len(PRODUCTION_TASK_NAMES) == 7


def test_isolated_bootstrap_relay_failure_rewrites_clean_success_evidence(
    tmp_path: Path,
) -> None:
    reports = tmp_path / "reports"
    notifications = tmp_path / "notifications"
    report = reports / "bootstrap-abc.json"
    notification = notifications / "bootstrap-abc.json"
    commit = "a" * 40
    supervisor = tmp_path / "isolated-supervisor"
    # Mirror installer: write success evidence first, then apply isolated fail-closed rewrite.
    seed = _run_bootstrap_evidence_helper_script(
        f"""
$Report = '{report.as_posix()}'
$Notification = '{notification.as_posix()}'
Write-Utf8NoBom -Path $Report -Value ((@{{
  version = 1
  type = 'initial-bootstrap'
  attempt_id = 'bootstrap-abc'
  outcome = 'success'
  requested_commit = '{commit}'
  commit = '{commit}'
  stable_supervisor_root = '{supervisor.as_posix()}'
  recorded_at = [DateTimeOffset]::UtcNow.ToString('o')
}} | ConvertTo-Json -Depth 10) + [Environment]::NewLine)
Write-Utf8NoBom -Path $Notification -Value ((@{{
  version = 1
  type = 'autobuilder-self-update'
  attempt_id = 'bootstrap-abc'
  outcome = 'success'
  requested_commit = '{commit}'
  report_path = $Report
  requires_founder_attention = $false
  recorded_at = [DateTimeOffset]::UtcNow.ToString('o')
}} | ConvertTo-Json -Depth 10) + [Environment]::NewLine)
$Message = Write-IsolatedBootstrapRelayFailureEvidence `
  -ReportPath $Report `
  -NotificationPath $Notification `
  -AttemptId 'bootstrap-abc' `
  -RequestedCommit '{commit}' `
  -SupervisorRootPath '{supervisor.as_posix()}' `
  -RelayExitCode 7 `
  -TaskStates @{{ update = 'Ready' }} `
  -ServiceWitnesses @{{ host = @{{ state = 'running' }} }} `
  -VersionPath '{(supervisor / "versions" / commit).as_posix()}' `
  -ManifestUrl 'https://example.test/latest.yaml' `
  -Note 'Isolated bootstrap remained blocked because required results-branch evidence relay failed.'
Write-Output $Message
$ReportJson = Get-Content -LiteralPath $Report -Raw | ConvertFrom-Json
$NotificationJson = Get-Content -LiteralPath $Notification -Raw | ConvertFrom-Json
@{{
  report_outcome = [string]$ReportJson.outcome
  notification_outcome = [string]$NotificationJson.outcome
  report_attention = [bool]$ReportJson.requires_founder_attention
  notification_attention = [bool]$NotificationJson.requires_founder_attention
  blocked_reason = [string]$ReportJson.blocked_reason
  relay_exit_code = [int]$ReportJson.relay_exit_code
  message = [string]$ReportJson.message
}} | ConvertTo-Json -Compress
"""
    )
    assert seed.returncode == 0, seed.stderr or seed.stdout
    lines = [line for line in seed.stdout.strip().splitlines() if line.strip()]
    assert "Isolated bootstrap evidence relay failed (exit 7)" in lines[0]
    payload = json.loads(lines[-1])
    assert payload["report_outcome"] == "blocked"
    assert payload["notification_outcome"] == "blocked"
    assert payload["report_attention"] is True
    assert payload["notification_attention"] is True
    assert payload["blocked_reason"] == "bootstrap_evidence_relay_failed"
    assert payload["relay_exit_code"] == 7
    assert "evidence relay failed" in payload["message"].lower()

    report_data = json.loads(report.read_text(encoding="utf-8"))
    notification_data = json.loads(notification.read_text(encoding="utf-8"))
    assert report_data["outcome"] != "success"
    assert notification_data["outcome"] != "success"
    assert notification_data["requires_founder_attention"] is True
    assert report_data["requires_founder_attention"] is True


def test_isolated_bootstrap_relay_failure_exits_nonzero_before_success_output(
    tmp_path: Path,
) -> None:
    report = tmp_path / "reports" / "bootstrap-abc.json"
    notification = tmp_path / "notifications" / "bootstrap-abc.json"
    result = _run_bootstrap_evidence_helper_script(
        f"""
$Report = '{report.as_posix()}'
$Notification = '{notification.as_posix()}'
Write-Utf8NoBom -Path $Report -Value ((@{{
  version = 1; type = 'initial-bootstrap'; attempt_id = 'bootstrap-abc'
  outcome = 'success'; requested_commit = '{'b' * 40}'; commit = '{'b' * 40}'
}} | ConvertTo-Json -Compress) + [Environment]::NewLine)
Write-Utf8NoBom -Path $Notification -Value ((@{{
  version = 1; type = 'autobuilder-self-update'; attempt_id = 'bootstrap-abc'
  outcome = 'success'; report_path = $Report; requires_founder_attention = $false
}} | ConvertTo-Json -Compress) + [Environment]::NewLine)
$IsolatedTaskNamespace = $true
$LASTEXITCODE = 9
if ($LASTEXITCODE -ne 0) {{
  if ($IsolatedTaskNamespace) {{
    $RelayFailureMessage = Write-IsolatedBootstrapRelayFailureEvidence `
      -ReportPath $Report `
      -NotificationPath $Notification `
      -AttemptId 'bootstrap-abc' `
      -RequestedCommit '{'b' * 40}' `
      -SupervisorRootPath '{(tmp_path / "supervisor").as_posix()}' `
      -RelayExitCode ([int]$LASTEXITCODE)
    throw $RelayFailureMessage
  }}
  Write-Warning 'production retry path'
}}
Write-Output 'Fail-safe Autobuilder self-update supervisor installed.'
"""
    )
    assert result.returncode != 0
    combined = (result.stdout or "") + (result.stderr or "")
    assert "Fail-safe Autobuilder self-update supervisor installed." not in combined
    assert "Isolated bootstrap evidence relay failed (exit 9)" in combined
    report_data = json.loads(report.read_text(encoding="utf-8"))
    notification_data = json.loads(notification.read_text(encoding="utf-8"))
    assert report_data["outcome"] == "blocked"
    assert notification_data["outcome"] == "blocked"
    assert notification_data["requires_founder_attention"] is True


def test_successful_isolated_bootstrap_evidence_remains_success_contract() -> None:
    script = INSTALLER.read_text(encoding="utf-8").replace("\r\n", "\n")
    success_report_write = script.index(
        'outcome = "success"\n    requested_commit = $CurrentCommit\n    commit = $CurrentCommit'
    )
    relay_invoke = script.index(
        "& $BootstrapPython $EvidenceRelayModule --config $SupervisorConfigPath"
    )
    isolated_failure = script.index("Write-IsolatedBootstrapRelayFailureEvidence `")
    production_warning = script.index(
        "The scheduled updater will retry the durable local evidence automatically."
    )
    assert success_report_write < relay_invoke < isolated_failure
    assert "requires_founder_attention = $false" in script
    assert production_warning > isolated_failure
    assert "Write-Warning" in script[isolated_failure:production_warning + 20]
    assert "if ($IsolatedTaskNamespace)" in script[relay_invoke:production_warning]


def test_omitted_task_namespace_does_not_overwrite_production_host_configs(
    tmp_path: Path,
) -> None:
    result = _run_installer_config_generation_probe(
        tmp_path,
        task_namespace=None,
        seed_production_configs=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["isolated"] is False

    host_root = Path(payload["host_root"])
    assert (host_root / "service.yaml").read_text(encoding="utf-8") == "sentinel service.yaml\n"
    assert (host_root / "host.yaml").read_text(encoding="utf-8") == "sentinel host.yaml\n"
    assert (
        (host_root / "revision-loop.yaml").read_text(encoding="utf-8")
        == "sentinel revision-loop.yaml\n"
    )
    supervisor_root = Path(payload["supervisor_root"])
    assert not (supervisor_root / "state" / "active-release.json").exists()
    assert not (supervisor_root / "state" / "update-supervisor-policy.json").exists()
    assert "update_supervisor_policy" not in payload.get("generated", {})


def test_installer_binding_pilot_namespace_generates_distinct_names(
    tmp_path: Path,
) -> None:
    host = tmp_path / "pilot-host"
    supervisor = tmp_path / "pilot-supervisor"
    protected_host = Path.home() / ".msos-autobuilder"
    protected_supervisor = Path.home() / ".msos-autobuilder-supervisor"
    # Keep pilot roots outside protected production defaults.
    host_key = str(host.resolve()).lower()
    protected_prefix = str(protected_host.resolve()).lower() + os.sep
    assert not host_key.startswith(protected_prefix)
    result = _run_installer_binding_probe(
        tmp_path,
        bind_task_namespace=True,
        task_namespace="Pilot Issue119",
        host_root=host,
        supervisor_root=supervisor,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["task_namespace_was_bound"] is True
    assert payload["effective_namespace"] == "Pilot Issue119"
    assert payload["isolated"] is True
    names = [entry["task"] for entry in payload["managed_tasks"]]
    names.append(payload["update_task_name"])
    expected = [
        "MSOS Autobuilder Pilot Issue119 Host",
        "MSOS Autobuilder Pilot Issue119 Result Relay",
        "MSOS Autobuilder Pilot Issue119 Candidate Gate",
        "MSOS Autobuilder Pilot Issue119 Revision Loop",
        "MSOS Autobuilder Pilot Issue119 Controlled Publisher",
        "MSOS Autobuilder Pilot Issue119 Capacity-One Refill",
        "MSOS Autobuilder Pilot Issue119 Update Supervisor",
    ]
    assert names == expected
    assert len(set(names)) == 7
    assert set(names).isdisjoint(PRODUCTION_TASK_NAMES)
    assert not host.exists()
    assert not supervisor.exists()
    del protected_supervisor


def test_installer_pilot_task_namespace_generates_distinct_names() -> None:
    result = _run_installer_helper_script(
        r"""
$Resolved = Resolve-InstallerTaskNames -Namespace 'Pilot Issue119'
$Names = @($Resolved.managed_tasks | ForEach-Object { $_.task }) + @($Resolved.update_task_name)
$Names | ConvertTo-Json -Compress
"""
    )
    assert result.returncode == 0, result.stderr or result.stdout
    names = json.loads(result.stdout.strip().splitlines()[-1])
    expected = [
        "MSOS Autobuilder Pilot Issue119 Host",
        "MSOS Autobuilder Pilot Issue119 Result Relay",
        "MSOS Autobuilder Pilot Issue119 Candidate Gate",
        "MSOS Autobuilder Pilot Issue119 Revision Loop",
        "MSOS Autobuilder Pilot Issue119 Controlled Publisher",
        "MSOS Autobuilder Pilot Issue119 Capacity-One Refill",
        "MSOS Autobuilder Pilot Issue119 Update Supervisor",
    ]
    assert names == expected
    assert len(set(names)) == 7
    assert set(names).isdisjoint(PRODUCTION_TASK_NAMES)


def test_installer_task_namespace_rejects_malformed_and_overlong_names(
    tmp_path: Path,
) -> None:
    host = tmp_path / "pilot-host"
    supervisor = tmp_path / "pilot-supervisor"
    protected_host = tmp_path / "protected-host"
    protected_supervisor = tmp_path / "protected-supervisor"
    for path in (host, supervisor, protected_host, protected_supervisor):
        path.mkdir()

    malformed = _run_installer_helper_script(
        f"""
$Resolved = Resolve-InstallerTaskNames -Namespace 'bad/name'
try {{
  Assert-InstallerTaskNamespaceReady -ResolvedNames $Resolved `
    -HostRootPath '{host.as_posix()}' `
    -SupervisorRootPath '{supervisor.as_posix()}' `
    -ProtectedHostRoot '{protected_host.as_posix()}' `
    -ProtectedSupervisorRoot '{protected_supervisor.as_posix()}'
  throw 'expected malformed namespace failure'
}} catch {{
  Write-Output $_.Exception.Message
  exit 0
}}
"""
    )
    assert malformed.returncode == 0, malformed.stderr or malformed.stdout
    assert "TaskNamespace is malformed" in malformed.stdout

    colon = _run_installer_helper_script(
        f"""
$Resolved = Resolve-InstallerTaskNames -Namespace 'Pilot:Issue119'
try {{
  Assert-InstallerTaskNamespaceReady -ResolvedNames $Resolved `
    -HostRootPath '{host.as_posix()}' `
    -SupervisorRootPath '{supervisor.as_posix()}' `
    -ProtectedHostRoot '{protected_host.as_posix()}' `
    -ProtectedSupervisorRoot '{protected_supervisor.as_posix()}'
  throw 'expected colon namespace failure'
}} catch {{
  Write-Output $_.Exception.Message
  exit 0
}}
"""
    )
    assert colon.returncode == 0, colon.stderr or colon.stdout
    assert "TaskNamespace is malformed" in colon.stdout

    illegal_name = _run_installer_helper_script(
        f"""
$Resolved = [pscustomobject]@{{
  namespace = 'Pilot'
  isolated = $true
  managed_tasks = @(
    [pscustomobject]@{{
      service = 'host'
      # '/' is invalid on Windows and Linux; '<' is Windows-only.
      task = 'MSOS Autobuilder Pilot/Host'
    }},
    [pscustomobject]@{{
      service = 'relay'
      task = 'MSOS Autobuilder Pilot Result Relay'
    }},
    [pscustomobject]@{{
      service = 'gate'
      task = 'MSOS Autobuilder Pilot Candidate Gate'
    }},
    [pscustomobject]@{{
      service = 'revision'
      task = 'MSOS Autobuilder Pilot Revision Loop'
    }},
    [pscustomobject]@{{
      service = 'publisher'
      task = 'MSOS Autobuilder Pilot Controlled Publisher'
    }},
    [pscustomobject]@{{
      service = 'refill'
      task = 'MSOS Autobuilder Pilot Capacity-One Refill'
    }}
  )
  update_task_name = 'MSOS Autobuilder Pilot Update Supervisor'
}}
try {{
  Assert-InstallerTaskNamespaceReady -ResolvedNames $Resolved `
    -HostRootPath '{host.as_posix()}' `
    -SupervisorRootPath '{supervisor.as_posix()}' `
    -ProtectedHostRoot '{protected_host.as_posix()}' `
    -ProtectedSupervisorRoot '{protected_supervisor.as_posix()}'
  throw 'expected illegal character failure'
}} catch {{
  Write-Output $_.Exception.Message
  exit 0
}}
"""
    )
    assert illegal_name.returncode == 0, illegal_name.stderr or illegal_name.stdout
    assert "contains illegal characters" in illegal_name.stdout

    overlong_name = "N" * 239
    overlong = _run_installer_helper_script(
        f"""
$Resolved = [pscustomobject]@{{
  namespace = 'Pilot'
  isolated = $true
  managed_tasks = @(
    [pscustomobject]@{{ service = 'host'; task = '{overlong_name}' }},
    [pscustomobject]@{{
      service = 'relay'
      task = 'MSOS Autobuilder Pilot Result Relay'
    }},
    [pscustomobject]@{{
      service = 'gate'
      task = 'MSOS Autobuilder Pilot Candidate Gate'
    }},
    [pscustomobject]@{{
      service = 'revision'
      task = 'MSOS Autobuilder Pilot Revision Loop'
    }},
    [pscustomobject]@{{
      service = 'publisher'
      task = 'MSOS Autobuilder Pilot Controlled Publisher'
    }},
    [pscustomobject]@{{
      service = 'refill'
      task = 'MSOS Autobuilder Pilot Capacity-One Refill'
    }}
  )
  update_task_name = 'MSOS Autobuilder Pilot Update Supervisor'
}}
try {{
  Assert-InstallerTaskNamespaceReady -ResolvedNames $Resolved `
    -HostRootPath '{host.as_posix()}' `
    -SupervisorRootPath '{supervisor.as_posix()}' `
    -ProtectedHostRoot '{protected_host.as_posix()}' `
    -ProtectedSupervisorRoot '{protected_supervisor.as_posix()}'
  throw 'expected overlong failure'
}} catch {{
  Write-Output $_.Exception.Message
  exit 0
}}
"""
    )
    assert overlong.returncode == 0, overlong.stderr or overlong.stdout
    assert "exceeds Windows limit" in overlong.stdout


def test_installer_task_namespace_rejects_collision_and_duplicate_names(
    tmp_path: Path,
) -> None:
    host = tmp_path / "pilot-host"
    supervisor = tmp_path / "pilot-supervisor"
    protected_host = tmp_path / "protected-host"
    protected_supervisor = tmp_path / "protected-supervisor"
    for path in (host, supervisor, protected_host, protected_supervisor):
        path.mkdir()

    collision = _run_installer_helper_script(
        f"""
$Resolved = [pscustomobject]@{{
  namespace = 'Pilot'
  isolated = $true
  managed_tasks = @(
    [pscustomobject]@{{ service = 'host'; task = 'MSOS Autobuilder Host' }},
    [pscustomobject]@{{
      service = 'relay'
      task = 'MSOS Autobuilder Pilot Result Relay'
    }},
    [pscustomobject]@{{
      service = 'gate'
      task = 'MSOS Autobuilder Pilot Candidate Gate'
    }},
    [pscustomobject]@{{
      service = 'revision'
      task = 'MSOS Autobuilder Pilot Revision Loop'
    }},
    [pscustomobject]@{{
      service = 'publisher'
      task = 'MSOS Autobuilder Pilot Controlled Publisher'
    }},
    [pscustomobject]@{{
      service = 'refill'
      task = 'MSOS Autobuilder Pilot Capacity-One Refill'
    }}
  )
  update_task_name = 'MSOS Autobuilder Pilot Update Supervisor'
}}
try {{
  Assert-InstallerTaskNamespaceReady -ResolvedNames $Resolved `
    -HostRootPath '{host.as_posix()}' `
    -SupervisorRootPath '{supervisor.as_posix()}' `
    -ProtectedHostRoot '{protected_host.as_posix()}' `
    -ProtectedSupervisorRoot '{protected_supervisor.as_posix()}'
  throw 'expected collision failure'
}} catch {{
  Write-Output $_.Exception.Message
  exit 0
}}
"""
    )
    assert collision.returncode == 0, collision.stderr or collision.stdout
    assert "collides with protected production task name" in collision.stdout

    duplicate = _run_installer_helper_script(
        f"""
$Resolved = [pscustomobject]@{{
  namespace = 'Pilot'
  isolated = $true
  managed_tasks = @(
    [pscustomobject]@{{
      service = 'host'
      task = 'MSOS Autobuilder Pilot Host'
    }},
    [pscustomobject]@{{
      service = 'relay'
      task = 'MSOS Autobuilder Pilot Host'
    }},
    [pscustomobject]@{{
      service = 'gate'
      task = 'MSOS Autobuilder Pilot Candidate Gate'
    }},
    [pscustomobject]@{{
      service = 'revision'
      task = 'MSOS Autobuilder Pilot Revision Loop'
    }},
    [pscustomobject]@{{
      service = 'publisher'
      task = 'MSOS Autobuilder Pilot Controlled Publisher'
    }},
    [pscustomobject]@{{
      service = 'refill'
      task = 'MSOS Autobuilder Pilot Capacity-One Refill'
    }}
  )
  update_task_name = 'MSOS Autobuilder Pilot Update Supervisor'
}}
try {{
  Assert-InstallerTaskNamespaceReady -ResolvedNames $Resolved `
    -HostRootPath '{host.as_posix()}' `
    -SupervisorRootPath '{supervisor.as_posix()}' `
    -ProtectedHostRoot '{protected_host.as_posix()}' `
    -ProtectedSupervisorRoot '{protected_supervisor.as_posix()}'
  throw 'expected duplicate failure'
}} catch {{
  Write-Output $_.Exception.Message
  exit 0
}}
"""
    )
    assert duplicate.returncode == 0, duplicate.stderr or duplicate.stdout
    assert "Duplicate scheduled task name resolved" in duplicate.stdout


def test_installer_task_namespace_rejects_protected_root_overlap_and_reparse(
    tmp_path: Path,
) -> None:
    protected_host = tmp_path / "protected-host"
    protected_supervisor = tmp_path / "protected-supervisor"
    pilot_supervisor = tmp_path / "pilot-supervisor"
    for path in (protected_host, protected_supervisor, pilot_supervisor):
        path.mkdir()

    overlap = _run_installer_helper_script(
        f"""
$Resolved = Resolve-InstallerTaskNames -Namespace 'Pilot Issue119'
try {{
  Assert-InstallerTaskNamespaceReady -ResolvedNames $Resolved `
    -HostRootPath '{(protected_host / "child").as_posix()}' `
    -SupervisorRootPath '{pilot_supervisor.as_posix()}' `
    -ProtectedHostRoot '{protected_host.as_posix()}' `
    -ProtectedSupervisorRoot '{protected_supervisor.as_posix()}'
  throw 'expected overlap failure'
}} catch {{
  Write-Output $_.Exception.Message
  exit 0
}}
"""
    )
    assert overlap.returncode == 0, overlap.stderr or overlap.stdout
    assert "overlaps protected Issue #50 host root" in overlap.stdout

    if os.name != "nt":
        return

    junction_parent = tmp_path / "junction-parent"
    junction_target = tmp_path / "junction-target"
    junction_parent.mkdir()
    junction_target.mkdir()
    junction = junction_parent / "linked-host"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(junction_target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip(
            f"Could not create reparse-point fixture: {created.stderr or created.stdout}"
        )

    reparse = _run_installer_helper_script(
        f"""
$Resolved = Resolve-InstallerTaskNames -Namespace 'Pilot Issue119'
try {{
  Assert-InstallerTaskNamespaceReady -ResolvedNames $Resolved `
    -HostRootPath '{junction.as_posix()}' `
    -SupervisorRootPath '{pilot_supervisor.as_posix()}' `
    -ProtectedHostRoot '{protected_host.as_posix()}' `
    -ProtectedSupervisorRoot '{protected_supervisor.as_posix()}'
  throw 'expected reparse failure'
}} catch {{
  Write-Output $_.Exception.Message
  exit 0
}}
"""
    )
    assert reparse.returncode == 0, reparse.stderr or reparse.stdout
    assert "resolves through a reparse point" in reparse.stdout


def test_installer_generated_supervisor_yaml_uses_selected_task_names() -> None:
    script = INSTALLER.read_text(encoding="utf-8")
    assert "$ManagedTasksYaml = ($ManagedTaskYamlLines -join" in script
    assert "managed_tasks:\n$ManagedTasksYaml" in script
    assert "$UpdateTaskName = [string]$ResolvedTaskNames.update_task_name" in script
    assert "if ($IsolatedTaskNamespace)" in script
    assert 'Join-Path $HostRoot "candidate-gate.yaml"' in script
    assert 'Join-Path $HostRoot "controlled-publisher.yaml"' in script
    assert "state\\refill-policy.json" in script or r"state\refill-policy.json" in script


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


def test_managed_runner_substitutes_every_template_placeholder() -> None:
    script = RUNNER.read_text(encoding="utf-8")
    render = next(line for line in script.splitlines() if "$Rendered = $Template" in line)

    assert '.Replace("{managed_release_root}"' in render
    assert '.Replace("{managed_python}"' in render
    assert '.Replace("{host_root}", $HostRoot.Replace("\\", "/"))' in render
    assert '.Replace("{machine_id}", $env:COMPUTERNAME)' in render
    assert "unsubstituted placeholder" in script


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

    legacy_imported = probe.probe_release(root, importer=importer)
    assert "msos_autobuilder.refill_controller" not in legacy_imported
    (root / "src" / "msos_autobuilder" / "refill_controller.py").parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    (root / "src" / "msos_autobuilder" / "refill_controller.py").write_text(
        "# fixture\n",
        encoding="utf-8",
    )
    imported = probe.probe_release(root, importer=importer)
    assert set(imported) == set(probe.MANAGED_MODULES)
    assert set(probe.probe_release(root, "host", importer=importer)) == {
        "msos_autobuilder.persistent_host"
    }

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

    assert '[ValidateSet("stop", "start", "disable", "states")]' in script
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
    assert "Refill task must remain exactly Disabled" in script
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
    health_timeout_seconds: float = 5,
    health_poll_seconds: float = 0.1,
    health_stability_seconds: float = 0,
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
                    legacy_interface=True,
                ),
                encoding="utf-8",
            )
        elif relative == "src/msos_autobuilder/self_update_supervisor.py":
            path.write_text(_legacy_supervisor_source(), encoding="utf-8")
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

    shutil.copy2(
        ROOT / "src" / "msos_autobuilder" / "self_update_supervisor.py",
        repo / "src/msos_autobuilder/self_update_supervisor.py",
    )
    (repo / "scripts/windows_self_update_task_control.ps1").write_text(
        _stubbed_task_control_script(
            stderr_failure=task_control_stderr_failure,
            stderr_repeat=task_control_stderr_repeat,
        ),
        encoding="utf-8",
    )
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
    host_root = tmp_path / "host"
    (live_bootstrap / "supervisor.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                f"supervisor_root: '{supervisor.as_posix()}'",
                f"host_root: '{host_root.as_posix()}'",
                "repo_url: 'https://github.com/DanielTabakman/msos-autobuilder.git'",
                "repository: 'DanielTabakman/msos-autobuilder'",
                "task_controller_script: "
                f"'{(live_bootstrap / 'windows_self_update_task_control.ps1').as_posix()}'",
                "release_probe_script: "
                f"'{(live_bootstrap / 'managed_release_health_probe.py').as_posix()}'",
                f"health_timeout_seconds: {health_timeout_seconds}",
                f"health_poll_seconds: {health_poll_seconds}",
                f"health_stability_seconds: {health_stability_seconds}",
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
    active_release = supervisor / "versions" / old_commit
    active_release.mkdir(parents=True)
    (active_release / "release.json").write_text(
        json.dumps({"version": 1, "commit": old_commit, "release_id": f"bootstrap-{old_commit}"})
        + "\n",
        encoding="utf-8",
    )
    state = supervisor / "state"
    state.mkdir()
    (state / "active-release.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commit": old_commit,
                "release_path": active_release.as_posix(),
                "activated_at": "2026-07-16T00:00:00Z",
            }
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
        "host_root": host_root,
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


def _convert_installed_bootstrap_to_six_service_baseline(fixture: dict[str, object]) -> None:
    live_bootstrap = fixture["live_bootstrap"]
    assert isinstance(live_bootstrap, Path)
    supervisor_yaml = live_bootstrap / "supervisor.yaml"
    supervisor_text = supervisor_yaml.read_text(encoding="utf-8")
    supervisor_text = supervisor_text.replace(
        "  - service: publisher\n    task_name: 'MSOS Autobuilder Controlled Publisher'\n",
        "  - service: publisher\n"
        "    task_name: 'MSOS Autobuilder Controlled Publisher'\n"
        "  - service: refill\n"
        "    task_name: 'MSOS Autobuilder Capacity-One Refill'\n",
    )
    supervisor_yaml.write_text(supervisor_text, encoding="utf-8")

    services_path = live_bootstrap / "managed-services.json"
    services = json.loads(services_path.read_text(encoding="utf-8"))
    services["services"]["refill"] = {
        "argv": [
            "-m",
            "msos_autobuilder",
            "refill-run",
            "--service-config",
            "{host_root}/service.yaml",
            "--interval-seconds",
            "30",
        ],
        "log_file": "{host_root}/logs/capacity-one-refill.log",
    }
    services_path.write_text(
        json.dumps(services, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _prepare_policy_paused_refill_runtime(
    fixture: dict[str, object],
    *,
    policy: dict[str, object] | str | None = None,
    witness_commit: str | None = None,
    witness_started_at: str | None = None,
    include_refill_controller: bool = True,
    include_witness: bool = True,
) -> dict[str, bytes | None]:
    supervisor = fixture["supervisor"]
    host_root = fixture["host_root"]
    active_commit = fixture["old_commit"]
    assert isinstance(supervisor, Path)
    assert isinstance(host_root, Path)
    assert isinstance(active_commit, str)

    active_release = supervisor / "versions" / active_commit
    if include_refill_controller:
        refill_controller = active_release / "src" / "msos_autobuilder" / "refill_controller.py"
        refill_controller.parent.mkdir(parents=True, exist_ok=True)
        refill_controller.write_text("# refill fixture\n", encoding="utf-8")

    state = supervisor / "state"
    previous_pointer = state / "previous-release.json"
    previous_pointer.write_text(
        json.dumps(
            {
                "version": 1,
                "commit": "0" * 40,
                "release_path": (supervisor / "versions" / ("0" * 40)).as_posix(),
                "activated_at": "2026-07-15T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if include_witness:
        witness_root = state / "service-witnesses"
        witness_root.mkdir(parents=True, exist_ok=True)
        (witness_root / "refill.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "service": "refill",
                    "state": "running",
                    "release_commit": witness_commit or active_commit,
                    "child_pid": 1,
                    "started_at": witness_started_at
                    or (datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    host_state = host_root / "state"
    host_state.mkdir(parents=True, exist_ok=True)

    protected_fixture_paths = {
        "queue/pending/job-a.json": "queued-a\n",
        "queue/running/job-running.json": "running-a\n",
        "state/jobs/job-a.json": "job-a\n",
        "state/feed-seen.json": "{}\n",
        "state/refill-generation.json": json.dumps(
            {
                "version": 1,
                "generation_id": "generation-a",
                "current_attempt": {"attempt_id": "attempt-a"},
                "prepared_dispatch": {"action_id": "action-a"},
            },
            sort_keys=True,
        )
        + "\n",
        "state/refill-generation-history/generation-a.json": "generation-history\n",
        "state/refill-generation-supersessions/generation-a.json": "supersession\n",
        "state/refill-evidence/sources/dispatch-prepared/generation-a/job-a.json": (
            "prepared-source\n"
        ),
        "state/refill-evidence/dispatch/prepared/attempt-a.json": "prepared\n",
        "state/refill-evidence/dispatch/submitted/attempt-a.json": "submitted\n",
        "state/refill-evidence/heads/dispatch/prepared/attempt-a.json": "prepared-head\n",
        "state/refill-evidence/heads/dispatch/submitted/attempt-a.json": "submitted-head\n",
        "state/results-relay-seen.json": "{}\n",
        "state/candidate-gate-seen.json": "{}\n",
        "state/revision-loop-seen.json": "{}\n",
        "state/controlled-publisher-seen.json": "{}\n",
        "state/host-evidence/execution/attempt-a.json": "host-evidence\n",
        "state/relay-evidence/result/attempt-a.json": "relay-evidence\n",
        "state/gate-evidence/validation/attempt-a.json": "gate-evidence\n",
        "state/revision-evidence/disposition/attempt-a.json": "revision-evidence\n",
        "state/publisher-evidence/publication-review/attempt-a.json": "publisher-evidence\n",
    }
    for relative, value in protected_fixture_paths.items():
        protected_path = host_root / relative
        protected_path.parent.mkdir(parents=True, exist_ok=True)
        protected_path.write_text(value, encoding="utf-8")
    if policy is None:
        policy = {
            "version": 1,
            "enabled": False,
            "desired_capacity": 0,
            "resume_desired_capacity": 1,
            "status": "PAUSED",
            "message": "Refill is paused; no new dispatch was attempted.",
        }
    policy_path = host_state / "refill-policy.json"
    if isinstance(policy, str):
        policy_path.write_text(policy, encoding="utf-8")
    else:
        policy_path.write_text(json.dumps(policy, sort_keys=True) + "\n", encoding="utf-8")

    protected_paths = [
        state / "active-release.json",
        state / "previous-release.json",
        policy_path,
        host_state / "refill-generation.json",
        host_state / "prepared-dispatch.json",
        host_state / "queue.json",
        host_state / "feed.json",
        host_state / "ledger.json",
        host_state / "lifecycle.json",
    ]
    for path in protected_paths[3:]:
        path.write_text(path.name + "\n", encoding="utf-8")
    return {
        path.as_posix(): path.read_bytes() if path.exists() else None
        for path in protected_paths
    }


def _running_refill_before_script(fixture: dict[str, object]) -> str:
    host_root = fixture["host_root"]
    supervisor = fixture["supervisor"]
    assert isinstance(host_root, Path)
    assert isinstance(supervisor, Path)
    runner = supervisor / "bootstrap" / "run_windows_managed_service.ps1"
    return f"""
$global:RefillRegistered = $true
$global:RefillState = 'Running'
$global:RefillAction = [pscustomobject]@{{
    Execute = 'pwsh'
    Arguments = (
        '-NoProfile -File "{runner.as_posix()}" ' +
        '-ServiceName refill ' +
        '-SupervisorRoot "{supervisor.as_posix()}" ' +
        '-HostRoot "{host_root.as_posix()}"'
    )
}}
"""


def _read_handoff_report(fixture: dict[str, object]) -> dict[str, object]:
    supervisor = fixture["supervisor"]
    assert isinstance(supervisor, Path)
    reports = list((supervisor / "reports").glob("stable-bootstrap-update-*.json"))
    assert len(reports) == 1
    return json.loads(reports[0].read_text(encoding="utf-8-sig"))


def _live_bootstrap_bytes(fixture: dict[str, object]) -> dict[str, bytes]:
    live_bootstrap = fixture["live_bootstrap"]
    target_names = fixture["target_names"]
    assert isinstance(live_bootstrap, Path)
    assert isinstance(target_names, dict)
    return {
        target: (live_bootstrap / target).read_bytes()
        for target in [*target_names.values(), "supervisor.yaml", "managed-services.json"]
    }


def _stubbed_task_control_script(
    *,
    state: str = "Running",
    stderr_failure: str | None = None,
    stderr_repeat: int = 0,
    legacy_interface: bool = False,
) -> str:
    if legacy_interface:
        script = _legacy_task_control_source()
    else:
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
            "$StubbedSupervisorRoot = $env:MSOS_STUBBED_SUPERVISOR_ROOT",
            "$script:StubbedStartCount = 0",
            "$script:ProtectedMutationApplied = $false",
            "function Add-Call([string]$Action, [string]$Name) {",
            "    [pscustomobject]@{ action = $Action; name = $Name } |",
            "        ConvertTo-Json -Compress |",
            "        Add-Content -Path $CallsPath -Encoding UTF8",
            "}",
            "function Get-ServiceName([string]$TaskName) {",
            "    if ($TaskName -eq 'MSOS Autobuilder Host') { return 'host' }",
            "    if ($TaskName -eq 'MSOS Autobuilder Result Relay') { return 'relay' }",
            "    if ($TaskName -eq 'MSOS Autobuilder Candidate Gate') { return 'gate' }",
            "    if ($TaskName -eq 'MSOS Autobuilder Revision Loop') { return 'revision' }",
            "    if ($TaskName -eq 'MSOS Autobuilder Controlled Publisher') { return 'publisher' }",
            "    if ($TaskName -eq 'MSOS Autobuilder Capacity-One Refill') { return 'refill' }",
            "    return $null",
            "}",
            "function Get-RefillDisabledMarker {",
            "    if (-not $StubbedSupervisorRoot) { return $null }",
            "    $StateRoot = Join-Path $StubbedSupervisorRoot 'state'",
            "    return Join-Path $StateRoot 'refill-disabled.marker'",
            "}",
            "function Write-StubbedWitness([string]$TaskName) {",
            "    if (-not $StubbedSupervisorRoot) { return }",
            "    $Service = Get-ServiceName $TaskName",
            "    if (-not $Service) { return }",
            "    if ($env:MSOS_STUBBED_SKIP_WITNESS_SERVICE -eq $Service) { return }",
            "    $StateRoot = Join-Path $StubbedSupervisorRoot 'state'",
            "    $ActivePath = Join-Path $StateRoot 'active-release.json'",
            "    if (-not (Test-Path $ActivePath -PathType Leaf)) { return }",
            "    $Active = Get-Content -Raw $ActivePath | ConvertFrom-Json",
            "    $WitnessRoot = Join-Path $StateRoot 'service-witnesses'",
            "    New-Item -ItemType Directory -Force -Path $WitnessRoot | Out-Null",
            "    $Json = (@{",
            "        version = 1",
            "        service = $Service",
            "        state = 'running'",
            "        release_commit = [string]$Active.commit",
            "        child_pid = 1",
            "        started_at = [DateTimeOffset]::UtcNow.ToString('o')",
            "    } | ConvertTo-Json -Depth 10) + [Environment]::NewLine",
            "    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)",
            "    $WitnessPath = Join-Path $WitnessRoot ($Service + '.json')",
            "    [System.IO.File]::WriteAllText($WitnessPath, $Json, $Utf8NoBom)",
            "}",
            "function Get-ScheduledTask {",
            "    param([string]$TaskName, [object]$ErrorAction)",
            "    $Marker = Get-RefillDisabledMarker",
            "    if (",
            "        $TaskName -eq 'MSOS Autobuilder Capacity-One Refill' -and",
            "        $Marker -and",
            "        (Test-Path $Marker -PathType Leaf)",
            "    ) {",
            "        Add-Call -Action 'get' -Name $TaskName",
            "        [pscustomobject]@{ TaskName = $TaskName; State = 'Disabled' }",
            "        return",
            "    }",
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
            "function Disable-ScheduledTask {",
            "    param([string]$TaskName, [object]$ErrorAction)",
            "    Add-Call -Action 'disable' -Name $TaskName",
            "    $Marker = Get-RefillDisabledMarker",
            "    if ($TaskName -eq 'MSOS Autobuilder Capacity-One Refill' -and $Marker) {",
            "        $MarkerParent = Split-Path -Parent $Marker",
            "        New-Item -ItemType Directory -Force -Path $MarkerParent | Out-Null",
            "        Set-Content -Path $Marker -Value 'disabled' -Encoding UTF8",
            "    }",
            "}",
            "function Start-ScheduledTask {",
            "    param([string]$TaskName, [object]$ErrorAction)",
            "    Add-Call -Action 'start' -Name $TaskName",
            "    $FailAfter = [int]($env:MSOS_STUBBED_FAIL_START_AFTER_COUNT -as [int])",
            "    $FailStartForCommit = $env:MSOS_STUBBED_FAIL_START_COMMIT",
            "    $ShouldFailStart = $true",
            "    if ($FailStartForCommit) {",
            "        $ShouldFailStart = $false",
            "        if ($StubbedSupervisorRoot) {",
            "            $StateRoot = Join-Path $StubbedSupervisorRoot 'state'",
            "            $ActivePath = Join-Path $StateRoot 'active-release.json'",
            "            if (Test-Path $ActivePath -PathType Leaf) {",
            "                $Active = Get-Content -Raw $ActivePath | ConvertFrom-Json",
            "                $ShouldFailStart = ([string]$Active.commit -eq $FailStartForCommit)",
            "            }",
            "        }",
            "    }",
            "    $FailOnceMarker = $env:MSOS_STUBBED_FAIL_START_ONCE_MARKER",
            "    if ($FailOnceMarker -and (Test-Path $FailOnceMarker -PathType Leaf)) {",
            "        $ShouldFailStart = $false",
            "    }",
            "    if ($ShouldFailStart -and $FailAfter -gt 0 -and (Get-ServiceName $TaskName) -and "
            "$TaskName -ne 'MSOS Autobuilder Capacity-One Refill') {",
            "        $script:StubbedStartCount += 1",
            "        if ($script:StubbedStartCount -ge $FailAfter) {",
            "            if ($FailOnceMarker) {",
            "                $MarkerParent = Split-Path -Parent $FailOnceMarker",
            "                New-Item -ItemType Directory -Force -Path $MarkerParent | Out-Null",
            "                Set-Content -Path $FailOnceMarker -Value 'failed' -Encoding UTF8",
            "            }",
            "            throw 'simulated restart witness start failure'",
            "        }",
            "    }",
            (
                "    if (-not $script:ProtectedMutationApplied -and "
                "$TaskName -eq 'MSOS Autobuilder Host') {"
            ),
            "        $MutationPath = $env:MSOS_STUBBED_PROTECTED_MUTATION_PATH",
            "        $MutationOperation = $env:MSOS_STUBBED_PROTECTED_MUTATION_OPERATION",
            "        if ($MutationPath -and $MutationOperation) {",
            "            $script:ProtectedMutationApplied = $true",
            "            switch ($MutationOperation) {",
            "                'append' { Add-Content -Path $MutationPath -Value 'changed' }",
            "                'create' {",
            "                    $MutationParent = Split-Path -Parent $MutationPath",
            (
                "                    New-Item -ItemType Directory -Force "
                "-Path $MutationParent | Out-Null"
            ),
            "                    New-Item -ItemType File -Force -Path $MutationPath | Out-Null",
            "                }",
            "                'delete' { Remove-Item -Force $MutationPath }",
            (
                "                default { throw 'unsupported protected "
                "mutation operation: $MutationOperation' }"
            ),
            "            }",
            "        }",
            "    }",
            "    $ActionChangeMarker = $env:MSOS_STUBBED_ACTION_CHANGE_MARKER",
            "    if ($ActionChangeMarker -and $TaskName -eq 'MSOS Autobuilder Host') {",
            "        $MarkerParent = Split-Path -Parent $ActionChangeMarker",
            "        New-Item -ItemType Directory -Force -Path $MarkerParent | Out-Null",
            "        Set-Content -Path $ActionChangeMarker -Value 'changed' -Encoding UTF8",
            "    }",
            "    Write-StubbedWitness $TaskName",
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
    host_root = fixture["host_root"]
    old_commit = fixture["old_commit"]
    new_commit = fixture["new_commit"]
    assert isinstance(repo, Path)
    assert isinstance(supervisor, Path)
    assert isinstance(host_root, Path)
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
function Get-ServiceName([string]$TaskName) {{
    if ($TaskName -eq 'MSOS Autobuilder Host') {{ return 'host' }}
    if ($TaskName -eq 'MSOS Autobuilder Result Relay') {{ return 'relay' }}
    if ($TaskName -eq 'MSOS Autobuilder Candidate Gate') {{ return 'gate' }}
    if ($TaskName -eq 'MSOS Autobuilder Revision Loop') {{ return 'revision' }}
    if ($TaskName -eq 'MSOS Autobuilder Controlled Publisher') {{ return 'publisher' }}
    if ($TaskName -eq 'MSOS Autobuilder Capacity-One Refill') {{ return 'refill' }}
    return $null
}}
function Write-OuterStubbedWitness([string]$TaskName) {{
    $Service = Get-ServiceName $TaskName
    if (-not $Service) {{ return }}
    $ActivePath = Join-Path (Join-Path '{supervisor.as_posix()}' 'state') 'active-release.json'
    if (-not (Test-Path $ActivePath -PathType Leaf)) {{ return }}
    $Active = Get-Content -Raw $ActivePath | ConvertFrom-Json
    $WitnessRoot = Join-Path (Join-Path '{supervisor.as_posix()}' 'state') 'service-witnesses'
    New-Item -ItemType Directory -Force -Path $WitnessRoot | Out-Null
    $Json = (@{{
        version = 1
        service = $Service
        state = 'running'
        release_commit = [string]$Active.commit
        child_pid = 1
        started_at = [DateTimeOffset]::UtcNow.ToString('o')
    }} | ConvertTo-Json -Depth 10) + [Environment]::NewLine
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $WitnessPath = Join-Path $WitnessRoot ($Service + '.json')
    [System.IO.File]::WriteAllText($WitnessPath, $Json, $Utf8NoBom)
}}
function Get-ScheduledTask {{
    param([string]$TaskName, [object]$ErrorAction)
    Add-Call -Action 'get' -Name $TaskName
    if ($TaskName -eq 'MSOS Autobuilder Capacity-One Refill' -and -not $global:RefillRegistered) {{
        return $null
    }}
    if ($TaskName -eq 'MSOS Autobuilder Capacity-One Refill') {{
        $State = $global:RefillState
        if (
            -not $global:RefillActionChanged -and
            $env:MSOS_STUBBED_ACTION_CHANGE_MARKER -and
            (Test-Path $env:MSOS_STUBBED_ACTION_CHANGE_MARKER -PathType Leaf)
        ) {{
            $global:RefillAction.Arguments += ' -HostRoot C:/conflict'
            $global:RefillActionChanged = $true
        }}
        if ($null -ne $global:RefillAction) {{
            $Execute = $global:RefillAction.Execute
            $Arguments = $global:RefillAction.Arguments
        }} else {{
            $Execute = 'powershell.exe'
            $Arguments = '-stub'
        }}
    }} elseif ($TaskName -eq 'MSOS Autobuilder Update Supervisor') {{
        $State = $global:UpdateTaskState
        $Execute = 'pwsh'
        $Arguments = '-NoProfile -File invoke_windows_self_update.ps1'
    }} else {{
        if (-not $global:ManagedTaskStates.ContainsKey($TaskName)) {{
            $global:ManagedTaskStates[$TaskName] = 'Ready'
        }}
        $State = $global:ManagedTaskStates[$TaskName]
        $Execute = 'pwsh'
        $Arguments = '-NoProfile -File run_windows_managed_service.ps1 -ServiceName host'
    }}
    [pscustomobject]@{{
        TaskName = $TaskName
        State = $State
        TaskPath = '\\'
        Actions = @([pscustomobject]@{{
            Execute = $Execute
            Arguments = $Arguments
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
    if ($TaskName -eq 'MSOS Autobuilder Update Supervisor') {{ $global:UpdateTaskState = 'Ready' }}
    if (Get-ServiceName $TaskName) {{
        $global:ManagedTaskStates[$TaskName] = 'Ready'
    }}
}}
function Disable-ScheduledTask {{
    param([string]$TaskName, [object]$ErrorAction)
    Add-Call -Action 'disable' -Name $TaskName
    if ($TaskName -eq 'MSOS Autobuilder Capacity-One Refill') {{
        $global:RefillState = 'Disabled'
        $Marker = Join-Path (Join-Path '{supervisor.as_posix()}' 'state') 'refill-disabled.marker'
        $MarkerParent = Split-Path -Parent $Marker
        New-Item -ItemType Directory -Force -Path $MarkerParent | Out-Null
        Set-Content -Path $Marker -Value 'disabled' -Encoding UTF8
    }}
    if ($TaskName -eq 'MSOS Autobuilder Update Supervisor') {{
        $global:UpdateTaskState = 'Disabled'
    }}
    [pscustomobject]@{{ TaskName = $TaskName; State = 'Disabled' }}
}}
function Enable-ScheduledTask {{
    param([string]$TaskName, [object]$ErrorAction)
    Add-Call -Action 'enable' -Name $TaskName
    if ($TaskName -eq 'MSOS Autobuilder Update Supervisor') {{ $global:UpdateTaskState = 'Ready' }}
    if (Get-ServiceName $TaskName) {{
        $global:ManagedTaskStates[$TaskName] = 'Ready'
    }}
    [pscustomobject]@{{ TaskName = $TaskName; State = 'Ready' }}
}}
function Start-ScheduledTask {{
    param([string]$TaskName, [object]$ErrorAction)
    Add-Call -Action 'start' -Name $TaskName
    if (Get-ServiceName $TaskName) {{
        $global:ManagedTaskStates[$TaskName] = 'Running'
    }}
    Write-OuterStubbedWitness $TaskName
}}
$global:RefillRegistered = $false
$global:RefillState = 'Ready'
$global:RefillAction = $null
$global:RefillActionChanged = $false
$global:UpdateTaskState = 'Ready'
$global:ManagedTaskStates = @{{}}
$global:UpdateTaskXml = @'
<Task><RegistrationInfo><URI>\\MSOS Autobuilder Update Supervisor</URI></RegistrationInfo></Task>
'@
function Export-ScheduledTask {{
    param([string]$TaskName)
    Add-Call -Action 'export' -Name $TaskName
    if ($TaskName -eq 'MSOS Autobuilder Update Supervisor') {{ return $global:UpdateTaskXml }}
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
        $global:RefillAction = $Action
    }}
    if ($TaskName -eq 'MSOS Autobuilder Update Supervisor') {{
        $global:UpdateTaskXml = $Xml
        $global:UpdateTaskState = 'Ready'
    }}
}}
$env:MSOS_STUBBED_TASK_CALLS_PATH = '{nested_calls_path.as_posix()}'
$env:MSOS_STUBBED_SUPERVISOR_ROOT = '{supervisor.as_posix()}'
{before_script}
& '{BOOTSTRAP_HANDOFF.as_posix()}' `
    -Commit '{new_commit}' `
    -ExpectedOldBootstrapCommit '{old_commit}' `
    -RepoRoot '{repo.as_posix()}' `
    -HostRoot '{host_root.as_posix()}' `
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
    active_release = report["service_configuration"]["preflight"]["active_release"]
    assert active_release["exists"] is True
    assert active_release["commit"] == fixture["old_commit"]
    assert active_release["release_path"].endswith(str(fixture["old_commit"]))
    assert report["service_configuration"]["supervisor.yaml"]["activated"]["exists"] is True
    assert report["service_configuration"]["managed-services.json"]["activated"]["exists"] is True
    assert report["service_configuration"]["staged_generation"]["semantic_change"][
        "added_task"
    ] == {
        "service": "refill",
        "task_name": "MSOS Autobuilder Capacity-One Refill",
    }
    assert report["scheduled_tasks"]["preflight"]["MSOS Autobuilder Capacity-One Refill"][
        "exists"
    ] is False
    assert "xml_sha256" in report["scheduled_tasks"]["preflight"]["MSOS Autobuilder Host"]
    assert report["scheduled_tasks"]["staged_refill"]["state"] == "Disabled"
    assert "xml_sha256" in report["scheduled_tasks"]["staged_refill"]
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
    assert update_task_actions[:4] == ["get", "export", "get", "export"]
    assert update_task_actions[4:] == [
        "stop",
        "get",
        "get",
        "disable",
        "get",
        "stop",
        "unregister",
        "register",
        "enable",
        "get",
        "export",
    ]
    assert "start" not in update_task_actions
    nested_calls = [
        json.loads(line)
        for line in (tmp_path / "nested-scheduled-task-calls.jsonl")
        .read_text(encoding="utf-8-sig")
        .splitlines()
    ]
    assert [call["name"] for call in nested_calls if call["action"] == "get"][
        : len(MANAGED_TASK_NAMES)
    ] == MANAGED_TASK_NAMES
    assert any(
        call["action"] == "register" and call["name"] == "MSOS Autobuilder Capacity-One Refill"
        for call in calls
    )
    assert any(
        call["action"] == "disable" and call["name"] == "MSOS Autobuilder Capacity-One Refill"
        for call in calls
    )


def test_stable_bootstrap_handoff_restores_running_updater_as_enabled_ready(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    live_bootstrap = fixture["live_bootstrap"]
    assert isinstance(live_bootstrap, Path)
    preflight_xml = (
        "<Task><RegistrationInfo><URI>\\MSOS Autobuilder Update Supervisor"
        "</URI><Description>preflight-running</Description></RegistrationInfo></Task>"
    )
    before_script = f"""
$global:UpdateTaskState = 'Running'
$global:UpdateTaskXml = '{preflight_xml}'
"""

    result, calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=before_script,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    report = _read_handoff_report(fixture)
    assert report["outcome"] == "success"
    assert report["rollback"]["performed"] is False
    assert report["update_task"]["initial_state"] == "Running"
    assert report["update_task"]["preflight"]["state"] == "Running"
    assert report["update_task"]["preflight_state"] == "Running"
    assert report["update_task"]["preflight"]["durable_enabled"] is True
    assert report["update_task"]["preflight_durable_enabled"] is True
    assert report["update_task"]["preflight_enabled_contract"] == "enabled"
    assert report["update_task"]["restore_enabled_contract"] == "enabled"
    assert report["update_task"]["final_enabled_contract"] == "enabled"
    assert report["update_task"]["expected_final_state"] == "Ready"
    assert report["update_task"]["final_state"] == "Ready"
    assert (
        report["update_task"]["final_xml_sha256"]
        == report["update_task"]["preflight_xml_sha256"]
    )
    assert (
        report["update_task"]["final_xml_sha256"]
        == report["update_task"]["preflight"]["xml_sha256"]
    )
    assert report["update_task"]["restored"] is True
    assert report["service_configuration"]["supervisor.yaml"]["activated"]["exists"] is True
    assert report["service_configuration"]["managed-services.json"]["activated"]["exists"] is True
    assert report["service_configuration"]["staged_generation"]["semantic_change"][
        "added_task"
    ] == {
        "service": "refill",
        "task_name": "MSOS Autobuilder Capacity-One Refill",
    }
    assert report["scheduled_tasks"]["staged_refill"]["state"] == "Disabled"
    assert (live_bootstrap / "supervisor.yaml").exists()
    assert (live_bootstrap / "managed-services.json").exists()

    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8-sig").splitlines()
    ]
    update_task_actions = [
        call["action"]
        for call in calls
        if call["name"] == "MSOS Autobuilder Update Supervisor"
    ]
    assert update_task_actions[:8] == [
        "get",
        "export",
        "get",
        "export",
        "stop",
        "get",
        "get",
        "disable",
    ]
    assert "start" not in update_task_actions
    assert "disable" in update_task_actions
    assert update_task_actions[-6:] == [
        "stop",
        "unregister",
        "register",
        "enable",
        "get",
        "export",
    ]

    refill_actions = [
        call["action"]
        for call in calls
        if call["name"] == "MSOS Autobuilder Capacity-One Refill"
    ]
    assert "register" in refill_actions
    assert "disable" in refill_actions


def test_stable_bootstrap_handoff_repairs_current_six_task_state_without_reregistering_refill(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    _convert_installed_bootstrap_to_six_service_baseline(fixture)
    host_root = fixture["host_root"]
    assert isinstance(host_root, Path)
    before_script = f"""
$global:RefillRegistered = $true
$global:RefillState = 'Disabled'
$global:RefillAction = [pscustomobject]@{{
    Execute = 'pwsh'
    Arguments = (
        '-NoProfile -File run_windows_managed_service.ps1 ' +
        '-ServiceName refill -HostRoot "{host_root.as_posix()}"'
    )
}}
"""

    result, calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=before_script,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    report = _read_handoff_report(fixture)
    assert report["outcome"] == "success"
    assert report["scheduled_tasks"]["baseline_mode"] == "six-task-disabled-refill"
    assert report["scheduled_tasks"]["preflight"]["MSOS Autobuilder Capacity-One Refill"][
        "state"
    ] == "Disabled"
    assert report["service_configuration"]["staged_generation"]["semantic_change"] == {
        "added_service": None,
        "added_task": None,
        "mode": "preserve-six-service-baseline",
    }
    restart = report["activation"]["legacy_restart_witness"]
    assert restart["selected_services"] == ["host", "relay", "gate", "revision", "publisher"]
    assert restart["disabled_services"] == ["refill"]
    assert restart["health"]["disabled_task_states"] == {"refill": "Disabled"}
    assert restart["health"]["service_set"] == [
        "host",
        "relay",
        "gate",
        "revision",
        "publisher",
    ]

    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8-sig").splitlines()
    ]
    refill_actions = [
        call["action"]
        for call in calls
        if call["name"] == "MSOS Autobuilder Capacity-One Refill"
    ]
    assert "register" not in refill_actions
    assert "unregister" not in refill_actions


def test_stable_bootstrap_handoff_accepts_running_policy_paused_refill(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    _convert_installed_bootstrap_to_six_service_baseline(fixture)
    protected_before = _prepare_policy_paused_refill_runtime(fixture)

    result, calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=_running_refill_before_script(fixture),
    )

    assert result.returncode == 0, result.stderr or result.stdout
    report = _read_handoff_report(fixture)
    assert report["outcome"] == "success"
    protected = report["protected_runtime_state"]
    assert protected["mode"] == "six-task-running-policy-paused"
    assert protected["before"]["paths"] == protected["after"]["paths"]
    assert protected["differences"] == []
    assert report["scheduled_tasks"]["baseline_mode"] == "six-task-running-policy-paused"
    assert report["scheduled_tasks"]["preflight"]["MSOS Autobuilder Capacity-One Refill"][
        "state"
    ] == "Running"
    assert report["scheduled_tasks"]["staged_refill"]["state"] == "Running"
    assert report["activation"]["refill_task_state"] == "Running"
    restart = report["activation"]["legacy_restart_witness"]
    assert restart["selected_services"] == [
        "host",
        "relay",
        "gate",
        "revision",
        "publisher",
        "refill",
    ]
    assert restart["disabled_services"] == []
    assert restart["health"]["service_set"] == [
        "host",
        "relay",
        "gate",
        "revision",
        "publisher",
        "refill",
    ]
    refill_witness = restart["health"]["witnesses"]["refill"]
    assert refill_witness["state"] == "running"
    assert refill_witness["release_commit"] == fixture["old_commit"]
    policy_preflight = report["service_configuration"]["preflight"]["refill_policy"]
    policy_post = report["service_configuration"]["post_handoff_invariants"]["refill_policy"]
    assert policy_preflight["sha256"] == policy_post["sha256"]
    assert policy_preflight["sha256"] == report["service_configuration"][
        "refill_policy_preflight"
    ]["sha256"]
    assert (
        report["service_configuration"]["preflight"]["active_release"]["sha256"]
        == report["service_configuration"]["post_handoff_invariants"]["active_release"]["sha256"]
    )
    assert (
        report["service_configuration"]["preflight"]["previous_release"]["sha256"]
        == report["service_configuration"]["post_handoff_invariants"]["previous_release"][
            "sha256"
        ]
    )
    for path_text, before in protected_before.items():
        path = Path(path_text)
        assert before is not None
        assert path.read_bytes() == before

    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8-sig").splitlines()
    ]
    refill_actions = [
        call["action"]
        for call in calls
        if call["name"] == "MSOS Autobuilder Capacity-One Refill"
    ]
    assert "register" not in refill_actions
    assert "unregister" not in refill_actions
    assert "disable" not in refill_actions


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        ({"version": 1, "enabled": True, "desired_capacity": 0}, "enabled exactly false"),
        ({"version": 1, "enabled": False, "desired_capacity": 1}, "desired_capacity exactly 0"),
        ({"version": 2, "enabled": False, "desired_capacity": 0}, "unsupported"),
        ("{not json", "malformed"),
    ],
)
def test_stable_bootstrap_handoff_rejects_running_refill_policy_contradictions(
    tmp_path: Path,
    policy: dict[str, object] | str,
    message: str,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    _convert_installed_bootstrap_to_six_service_baseline(fixture)
    _prepare_policy_paused_refill_runtime(fixture, policy=policy)

    result, _calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=_running_refill_before_script(fixture),
    )

    assert result.returncode != 0
    report = _read_handoff_report(fixture)
    assert message in json.dumps(report)
    assert report["activation"]["performed"] is False


def test_stable_bootstrap_handoff_rejects_running_refill_without_policy(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    _convert_installed_bootstrap_to_six_service_baseline(fixture)
    _prepare_policy_paused_refill_runtime(fixture)
    policy_path = fixture["host_root"] / "state" / "refill-policy.json"
    assert isinstance(policy_path, Path)
    policy_path.unlink()

    result, _calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=_running_refill_before_script(fixture),
    )

    assert result.returncode != 0
    report = _read_handoff_report(fixture)
    assert "requires an existing paused refill policy" in json.dumps(report)
    assert report["activation"]["performed"] is False


@pytest.mark.parametrize(
    ("witness_commit", "include_witness", "message"),
    [
        ("1" * 40, True, "match the active release commit"),
        (None, False, "requires a fresh refill service witness"),
    ],
)
def test_stable_bootstrap_handoff_rejects_running_refill_bad_witness(
    tmp_path: Path,
    witness_commit: str | None,
    include_witness: bool,
    message: str,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    _convert_installed_bootstrap_to_six_service_baseline(fixture)
    _prepare_policy_paused_refill_runtime(
        fixture,
        witness_commit=witness_commit,
        include_witness=include_witness,
    )

    result, _calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=_running_refill_before_script(fixture),
    )

    assert result.returncode != 0
    report = _read_handoff_report(fixture)
    assert message in json.dumps(report)
    assert report["activation"]["performed"] is False


@pytest.mark.parametrize(
    ("before_script_suffix", "message"),
    [
        (
            "$global:RefillAction.Arguments = "
            "'-NoProfile -File other.ps1 -ServiceName refill'",
            "missing required parameter -supervisorroot",
        ),
        (
            "$global:RefillAction.Arguments = "
            "$global:RefillAction.Arguments.Replace('-HostRoot', '-OtherRoot')",
            "unsupported parameter -OtherRoot",
        ),
    ],
)
def test_stable_bootstrap_handoff_rejects_running_refill_action_mismatch(
    tmp_path: Path,
    before_script_suffix: str,
    message: str,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    _convert_installed_bootstrap_to_six_service_baseline(fixture)
    _prepare_policy_paused_refill_runtime(fixture)

    result, _calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=_running_refill_before_script(fixture) + before_script_suffix,
    )

    assert result.returncode != 0
    report = _read_handoff_report(fixture)
    assert message in json.dumps(report)
    assert report["activation"]["performed"] is False


def test_stable_bootstrap_handoff_recovers_legacy_services_after_restart_witness_failure(
    tmp_path: Path,
) -> None:
    if sys.platform != "win32":
        pytest.skip("restart-witness recovery regression targets Windows Scheduled Task semantics")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(
        tmp_path,
        health_timeout_seconds=1,
        health_poll_seconds=0.05,
        health_stability_seconds=0.1,
    )
    _convert_installed_bootstrap_to_six_service_baseline(fixture)
    live_bootstrap = fixture["live_bootstrap"]
    host_root = fixture["host_root"]
    supervisor = fixture["supervisor"]
    assert isinstance(live_bootstrap, Path)
    assert isinstance(host_root, Path)
    assert isinstance(supervisor, Path)
    old_bytes = _live_bootstrap_bytes(fixture)
    before_script = f"""
$global:RefillRegistered = $true
$global:RefillState = 'Disabled'
$global:RefillAction = [pscustomobject]@{{
    Execute = 'pwsh'
    Arguments = (
        '-NoProfile -File run_windows_managed_service.ps1 ' +
        '-ServiceName refill -HostRoot "{host_root.as_posix()}"'
    )
}}
$env:MSOS_STUBBED_FAIL_START_AFTER_COUNT = '2'
$env:MSOS_STUBBED_FAIL_START_ONCE_MARKER = '{(tmp_path / "restart-failed.marker").as_posix()}'
"""

    result, calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=before_script,
    )

    assert result.returncode != 0
    assert all(
        (live_bootstrap / target).read_bytes() == content
        for target, content in old_bytes.items()
    )
    report = _read_handoff_report(fixture)
    expected_services = ["host", "relay", "gate", "revision", "publisher"]
    assert report["outcome"] == "rolled_back"
    assert report["activation"]["attempted"] is True
    assert report["activation"]["performed"] is True
    assert report["activation"]["legacy_restart_witness_started"] is True
    assert report["activation"]["live_task_mutation_touched"] is True
    assert report["activation"]["status"] == "restart_witness_failed"
    assert report["activation"]["selected_services"] == expected_services
    assert report["activation"]["disabled_services"] == ["refill"]
    assert "simulated restart witness start failure" in report["activation"][
        "legacy_restart_witness_error"
    ]
    assert report["rollback"]["performed"] is True
    assert report["rollback"]["service_recovery_passed"] is True
    recovery = report["rollback"]["service_recovery"]
    assert recovery["selected_services"] == expected_services
    assert recovery["disabled_services"] == ["refill"]
    assert recovery["legacy_interface"] == {
        "release_managed_tasks_helper": False,
        "task_controller_disable": False,
        "wait_for_arguments": 2,
    }
    assert recovery["outer_refill_disable_state"] == "Disabled"
    assert recovery["health_timeout_seconds"] == 1
    assert recovery["health_poll_seconds"] == 0.05
    assert recovery["configured_stability_seconds"] == 0.1
    assert recovery["achieved_stability_seconds"] >= 0.1
    assert recovery["disabled_task_states"] == {"refill": "Disabled"}
    assert recovery["active_commit"] == fixture["old_commit"]
    assert recovery["task_states"]["MSOS Autobuilder Capacity-One Refill"] == "Disabled"
    assert all(recovery["task_states"][task] == "Running" for task in MANAGED_TASK_NAMES[:-1])
    assert sorted(recovery["witnesses"]) == sorted(expected_services)
    for service in expected_services:
        witness_path = supervisor / "state" / "service-witnesses" / f"{service}.json"
        witness = json.loads(witness_path.read_text(encoding="utf-8-sig"))
        assert witness["state"] == "running"
        assert witness["release_commit"] == fixture["old_commit"]
        assert isinstance(witness["child_pid"], int)

    assert calls_path.read_text(encoding="utf-8-sig")
    nested_calls = [
        json.loads(line)
        for line in (tmp_path / "nested-scheduled-task-calls.jsonl")
        .read_text(encoding="utf-8-sig")
        .splitlines()
    ]
    assert [call["name"] for call in nested_calls if call["action"] == "start"] == (
        MANAGED_TASK_NAMES[:2] + MANAGED_TASK_NAMES[:-1]
    )
    assert not any(call["action"] == "disable" for call in nested_calls)
    outer_calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8-sig").splitlines()
    ]
    assert any(
        call["action"] == "disable"
        and call["name"] == "MSOS Autobuilder Capacity-One Refill"
        for call in outer_calls
    )


def test_stable_bootstrap_handoff_reports_rollback_failed_when_recovery_health_fails(
    tmp_path: Path,
) -> None:
    if sys.platform != "win32":
        pytest.skip("restart-witness recovery regression targets Windows Scheduled Task semantics")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(
        tmp_path,
        health_timeout_seconds=0.4,
        health_poll_seconds=0.05,
        health_stability_seconds=0.1,
    )
    _convert_installed_bootstrap_to_six_service_baseline(fixture)
    live_bootstrap = fixture["live_bootstrap"]
    host_root = fixture["host_root"]
    assert isinstance(live_bootstrap, Path)
    assert isinstance(host_root, Path)
    old_bytes = _live_bootstrap_bytes(fixture)
    before_script = f"""
$global:RefillRegistered = $true
$global:RefillState = 'Disabled'
$global:RefillAction = [pscustomobject]@{{
    Execute = 'pwsh'
    Arguments = (
        '-NoProfile -File run_windows_managed_service.ps1 ' +
        '-ServiceName refill -HostRoot "{host_root.as_posix()}"'
    )
}}
$env:MSOS_STUBBED_FAIL_START_AFTER_COUNT = '2'
$env:MSOS_STUBBED_FAIL_START_ONCE_MARKER = '{(tmp_path / "restart-failed.marker").as_posix()}'
$env:MSOS_STUBBED_SKIP_WITNESS_SERVICE = 'gate'
"""

    result, _calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=before_script,
    )

    assert result.returncode != 0
    assert all(
        (live_bootstrap / target).read_bytes() == content
        for target, content in old_bytes.items()
    )
    report = _read_handoff_report(fixture)
    assert report["outcome"] == "rollback_failed"
    assert report["activation"]["attempted"] is True
    assert report["activation"]["performed"] is True
    assert report["activation"]["status"] == "restart_witness_failed"
    assert report["rollback"]["performed"] is True
    assert report["rollback"]["service_recovery_passed"] is False
    error = report["rollback"]["service_recovery_error"]
    assert "Restored legacy recovery witness failed" in error
    assert "managed tasks did not produce a complete post-cutover health witness" in error
    assert '"gate": {}' in error
    assert "Rollback service recovery failed" in json.dumps(report["errors"])


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
    assert report["update_task"]["restored"] is False
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
    assert update_task_actions == []


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
    live_bootstrap = fixture["live_bootstrap"]
    assert isinstance(live_bootstrap, Path)
    old_bytes = _live_bootstrap_bytes(fixture)
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
    assert report["rollback"]["performed"] is False
    assert report["rollback"]["refill_task_restored"] is True
    assert report["update_task"]["restored"] is True
    assert live_bootstrap.exists()
    assert all(
        (live_bootstrap / target).read_bytes() == content
        for target, content in old_bytes.items()
    )
    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8-sig").splitlines()
    ]
    assert any(
        call["action"] == "enable"
        and call["name"] == "MSOS Autobuilder Update Supervisor"
        for call in calls
    )
    refill_actions = [
        call["action"]
        for call in calls
        if call["name"] == "MSOS Autobuilder Capacity-One Refill"
    ]
    assert refill_actions.count("unregister") == 1


def test_stable_bootstrap_handoff_restores_refill_absence_on_registration_failure(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    before_script = """
function Register-ScheduledTask {
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
    if ($TaskName -eq 'MSOS Autobuilder Capacity-One Refill') {
        $global:RefillRegistered = $true
        throw 'simulated refill registration failure'
    }
}
"""

    result, calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=before_script,
    )

    assert result.returncode != 0
    report = _read_handoff_report(fixture)
    assert report["activation"]["performed"] is False
    assert "simulated refill registration failure" in json.dumps(report)
    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8-sig").splitlines()
    ]
    refill_actions = [
        call["action"]
        for call in calls
        if call["name"] == "MSOS Autobuilder Capacity-One Refill"
    ]
    assert "register" in refill_actions
    assert "unregister" in refill_actions
    assert refill_actions.index("register") < refill_actions.index("unregister")


def test_stable_bootstrap_handoff_restores_refill_absence_on_disable_failure(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    before_script = """
function Disable-ScheduledTask {
    param([string]$TaskName, [object]$ErrorAction)
    Add-Call -Action 'disable' -Name $TaskName
    if ($TaskName -eq 'MSOS Autobuilder Capacity-One Refill') {
        $global:RefillState = 'Ready'
        throw 'simulated refill disable failure'
    }
    [pscustomobject]@{ TaskName = $TaskName; State = 'Disabled' }
}
"""

    result, calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=before_script,
    )

    assert result.returncode != 0
    report = _read_handoff_report(fixture)
    assert report["activation"]["performed"] is False
    assert "simulated refill disable failure" in json.dumps(report)
    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8-sig").splitlines()
    ]
    refill_actions = [
        call["action"]
        for call in calls
        if call["name"] == "MSOS Autobuilder Capacity-One Refill"
    ]
    assert "register" in refill_actions
    assert "disable" in refill_actions
    assert "unregister" in refill_actions
    assert refill_actions.index("register") < refill_actions.index("disable")
    assert refill_actions.index("disable") < refill_actions.index("unregister")


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
    live_bootstrap = fixture["live_bootstrap"]
    assert isinstance(live_bootstrap, Path)
    old_bytes = _live_bootstrap_bytes(fixture)

    result, calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script="$env:MSOS_STABLE_BOOTSTRAP_HANDOFF_TEST_REPORT_WRITE_FAILURE = '1'",
    )

    assert result.returncode != 0
    assert "Stable supervisor bootstrap updated" not in result.stdout
    assert "Could not write stable bootstrap update report" in (result.stderr + result.stdout)
    assert not list((fixture["supervisor"] / "reports").glob("stable-bootstrap-update-*.json"))
    assert all(
        (live_bootstrap / target).read_bytes() == content
        for target, content in old_bytes.items()
    )
    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8-sig").splitlines()
    ]
    refill_actions = [
        call["action"]
        for call in calls
        if call["name"] == "MSOS Autobuilder Capacity-One Refill"
    ]
    assert "unregister" in refill_actions


def test_stable_bootstrap_handoff_restores_old_bootstrap_on_updater_restore_failure(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    live_bootstrap = fixture["live_bootstrap"]
    assert isinstance(live_bootstrap, Path)
    old_bytes = _live_bootstrap_bytes(fixture)
    before_script = """
$global:UpdateRestoreFailureInjected = $false
function Enable-ScheduledTask {
    param([string]$TaskName, [object]$ErrorAction)
    Add-Call -Action 'enable' -Name $TaskName
    if (
        $TaskName -eq 'MSOS Autobuilder Update Supervisor' -and
        -not $global:UpdateRestoreFailureInjected
    ) {
        $global:UpdateRestoreFailureInjected = $true
        throw 'simulated updater restore failure'
    }
    [pscustomobject]@{ TaskName = $TaskName; State = 'Ready' }
}
"""

    result, calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=before_script,
    )

    assert result.returncode != 0
    assert all(
        (live_bootstrap / target).read_bytes() == content
        for target, content in old_bytes.items()
    )
    report = _read_handoff_report(fixture)
    assert report["outcome"] == "failed_after_activation"
    assert report["rollback"]["performed"] is True
    assert report["update_task"]["restored"] is True
    assert report["update_task"]["reregistered_from_preflight_xml"] is True
    assert report["update_task"]["final_state"] == report["update_task"]["preflight"]["state"]
    assert (
        report["update_task"]["final_xml_sha256"]
        == report["update_task"]["preflight"]["xml_sha256"]
    )
    assert "Failed to restore updater Scheduled Task" in json.dumps(report)
    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8-sig").splitlines()
    ]
    update_actions = [
        call["action"]
        for call in calls
        if call["name"] == "MSOS Autobuilder Update Supervisor"
    ]
    assert "unregister" in update_actions
    assert "register" in update_actions
    assert any(
        call["action"] == "unregister"
        and call["name"] == "MSOS Autobuilder Capacity-One Refill"
        for call in calls
    )


def test_stable_bootstrap_handoff_fails_closed_when_refill_task_preexists_enabled(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    before_script = "$global:RefillRegistered = $true; $global:RefillState = 'Ready'"

    result, calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=before_script,
    )

    assert result.returncode != 0
    report = _read_handoff_report(fixture)
    assert (
        "baseline requires refill to be absent, exactly Disabled, or exactly Running"
        in json.dumps(report)
    )
    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8-sig").splitlines()
    ]
    refill_actions = [
        call["action"]
        for call in calls
        if call["name"] == "MSOS Autobuilder Capacity-One Refill"
    ]
    assert refill_actions == ["get", "export"]


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
        controller.disable([task_names[-1]])
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
    assert [call["name"] for call in calls if call["action"] == "stop"] == [
        *task_names,
        task_names[-1],
    ]
    assert [call["name"] for call in calls if call["action"] == "enable"] == task_names
    assert [call["name"] for call in calls if call["action"] == "start"] == task_names
    assert [call["name"] for call in calls if call["action"] == "disable"] == [
        task_names[-1]
    ]
    assert [call["name"] for call in calls if call["action"] == "get"] == [
        *task_names,
        *task_names,
        task_names[-1],
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



def _set_refill_witness_started_at(fixture: dict[str, object], started_at: str) -> None:
    supervisor = fixture["supervisor"]
    assert isinstance(supervisor, Path)
    witness_path = supervisor / "state" / "service-witnesses" / "refill.json"
    witness = json.loads(witness_path.read_text(encoding="utf-8"))
    witness["started_at"] = started_at
    witness_path.write_text(json.dumps(witness, sort_keys=True) + "\n", encoding="utf-8")


def _restart_mutation_script(mutation_path: Path, operation: str) -> str:
    return f"""
$env:MSOS_STUBBED_PROTECTED_MUTATION_PATH = '{mutation_path.as_posix()}'
$env:MSOS_STUBBED_PROTECTED_MUTATION_OPERATION = '{operation}'
"""


@pytest.mark.parametrize(
    ("relative_path", "operation", "expected_change"),
    [
        (
            "queue/pending/job-a.json",
            "append",
            "child_content_changed",
        ),
        (
            "state/feed-seen.json",
            "append",
            "content_changed",
        ),
        (
            "state/refill-generation.json",
            "append",
            "content_changed",
        ),
        (
            "state/refill-evidence/dispatch/prepared/created-during-restart.json",
            "create",
            "child_appeared",
        ),
        (
            "state/refill-evidence/sources/dispatch-prepared/generation-a/job-a.json",
            "append",
            "child_content_changed",
        ),
        ("state/controlled-publisher-seen.json", "delete", "disappeared"),
        (
            "state/publisher-evidence/publication-review/attempt-a.json",
            "append",
            "child_content_changed",
        ),
    ],
)
def test_stable_bootstrap_handoff_rejects_protected_runtime_mutation(
    tmp_path: Path,
    relative_path: str,
    operation: str,
    expected_change: str,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    _convert_installed_bootstrap_to_six_service_baseline(fixture)
    _prepare_policy_paused_refill_runtime(fixture)
    host_root = fixture["host_root"]
    assert isinstance(host_root, Path)
    mutation_path = host_root / relative_path
    mutation_script = _restart_mutation_script(mutation_path, operation)

    result, _calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=_running_refill_before_script(fixture) + mutation_script,
    )

    assert result.returncode != 0
    report = _read_handoff_report(fixture)
    assert report["outcome"] != "success"
    protected = report["protected_runtime_state"]
    assert protected.get("snapshot_error") in (None, "")
    assert protected["before"] is not None
    assert protected["after"] is not None
    before_text = json.dumps(protected["before"])
    after_text = json.dumps(protected["after"])
    if operation == "create":
        assert mutation_path.is_file()
        assert Path(relative_path).name not in before_text
        assert Path(relative_path).name in after_text
    differences = protected["differences"]
    assert differences
    assert any(item["change"] == expected_change for item in differences)
    assert any(relative_path in item["relative_path"] for item in differences)
    assert "Protected runtime state changed" in json.dumps(report)
    assert "unsupported protected mutation operation" not in json.dumps(report)


@pytest.mark.parametrize("future_seconds", [0, 60])
def test_stable_bootstrap_handoff_accepts_recent_or_bounded_future_witness(
    tmp_path: Path,
    future_seconds: int,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    _convert_installed_bootstrap_to_six_service_baseline(fixture)
    started_at = (datetime.now(UTC) + timedelta(seconds=future_seconds)).isoformat()
    _prepare_policy_paused_refill_runtime(fixture, witness_started_at=started_at)

    result, _calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=_running_refill_before_script(fixture),
    )

    assert result.returncode == 0, result.stderr or result.stdout
    report = _read_handoff_report(fixture)
    time_evidence = report["service_configuration"]["refill_witness_preflight"]
    assert time_evidence["max_age_seconds"] == 600
    assert time_evidence["max_future_skew_seconds"] == 120
    assert time_evidence["started_at_utc"].endswith("+00:00")
    assert time_evidence["validated_at_utc"].endswith("+00:00")


@pytest.mark.parametrize(
    ("witness_case", "message"),
    [
        ("future", "future clock skew"),
        ("extreme_future", "future clock skew"),
        ("old", "fresh refill service witness"),
    ],
)
def test_stable_bootstrap_handoff_rejects_out_of_window_witness(
    tmp_path: Path,
    witness_case: str,
    message: str,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    _convert_installed_bootstrap_to_six_service_baseline(fixture)
    if witness_case == "future":
        started_at = (datetime.now(UTC) + timedelta(seconds=300)).isoformat()
    elif witness_case == "extreme_future":
        started_at = "2999-01-01T00:00:00+00:00"
    else:
        started_at = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    _prepare_policy_paused_refill_runtime(fixture, witness_started_at=started_at)

    result, _calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=_running_refill_before_script(fixture),
    )

    assert result.returncode != 0
    report = _read_handoff_report(fixture)
    assert message in json.dumps(report)
    assert report["activation"]["performed"] is False


@pytest.mark.parametrize(
    ("suffix", "message"),
    [
        (
            "$global:RefillAction.Execute = 'cmd.exe'; "
            "$global:RefillAction.Arguments = "
            "'/c echo run_windows_managed_service.ps1 -ServiceName refill'",
            "approved PowerShell executable",
        ),
        (
            "$global:RefillAction.Arguments = "
            "'-NoProfile -Command \"echo run_windows_managed_service.ps1\" '",
            "may not use PowerShell -Command",
        ),
        (
            "$global:RefillAction.Arguments = $global:RefillAction.Arguments.Replace("
            "'run_windows_managed_service.ps1', 'other.ps1 run_windows_managed_service.ps1')",
            "approved stable runner",
        ),
        ("$global:RefillAction.Execute = 'other-powershell.exe'", "approved PowerShell executable"),
        (
            "$global:RefillAction.Arguments = $global:RefillAction.Arguments.Replace("
            "'run_windows_managed_service.ps1', 'other.ps1')",
            "approved stable runner",
        ),
        (
            "$global:RefillAction.Arguments = $global:RefillAction.Arguments.Replace("
            "'-ServiceName refill', '-ServiceName other')",
            "ServiceName",
        ),
        (
            "$global:RefillAction.Arguments = $global:RefillAction.Arguments.Replace("
            "'-HostRoot', '-HostRootX')",
            "unsupported parameter",
        ),
        (
            "$global:RefillAction.Arguments = $global:RefillAction.Arguments.Replace("
            "'-SupervisorRoot', '-SupervisorRootX')",
            "unsupported parameter",
        ),
        (
            "$global:RefillAction.Arguments += ' -HostRoot C:/conflict'",
            "duplicate parameter",
        ),
    ],
)
def test_stable_bootstrap_handoff_rejects_noncanonical_refill_action(
    tmp_path: Path,
    suffix: str,
    message: str,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    _convert_installed_bootstrap_to_six_service_baseline(fixture)
    _prepare_policy_paused_refill_runtime(fixture)

    result, _calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=_running_refill_before_script(fixture) + suffix,
    )

    assert result.returncode != 0
    report = _read_handoff_report(fixture)
    assert message in json.dumps(report)
    assert report["activation"]["performed"] is False


@pytest.mark.parametrize(
    ("wrong_field", "message"),
    [("host", "HostRoot"), ("supervisor", "SupervisorRoot")],
)
def test_stable_bootstrap_handoff_rejects_wrong_refill_root_value(
    tmp_path: Path,
    wrong_field: str,
    message: str,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    _convert_installed_bootstrap_to_six_service_baseline(fixture)
    _prepare_policy_paused_refill_runtime(fixture)
    host_root = fixture["host_root"]
    supervisor = fixture["supervisor"]
    assert isinstance(host_root, Path)
    assert isinstance(supervisor, Path)
    runner = supervisor / "bootstrap" / "run_windows_managed_service.ps1"
    action_host = "C:/wrong-host-root" if wrong_field == "host" else host_root.as_posix()
    action_supervisor = (
        "C:/wrong-supervisor-root"
        if wrong_field == "supervisor"
        else supervisor.as_posix()
    )
    suffix = f"""
$global:RefillAction.Arguments = (
    '-NoProfile -File "{runner.as_posix()}" ' +
    '-ServiceName refill ' +
    '-SupervisorRoot "{action_supervisor}" ' +
    '-HostRoot "{action_host}"'
)
"""

    result, _calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=_running_refill_before_script(fixture) + suffix,
    )

    assert result.returncode != 0
    report = _read_handoff_report(fixture)
    assert message in json.dumps(report)
    assert report["activation"]["performed"] is False



def test_stable_bootstrap_handoff_rejects_refill_action_changed_during_restart(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    fixture = _build_stable_bootstrap_handoff_fixture(tmp_path)
    _convert_installed_bootstrap_to_six_service_baseline(fixture)
    _prepare_policy_paused_refill_runtime(fixture)
    action_change_marker = tmp_path / "refill-action-changed.marker"
    mutation_script = (
        "$env:MSOS_STUBBED_ACTION_CHANGE_MARKER = "
        f"'{action_change_marker.as_posix()}'"
    )

    result, _calls_path = _run_handoff_fixture(
        fixture,
        powershell,
        tmp_path,
        before_script=_running_refill_before_script(fixture) + mutation_script,
    )

    assert result.returncode != 0
    report = _read_handoff_report(fixture)
    assert "duplicate parameter" in json.dumps(report)
    assert report["scheduled_tasks"]["post_handoff_refill"]
    assert report["outcome"] != "success"
