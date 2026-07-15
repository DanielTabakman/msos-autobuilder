from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from msos_autobuilder.candidate_gate import (
    CandidateGate,
    CandidateGateError,
    load_candidate_gate_config,
)
from msos_autobuilder.validation_contract import (
    build_ppe_validation_contract,
    stable_contract_sha256,
)


def _git(path: Path | None, *args: str, check: bool = True) -> str:
    command = ["git"]
    if path is not None:
        command.extend(["-C", str(path)])
    command.extend(args)
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "app.txt").write_bytes(b"base\n")
    (path / "requirements.txt").write_bytes(b"# fixture requirements\n")
    (path / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools>=68"]\n'
        'build-backend = "setuptools.build_meta"\n'
        "\n"
        "[project]\n"
        'name = "candidate-fixture"\n'
        'version = "0.0.0"\n',
        encoding="utf-8",
    )
    (path / "fixture_check.py").write_text(
        "from pathlib import Path\n"
        "assert Path('app.txt').read_text() == 'candidate\\n'\n"
        "assert Path('new.txt').read_text() == 'new\\n'\n",
        encoding="utf-8",
    )
    tests = path / "tests"
    tests.mkdir()
    (tests / "test_smoke.py").write_text(
        "from pathlib import Path\n"
        "\n"
        "def test_candidate_patch_applied():\n"
        "    assert Path('app.txt').read_text() == 'candidate\\n'\n"
        "    assert Path('new.txt').read_text() == 'new\\n'\n",
        encoding="utf-8",
    )
    (tests / "test_nested_python.py").write_text(
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "\n"
        "def test_nested_python_uses_candidate_venv():\n"
        "    nested = subprocess.check_output(\n"
        "        ['python', '-c', 'import sys; print(sys.executable)'],\n"
        "        text=True,\n"
        "    ).strip()\n"
        "    assert '.msos-candidate-env' in nested.replace('\\\\', '/')\n"
        "    assert nested != sys.executable or '.msos-candidate-env' in sys.executable.replace('\\\\', '/')\n"
        "    assert os.environ.get('VIRTUAL_ENV')\n"
        "    assert '.msos-candidate-env' in os.environ['VIRTUAL_ENV'].replace('\\\\', '/')\n",
        encoding="utf-8",
    )
    _git(path, "add", "app.txt")
    _git(path, "add", "requirements.txt", "pyproject.toml", "fixture_check.py", "tests")
    _git(path, "commit", "-qm", "initial source")
    return path


def _candidate_patch(source: Path) -> tuple[str, tuple[str, ...]]:
    (source / "app.txt").write_bytes(b"candidate\n")
    (source / "new.txt").write_bytes(b"new\n")
    _git(source, "add", "-N", "new.txt")
    patch = _git(source, "diff", "--binary", "HEAD") + "\n"
    changed = tuple(sorted(_git(source, "diff", "--name-only", "HEAD").splitlines()))
    _git(source, "reset", "--hard", "HEAD")
    _git(source, "clean", "-fd")
    return patch, changed


def _results_remote(
    root: Path,
    *,
    source_head: str,
    patch: str,
    changed_paths: tuple[str, ...],
    patch_sha: str | None = None,
    job_id: str = "job-1",
    job_yaml: str | None = None,
) -> Path:
    seed = root / "results-seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    _git(seed, "checkout", "-qb", "results")
    job = seed / "results" / "test-host" / job_id
    patches = job / "patches"
    patches.mkdir(parents=True)
    patch_path = patches / "task-a.patch"
    patch_path.write_bytes(patch.encode("utf-8"))
    digest = patch_sha or hashlib.sha256(patch_path.read_bytes()).hexdigest()
    source_report = {
        "version": 1,
        "job_id": job_id,
        "outcome": "completed",
        "publication_enabled": False,
        "codex_report": {
            "source_head": source_head,
            "publication_enabled": False,
            "evidence": [{"task_id": "task-a", "changed_paths": list(changed_paths)}],
        },
        "patches": [
            {
                "task_id": "task-a",
                "lane_id": "lane-a",
                "allow_changes": True,
                "complete_patch": False,
                "patch_file": "patches/task-a.patch",
                "patch_sha256": "worker-noncanonical-hash",
                "changed_paths": list(changed_paths),
            }
        ],
    }
    report = {
        "version": 1,
        "job_id": job_id,
        "outcome": "completed",
        "publication_enabled": False,
        "codex_report": {
            "source_head": source_head,
            "publication_enabled": False,
            "evidence": [{"task_id": "task-a", "changed_paths": list(changed_paths)}],
        },
        "patches": [
            {
                "task_id": "task-a",
                "lane_id": "lane-a",
                "allow_changes": True,
                "complete_patch": True,
                "patch_file": "patches/task-a.patch",
                "patch_sha256": digest,
                "changed_paths": list(changed_paths),
            }
        ],
        "relay": {
            "version": 1,
            "machine_id": "test-host",
            "complete_patch_reconstruction": True,
            "source_report_role": "original-worker-report-noncanonical-for-patch-identity",
            "canonical_report_role": "relay-corrected-canonical-downstream-report",
            "publication_enabled": False,
        },
    }
    source_path = job / "source-report.json"
    source_path.write_text(json.dumps(source_report), encoding="utf-8")
    (job / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (job / "result-integrity.json").write_text(
        json.dumps(
            {
                "version": 1,
                "source_report_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "corrected_report_sha256": hashlib.sha256(
                    (job / "report.json").read_bytes()
                ).hexdigest(),
                "canonical_patch_sha256_by_task": {"task-a": digest},
                "publication_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    (job / "job.yaml").write_text(job_yaml or f"version: 1\njob_id: {job_id}\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "seed result")
    remote = root / "results.git"
    _git(None, "clone", "-q", "--bare", str(seed), str(remote))
    return remote


def _generic_contract(
    *,
    job_id: str,
    source_head: str,
    changed_paths: tuple[str, ...],
) -> dict:
    return build_ppe_validation_contract(
        pipeline_id="ppe",
        job_id=job_id,
        work_item_id="fixture-work",
        native_slice_id="Fixture-Slice002",
        source_commit=source_head,
        allowed_changed_paths=changed_paths,
        target_repository="DanielTabakman/Probability-prediction-engine",
        dependency_source_sha256=hashlib.sha256(b"# fixture requirements\n").hexdigest(),
    )


def _generic_job_yaml(job_id: str, source_head: str, changed_paths: tuple[str, ...], contract: dict) -> str:
    return yaml.safe_dump(
        {
            "version": 1,
            "job_id": job_id,
            "publication_enabled": False,
            "founder_build_next": {
                "version": 1,
                "pipeline_id": "ppe",
                "work_item_id": "fixture-work",
                "repository": "DanielTabakman/Probability-prediction-engine",
                "registered_adapter": "ppe_operator",
                "source": {"commit": source_head},
                "native_slice": {
                    "slice_id": "Fixture-Slice002",
                    "touch_set": list(changed_paths),
                },
                "authority": {
                    "publication_enabled": False,
                    "merge_enabled": False,
                    "product_main_write_enabled": False,
                },
            },
            "candidate_validation": contract,
        },
        sort_keys=False,
    )


def _write_config(
    root: Path,
    *,
    host_root: Path,
    source: Path,
    remote: Path,
    check_code: str,
    policy_blocks: tuple[str, ...] = (),
    job_id: str = "job-1",
) -> Path:
    config = {
        "version": 1,
        "publication_enabled": False,
        "host_root": str(host_root),
        "source_repo": str(source),
        "results_repo_url": str(remote),
        "results_branch": "results",
        "machine_id": "test-host",
        "poll_seconds": 1,
        "plans": {
            job_id: {
                "checks": [
                    {
                        "name": "fixture-check",
                        "argv": [sys.executable, "-c", check_code],
                        "cwd": ".",
                        "timeout_seconds": 30,
                    }
                ],
                "policy_blocks": list(policy_blocks),
            }
        },
    }
    path = root / "candidate-gate.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _write_generic_config(root: Path, *, host_root: Path, source: Path, remote: Path) -> Path:
    config = {
        "version": 1,
        "publication_enabled": False,
        "host_root": str(host_root),
        "source_repo": str(source),
        "results_repo_url": str(remote),
        "results_branch": "results",
        "machine_id": "test-host",
        "poll_seconds": 1,
        "plans": {},
    }
    path = root / "candidate-gate.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _pip_freeze() -> str:
    return subprocess.check_output(
        [sys.executable, "-m", "pip", "freeze"],
        text=True,
        encoding="utf-8",
    )


def _read_gate_report(root: Path, remote: Path, job_id: str = "job-1") -> dict:
    review = root / "r"
    if review.exists():
        shutil.rmtree(review)
    _git(
        None,
        "-c",
        "core.longpaths=true",
        "clone",
        "-q",
        "--branch",
        "results",
        str(remote),
        str(review),
    )
    report_path = review / "results" / "test-host" / job_id / "gate-report.json"
    return json.loads(report_path.read_text(encoding="utf-8"))


def _edit_results_remote(root: Path, remote: Path, edit) -> None:
    checkout = root / "edit"
    if checkout.exists():
        shutil.rmtree(checkout)
    _git(None, "-c", "core.longpaths=true", "clone", "-q", "--branch", "results", str(remote), str(checkout))
    _git(checkout, "config", "user.email", "test@example.com")
    _git(checkout, "config", "user.name", "Test")
    edit(checkout)
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "mutate result")
    _git(checkout, "push", "-q", "origin", "HEAD:results")


def test_candidate_gate_applies_complete_patch_and_runs_checks(tmp_path: Path) -> None:
    source = _init_repo(tmp_path / "source")
    source_head = _git(source, "rev-parse", "HEAD")
    patch, changed_paths = _candidate_patch(source)
    remote = _results_remote(
        tmp_path,
        source_head=source_head,
        patch=patch,
        changed_paths=changed_paths,
    )
    host_root = tmp_path / "host"
    config_path = _write_config(
        tmp_path,
        host_root=host_root,
        source=source,
        remote=remote,
        check_code=(
            "from pathlib import Path; "
            "assert Path('app.txt').read_text() == 'candidate\\n'; "
            "assert Path('new.txt').read_text() == 'new\\n'"
        ),
    )

    gate = CandidateGate(load_candidate_gate_config(config_path))
    assert gate.run_once() == ("job-1",)
    assert gate.run_once() == ()

    report = _read_gate_report(tmp_path, remote)
    assert report["status"] == "passed"
    assert report["publication_enabled"] is False
    assert report["product_write_performed"] is False
    assert report["changed_paths"] == ["app.txt", "new.txt"]
    assert report["checks"][0]["passed"] is True
    assert report["workspace_removed"] is True
    assert _git(source, "rev-parse", "HEAD") == source_head
    assert _git(source, "status", "--porcelain") == ""


def test_candidate_gate_records_check_failure_and_policy_block(tmp_path: Path) -> None:
    source = _init_repo(tmp_path / "source")
    source_head = _git(source, "rev-parse", "HEAD")
    patch, changed_paths = _candidate_patch(source)
    remote = _results_remote(
        tmp_path,
        source_head=source_head,
        patch=patch,
        changed_paths=changed_paths,
    )
    config_path = _write_config(
        tmp_path,
        host_root=tmp_path / "host",
        source=source,
        remote=remote,
        check_code="raise SystemExit(7)",
        policy_blocks=("schema compatibility decision unresolved",),
    )

    assert CandidateGate(load_candidate_gate_config(config_path)).run_once() == ("job-1",)
    report = _read_gate_report(tmp_path, remote)
    assert report["status"] == "failed"
    assert report["checks"][0]["returncode"] == 7
    assert report["checks"][0]["passed"] is False
    assert report["policy_blocks"] == ["schema compatibility decision unresolved"]
    assert report["errors"] == []


def test_candidate_gate_fails_closed_on_patch_hash_mismatch(tmp_path: Path) -> None:
    source = _init_repo(tmp_path / "source")
    source_head = _git(source, "rev-parse", "HEAD")
    patch, changed_paths = _candidate_patch(source)
    remote = _results_remote(
        tmp_path,
        source_head=source_head,
        patch=patch,
        changed_paths=changed_paths,
        patch_sha="0" * 64,
    )
    config_path = _write_config(
        tmp_path,
        host_root=tmp_path / "host",
        source=source,
        remote=remote,
        check_code="raise AssertionError('must not run')",
    )

    assert CandidateGate(load_candidate_gate_config(config_path)).run_once() == ("job-1",)
    report = _read_gate_report(tmp_path, remote)
    assert report["status"] == "failed"
    assert report["checks"] == []
    assert "patch hash mismatch" in report["errors"][0]["message"]
    assert report["publication_enabled"] is False


def test_candidate_gate_rejects_default_branch_and_mutated_result(tmp_path: Path) -> None:
    source = _init_repo(tmp_path / "source")
    source_head = _git(source, "rev-parse", "HEAD")
    patch, changed_paths = _candidate_patch(source)
    remote = _results_remote(
        tmp_path,
        source_head=source_head,
        patch=patch,
        changed_paths=changed_paths,
    )
    config_path = _write_config(
        tmp_path,
        host_root=tmp_path / "host",
        source=source,
        remote=remote,
        check_code="pass",
    )
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["results_branch"] = "main"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match="main or master"):
        load_candidate_gate_config(config_path)

    config["results_branch"] = "results"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    gate = CandidateGate(load_candidate_gate_config(config_path))
    assert gate.run_once() == ("job-1",)
    local_report = (
        gate.results.checkout / "results" / "test-host" / "job-1" / "report.json"
    )
    payload = json.loads(local_report.read_text(encoding="utf-8"))
    payload["tampered"] = True
    local_report.write_text(json.dumps(payload), encoding="utf-8")
    _git(gate.results.checkout, "add", str(local_report))
    _git(gate.results.checkout, "commit", "-qm", "tamper result")
    _git(gate.results.checkout, "push", "origin", "HEAD:results")

    with pytest.raises(CandidateGateError, match="changed after gate processing"):
        gate.run_once()


def test_candidate_gate_discovers_generic_build_next_contract(tmp_path: Path) -> None:
    packages_before = _pip_freeze()
    active_python = Path(sys.executable).resolve()
    source = _init_repo(tmp_path / "source")
    source_head = _git(source, "rev-parse", "HEAD")
    patch, changed_paths = _candidate_patch(source)
    job_id = "build-next-ppe-fixture-work-Fixture-Slice002-abcdef123456"
    contract = _generic_contract(
        job_id=job_id,
        source_head=source_head,
        changed_paths=changed_paths,
    )
    remote = _results_remote(
        tmp_path,
        source_head=source_head,
        patch=patch,
        changed_paths=changed_paths,
        job_id=job_id,
        job_yaml=_generic_job_yaml(job_id, source_head, changed_paths, contract),
    )
    config_path = _write_generic_config(
        tmp_path,
        host_root=tmp_path / "host",
        source=source,
        remote=remote,
    )

    assert CandidateGate(load_candidate_gate_config(config_path)).run_once() == (job_id,)
    report = _read_gate_report(tmp_path, remote, job_id=job_id)
    assert report["status"] == "passed"
    assert report["state"] == "candidate_passed"
    assert report["plan_source"] == "candidate_validation"
    assert [check["name"] for check in report["bootstrap"]] == [
        "upgrade-pip",
        "install-requirements",
        "install-test-tooling",
        "install-candidate-package",
    ]
    assert [check["phase"] for check in report["bootstrap"]] == ["bootstrap"] * 4
    assert [check["phase"] for check in report["checks"]] == ["check"]
    assert report["candidate_environment"]["python"]
    assert Path(report["candidate_environment"]["python"]).resolve() != active_python
    assert ".msos-candidate-env" in report["candidate_environment"]["python"].replace("\\", "/")
    assert ".msos-candidate-env" in report["candidate_environment"]["path_prepend"].replace("\\", "/")
    assert report["candidate_environment_removed"] is True
    assert all(check["required"] is True for check in [*report["bootstrap"], *report["checks"]])
    assert _pip_freeze() == packages_before


def test_generic_build_next_without_valid_contract_is_unvalidated(tmp_path: Path) -> None:
    source = _init_repo(tmp_path / "source")
    source_head = _git(source, "rev-parse", "HEAD")
    patch, changed_paths = _candidate_patch(source)
    job_id = "build-next-ppe-fixture-work-Fixture-Slice002-abcdef123456"
    remote = _results_remote(
        tmp_path,
        source_head=source_head,
        patch=patch,
        changed_paths=changed_paths,
        job_id=job_id,
        job_yaml=yaml.safe_dump(
            {
                "version": 1,
                "job_id": job_id,
                "publication_enabled": False,
                "founder_build_next": {
                    "version": 1,
                    "pipeline_id": "ppe",
                    "work_item_id": "fixture-work",
                    "repository": "DanielTabakman/Probability-prediction-engine",
                    "registered_adapter": "ppe_operator",
                    "source": {"commit": source_head},
                    "native_slice": {
                        "slice_id": "Fixture-Slice002",
                        "touch_set": list(changed_paths),
                    },
                    "authority": {
                        "publication_enabled": False,
                        "merge_enabled": False,
                        "product_main_write_enabled": False,
                    },
                },
            },
            sort_keys=False,
        ),
    )
    config_path = _write_generic_config(
        tmp_path,
        host_root=tmp_path / "host",
        source=source,
        remote=remote,
    )

    assert CandidateGate(load_candidate_gate_config(config_path)).run_once() == (job_id,)
    report = _read_gate_report(tmp_path, remote, job_id=job_id)
    assert report["status"] == "unvalidated"
    assert report["state"] == "awaiting_validation"
    assert report["checks"] == []
    assert "candidate_validation" in report["errors"][0]["message"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing-integrity", "result-integrity"),
        ("changed-source", "source report SHA-256"),
        ("changed-report", "corrected report SHA-256"),
        ("changed-patch", "canonical patch bytes"),
        ("task-map-mismatch", "task map"),
        ("source-report-only", "distinct"),
    ],
)
def test_generic_build_next_requires_canonical_integrity(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    source = _init_repo(tmp_path / "source")
    source_head = _git(source, "rev-parse", "HEAD")
    patch, changed_paths = _candidate_patch(source)
    job_id = "build-next-ppe-fixture-work-Fixture-Slice002-abcdef123456"
    contract = _generic_contract(
        job_id=job_id,
        source_head=source_head,
        changed_paths=changed_paths,
    )
    remote = _results_remote(
        tmp_path,
        source_head=source_head,
        patch=patch,
        changed_paths=changed_paths,
        job_id=job_id,
        job_yaml=_generic_job_yaml(job_id, source_head, changed_paths, contract),
    )

    def mutate(checkout: Path) -> None:
        job_dir = checkout / "results" / "test-host" / job_id
        if mutation == "missing-integrity":
            (job_dir / "result-integrity.json").unlink()
        elif mutation == "changed-source":
            (job_dir / "source-report.json").write_text("{}", encoding="utf-8")
        elif mutation == "changed-report":
            payload = json.loads((job_dir / "report.json").read_text(encoding="utf-8"))
            payload["tampered"] = True
            (job_dir / "report.json").write_text(json.dumps(payload), encoding="utf-8")
        elif mutation == "changed-patch":
            (job_dir / "patches" / "task-a.patch").write_text("tampered\n", encoding="utf-8")
        elif mutation == "task-map-mismatch":
            integrity = json.loads((job_dir / "result-integrity.json").read_text(encoding="utf-8"))
            integrity["canonical_patch_sha256_by_task"]["extra-task"] = "0" * 64
            (job_dir / "result-integrity.json").write_text(json.dumps(integrity), encoding="utf-8")
        elif mutation == "source-report-only":
            report_bytes = (job_dir / "report.json").read_bytes()
            (job_dir / "source-report.json").write_bytes(report_bytes)
            digest = hashlib.sha256(report_bytes).hexdigest()
            integrity = json.loads((job_dir / "result-integrity.json").read_text(encoding="utf-8"))
            integrity["corrected_report_sha256"] = digest
            integrity["source_report_sha256"] = digest
            (job_dir / "result-integrity.json").write_text(json.dumps(integrity), encoding="utf-8")

    _edit_results_remote(tmp_path, remote, mutate)
    config_path = _write_generic_config(
        tmp_path,
        host_root=tmp_path / "host",
        source=source,
        remote=remote,
    )

    assert CandidateGate(load_candidate_gate_config(config_path)).run_once() == (job_id,)
    report = _read_gate_report(tmp_path, remote, job_id=job_id)
    assert report["status"] == "unvalidated"
    assert message in report["errors"][0]["message"]


def test_generic_dependency_source_must_exist_and_match_contract(tmp_path: Path) -> None:
    source = _init_repo(tmp_path / "source")
    (source / "requirements.txt").unlink()
    _git(source, "add", "-u", "requirements.txt")
    _git(source, "commit", "-qm", "remove requirements")
    source_head = _git(source, "rev-parse", "HEAD")
    patch, changed_paths = _candidate_patch(source)
    job_id = "build-next-ppe-fixture-work-Fixture-Slice002-abcdef123456"
    contract = _generic_contract(
        job_id=job_id,
        source_head=source_head,
        changed_paths=changed_paths,
    )
    remote = _results_remote(
        tmp_path,
        source_head=source_head,
        patch=patch,
        changed_paths=changed_paths,
        job_id=job_id,
        job_yaml=_generic_job_yaml(job_id, source_head, changed_paths, contract),
    )
    config_path = _write_generic_config(
        tmp_path,
        host_root=tmp_path / "host",
        source=source,
        remote=remote,
    )

    assert CandidateGate(load_candidate_gate_config(config_path)).run_once() == (job_id,)
    report = _read_gate_report(tmp_path, remote, job_id=job_id)
    assert report["status"] == "failed"
    assert "dependency source is missing" in report["errors"][0]["message"]


def test_failed_dependency_bootstrap_cannot_pass(tmp_path: Path) -> None:
    source = _init_repo(tmp_path / "source")
    (source / "requirements.txt").write_text(
        "definitely-not-a-real-package-for-msos-autobuilder-tests==0\n",
        encoding="utf-8",
    )
    _git(source, "add", "requirements.txt")
    _git(source, "commit", "-qm", "bad requirements")
    source_head = _git(source, "rev-parse", "HEAD")
    patch, changed_paths = _candidate_patch(source)
    job_id = "build-next-ppe-fixture-work-Fixture-Slice002-abcdef123456"
    contract = _generic_contract(
        job_id=job_id,
        source_head=source_head,
        changed_paths=changed_paths,
    )
    contract["dependency_policy"]["dependency_source_sha256"] = hashlib.sha256(
        (source / "requirements.txt").read_bytes()
    ).hexdigest()
    contract["contract_sha256"] = stable_contract_sha256(contract)
    remote = _results_remote(
        tmp_path,
        source_head=source_head,
        patch=patch,
        changed_paths=changed_paths,
        job_id=job_id,
        job_yaml=_generic_job_yaml(job_id, source_head, changed_paths, contract),
    )
    config_path = _write_generic_config(
        tmp_path,
        host_root=tmp_path / "host",
        source=source,
        remote=remote,
    )

    assert CandidateGate(load_candidate_gate_config(config_path)).run_once() == (job_id,)
    report = _read_gate_report(tmp_path, remote, job_id=job_id)
    assert report["status"] == "failed"
    assert report["bootstrap"][1]["name"] == "install-requirements"
    assert report["bootstrap"][1]["passed"] is False
    assert report["checks"] == []


@pytest.mark.parametrize(
    "field",
    [
        "job_id",
        "pipeline_id",
        "work_item_id",
        "native_slice_id",
        "adapter",
        "target_repository",
        "source_commit",
        "allowed_changed_paths",
        "publication_enabled",
        "merge_enabled",
        "product_main_write_enabled",
    ],
)
def test_generic_contract_identity_must_match_job_and_report(
    tmp_path: Path,
    field: str,
) -> None:
    source = _init_repo(tmp_path / "source")
    source_head = _git(source, "rev-parse", "HEAD")
    patch, changed_paths = _candidate_patch(source)
    job_id = "build-next-ppe-fixture-work-Fixture-Slice002-abcdef123456"
    contract = _generic_contract(
        job_id=job_id,
        source_head=source_head,
        changed_paths=changed_paths,
    )
    remote = _results_remote(
        tmp_path,
        source_head=source_head,
        patch=patch,
        changed_paths=changed_paths,
        job_id=job_id,
        job_yaml=_generic_job_yaml(job_id, source_head, changed_paths, contract),
    )

    def mutate(checkout: Path) -> None:
        job_path = checkout / "results" / "test-host" / job_id / "job.yaml"
        job = yaml.safe_load(job_path.read_text(encoding="utf-8"))
        founder = job["founder_build_next"]
        if field == "job_id":
            job["job_id"] = "other-job"
        elif field == "pipeline_id":
            founder["pipeline_id"] = "other"
        elif field == "work_item_id":
            founder["work_item_id"] = "other-work"
        elif field == "native_slice_id":
            founder["native_slice"]["slice_id"] = "Other-Slice"
        elif field == "adapter":
            founder["registered_adapter"] = "other_adapter"
        elif field == "target_repository":
            founder["repository"] = "Owner/Other"
        elif field == "source_commit":
            founder["source"]["commit"] = "f" * 40
        elif field == "allowed_changed_paths":
            founder["native_slice"]["touch_set"] = ["app.txt"]
        elif field == "publication_enabled":
            founder["authority"]["publication_enabled"] = True
        elif field == "merge_enabled":
            founder["authority"]["merge_enabled"] = True
        elif field == "product_main_write_enabled":
            founder["authority"]["product_main_write_enabled"] = True
        job_path.write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")

    _edit_results_remote(tmp_path, remote, mutate)
    config_path = _write_generic_config(
        tmp_path,
        host_root=tmp_path / "host",
        source=source,
        remote=remote,
    )

    assert CandidateGate(load_candidate_gate_config(config_path)).run_once() == (job_id,)
    report = _read_gate_report(tmp_path, remote, job_id=job_id)
    assert report["status"] == "unvalidated"
    assert field in report["errors"][0]["message"]
