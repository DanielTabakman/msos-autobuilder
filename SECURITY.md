# Security policy

MSOS Autobuilder is a public repository. Never commit:

- GitHub, cloud, SSH, model-provider, or notification credentials;
- production hostnames, private IP addresses, or operator inventories;
- live queue, lease, worker, or operator runtime state;
- logs or artifacts that may contain prompts, tokens, or private source code;
- production repository write configuration.

Fixtures must use synthetic values. Production credentials belong in the runtime secret store of the eventual deployment and must be scoped to the minimum required repository permissions.

During bootstrap, all included backends are read-only and publication is disabled.
