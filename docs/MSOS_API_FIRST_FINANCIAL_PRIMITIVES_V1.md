# MSOS API-First Financial Primitives V1

Status: Founder-authorized product direction
Date: 2026-09-11
Owner: Daniel Tabakman

## Founder decision

Market Structure OS is now API-first.

MSOS will build small, useful, composable financial-intelligence products as owned HTTP APIs. These products may be distributed through Qatom first, but Qatom is a replaceable distribution and payment adapter, not the MSOS system of record, not the owner of MSOS product logic, and not an architectural dependency of the financial engine.

The consumer UI is no longer the primary product-development surface. Preserve it as a demo, playground, validation surface, and possible future human interface, but do not let UI polish, consumer onboarding, traditional payment rails, or brokerage-style integration block API product delivery.

## Product thesis

MSOS should make financial expertise composable.

Long term, a non-financial expert should be able to express a domain belief, compare it with market-implied belief, and use MSOS primitives to turn that disagreement into a structured, machine-readable trading expression without needing to perform the options math manually.

The company should discover the required primitive set by shipping useful tools, observing real usage, and composing the successful primitives into a structured-trade format rather than attempting to design the entire system up front.

## Architecture rule

Own the financial intelligence. Wrap distribution.

```text
market/data sources
      |
      v
MSOS financial engine
      |
      v
MSOS-owned versioned HTTP APIs
      |
      +--> Qatom adapter / catalog / pay-per-call
      +--> future MCP or agent marketplaces
      +--> direct API customers
      +--> MSOS human UI
```

Qatom-specific concerns must remain outside the financial-engine core.

Deleting the Qatom adapter must not require rewriting the financial calculations, schemas, tests, telemetry, or product catalog.

## What MSOS owns

- financial calculations and models;
- API contracts and schemas;
- versioning and backward-compatibility policy;
- data-quality semantics;
- timestamps and provenance where available;
- product catalog and documentation;
- usage telemetry collected lawfully and with appropriate privacy controls;
- contract tests and numerical validation;
- the future structured-trade format;
- the mapping from belief/constraints to financial primitives.

## What Qatom may own

- marketplace discovery;
- agent-facing catalog UX;
- pay-per-call settlement;
- Qatom wallet/twin identity;
- receipts and Qatom-native transaction plumbing;
- Qatom-specific onboarding.

Qatom must not become the only copy of MSOS product definitions, the only place prices/usage can be understood, or a required runtime dependency for MSOS calculation logic.

## Product sequence

Ship one primitive at a time and use observed demand to determine the next tool.

Initial sequence:

1. `implied_range` — read-only options-implied market distribution and threshold probability.
2. `thesis_gap` — compare a stated user/agent belief with market-implied belief.
3. `structure_fit` — map thesis + horizon + risk constraints to candidate payoff structures.
4. `payoff` — calculate machine-readable payoff characteristics for a structure.
5. `stress_test` — evaluate a structure/portfolio across explicit scenarios.
6. `position_explain` — translate an existing position into plain-language and machine-readable exposure.

This order is a default, not a promise. Real call volume, failure data, repeat use, and requested capabilities should move later priorities.

## Financial Primitive #001: implied_range

This is the next MSOS product build.

Rationale: the Options Horizon product already computes most or all required information, so the first Qatom listing should validate the full publish/discover/pay/call/return loop with minimal new financial logic.

### Contract intent

Versioned endpoint:

`GET /v1/implied-range`

Required input:

- `asset`: initially BTC or ETH;
- `expiry`: explicit supported expiry.

Optional input:

- `strike`: threshold used to return probability above/below.

Required output, when supported by source data:

- asset;
- expiry;
- `as_of` timestamp;
- spot;
- implied forward;
- ATM IV;
- median;
- middle-50% range (`p25`, `p75`);
- one-sigma range;
- probability above/below optional strike;
- method identifier;
- data-quality status and flags;
- disclaimer.

### Quality contract

Never silently manufacture or interpolate a value merely to fill the response contract.

The API must make degraded or missing data machine-readable. Example classes may include:

- `ATM_IV_UNAVAILABLE`;
- `THIN_LIQUIDITY`;
- `EXPIRY_UNSUPPORTED`;
- `SOURCE_STALE`;
- `SOURCE_UNAVAILABLE`;
- `INSUFFICIENT_CHAIN_DATA`.

A buyer must be able to distinguish `good`, `degraded`, and `unavailable` responses without parsing prose.

### API hardening requirements

- versioned path from day one;
- deterministic JSON schema;
- UTC timestamp for the market snapshot;
- explicit unit conventions;
- machine-readable error body;
- request validation;
- bounded timeout behavior;
- no secrets in responses/logs;
- basic rate limiting or abuse protection at the public boundary;
- structured request/result telemetry;
- contract and numerical regression tests;
- at least one known-good BTC fixture and one ETH fixture;
- OpenAPI description suitable for external agent registration;
- Qatom registration kept in an adapter/config layer rather than embedded in the financial engine.

## Telemetry and product discovery

The API program should answer:

- which primitive was called;
- which supported input shape was used;
- success/degraded/failure state;
- response latency;
- repeat usage where a privacy-safe identifier is available;
- downstream MSOS primitive calls when observable;
- unsupported requests and validation failures;
- optional explicit user/agent feedback when the distribution platform supports it.

Do not collect secrets or unnecessary personally identifying information. Do not make Qatom's analytics the only source of product-learning evidence.

## Structured-trade format direction

Do not attempt to finalize the full format before the primitive set is proven.

The eventual MSOS structured-trade format should be able to represent at minimum:

- market/instrument context;
- user or agent thesis;
- time horizon;
- confidence/uncertainty where supplied;
- risk and loss constraints;
- payoff objective;
- market-implied belief;
- thesis gap;
- selected structure and alternatives;
- payoff/stress characteristics;
- data-quality/provenance metadata;
- machine-readable reasoning references to the primitives used.

The format should compose proven primitives rather than duplicate their calculations.

## Commercial default

Start with low-friction pay-per-call pricing to prove demand and learn usage. Do not optimize price before proving that external agents repeatedly find the primitive useful.

Price, packaging, and Qatom catalog presentation are distribution concerns and may change without changing the MSOS financial API contract.

## Compliance boundary

Initial primitives should remain read-only decision-support and analytical tools. Avoid live order execution and avoid presenting outputs as guaranteed or individualized investment advice. Personalized recommendation, managed-account, execution, custody, or brokerage behavior requires separate legal/compliance review before launch.

Disclaimers do not replace substantive compliance review when product behavior changes.

## Product-development rules

1. Build the smallest useful financial primitive, not a platform around the primitive.
2. Reuse validated MSOS calculations before writing new math.
3. Every public primitive gets a versioned contract and quality semantics.
4. Every external platform is connected through a replaceable adapter.
5. Real usage outranks speculative roadmap preference after launch.
6. Consumer UI work is subordinate to primitive delivery unless it is required for demo, validation, or a proven customer workflow.
7. Traditional banking/payment integration is not a prerequisite for Qatom-distributed tools.
8. Do not couple core MSOS logic to Qatom identity, wallet, settlement, or catalog APIs.
9. Maintain an exit path: another distributor or direct client must be able to call the same MSOS endpoint.
10. The next approved product build is `implied_range` until completed, explicitly blocked, or superseded by a new founder decision.

## Success criteria for the first cohort

The first Qatom cohort is successful if MSOS can prove the complete external loop:

1. a real MSOS-owned endpoint is reachable;
2. Qatom can register/discover it;
3. an external agent can purchase/call it;
4. the call returns valid machine-readable financial output;
5. MSOS can observe success/failure/quality telemetry independently;
6. Qatom can account for payment/receipt;
7. the core endpoint remains callable outside Qatom;
8. the results create concrete evidence for what primitive to build next.

Call volume and revenue are important signals, but initial success does not require large revenue. The first objective is to validate the architecture, distribution loop, and product-learning loop.

## Explicit non-goals for this pivot

- rebuilding a brokerage;
- perfecting the consumer UI before API launch;
- integrating traditional banking before there is demonstrated need;
- making Qatom the canonical product database;
- designing every future financial primitive now;
- enabling live execution in the first primitive;
- hiding poor market-data quality behind a clean-looking response;
- treating marketplace dependency as product ownership.

## Founder priority

As of 2026-09-11, this document is the authoritative product-direction default for new MSOS product work in the Autobuilder planning/control plane. Existing infrastructure safety work retains its existing ownership and runtime boundaries; this decision changes product priority, not the safety contracts of the build system.
