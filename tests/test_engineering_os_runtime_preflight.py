from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "engineering_os_runtime_preflight.ps1"


def test_runtime_preflight_is_read_only_and_reports_required_surfaces() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    required = [
        "engineering-os-runtime-preflight",
        "active-release.json",
        "service-witnesses",
        "refill-policy.json",
        "controlled-publisher.yaml",
        "Get-ScheduledTask",
        "login status",
        "mutations_performed = $false",
    ]
    for marker in required:
        assert marker in text

    forbidden = [
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Enable-ScheduledTask",
        "Disable-ScheduledTask",
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "Set-Content",
        "Add-Content",
        "Move-Item",
        "Remove-Item",
        "git push",
    ]
    for marker in forbidden:
        assert marker not in text