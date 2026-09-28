# Reuse Assessment Task Packet V1

Use this section in a charter or implementation handoff whenever `docs/ADOPT_BEFORE_BUILD_V1.md` applies.

```markdown
## Reuse assessment

### Gate

- Required: yes | no
- Exemption reason: <required when no>

### Capability

<Describe the required capability without assuming a library, framework, SDK, or custom implementation.>

### Existing internal overlap

- Repository/path: <path or none>
- Reusable behavior: <what already exists>
- Why it is sufficient or insufficient: <evidence>

### Candidate assessment

| Candidate | Canonical source | Version/commit | Official? | Licence evidence | Maintenance status | Fit | Missing behavior | Coupling/exit cost | Security/data surface | Disposition |
|---|---|---|---|---|---|---|---|---|---|---|
| <name> | <source> | <identity> | yes/no | <licence source> | <active/stable/deprecated/archived/replaced/unclear> | <fit> | <gaps> | <low/medium/high + reason> | <surface> | adopt/wrap/extract/fork/reject/defer |

### Decision

- Class: ADOPT | WRAP | EXTRACT | FORK | BUILD | NOT_APPLICABLE
- Selected candidate: <name or internal>
- Decision rationale: <decisive evidence>
- Rejected alternatives: <candidate → reason>

### Owned boundary

- Interface/service/data contract: <name>
- Third-party types normalized at: <path/boundary>
- Credential boundary: <location/owner>
- Version/update policy: <pin/range/commit and review rule>
- Exit/migration path: <how the capability can be replaced>

### Validation

- Contract tests: <required behaviors and failures>
- Integration spike: <bounded disposable work, if needed>
- Parity/performance evidence: <comparison>
- Licence/maintenance evidence rechecked at: <review point>

### Implementation constraints

- Allowed dependency/repository changes: <exact>
- Forbidden dependency/repository changes: <exact>
- Source copying permitted: no | yes, with provenance and licence details
- Fork permitted: no | yes, with maintenance owner
```

## Review outcome

A task is implementation-ready only when:

- the gate status is explicit;
- the capability is implementation-neutral;
- serious internal, official, and maintained open-source alternatives are represented;
- licence and maintenance evidence come from canonical sources;
- one decision class is selected;
- the owned boundary and tests are concrete;
- the accepted dependency or repository change matches implementation authority.

## Compact form for narrow work

For a small but non-exempt capability, this compact form is sufficient:

```markdown
## Reuse assessment

Capability: <implementation-neutral requirement>
Candidates checked: <internal + official/OSS>
Decision: <ADOPT/WRAP/EXTRACT/FORK/BUILD>
Why: <one paragraph with licence, maintenance, fit, and coupling evidence>
Owned boundary: <interface/path>
Validation: <contract test or spike>
```
