"""Command-line interface for read-only Autobuilder tools."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from .inventory import build_inventory, render_json, render_markdown


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
