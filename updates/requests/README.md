# Approved Update Requests

Files in this directory are release-control inputs, not live manifests.

Each `*.yaml` request must be reviewed through a pull request and merged to `main`. The publication workflow requires exactly one changed request, resolves `commit: self` or an explicit exact Git SHA, calculates every expected-file hash from that commit, self-validates the completed manifest, and publishes it to the dedicated `updates` branch.

Do not place credentials, mutable branch names, short SHAs, or manually calculated file hashes in request files.
