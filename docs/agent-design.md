# Agent design

`app/agent/orchestrator.py` uses Pydantic AI. Typed tools in `app/agent/tools/` expose narrow operations rather than database access or arbitrary code execution. `app/llm/roles.py` selects models by role; routing, onboarding, ordinary actions and media extraction have distinct responsibilities. Prompts are specialized by business vertical and role.

Trusted application context supplies business and actor identity. Model arguments describe proposed operations; they do not grant tenant scope or authority. Tools dispatch application commands, where deterministic authorization, monetary calculations, concurrency and persistence take place. Model prose is not proof that a command succeeded.

The pipeline coordinates conversation history, pending choices, confirmations and entity references. `app/agent/context_builder.py` builds context; Redis conversation storage retains ephemeral state. Handles and server-side references reduce the need to expose internal database identifiers to the model.

`app/agent/money_evidence.py`, grounding modules and structured action checks constrain unsupported monetary claims and success statements. Receipts are rendered from application outcomes. Confirmation and deduplication mechanisms supplement command authorization; they do not replace it. Open-ended intent belongs to model interpretation, while schemas and objective financial facts remain deterministic.

Messages, attachments and provider output are untrusted input. Prompt instructions alone cannot establish a security boundary. External model calls may disclose supplied conversation content to the configured provider; use synthetic data for evaluation and review provider terms before connecting a real business.
