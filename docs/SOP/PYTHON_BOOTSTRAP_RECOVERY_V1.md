# Python bootstrap recovery v1

The Windows Codex host bootstrap requires Python 3.11 or newer.

When the Windows Python launcher exists but no compatible runtime is installed, the bootstrap should:

1. probe installed Python 3.13, 3.12, and 3.11 runtimes;
2. use an existing compatible `python` or `python3` executable when available;
3. otherwise run `py install 3.11` through the official Windows Python install manager;
4. verify the runtime before creating `.venv`;
5. fail with an explicit recovery command if installation is unavailable.

This behavior is host setup only. It does not affect MSOS source, workspaces, publishing, or credentials.
