from __future__ import annotations

import io

import pytest
from PIL import Image

from app.application.attachments import MAX_ATTACHMENT_BYTES, prepare_image
from app.domain.errors import ValidationFailed
from app.infra.storage.memory import MemoryAttachmentStore
from app.infra.storage.s3 import S3AttachmentStore


def image_bytes(fmt: str = "PNG", *, size: tuple[int, int] = (900, 600)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, fmt)
    return out.getvalue()


@pytest.mark.parametrize(("fmt", "mime"), [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")])
def test_prepare_image_validates_content_and_builds_small_metadata_free_webp(fmt: str, mime: str) -> None:
    original = image_bytes(fmt)

    prepared = prepare_image(original)

    assert prepared.original == original
    assert prepared.mime_type == mime
    assert len(prepared.sha256) == 64
    with Image.open(io.BytesIO(prepared.thumbnail)) as thumbnail:
        assert thumbnail.format == "WEBP"
        assert max(thumbnail.size) == 480
        assert not thumbnail.getexif()


def test_prepare_image_rejects_invalid_animated_and_oversized_content() -> None:
    with pytest.raises(ValidationFailed):
        prepare_image(b"not an image")
    with pytest.raises(ValidationFailed):
        prepare_image(b"x" * (MAX_ATTACHMENT_BYTES + 1))
    frames = [Image.new("RGB", (10, 10), color) for color in ("red", "blue")]
    animated = io.BytesIO()
    frames[0].save(animated, "WEBP", save_all=True, append_images=frames[1:], duration=20)
    with pytest.raises(ValidationFailed, match="animados"):
        prepare_image(animated.getvalue())


@pytest.mark.asyncio
async def test_memory_store_is_idempotent_and_returns_a_readable_url() -> None:
    store = MemoryAttachmentStore()
    await store.put("same", b"first", "image/png")
    await store.put("same", b"second", "image/png")

    assert await store.get("same") == b"second"
    assert (await store.signed_url("same", expires_s=900)).startswith("data:image/png;base64,")
    await store.delete("same")
    assert "same" not in store.objects


@pytest.mark.asyncio
async def test_s3_signed_url_uses_requested_expiration_without_exposing_the_key_elsewhere() -> None:
    calls = []

    class Client:
        def generate_presigned_url(self, operation, *, Params, ExpiresIn):
            calls.append((operation, Params, ExpiresIn))
            return "https://signed.example/object"

    store = object.__new__(S3AttachmentStore)
    store._bucket = "private"  # noqa: SLF001
    store._client = Client()  # noqa: SLF001

    assert await store.signed_url("business/hash/original.png", expires_s=900) == "https://signed.example/object"
    assert calls == [("get_object", {"Bucket": "private", "Key": "business/hash/original.png"}, 900)]
