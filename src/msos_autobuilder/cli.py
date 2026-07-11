"""Command-line interface for read-only Autobuilder tools."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from .inventory import build_inventory, render_json, render_markdown
from .workspace_witness import render_workspace_witness_json, run_workspace_witness


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
    output = render_workspace_witness_json(report)
    if args.json_out:
        Path(args.json_out).write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


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

    witness = subparsers.add_parser(
        "workspace-witness",
        help="materialize two read-only isolated product workspaces",
    )
    witness.add_argument("--source", required=True)
    witness.add_argument("--workspace-root", required=True)
    witness.add_argument("--runtime-root", required=True)
    witness.add_argument("--json-out")
    witness.set_defaults(func=_workspace_witness_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
