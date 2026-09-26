# Initial Capability Reuse Audit — 2026-07-25

**Status:** Draft technical-founder assessment  
**Tracking:** Issue #86  
**Scope:** Current PPE/MSOS capability surface; no dependency or runtime changes

## Executive decision

The first reuse posture should be selective:

1. **Polymarket:** wrap the official successor SDK for future authenticated and broader API work, but retain the current read-only path until a parity spike succeeds because the new SDK is beta.
2. **Canadian/crypto market connectivity:** use CCXT immediately for a bounded NDAX market-data and order-book spike; evaluate Hummingbot as the longer-lived wrapped execution/connector service.
3. **Options mathematics:** keep validated narrow PPE math; spike QuantLib only where calendars, term structures, volatility surfaces, or instrument conventions would otherwise require substantial custom infrastructure.
4. **Large trading/backtesting engines:** do not adopt now. Their licence, coupling, and operational surface exceed the current capability need.

This audit does not authorize credentials, live trading, fund movement, paid services, or production dependency changes.

## Current repository evidence

PPE currently declares Python 3.11+, pandas, NumPy, yfinance, requests, Streamlit, Plotly, Playwright, PyYAML, and python-dotenv. It does not currently declare an official Polymarket SDK, CCXT, Hummingbot, QuantLib, VectorBT, or NautilusTrader dependency.

The README describes custom `src/data/` fetchers for Yahoo Finance and Polymarket, custom `src/engine/` probability/opportunity logic, and a Streamlit interface.

The differentiated layer is the canonical event model, belief/probability comparison, opportunity scoring, payoff fit, explanation, and workflow. Exchange protocol clients, generic pricing conventions, and execution plumbing are commodity candidates.

---

## Capability 1 — Polymarket public data and trading integration

### Required capability

Retrieve normalized prediction-market data now; later support authenticated account, order, trading, wallet, and real-time workflows without maintaining provider protocol details throughout PPE.

### Candidates

#### Current custom HTTP integration

- **Source:** PPE `src/data/` and current `requests` dependency.
- **Fit:** already supports the current narrow read-only product behavior.
- **Strength:** minimal coupling and no beta SDK dependency.
- **Weakness:** MSOS/PPE owns provider endpoint churn, schemas, authentication, retries, and future trading workflows.

#### Archived `Polymarket/py-clob-client`

- **Source:** https://github.com/Polymarket/py-clob-client
- **Licence:** MIT.
- **Maintenance:** archived May 25, 2026; repository states that it is no longer maintained or functional and directs users to `Polymarket/py-sdk`.
- **Disposition:** reject.

#### Official `Polymarket/py-sdk`

- **Source:** https://github.com/Polymarket/py-sdk
- **Licence:** MIT.
- **Relationship:** official Polymarket Python SDK.
- **Maintenance:** active successor.
- **Status:** beta; prerelease installation and public API churn remain possible.
- **Fit:** unified interface across public data, account, trading, builder attribution, and wallet workflows.
- **Risk:** adopting beta provider types directly into product modules would create churn and lock-in.

### Decision

**Class: WRAP**

Create or preserve an MSOS-owned `PredictionMarketProvider` boundary, with a `PolymarketProvider` implementation.

- Keep the current read-only integration until an SDK parity spike proves equivalent market/event/order-book coverage and acceptable performance.
- Use the official SDK for new authenticated/trading/wallet behavior instead of extending bespoke protocol code.
- Normalize SDK objects into existing canonical event and quote models at the adapter boundary.
- Pin a tested prerelease version until the SDK reaches a stable API.

### Validation spike

- compare current fetcher and official SDK outputs for the same set of markets;
- verify event identity, market identity, outcomes, prices, liquidity/order-book fields, pagination, rate-limit behavior, and failures;
- run without credentials first;
- keep metered/live-order tests disabled by default;
- prove that replacing the adapter does not change the engine or UI contracts.

---

## Capability 2 — NDAX and cross-exchange crypto connectivity

### Required capability

Fetch normalized NDAX and comparison-venue market data, inspect order books and trading rules, detect Canadian pricing asymmetries, and later submit/cancel orders through a controlled execution boundary.

### Candidate A — CCXT

- **Source:** https://github.com/ccxt/ccxt
- **Licence:** MIT.
- **Maintenance:** active; broad exchange list.
- **Current fit:** CCXT lists NDAX as a supported Canadian exchange and provides unified public/private REST methods.
- **Strength:** fastest path to test whether NDAX-SOL or other Canadian asymmetries exist without building a new exchange client.
- **Constraint:** WebSocket support is part of CCXT Pro rather than the free core.
- **Coupling:** moderate if PPE imports CCXT exchange objects broadly; low if normalized behind a provider interface.

### Candidate B — Hummingbot

- **Source:** https://github.com/hummingbot/hummingbot
- **Licence:** Apache 2.0.
- **Maintenance:** active; modular exchange connectors, strategy framework, and live-bot infrastructure.
- **Current fit:** Hummingbot standardizes REST and WebSocket exchange connectors and recent release history includes an NDAX connector.
- **Strength:** existing market-making, connector, strategy, order lifecycle, and bot runtime infrastructure.
- **Constraint:** large operational/runtime surface relative to a read-only asymmetry detector.
- **Coupling:** high if embedded directly; acceptable if run as a separate execution service with a narrow contract.

### Decision

**Stage 1 class: WRAP CCXT** for a bounded read-only NDAX data spike.

Define an `ExchangeMarketDataProvider` boundary with normalized:

- venue and symbol identity;
- market metadata and precision;
- ticker and timestamp;
- best bid/offer;
- order-book depth;
- fees where available;
- recent trades and candles;
- explicit stale/error state.

Use CCXT to answer the first business question: does an actionable Canadian asymmetry exist after fees, depth, transfer latency, and quote staleness?

**Stage 2 class: WRAP Hummingbot** when the objective requires persistent WebSockets, order lifecycle, market making, multi-venue execution, or live-bot operation.

Run Hummingbot as an execution/connector service rather than turning PPE/MSOS into a Hummingbot application. PPE/MSOS owns opportunity detection, decision policy, risk limits, intent, and evidence; Hummingbot owns venue-specific connectivity and execution mechanics.

### Rejected alternatives

- **Custom NDAX client now:** rejected because both CCXT and Hummingbot currently expose NDAX support; custom protocol work is not justified before a parity/failure test.
- **Embed all of Hummingbot in PPE:** rejected because the runtime and architecture are much larger than the current read-only opportunity question.
- **CCXT as final live WebSocket execution layer by default:** deferred because free core WebSocket coverage does not meet the assumed live requirement.

### Validation spike

1. Read-only CCXT NDAX connector test with no order authority.
2. Enumerate available SOL/CAD, SOL/USD, BTC/CAD, stablecoin, and comparison pairs actually exposed by NDAX.
3. Compare timestamps, fees, precision, minimum sizes, best bid/offer, and executable depth against at least one liquid comparison venue.
4. Calculate gross spread, fee-adjusted spread, depth-adjusted spread, and transfer/settlement assumptions separately.
5. Record connector gaps and stale/error behavior.
6. Only after evidence of opportunity, evaluate a Hummingbot service spike and credentials boundary.

---

## Capability 3 — Options pricing, calendars, and term structures

### Required capability

Support options valuation and payoff comparison accurately enough for PPE’s decision workflow, including future calendar spreads, expiries, rates, dividends/carry, volatility term structures, and instrument conventions.

### Candidate A — current PPE math

- **Fit:** narrow, understandable, already aligned with the product workflow.
- **Strength:** low coupling and easy explanation.
- **Weakness:** custom work grows rapidly as calendars, surfaces, conventions, and instruments expand.

### Candidate B — QuantLib

- **Source:** https://github.com/lballabio/QuantLib
- **Licence:** non-copylefted free software; canonical licence must be confirmed for the selected Python package/binding.
- **Maintenance:** active; latest observed release in the audit was 1.42.1 on April 17, 2026.
- **Fit:** comprehensive quantitative-finance framework for instruments, pricing, risk, calendars, curves, and models.
- **Constraint:** large conceptual surface and C++/binding complexity for simple payoff calculations.

### Decision

**Class: BUILD for narrow product math; WRAP QuantLib for complex conventions.**

Do not replace simple, validated PPE probability/payoff logic merely because QuantLib exists.

Create an owned `OptionsPricer`/`MarketConventionProvider` boundary before adding complex calendar or term-structure behavior. Run a spike when the next feature would otherwise require hand-building:

- exchange/business-day calendars;
- yield/dividend/borrow curves;
- volatility surfaces;
- multi-leg valuation with instrument conventions;
- Greeks or model comparisons beyond the current narrow formulas.

QuantLib becomes a reference and optional engine behind the boundary, not the source of product meaning or user explanation.

### Validation spike

- compare a small set of vanilla calls/puts and calendar structures against existing PPE calculations and independent fixtures;
- test date/calendar and expiry edge cases;
- separate model inputs from user belief distributions;
- prove deterministic serialization of inputs and outputs;
- quantify install/runtime complexity before acceptance.

---

## Capability 4 — backtesting and full trading engines

### VectorBT

- **Source:** https://github.com/polakowo/vectorbt
- **Licence:** Apache 2.0 with Commons Clause according to the project; commercial products or services primarily comprising the software are restricted.
- **Fit:** powerful research and parameter-sweep engine.
- **Decision:** reject for product embedding now. The licence requires deliberate legal/commercial review, and PPE does not currently need a large backtesting platform.

### NautilusTrader

- **Source:** https://github.com/nautechsystems/nautilus_trader
- **Licence:** LGPLv3.
- **Fit:** production-grade multi-asset research, simulation, and live execution engine.
- **Decision:** defer. It is a credible future platform candidate, but adopting it now would replace architecture rather than provide one bounded capability. Reconsider only when research-to-live parity and multi-asset execution become an accepted core objective.

---

## Owned interfaces to establish

These interfaces are architectural targets, not an instruction to build them all immediately:

```text
PredictionMarketProvider
ExchangeMarketDataProvider
ExecutionProvider
OptionsPricer
MarketConventionProvider
OpportunityDetector
```

PPE/MSOS should own canonical inputs, outputs, timestamps, error/stale states, evidence, and decision semantics. Provider-specific types stay inside adapters.

## First implementation order

1. Merge the adopt-before-build charter.
2. Create a bounded CCXT NDAX read-only capability spike.
3. Create a Polymarket official-SDK parity spike before new provider functionality.
4. Add a QuantLib spike only when a specific options feature crosses the complexity threshold.
5. Evaluate Hummingbot service integration after the read-only asymmetry evidence demonstrates a reason to execute.

## Decision rules

- No custom exchange client while a maintained connector candidate exists, unless a focused test proves the connector inadequate.
- No provider SDK types in engine or UI modules.
- No live credentials or order authority in a discovery spike.
- No large framework adoption without a narrow capability boundary, deletion path, and operational-cost comparison.
- No dependency acceptance based on stars or “open source” wording alone.
- No replacement of differentiated PPE logic unless parity and product benefit are demonstrated.

## Sources reviewed

- PPE README and `pyproject.toml` on current `main`.
- https://github.com/Polymarket/py-clob-client
- https://github.com/Polymarket/py-sdk
- https://github.com/ccxt/ccxt
- https://github.com/ccxt/ccxt/wiki/Exchange-Markets
- https://github.com/hummingbot/hummingbot
- https://github.com/hummingbot/hummingbot/releases
- https://github.com/lballabio/QuantLib
- https://github.com/polakowo/vectorbt
- https://github.com/nautechsystems/nautilus_trader

Source status and licences must be rechecked at implementation time.

## Coordination Status

Agreement: partial  
Compared: issue #86, current PPE README/dependencies, current official project repositories, draft PRs #71 and #72  
Disagreement: none; decisions remain hypotheses until the charter and bounded spikes are reviewed  
Evidence gap: local integration tests, exact package versions, NDAX live public-data behavior, and Polymarket SDK parity  
Ownership overlap: none; audit changes no runtime or active-PR-owned paths  
Risk if unresolved: custom NDAX/Polymarket plumbing is built unnecessarily or a large framework is adopted before product need is proven  
Recommended default: CCXT NDAX read-only spike first; Hummingbot only after opportunity evidence; official Polymarket SDK behind an adapter; QuantLib only at a concrete complexity threshold  
Founder decision required: no
