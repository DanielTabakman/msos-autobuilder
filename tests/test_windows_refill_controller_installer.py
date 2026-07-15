from __future__ import annotations

from pathlib import Path

INSTALLER = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "install_windows_refill_controller.ps1"
)


def test_refill_installer_registers_capacity_one_task() -> None:
    script = INSTALLER.read_text(encoding="utf-8")

    assert '[string]$TaskName = "MSOS Autobuilder Capacity-One Refill"' in script
    assert "refill-run --service-config" in script
    assert "--interval-seconds $IntervalSeconds" in script
    assert "refill-status.json" in script


def test_refill_installer_keeps_build_next_only_boundary() -> None:
    script = INSTALLER.read_text(encoding="utf-8")

    assert "Capacity remains hard-bounded to one" in script
    assert "delegated to build-next" in script
    assert "build-next" in script
    assert "issue #51" not in script.lower()
