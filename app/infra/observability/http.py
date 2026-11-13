"""Unhandled API/webhook errors, without request bodies, cookies or auth headers."""
from __future__ import annotations


class IncidentMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            container = getattr(scope.get("app").state, "container", None)
            if container:
                route = scope.get("route")
                context = {"stage": "http", "method": scope["method"],
                           "route": getattr(route, "path", "unmatched"), "channel": "api"}
                context.update(scope.get("state", {}).get("incident_actor", {}))
                try:
                    await container.incidents.report(exc, context=context)
                except Exception:
                    pass  # Keep the original HTTP exception even if reporting fails.
            raise
