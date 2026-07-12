# Approved Autobuilder Jobs

This directory is the durable, read-only manifest feed for the persistent local Autobuilder host.

Rules:

- every executable file must be YAML;
- `version: 1`;
- immutable unique `job_id`;
- `approved: true`;
- `publication_enabled: false` at both job and embedded manifest layers;
- inline instructions only;
- no secrets;
- no product publication authority.

The host imports each job ID once into its local atomic queue. Replacing content under an existing job ID fails closed.
