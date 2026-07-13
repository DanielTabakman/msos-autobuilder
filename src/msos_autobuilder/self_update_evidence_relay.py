"""Relay immutable self-update evidence to the review-only Git results branch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EvidenceRelayError(RuntimeError):
    """Raised when immutable update evidence cannot be relayed safely."""


@dataclass(frozen=True)
class EvidenceRelayConfig:
    supervisor_root: Path
    repo_url: str
    branch: str = "results"
    machine_id: str = ""
    max_push_attempts: int = 3

    def __post_init__(self) -> None:
        if not self.repo_url.strip():
            raise EvidenceRelayError("evidence repo_url is required")
        parsed = urlsplit(self.repo_url) if "://" in self.repo_url else None
        if parsed and (parsed.username is not None or parsed.password is not None):
            raise EvidenceRelayError("evidence repo_url may not contain embedded credentials")
        if not self.branch.strip() or self.branch in {"main", "master"}:
            raise EvidenceRelayError("evidence relay requires a dedicated non-default branch")
        if self.max_push_attempts < 1 or self.max_push_attempts > 10:
            raise EvidenceRelayError("max_push_attempts must be between 1 and 10")

    @property
    def reports_root(self) -> Path:
        return self.supervisor_root / "reports"

    @property
    def notifications_root(self) -> Path:
        return self.supervisor_root / "notifications"

    @property
    def state_root(self) -> Path:
        return self.supervisor_root / "state"

    @property
    def checkout(self) -> Path:
        return self.state_root / "self-update-evidence-repo"

    @property
    def ledger_path(self) -> Path:
        return self.state_root / "self-update-evidence-ledger.json"


def _safe_segment(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return cleaned[:96] or fallback


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceRelayError(f"invalid JSON evidence: {path}") from exc
    if not isinstance(raw, dict):
        raise EvidenceRelayError(f"JSON evidence must be an object: {path}")
    return raw


def load_evidence_relay_config(path: str | Path) -> EvidenceRelayConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise EvidenceRelayError("invalid supervisor config YAML") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise EvidenceRelayError("only supervisor config version 1 is supported")

    root_text = str(raw.get("supervisor_root") or "").strip()
    if not root_text:
        raise EvidenceRelayError("supervisor_root is required")
    supervisor_root = Path(root_text).expanduser()
    if not supervisor_root.is_absolute():
        supervisor_root = config_path.parent / supervisor_root

    repo_url = str(raw.get("evidence_repo_url") or raw.get("repo_url") or "").strip()
    branch = str(raw.get("evidence_branch") or "results").strip()
    machine_id = _safe_segment(
        str(raw.get("machine_id") or socket.gethostname()),
        fallback="windows-host",
    )
    return EvidenceRelayConfig(
        supervisor_root=supervisor_root.resolve(),
        repo_url=repo_url,
        branch=branch,
        machine_id=machine_id,
        max_push_attempts=int(raw.get("evidence_max_push_attempts", 3)),
    )


def _git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_CONFIG_COUNT"] = "1"
    environment["GIT_CONFIG_KEY_0"] = "core.autocrlf"
    environment["GIT_CONFIG_VALUE_0"] = "false"
    return environment


def _run_git(
    repo: Path | None,
    *args: str,
    accepted: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    command = ["git"]
    if repo is not None:
        command.extend(["-C", str(repo)])
    command.extend(args)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        env=_git_environment(),
        check=False,
        timeout=180,
    )
    if completed.returncode not in accepted:
        detail = (completed.stderr or completed.stdout or "git command failed").strip()
        raise EvidenceRelayError(f"git {' '.join(args)} failed: {detail}")
    return completed


@dataclass(frozen=True)
class EvidencePair:
    attempt_id: str
    report_path: Path
    notification_path: Path
    report_sha256: str
    notification_sha256: str
    outcome: str


class SelfUpdateEvidenceRelay:
    """Publish each immutable report/notification pair once without touching main."""

    def __init__(self, config: EvidenceRelayConfig) -> None:
        self.config = config

    def _load_ledger(self) -> dict[str, Any]:
        if not self.config.ledger_path.exists():
            return {"version": 1, "attempts": {}}
        raw = _load_json(self.config.ledger_path)
        if raw.get("version") != 1 or not isinstance(raw.get("attempts"), dict):
            raise EvidenceRelayError("self-update evidence ledger is malformed")
        return raw

    def _save_ledger(self, ledger: Mapping[str, Any]) -> None:
        _atomic_write_json(self.config.ledger_path, ledger)

    def _discover(self) -> tuple[EvidencePair, ...]:
        self.config.notifications_root.mkdir(parents=True, exist_ok=True)
        reports_root = self.config.reports_root.resolve()
        pairs: list[EvidencePair] = []
        for notification_path in sorted(self.config.notifications_root.glob("*.json")):
            notification = _load_json(notification_path)
            attempt_id = str(notification.get("attempt_id") or "").strip()
            if not _SAFE_ID_RE.fullmatch(attempt_id):
                raise EvidenceRelayError(
                    f"notification has an unsafe attempt_id: {notification_path}"
                )
            report_text = str(notification.get("report_path") or "").strip()
            if not report_text:
                raise EvidenceRelayError(
                    f"notification is missing report_path: {notification_path}"
                )
            report_path = Path(report_text).expanduser().resolve()
            try:
                report_path.relative_to(reports_root)
            except ValueError as exc:
                raise EvidenceRelayError(
                    f"notification report_path escapes reports root: {notification_path}"
                ) from exc
            if not report_path.is_file():
                raise EvidenceRelayError(f"notification report is missing: {report_path}")
            report = _load_json(report_path)
            if str(report.get("attempt_id") or "") != attempt_id:
                raise EvidenceRelayError(f"report attempt_id mismatch for {attempt_id}")
            outcome = str(report.get("outcome") or notification.get("outcome") or "").strip()
            if not outcome or str(notification.get("outcome") or "") != outcome:
                raise EvidenceRelayError(f"notification outcome mismatch for {attempt_id}")
            pairs.append(
                EvidencePair(
                    attempt_id=attempt_id,
                    report_path=report_path,
                    notification_path=notification_path.resolve(),
                    report_sha256=_sha256(report_path),
                    notification_sha256=_sha256(notification_path),
                    outcome=outcome,
                )
            )
        return tuple(pairs)

    def _prepare_checkout(self) -> None:
        checkout = self.config.checkout
        if not (checkout / ".git").exists():
            if checkout.exists():
                shutil.rmtree(checkout)
            checkout.parent.mkdir(parents=True, exist_ok=True)
            _run_git(
                None,
                "clone",
                "--single-branch",
                "--branch",
                self.config.branch,
                "--no-tags",
                self.config.repo_url,
                str(checkout),
            )
        else:
            _run_git(checkout, "fetch", "--no-tags", "origin", self.config.branch)
            _run_git(
                checkout,
                "checkout",
                "-B",
                self.config.branch,
                f"origin/{self.config.branch}",
            )
            _run_git(checkout, "reset", "--hard", f"origin/{self.config.branch}")
            _run_git(checkout, "clean", "-fd")
        _run_git(checkout, "config", "user.name", "MSOS Self-Update Evidence Relay")
        _run_git(checkout, "config", "user.email", "autobuilder-update-relay@localhost")

    def _destination(self, pair: EvidencePair) -> Path:
        return (
            self.config.checkout
            / "results"
            / self.config.machine_id
            / "self-updates"
            / pair.attempt_id
        )

    def _verify_existing(self, destination: Path, pair: EvidencePair) -> None:
        report = destination / "update-report.json"
        notification = destination / "notification.json"
        metadata = destination / "relay.json"
        if not report.is_file() or not notification.is_file() or not metadata.is_file():
            raise EvidenceRelayError(
                f"remote evidence path is incomplete for immutable attempt {pair.attempt_id}"
            )
        relay = _load_json(metadata)
        if (
            _sha256(report) != pair.report_sha256
            or _sha256(notification) != pair.notification_sha256
            or relay.get("report_sha256") != pair.report_sha256
            or relay.get("notification_sha256") != pair.notification_sha256
        ):
            raise EvidenceRelayError(
                f"remote evidence differs for immutable attempt {pair.attempt_id}"
            )

    def _publish_pair(self, pair: EvidencePair) -> str:
        last_push_detail = ""
        for _ in range(self.config.max_push_attempts):
            self._prepare_checkout()
            destination = self._destination(pair)
            if destination.exists():
                self._verify_existing(destination, pair)
                return _run_git(self.config.checkout, "rev-parse", "HEAD").stdout.strip()

            destination.mkdir(parents=True, exist_ok=False)
            shutil.copy2(pair.report_path, destination / "update-report.json")
            shutil.copy2(pair.notification_path, destination / "notification.json")
            _atomic_write_json(
                destination / "relay.json",
                {
                    "version": 1,
                    "attempt_id": pair.attempt_id,
                    "outcome": pair.outcome,
                    "report_sha256": pair.report_sha256,
                    "notification_sha256": pair.notification_sha256,
                    "relayed_at": datetime.now(UTC).isoformat(),
                    "publication_enabled": False,
                },
            )
            relative = destination.relative_to(self.config.checkout)
            _run_git(self.config.checkout, "add", "--", relative.as_posix())
            _run_git(
                self.config.checkout,
                "commit",
                "-m",
                f"Relay self-update evidence {pair.attempt_id}",
            )
            commit = _run_git(self.config.checkout, "rev-parse", "HEAD").stdout.strip()
            pushed = _run_git(
                self.config.checkout,
                "push",
                "origin",
                f"HEAD:{self.config.branch}",
                accepted=(0, 1),
            )
            if pushed.returncode == 0:
                return commit
            last_push_detail = (pushed.stderr or pushed.stdout).strip()
        raise EvidenceRelayError(
            "could not push self-update evidence after retrying concurrent results-branch updates: "
            + last_push_detail
        )

    def run_once(self) -> tuple[str, ...]:
        ledger = self._load_ledger()
        relayed: list[str] = []
        for pair in self._discover():
            existing = ledger["attempts"].get(pair.attempt_id)
            if existing:
                if (
                    existing.get("report_sha256") != pair.report_sha256
                    or existing.get("notification_sha256") != pair.notification_sha256
                    or not re.fullmatch(r"[0-9a-f]{40}", str(existing.get("results_commit") or ""))
                ):
                    raise EvidenceRelayError(
                        f"relay ledger evidence mismatch for immutable attempt {pair.attempt_id}"
                    )
                continue
            commit = self._publish_pair(pair)
            ledger["attempts"][pair.attempt_id] = {
                "report_sha256": pair.report_sha256,
                "notification_sha256": pair.notification_sha256,
                "results_commit": commit,
                "recorded_at": datetime.now(UTC).isoformat(),
            }
            self._save_ledger(ledger)
            relayed.append(pair.attempt_id)
        return tuple(relayed)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    relay = SelfUpdateEvidenceRelay(load_evidence_relay_config(args.config))
    relayed = relay.run_once()
    print(json.dumps({"status": "completed", "relayed_attempts": list(relayed)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
