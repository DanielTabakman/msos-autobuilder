# Adopt Before Build V1

**Status:** Draft control-plane charter  
**Owner:** Founder / technical architecture  
**Tracking:** Issue #86  
**Scope:** MSOS Autobuilder and PPE/MSOS engineering objectives

## Purpose

The factory must not treat custom implementation as the default merely because an objective can be coded locally.

Before substantial implementation, agents determine whether the required capability should be adopted, wrapped, extracted, forked, or built. The objective is to remove unnecessary commodity code while keeping product meaning, strategic differentiation, and architectural control inside MSOS/PPE.

This is not a dependency-maximization policy. It is an evidence-before-implementation policy.

## Founder rule

> Search broadly, adopt narrowly, wrap aggressively, own the interfaces.

External code is useful when it removes work without silently owning the product architecture.

## Where this gate belongs

The reuse decision is made during chartering and task decomposition, before a bounded implementation job is approved.

The implementation agent executes the accepted decision. It does not silently replace the decision with a different library, framework, fork, or custom implementation.

GitHub remains canonical. A chat-only conclusion is not an accepted reuse decision.

## Trigger

A reuse assessment is required when an objective introduces or materially changes any of the following:

- a product capability;
- an external protocol, exchange, market, data source, or service;
- pricing, risk, portfolio, backtesting, execution, or market-data infrastructure;
- agent, scheduling, queueing, orchestration, memory, or workflow infrastructure;
- authentication, storage, observability, deployment, or integration infrastructure;
- a third-party dependency, SDK, repository, binary, container, or hosted component;
- a component expected to become a shared abstraction or long-lived platform surface.

The assessment is also required when the same commodity behavior appears to be implemented more than once.

## Exemptions

The gate may be `NOT_APPLICABLE` for:

- documentation-only work;
- isolated tests or diagnostics;
- a narrow bug fix that introduces no new capability, dependency, or shared abstraction;
- emergency security or reliability work where research delay would increase risk;
- generated or mechanical changes that preserve an already accepted architecture.

The task packet records the reason. `NOT_APPLICABLE` is not a generic convenience bypass.

## Required search order

Agents search in this order:

1. **Current repository** — existing modules, adapters, tests, dead paths, duplicated capability.
2. **Connected MSOS/PPE repositories** — capability already implemented elsewhere in the system.
3. **Official provider SDK or reference implementation** — vendor-owned integration surface.
4. **Maintained open-source project or library** — credible third-party capability.
5. **Managed service or commercial component** — only when the capability materially benefits from external operation and spending/legal review is appropriate.
6. **Custom build** — after alternatives are evaluated or the capability is genuinely differentiated.

Popularity is not evidence of suitability.

## Evidence fields

Every required assessment records:

### Capability

Describe the capability independently of any proposed library or implementation.

### Existing internal overlap

Identify current modules, code paths, tests, or repositories that already provide all or part of the capability.

### Candidates

For each serious candidate, record:

- project/package and canonical source;
- official provider relationship, if any;
- release or commit evaluated;
- licence and commercial-use constraints from the canonical licence source;
- maintenance evidence, including archived/deprecated/replacement status;
- supported runtime and language versions;
- functional fit;
- missing behavior;
- security, credential, data, and supply-chain surface;
- architectural coupling;
- operational weight;
- expected integration and exit cost;
- testability at an MSOS-owned boundary.

### Decision

Choose exactly one:

- `ADOPT`
- `WRAP`
- `EXTRACT`
- `FORK`
- `BUILD`
- `NOT_APPLICABLE`

### Rejected alternatives

State the decisive reason each serious alternative was rejected or deferred.

### Owned boundary

Name the interface, adapter, process boundary, service boundary, or data contract that MSOS/PPE owns.

### Validation

Specify the contract tests, fixtures, integration spike, parity comparison, performance evidence, or migration witness required before the capability is trusted.

## Decision classes

### ADOPT

Use the maintained capability substantially as designed.

Use when:

- the capability is commodity;
- the project closely fits the required behavior;
- licence and maintenance posture are acceptable;
- coupling is contained;
- replacement remains practical.

### WRAP

Use the capability behind an MSOS-owned interface or service boundary.

This is the default for substantial SDKs, exchange connectors, execution engines, data providers, and frameworks.

The wrapper prevents provider types, configuration, credentials, and lifecycle assumptions from spreading across the product.

### EXTRACT

Reuse a narrow component, algorithm, schema, test corpus, protocol implementation, or validated pattern.

Extraction requires licence compatibility and provenance. Copying code without a recorded source and licence is forbidden.

### FORK

Create or maintain an intentional fork.

Fork only when:

- the upstream is close to the required capability;
- changes cannot remain as a wrapper or contribution;
- MSOS/PPE is prepared to maintain security and upstream divergence;
- the fork has an explicit update and exit policy.

### BUILD

Implement internally.

Build is preferred when:

- the capability is part of the differentiated decision layer;
- suitable options are unmaintained, incompatible, insecure, or too coupled;
- integration cost exceeds the narrow internal implementation;
- required semantics are novel or product-defining;
- evidence or licensing prevents safe adoption.

A build decision includes the smallest coherent internal scope and a future replacement boundary.

## Licence rule

Agents must inspect the canonical licence file or official licensing documentation.

Repository visibility, package availability, stars, forks, or the phrase “open source” do not establish commercial-use rights.

Copyleft, network copyleft, Commons Clause, source-available, dual-licence, trademark, data, model-weight, and optional-dependency restrictions must be surfaced explicitly.

Legal interpretation or a new commercial commitment remains a founder/legal decision. The technical assessment identifies the constraint; it does not invent legal certainty.

## Maintenance rule

The assessment distinguishes:

- actively maintained;
- stable but low-change;
- deprecated;
- archived;
- replaced by a successor;
- community-maintained without provider ownership;
- abandoned or unclear.

A stale popular project is not preferred over a maintained official successor.

## Architecture rule

For `ADOPT`, `WRAP`, `EXTRACT`, or `FORK`:

- MSOS/PPE owns the interface;
- third-party types do not cross the boundary without normalization;
- credentials remain outside shared business logic;
- versions or commits are explicit where practical;
- contract tests cover required behavior and failure modes;
- the fallback or migration path is recorded;
- deletion/replacement should not require rewriting the differentiated product layer.

## Failure behavior

A substantial task without a required assessment is not implementation-ready.

If research is incomplete:

- mark the decision `UNKNOWN` in working notes, not as an accepted task state;
- perform a bounded read-only evaluation or integration spike;
- do not install, copy, fork, or commit a new dependency merely to “see if it works” unless the spike explicitly authorizes disposable use;
- escalate only when the remaining uncertainty changes product, legal, financial, credential, security, or strategic outcomes.

## Relationship to the Autobuilder

### Phase 1 — human-readable canon

The charter and task packet carry the decision and evidence. No runtime authority changes.

### Phase 2 — machine-readable enforcement

Approved jobs carry a structured reuse decision. Substantial jobs fail closed when the field is missing or inconsistent with dependency/path changes.

### Phase 3 — planner leverage

The continuous-improvement planner may identify repeated custom commodity work, duplicate integrations, or stale dependencies and propose one bounded reuse audit. It may not autonomously accept licences, spend money, create credentials, or change product semantics.

## Review checklist

A reviewer checks:

- Was the capability defined without assuming the implementation?
- Was internal overlap searched first?
- Were official and maintained alternatives evaluated?
- Was the canonical licence inspected?
- Was maintenance/replacement status verified?
- Is the decision class explicit?
- Does MSOS/PPE own a narrow boundary?
- Are contract tests and failure modes defined?
- Is the exit cost acceptable?
- Is custom code limited to differentiation or unavoidable glue?

## Coordination Status

Agreement: partial  
Compared: control-plane operating contract, Autobuilder operating manual, issue #33, issue #86, draft PRs #71 and #72  
Disagreement: none in principle; this charter is not accepted until merged  
Evidence gap: independent review, accepted initial capability audit, and later machine enforcement  
Ownership overlap: none; this file is new and avoids paths owned by PRs #71 and #72  
Risk if unresolved: avoidable custom commodity code, stale dependency adoption, and accidental framework lock-in  
Recommended default: accept the human-readable gate and first audit before adding runtime enforcement  
Founder decision required: no
