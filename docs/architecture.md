# Architecture

FastAPI composes dependencies through `app/container.py`. Operational panel routes and background workers share application commands and services. Some system provisioning and configuration routes still manage persistence in the API layer; this snapshot preserves that existing boundary rather than claiming every administrative write uses CommandBus. `app/domain/` holds framework-independent value rules; `app/application/` defines commands, queries, authorization and ports; `app/infra/` supplies persistence and external implementations. Import-linter contracts in `pyproject.toml` enforce selected dependency directions.

`app/api/webhooks.py` verifies incoming messaging requests and queues persisted inbound work. `app/worker.py` runs arq jobs and coordinates conversation locks. `app/agent/pipeline.py` builds a turn, handles media and conversation state, invokes orchestration and delivers responses. Redis stores ephemeral conversation state, coordination keys and queue jobs. PostgreSQL stores durable business records through SQLAlchemy; Alembic versions schema changes.

`app/application/bus.py` is the command boundary for business operations. Commands validate actor and tenant scope, enforce financial invariants and produce auditable results. Financial values use Decimal and historical monetary legs preserve their recorded rates. Retail and brokerage have distinct tools and accounting rules. Brokerage sales credit commission rather than principal; historical records retain frozen semantics.

Messaging implementations live behind `app/channels/port.py`. Model configuration lives in `app/llm/`; domain logic does not select providers. Attachments use an application service and storage port. Optional observability integrations are separate from financial persistence.

The source can run API and worker separately or embed a worker in a single API process. This describes supported code composition, not an assertion about any private deployment. The complementary web panel is outside this repository.
