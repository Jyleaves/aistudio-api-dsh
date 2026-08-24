import asyncio

import httpx

from aistudio_api.api.state import runtime_state
from aistudio_api.application.api_service_gemini import inline_media_bytes, large_media_lock_for
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


def normalized_with_inline_data(encoded: str) -> dict:
    return {
        "contents": [
            AistudioContent(
                role="user",
                parts=[AistudioPart(inline_data=("application/pdf", encoded))],
            )
        ],
        "system_instruction": None,
    }


def test_inline_media_bytes_estimates_decoded_size_without_decoding():
    assert inline_media_bytes(normalized_with_inline_data("QUJDRA==")) == 4


def test_large_inline_media_uses_dedicated_runtime_lock(monkeypatch):
    lock = asyncio.Semaphore(1)
    original_lock = runtime_state.large_media_lock
    runtime_state.large_media_lock = lock
    monkeypatch.setattr(settings, "large_media_threshold_bytes", 4)
    try:
        assert large_media_lock_for(normalized_with_inline_data("QUJDRA==")) is lock
        assert large_media_lock_for(normalized_with_inline_data("QQ==")) is None
    finally:
        runtime_state.large_media_lock = original_lock


def test_declared_oversized_request_is_rejected_before_route_parsing(monkeypatch):
    from aistudio_api.api.app import app

    monkeypatch.setattr(settings, "max_request_bytes", 10)

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/", content=b"x", headers={"content-length": "11"})

    response = asyncio.run(request())
    assert response.status_code == 413
    assert response.json()["detail"]["type"] == "request_too_large"
