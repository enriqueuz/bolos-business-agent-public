# Contributor instructions

Read docs/architecture.md, docs/agent-design.md and docs/evaluation.md before substantial changes.
Use Decimal for money. Preserve tenant scope from authenticated context. All business writes must use application commands.
Keep provider behavior in adapters. Models interpret intent; deterministic code authorizes, validates, calculates and persists.
Never use real customer data or production credentials in tests. Do not run live-model evaluations by default.
Use the Docker test profile described in README.md. Do not deploy this showcase or enable destructive system maintenance.
