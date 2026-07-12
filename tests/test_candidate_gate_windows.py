from __future__ import annotations

import hashlib
from pathlib import Path

from msos_autobuilder import candidate_gate_windows


def test_windows_patch_hash_uses_canonical_lf_bytes(tmp_path: Path) -> None:
    patch = tmp_path / "candidate.patch"
    patch.write_bytes(b"line one\r\nline two\r\n")

    expected = hashlib.sha256(b"line one\nline two\n").hexdigest()

    assert candidate_gate_windows._sha256_file(patch) == expected


def test_non_patch_hash_remains_byte_exact(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(b"{\r\n  \"ok\": true\r\n}\r\n")

    expected = hashlib.sha256(report.read_bytes()).hexdigest()

    assert candidate_gate_windows._sha256_file(report) == expected
