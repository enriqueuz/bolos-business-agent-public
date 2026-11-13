"""Almacén en memoria para desarrollo y pruebas."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field


@dataclass
class MemoryAttachmentStore:
    objects: dict[str, tuple[bytes, str]] = field(default_factory=dict)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (bytes(data), content_type)

    async def get(self, key: str) -> bytes:
        return self.objects[key][0]

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def signed_url(self, key: str, *, expires_s: int) -> str:
        del expires_s
        data, mime = self.objects[key]
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"
