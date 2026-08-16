# Issue #119 isolated approved-job feed

This directory is the dedicated useful-work feed for the isolated Issue #119 pilot host.

It is separate from the durable historical global feed at `jobs/approved`.
Do not move, delete, edit, supersede, or mark seen anything under `jobs/approved`.

Rules:

- every executable file must be YAML;
- `version: 1`;
- immutable unique `job_id`;
- `approved: true`;
- `publication_enabled: false` at both job and embedded manifest layers;
- inline instructions only;
- no secrets;
- no product publication authority.

This path starts README-only. YAML jobs are admitted later by authorized Issue #119 actions.
The host imports each job ID once into its local atomic queue. Replacing content under an existing job ID fails closed.
Non-YAML files, including this README, are ignored by feed import.
