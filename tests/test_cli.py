from __future__ import annotations

import pytest

from msos_autobuilder.cli import _refill_keep_one_command, build_parser


def test_refill_keep_one_accepts_explicit_supersession_binding() -> None:
    args = build_parser().parse_args(
        [
            "refill-keep-one",
            "--service-config",
            "service.yaml",
            "--supersede-generation",
            "refill-old",
            "--expected-generation-sha256",
            "a" * 64,
        ]
    )

    assert args.supersede_generation == "refill-old"
    assert args.expected_generation_sha256 == "a" * 64


def test_refill_keep_one_requires_complete_supersession_binding() -> None:
    args = build_parser().parse_args(
        [
            "refill-keep-one",
            "--service-config",
            "service.yaml",
            "--supersede-generation",
            "refill-old",
        ]
    )

    with pytest.raises(SystemExit, match="requires both"):
        _refill_keep_one_command(args)
