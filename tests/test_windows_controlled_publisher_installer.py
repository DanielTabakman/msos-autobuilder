import re
import shutil
import subprocess
from pathlib import Path

import pytest


INSTALLER = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "install_windows_controlled_publisher.ps1"
)


def test_controlled_publisher_installer_is_draft_only_single_writer() -> None:
    script = INSTALLER.read_text(encoding="utf-8")

    assert "MSOS Autobuilder Controlled Publisher" in script
    assert "draft_pr_publication_enabled: true" in script
    assert "merge_enabled: false" in script
    assert "main_write_enabled: false" in script
    assert "product-writer-owner.json" in script
    assert "PPE_GIT_AUTONOMOUS_WRITES" in script
    assert "PPE_ALLOW_LEGACY_GIT_PUBLISH" in script
    assert "Disable-ScheduledTask" in script
    assert "Stop-Process" in script
    assert "MSOS Autobuilder Host" in script
    assert "MSOS Autobuilder Result Relay" in script
    assert "MSOS Autobuilder Candidate Gate" in script
    assert "MSOS Autobuilder Revision Loop" in script
    assert "autobuilder/$WitnessJobId" in script
    assert "git push --force" not in script.lower()
    assert "ppe-automerge: true" not in script


def test_controlled_publisher_installer_has_safe_powershell_interpolation() -> None:
    script = INSTALLER.read_text(encoding="utf-8")
    publisher_yaml = script.split('$PublisherYaml = @"', 1)[1].split('"@', 1)[0]

    assert "  ${WitnessYaml}:" in publisher_yaml
    assert re.search(r"(?m)^\s*\$[A-Za-z_][A-Za-z0-9_]*:", publisher_yaml) is None
    assert "if (-not $CutoverSucceeded)" in script
    assert "$CutoverSucceedd" not in script


def test_controlled_publisher_installer_parses_in_powershell() -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")

    command = (
        "$tokens=$null; $errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{INSTALLER.as_posix()}', "
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
