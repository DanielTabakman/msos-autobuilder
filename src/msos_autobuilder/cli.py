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


def _workspace_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--runtime-root", required=True)
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
