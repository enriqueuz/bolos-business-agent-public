# Evaluation and reliability

`tests/` includes domain, application, API, agent and integration coverage. Scripted model responses exercise tool calling deterministically. PostgreSQL and Redis integration tests verify persistence and coordination rather than replacing those systems with in-memory approximations. The supported backend path is the isolated Docker Compose test profile shown in the README.

`evals/cases.py` defines seeded conversational scenarios and checks tool behavior and database outcomes. `evals/run.py` executes model-backed scenarios; these require credentials, incur cost and must never target operational data. Generated results are excluded because they can contain messages and financial context. This public snapshot does not ship historical execution results or claim their pass rates.

`security_tests/` provides service-free regressions for administrative authority, password rotation and disabled destructive maintenance. Run them separately with the command in the README. They complement the integration suite and do not establish complete endpoint coverage.

Structured logging, model usage measurement and optional incident reporting provide operational evidence. Incident output and automated analysis are diagnostic hypotheses and require verification. Avoid enabling external issue creation or sending customer conversations during local testing.

Ruff checks source quality, import-linter checks architectural constraints, Bandit identifies static security patterns, Gitleaks scans accidental secrets and pip-audit checks known dependency advisories. These tools have different blind spots. Record actual results and limitations rather than equating a clean scan with safety.
