"""Dependencias FastAPI: todo sale del Container que vive en app.state (nada de singletons globales)."""

from __future__ import annotations

from fastapi import Request

from app.container import Container


def get_container(request: Request) -> Container:
    return request.app.state.container
