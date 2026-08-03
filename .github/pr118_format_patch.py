from pathlib import Path

path = Path("tests/test_windows_self_update_supervisor.py")
text = path.read_text(encoding="utf-8")
replacements = {
    '''            "    if (-not $script:ProtectedMutationApplied -and $TaskName -eq 'MSOS Autobuilder Host') {",\n''': '''            (\n                "    if (-not $script:ProtectedMutationApplied -and "\n                "$TaskName -eq 'MSOS Autobuilder Host') {"\n            ),\n''',
    '''            "                    New-Item -ItemType Directory -Force -Path $MutationParent | Out-Null",\n''': '''            (\n                "                    New-Item -ItemType Directory -Force "\n                "-Path $MutationParent | Out-Null"\n            ),\n''',
    '''            "                default { throw 'unsupported protected mutation operation: $MutationOperation' }",\n''': '''            (\n                "                default { throw 'unsupported protected "\n                "mutation operation: $MutationOperation' }"\n            ),\n''',
}
for old, new in replacements.items():
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one formatting anchor, found {count}: {old!r}")
    text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")
