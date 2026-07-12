"""Command-line interface for read-only Autobuilder tools."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from .codex_shadow import (
    codex_host_preflight,
    load_codex_host_config,
    load_codex_shadow_manifest,
    render_codex_preflight_json,
    render_codex_shadow_json,
    run_codex_shadow,
)
from .inventory import build_inventory, render_json, render_markdown
from .persistent_host import (
    HostPaths,
    PersistentHost,
    approve_pending_job,
    enqueue_manifest,
    load_persistent_host_config,
    render_host_result_json,
    render_host_status_json,
    sync_git_job_feed,
)
from .process_witness import render_process_witness_json, run_process_witness
from .workspace_witness import render_workspace_witness_json, run_workspace_witness


def _write_or_print(output: str, destination: str | None) -> None:
    if destination:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


def _inventory_command(args: argparse.Namespace) -> int:
    report = build_inventory(
        repo_root=args.repo,
        contract_path=args.contract,
        rules_path=args.rules,
    )
    json_text = render_json(report)
    markdown_text = render_markdown(report)

    if args.json_out:
        Path(args.json_out).write_text(json_text, encoding="utf-8")
    if args.markdown_out:
        Path(args.markdown_out).write_text(markdown_text, encoding="utf-8")
    if not args.json_out and not args.markdown_out:
        print(markdown_text, end="")
    return 0


def _workspace_witness_command(args: argparse.Namespace) -> int:
    report = run_workspace_witness(
        source_repo=args.source,
        workspace_root=args.workspace_root,
        runtime_root=args.runtime_root,
    )
    _write_or_print(render_workspace_witness_json(report), args.json_out)
    return 0


def _process_witness_command(args: argparse.Namespace) -> int:
    report = run_process_witness(
        source_repo=args.source,
        workspace_root=args.workspace_root,
        runtime_root=args.runtime_root,
    )
    _write_or_print(render_process_witness_json(report), args.json_out)
    return 0


def _codex_preflight_command(args: argparse.Namespace) -> int:
    config = load_codex_host_config(args.host_config)
    report = codex_host_preflight(config)
    _write_or_print(render_codex_preflight_json(report), args.json_out)
    return 0 if report.ok else 2


def _codex_shadow_command(args: argparse.Namespace) -> int:
    config = load_codex_host_config(args.host_config)
    specs = load_codex_shadow_manifest(args.manifest)
    report = run_codex_shadow(config, specs)
    _write_or_print(render_codex_shadow_json(report), args.json_out)
    return 0


def _persistent_host(args: argparse.Namespace) -> PersistentHost:
    return PersistentHost(load_persistent_host_config(args.service_config))


def _host_init_command(args: argparse.Namespace) -> int:
    status = _persistent_host(args).initialize()
    _write_or_print(render_host_status_json(status), args.json_out)
    return 0


def _host_run_once_command(args: argparse.Namespace) -> int:
    result = _persistent_host(args).run_once(sync_feed=not args.no_feed)
    _write_or_print(render_host_result_json(result), args.json_out)
    return 1 if result.outcome == "failed" else 0


def _host_run_command(args: argparse.Namespace) -> int:
    _persistent_host(args).run_forever()
    return 0


def _host_status_command(args: argparse.Namespace) -> int:
    status = _persistent_host(args).read_status()
    _write_or_print(render_host_status_json(status), args.json_out)
    return 0


def _host_enqueue_command(args: argparse.Namespace) -> int:
    config = load_persistent_host_config(args.service_config)
    paths = HostPaths.from_root(config.host_root)
    path = enqueue_manifest(
        paths,
        args.manifest,
        job_id=args.job_id,
        approved=args.approve,
        requested_by=args.requested_by,
        expected_source_head=args.expected_source_head,
    )
    _write_or_print(str(path) + "\n", args.output)
    return 0


def _host_approve_command(args: argparse.Namespace) -> int:
    config = load_persistent_host_config(args.service_config)
    path = approve_pending_job(HostPaths.from_root(config.host_root), args.job_id)
    _write_or_print(str(path) + "\n", args.output)
    return 0


def _host_sync_feed_command(args: argparse.Namespace) -> int:
    config = load_persistent_host_config(args.service_config)
    imported = sync_git_job_feed(config, HostPaths.from_root(config.host_root))
    output = "\n".join(imported) + ("\n" if imported else "")
    _write_or_print(output, args.output)
    return 0


def _host_stop_command(args: argparse.Namespace) -> int:
    host = _persistent_host(args)
    host.request_stop()
    _write_or_print(str(host.paths.stop_file) + "\n", args.output)
    return 0


def _workspace_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--json-out")


def _service_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--service-config", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="msos-autobuilder")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser(
        "inventory",
        help="scan a product repository without modifying it",
    )
    inventory.add_argument("--repo", required=True)
    inventory.add_argument("--contract", required=True)
    inventory.add_argument("--rules", required=True)
    inventory.add_argument("--json-out")
    inventory.add_argument("--markdown-out")
    inventory.set_defaults(func=_inventory_command)

    workspace_witness = subparsers.add_parser(
        "workspace-witness",
        help="materialize two read-only isolated product workspaces",
    )
    _workspace_arguments(workspace_witness)
    workspace_witness.set_defaults(func=_workspace_witness_command)

    process_witness = subparsers.add_parser(
        "process-witness",
        help="run two path-scoped worker processes in isolated product clones",
    )
    _workspace_arguments(process_witness)
    process_witness.set_defaults(func=_process_witness_command)

    codex_preflight = subparsers.add_parser(
        "codex-preflight",
        help="validate a local Codex shadow host without running a build",
    )
    codex_preflight.add_argument("--host-config", required=True)
    codex_preflight.add_argument("--json-out")
    codex_preflight.set_defaults(func=_codex_preflight_command)

    codex_shadow = subparsers.add_parser(
        "codex-shadow",
        help="run configured Codex lanes in disposable clones with publication disabled",
    )
    codex_shadow.add_argument("--host-config", required=True)
    codex_shadow.add_argument("--manifest", required=True)
    codex_shadow.add_argument("--json-out")
    codex_shadow.set_defaults(func=_codex_shadow_command)

    host_init = subparsers.add_parser(
        "host-init",
        help="initialize the persistent local queue and status files",
    )
    _service_config_argument(host_init)
    host_init.add_argument("--json-out")
    host_init.set_defaults(func=_host_init_command)

    host_run_once = subparsers.add_parser(
        "host-run-once",
        help="synchronize the feed and process at most one approved job",
    )
    _service_config_argument(host_run_once)
    host_run_once.add_argument("--no-feed", action="store_true")
    host_run_once.add_argument("--json-out")
    host_run_once.set_defaults(func=_host_run_once_command)

    host_run = subparsers.add_parser(
        "host-run",
        help="run the approval-gated local Autobuilder host until stopped",
    )
    _service_config_argument(host_run)
    host_run.set_defaults(func=_host_run_command)

    host_status = subparsers.add_parser(
        "host-status",
        help="show persistent host heartbeat, queue counts, and last result",
    )
    _service_config_argument(host_status)
    host_status.add_argument("--json-out")
    host_status.set_defaults(func=_host_status_command)

    host_enqueue = subparsers.add_parser(
        "host-enqueue",
        help="copy a Codex manifest into the local approval-gated queue",
    )
    _service_config_argument(host_enqueue)
    host_enqueue.add_argument("--manifest", required=True)
    host_enqueue.add_argument("--job-id", required=True)
    host_enqueue.add_argument("--approve", action="store_true")
    host_enqueue.add_argument("--requested-by", default="local-operator")
    host_enqueue.add_argument("--expected-source-head")
    host_enqueue.add_argument("--output")
    host_enqueue.set_defaults(func=_host_enqueue_command)

    host_approve = subparsers.add_parser(
        "host-approve",
        help="approve one already-pending local job",
    )
    _service_config_argument(host_approve)
    host_approve.add_argument("--job-id", required=True)
    host_approve.add_argument("--output")
    host_approve.set_defaults(func=_host_approve_command)

    host_sync_feed = subparsers.add_parser(
        "host-sync-feed",
        help="synchronize approved Git job manifests without executing them",
    )
    _service_config_argument(host_sync_feed)
    host_sync_feed.add_argument("--output")
    host_sync_feed.set_defaults(func=_host_sync_feed_command)

    host_stop = subparsers.add_parser(
        "host-stop",
        help="request a graceful stop from the persistent local host",
    )
    _service_config_argument(host_stop)
    host_stop.add_argument("--output")
    host_stop.set_defaults(func=_host_stop_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
