from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "engineering_os_two_lane_witness.ps1"


def test_two_lane_witness_is_non_publishing_and_bounded() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'publication_enabled = $false' in text
    assert 'merge_enabled = $false' in text
    assert 'codex.max_concurrency >= 2' in text
    assert 'active_running", "active_queued", "feed_awaiting_import' in text
    assert 'status -ne "UNFILLED"' in text

    ui = "docs/ENGINEERING_OS/MSOS_UI_SURFACE_INVENTORY_V1.md"
    api = "docs/API/MSOS_CAPABILITY_CATALOG_V1.md"
    assert text.count(ui) >= 2
    assert text.count(api) >= 2
    assert ui != api

    forbidden = [
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Enable-ScheduledTask",
        "Disable-ScheduledTask",
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "git push",
        "merge_enabled = $true",
        "publication_enabled = $true",
    ]
    for marker in forbidden:
        assert marker not in text