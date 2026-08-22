"""OpenAI-compatible API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from aistudio_api.application.api_service import handle_chat, handle_image_edit, handle_image_generation
from aistudio_api.api.response_models import (
    ImageGenerationResponse,
    ModelCardResponse,
    ModelListResponse,
    OpenAIChatCompletionResponse,
)
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.model_catalog import (
    FALLBACK_GEMINI_MODELS,
    filter_gemini_models,
    model_metadata,
)

from .dependencies import get_client
from .schemas import ChatRequest, ImageRequest

router = APIRouter()

MODELS = [
    # Gemma 4 系列
    {"id": "gemma-4-31b-it", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemma-4-26b-a4b-it", "object": "model", "created": 1700000000, "owned_by": "google"},
    # Gemini 3 系列
    {"id": "gemini-3-flash-preview", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-3.5-flash", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-3.7-flash", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-3.1-pro-preview", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-3.1-flash-lite", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-3.1-flash-image-preview", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-3-pro-image-preview", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-3.1-flash-live-preview", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-3.1-flash-tts-preview", "object": "model", "created": 1700000000, "owned_by": "google"},
    # Gemini 2.5 系列
    # {"id": "gemini-2.5-pro", "object": "model", "created": 1700000000, "owned_by": "google"},
    # {"id": "gemini-2.5-flash", "object": "model", "created": 1700000000, "owned_by": "google"},
    # {"id": "gemini-2.5-flash-lite", "object": "model", "created": 1700000000, "owned_by": "google"},
    # {"id": "gemini-2.5-flash-image", "object": "model", "created": 1700000000, "owned_by": "google"},
    # {"id": "gemini-2.5-pro-preview-tts", "object": "model", "created": 1700000000, "owned_by": "google"},
    # {"id": "gemini-2.5-flash-preview-tts", "object": "model", "created": 1700000000, "owned_by": "google"},
    # # Gemini 2.0 系列
    # {"id": "gemini-2.0-flash-lite", "object": "model", "created": 1700000000, "owned_by": "google"},
    # # Latest 别名
    {"id": "gemini-pro-latest", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-flash-latest", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-flash-lite-latest", "object": "model", "created": 1700000000, "owned_by": "google"},
    # # Deep Research
    # {"id": "deep-research-preview-04-2026", "object": "model", "created": 1700000000, "owned_by": "google"},
    # {"id": "deep-research-max-preview-04-2026", "object": "model", "created": 1700000000, "owned_by": "google"},
    # # 图片生成 (Imagen)
    # {"id": "imagen-4.0-generate-001", "object": "model", "created": 1700000000, "owned_by": "google"},
    # {"id": "imagen-4.0-ultra-generate-001", "object": "model", "created": 1700000000, "owned_by": "google"},
    # {"id": "imagen-4.0-fast-generate-001", "object": "model", "created": 1700000000, "owned_by": "google"},
    # # 视频生成 (Veo)
    # {"id": "veo-3.1-generate-preview", "object": "model", "created": 1700000000, "owned_by": "google"},
    # {"id": "veo-3.1-fast-generate-preview", "object": "model", "created": 1700000000, "owned_by": "google"},
    # {"id": "veo-3.1-lite-generate-preview", "object": "model", "created": 1700000000, "owned_by": "google"},
    # {"id": "veo-2.0-generate-001", "object": "model", "created": 1700000000, "owned_by": "google"},
    # # 音乐生成 (Lyria)
    # {"id": "lyria-3-pro-preview", "object": "model", "created": 1700000000, "owned_by": "google"},
    # {"id": "lyria-3-clip-preview", "object": "model", "created": 1700000000, "owned_by": "google"},
    # # Robotics
    # {"id": "gemini-robotics-er-1.6-preview", "object": "model", "created": 1700000000, "owned_by": "google"},
] 
MODELS = [ModelCardResponse(**model) for model in MODELS]

MODEL_IDS = {m.id for m in MODELS}


def _model_cards(model_ids: list[str] | tuple[str, ...]) -> list[ModelCardResponse]:
    ids = filter_gemini_models(model_ids)
    return [ModelCardResponse(id=model_id, object="model", created=1700000000, owned_by="google", **model_metadata(model_id)) for model_id in ids]


async def _available_model_ids(client: AIStudioClient, force: bool = False) -> list[str]:
    try:
        discovered = await client.discover_models(force=force)
    except Exception:
        discovered = []
    return filter_gemini_models(discovered) or list(FALLBACK_GEMINI_MODELS)


@router.get("/v1/models", response_model=ModelListResponse)
async def list_models(refresh: bool = False, client: AIStudioClient = Depends(get_client)):
    return ModelListResponse(data=_model_cards(await _available_model_ids(client, force=refresh)))


@router.get("/v1/models/{model_id:path}", response_model=ModelCardResponse)
async def get_model(model_id: str, client: AIStudioClient = Depends(get_client)):
    if model_id.lower().startswith("gemini-") and model_id in await _available_model_ids(client):
        return _model_cards([model_id])[0]
    raise HTTPException(status_code=404, detail={"message": f"Model '{model_id}' not found", "type": "invalid_request_error"})


@router.post("/v1/chat/completions", response_model=OpenAIChatCompletionResponse)
async def chat_completions(req: ChatRequest, client: AIStudioClient = Depends(get_client)):
    return await handle_chat(req, client)


@router.post("/v1/images/generations", response_model=ImageGenerationResponse)
async def image_generations(req: ImageRequest, client: AIStudioClient = Depends(get_client)):
    return await handle_image_generation(req, client)


@router.post("/v1/images/edits", response_model=ImageGenerationResponse)
async def image_edits(
    prompt: str = Form(...),
    image: list[UploadFile] | None = File(None),
    mask: UploadFile | None = File(None),
    model: str = Form("gemini-3.1-flash-image-preview"),
    n: int = Form(1),
    size: str = Form("1024x1024"),
    client: AIStudioClient = Depends(get_client),
):
    return await handle_image_edit(
        prompt=prompt,
        image_files=image or [],
        mask_file=mask,
        model=model,
        n=n,
        size=size,
        client=client,
    )
