"""Trusted draft lifecycle; the existing agent interprets adjustment intent."""
from app.application.conversation import ReconciliationDraft
from app.application.reconciliation import RECONCILIATION_KINDS

RECONCILIATION_TOOLS = frozenset({"reconcile_balance", "delete_balance_adjustment", "cancel_balance_reconciliation"})


async def ensure_reconciliation(deps, *, new_request: bool = False) -> None:
    """Start trusted state after the agent proposes the typed reconciliation action."""
    if deps.reconciliation is None or new_request:
        deps.reconciliation = ReconciliationDraft(origin_message_id=str(deps.inbound_id))
        await deps.store.set_reconciliation(deps.scope, deps.reconciliation)


async def finish_reconciliation(deps, result) -> None:
    if result.kind in RECONCILIATION_KINDS and not result.needs_confirmation:
        await deps.store.clear_reconciliation(deps.scope)
        deps.reconciliation = None
