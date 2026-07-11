import subprocess
from pathlib import Path

from msos_autobuilder.process_witness import run_process_witness


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def make_source(root: Path) -> Path:
    source = root / "source"
    (source / "apps/msos-web").mkdir(parents=True)
    (source / "src/engine").mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    git(source, "config", "user.email", "fixture@example.com")
    git(source, "config", "user.name", "Fixture")
    (source / "apps/msos-web/.keep").write_text("\n", encoding="utf-8")
    (source / "src/engine/.keep").write_text("\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "fixture")
    return source


def test_two_process_witness_lanes_change_only_owned_paths(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    report = run_process_witness(
        source,
        tmp_path / "workspaces",
        tmp_path / "runtime",
    )

    assert report.source_clean_before is True
    assert report.source_clean_after is True
    by_lane = {lane.lane_id: lane for lane in report.lanes}
    assert by_lane["msos-web"].changed_paths == (
        "apps/msos-web/.autobuilder-process-witness",
    )
    assert by_lane["ppe-core"].changed_paths == (
        "src/engine/.autobuilder-process-witness",
    )
    assert all(lane.clean_source for lane in report.lanes)
