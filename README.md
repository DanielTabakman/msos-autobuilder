# MSOS Autobuilder

Open-source build-factory infrastructure for Market Structure OS (MSOS).

## Current status

This repository is in **extraction/bootstrap** mode. It is intentionally read-only with respect to production MSOS repositories.

It may:

- load and validate a product contract;
- plan isolated build lanes;
- check path ownership and conflicts;
- model worker capabilities and cost classes;
- run tests against synthetic fixture repositories.

It must not yet:

- commit or push to MSOS;
- open or merge pull requests;
- hold production credentials;
- use GitHub as a runtime-state bus;
- run concurrently as a second production publisher.

## Safety invariants

1. Parallel workers are allowed; overlapping path ownership is not.
2. Multiple builders are allowed; only one production publisher may be enabled.
3. Runtime queue, lease, worker, and operator state stays outside product Git repositories.
4. Product behavior is described through a versioned project contract, not imports from MSOS/PPE business modules.
5. Public fixtures and examples contain synthetic values only.

## Planned shape

```text
src/msos_autobuilder/
  backends/        worker-provider interfaces
  contracts.py     product contract loading and validation
  lanes.py         lane ownership and concurrency checks
  models.py        task, lane, lease, and capability models
fixtures/          synthetic product repositories
 tests/            factory-only tests
```

The first milestone is tracked in issue #1. The parent extraction chapter is tracked in `DanielTabakman/Probability-prediction-engine#5348`.
