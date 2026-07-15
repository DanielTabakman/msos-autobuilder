"""Command-line interface for read-only Autobuilder tools."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from .build_next import BuildNextConfig, build_next, render_receipt_json
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
from .refill_controller import (
    RefillConfig,
    RefillService,
    keep_one_running,
    load_refill_policy,
    pause_builds,
    reconcile_refill,
    render_refill_report_json,
    render_refill_service_status_json,
    resume_builds,
)
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


def _build_next_command(args: argparse.Namespace) -> int:
    if args.service_config:
        config = BuildNextConfig.from_service_config(
            args.service_config,
            checkout_root=Path(args.checkout_root) if args.checkout_root else None,
            max_snapshot_age_seconds=args.max_snapshot_age_seconds,
            requested_by=args.requested_by,
            submit=not args.dry_run,
        )
    else:
        if not args.ppe_repo or not args.feed_repo_url:
            raise SystemExit(
                "build-next requires --service-config or both --ppe-repo and --feed-repo-url"
            )
        config = BuildNextConfig(
            ppe_repo=Path(args.ppe_repo),
            feed_repo_url=args.feed_repo_url,
            jobs_branch=args.jobs_branch,
            jobs_path=args.jobs_path,
            checkout_root=Path(args.checkout_root) if args.checkout_root else None,
            host_root=Path(args.host_root) if args.host_root else None,
            max_snapshot_age_seconds=args.max_snapshot_age_seconds,
            requested_by=args.requested_by,
            submit=not args.dry_run,
        )
    receipt = build_next(config)
    _write_or_print(render_receipt_json(receipt), args.json_out)
    return 2 if receipt.status == "BLOCKED" else 0


def _refill_config(args: argparse.Namespace, *, submit: bool = True) -> RefillConfig:
    build_config = BuildNextConfig.from_service_config(
        args.service_config,
        checkout_root=Path(args.checkout_root) if args.checkout_root else None,
        max_snapshot_age_seconds=args.max_snapshot_age_seconds,
        requested_by=args.requested_by,
        submit=submit,
    )
    return RefillConfig(
        build_next=build_config,
        policy_path=Path(args.policy_path) if args.policy_path else None,
        max_host_heartbeat_age_seconds=args.max_host_heartbeat_age_seconds,
    )


def _refill_keep_one_command(args: argparse.Namespace) -> int:
    policy = keep_one_running(_refill_config(args))
    report = reconcile_refill(_refill_config(args))
    _write_or_print(render_refill_report_json(report), args.json_out)
    return 0 if policy.enabled else 2


def _refill_pause_command(args: argparse.Namespace) -> int:
    pause_builds(_refill_config(args))
    report = reconcile_refill(_refill_config(args))
    _write_or_print(render_refill_report_json(report), args.json_out)
    return 0


def _refill_resume_command(args: argparse.Namespace) -> int:
    resume_builds(_refill_config(args))
    report = reconcile_refill(_refill_config(args))
    _write_or_print(render_refill_report_json(report), args.json_out)
    return 2 if report.status in {"BLOCKED", "BACKPRESSURE"} else 0


def _refill_reconcile_command(args: argparse.Namespace) -> int:
    report = reconcile_refill(_refill_config(args, submit=not args.dry_run))
    _write_or_print(render_refill_report_json(report), args.json_out)
    return 2 if report.status in {"BLOCKED", "BACKPRESSURE"} else 0


def _refill_status_command(args: argparse.Namespace) -> int:
    config = _refill_config(args, submit=False)
    policy = load_refill_policy(config)
    report = reconcile_refill(config)
    _write_or_print(render_refill_report_json(report), args.json_out)
    return 0 if policy.enabled else 2


def _refill_run_command(args: argparse.Namespace) -> int:
    service = RefillService(
        _refill_config(args),
        interval_seconds=args.interval_seconds,
    )
    service.run_forever()
    return 0


def _refill_stop_command(args: argparse.Namespace) -> int:
    service = RefillService(_refill_config(args, submit=False))
    path = service.request_stop()
    _write_or_print(str(path) + "\n", args.output)
    return 0


def _refill_service_status_command(args: argparse.Namespace) -> int:
    service = RefillService(_refill_config(args, submit=False))
    _write_or_print(render_refill_service_status_json(service.read_status()), args.json_out)
    return 0


def _workspace_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--json-out")


def _service_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--service-config", required=True)


def _refill_arguments(parser: argparse.ArgumentParser) -> None:
    _service_config_argument(parser)
    parser.add_argument("--checkout-root")
    parser.add_argument("--policy-path")
    parser.add_argument("--max-snapshot-age-seconds", type=int, default=600)
    parser.add_argument("--max-host-heartbeat-age-seconds", type=int, default=300)
    parser.add_argument("--requested-by", default="capacity-one refill controller")
    parser.add_argument("--json-out")


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

    build_next_parser = subparsers.add_parser(
        "build-next",
        help="dispatch exactly one PPE READY_TO_BUILD item through the approved job feed",
    )
    build_next_parser.add_argument("--service-config")
    build_next_parser.add_argument("--ppe-repo")
    build_next_parser.add_argument("--feed-repo-url")
    build_next_parser.add_argument("--jobs-branch", default="jobs")
    build_next_parser.add_argument("--jobs-path", default="jobs/approved")
    build_next_parser.add_argument("--checkout-root")
    build_next_parser.add_argument("--host-root")
    build_next_parser.add_argument("--max-snapshot-age-seconds", type=int, default=600)
    build_next_parser.add_argument("--requested-by", default="founder build next")
    build_next_parser.add_argument("--dry-run", action="store_true")
    build_next_parser.add_argument("--json-out")
    build_next_parser.set_defaults(func=_build_next_command)

    refill_status = subparsers.add_parser(
        "refill-status",
        help="show bounded capacity-one refill state without submitting a job",
    )
    _refill_arguments(refill_status)
    refill_status.set_defaults(func=_refill_status_command)

    refill_keep_one = subparsers.add_parser(
        "refill-keep-one",
        help="persist founder intent to keep one approved build running",
    )
    _refill_arguments(refill_keep_one)
    refill_keep_one.set_defaults(func=_refill_keep_one_command)

    refill_pause = subparsers.add_parser(
        "refill-pause",
        help="pause new refill dispatch without stopping current workers",
    )
    _refill_arguments(refill_pause)
    refill_pause.set_defaults(func=_refill_pause_command)

    refill_resume = subparsers.add_parser(
        "refill-resume",
        help="resume capacity-one refill after reconciling current state",
    )
    _refill_arguments(refill_resume)
    refill_resume.set_defaults(func=_refill_resume_command)

    refill_reconcile = subparsers.add_parser(
        "refill-reconcile",
        help="reconcile bounded capacity-one refill and dispatch through build-next if empty",
    )
    _refill_arguments(refill_reconcile)
    refill_reconcile.add_argument("--dry-run", action="store_true")
    refill_reconcile.set_defaults(func=_refill_reconcile_command)

    refill_run = subparsers.add_parser(
        "refill-run",
        help="run the managed capacity-one refill service until gracefully stopped",
    )
    _refill_arguments(refill_run)
    refill_run.add_argument("--interval-seconds", type=float, default=30.0)
    refill_run.set_defaults(func=_refill_run_command)

    refill_stop = subparsers.add_parser(
        "refill-stop",
        help="request graceful stop from the managed refill service",
    )
    _refill_arguments(refill_stop)
    refill_stop.add_argument("--output")
    refill_stop.set_defaults(func=_refill_stop_command)

    refill_service_status = subparsers.add_parser(
        "refill-service-status",
        help="show durable managed refill service heartbeat and last reconciliation",
    )
    _refill_arguments(refill_service_status)
    refill_service_status.set_defaults(func=_refill_service_status_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
