import subprocess
from pathlib import Path

from msos_autobuilder.workspace_witness import run_workspace_witness


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
    source.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    git(source, "config", "user.email", "fixture@example.com")
    git(source, "config", "user.name", "Fixture")
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "fixture")
    return source


def test_workspace_witness_reports_two_clean_matching_clones(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    report = run_workspace_witness(
        source,
        tmp_path / "workspaces",
        tmp_path / "runtime",
    )

    assert report.source_head == git(source, "rev-parse", "HEAD")
    assert report.source_clean_before is True
    assert report.source_clean_after is True
    assert {lane.lane_id for lane in report.lanes} == {"msos-web", "ppe-core"}
    assert all(lane.head == report.source_head for lane in report.lanes)
    assert all(lane.clean for lane in report.lanes)
    assert len({lane.workspace for lane in report.lanes}) == 2
