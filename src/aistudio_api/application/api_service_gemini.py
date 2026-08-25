"""Gemini-compatible application service handlers."""

from __future__ import annotations

import json

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from aistudio_api.api.response_models import GeminiCandidateResponse, GeminiContentResponse, GeminiGenerateContentResponse
from aistudio_api.api.responses import to_gemini_parts, to_gemini_usage_metadata
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.api.state import runtime_state
from aistudio_api.application.api_service_common import (
    MAX_RETRIES,
    acquire_request_client,
    capture_retry_reason,
    logger,
    mark_request_worker_unhealthy,
    record_rotator_event,
)
from aistudio_api.application.chat_service import cleanup_files, normalize_gemini_request
from aistudio_api.domain.errors import AistudioError, AuthError, RequestError, UsageLimitExceeded
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.config import settings


def inline_media_bytes(normalized: dict) -> int:
    """Estimate decoded inline-media bytes without allocating decoded buffers."""
    total = 0
    contents = list(normalized.get("contents") or [])
    system_instruction = normalized.get("system_instruction")
    if system_instruction is not None:
        contents.append(system_instruction)
    for content in contents:
        for part in getattr(content, "parts", ()):
            inline_data = getattr(part, "inline_data", None)
            if not inline_data:
                continue
            _mime_type, data = inline_data
            if isinstance(data, str):
                padding = 2 if data.endswith("==") else 1 if data.endswith("=") else 0
                total += len(data) * 3 // 4 - padding
    return total


def large_media_lock_for(normalized: dict):
    if inline_media_bytes(normalized) < settings.large_media_threshold_bytes:
        return None
    return runtime_state.large_media_lock


async def handle_gemini_generate_content(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient,
    *,
    stream: bool,
):
    last_error = None
    attempted_accounts: set[str] = set()
    try:
        normalized = normalize_gemini_request(req, model_path, tmp_dir=settings.tmp_dir)
    except ValueError as exc:
        raise HTTPException(400, detail={"message": str(exc), "type": "bad_request"}) from exc

    if stream:
        return _build_gemini_streaming_response(client=client, normalized=normalized)

    large_media_lock = large_media_lock_for(normalized)
    large_media_lock_acquired = False
    try:
        if large_media_lock is not None:
            await large_media_lock.acquire()
            large_media_lock_acquired = True
            logger.info("Gemini 大媒体请求进入专用队列，inline_bytes=%d", inline_media_bytes(normalized))
        for attempt in range(MAX_RETRIES):
            async with acquire_request_client(
                client,
                attempt=attempt,
                exclude_account_ids=attempted_accounts,
            ) as execution:
                request_client = execution.client
                account_id = execution.account_id
                if account_id:
                    attempted_accounts.add(account_id)
                try:
                    logger.info(
                        "Gemini: model=%s, contents=%s, stream=%s, attempt=%d, worker=%s",
                        normalized["model"],
                        len(req.contents),
                        stream,
                        attempt + 1,
                        execution.worker_id,
                    )
                    output = await request_client.generate_content(
                        model=normalized["model"],
                        capture_prompt=normalized["capture_prompt"],
                        capture_images=normalized["capture_images"],
                        contents=normalized["contents"],
                        system_instruction_content=normalized["system_instruction"],
                        tools=normalized["tools"],
                        safety_settings=normalized["safety_settings"],
                        temperature=normalized["temperature"],
                        top_p=normalized["top_p"],
                        top_k=normalized["top_k"],
                        max_tokens=normalized["max_tokens"],
                        generation_config_overrides=normalized["generation_config_overrides"],
                        sanitize_plain_text=False,
                    )

                    execution.succeeded = True
                    record_rotator_event("success", account_id)
                    runtime_state.record(normalized["model"], "success", output.usage)
                    return GeminiGenerateContentResponse(
                        candidates=[
                            GeminiCandidateResponse(
                                content=GeminiContentResponse(
                                    parts=to_gemini_parts(
                                        output.text,
                                        function_calls=output.function_calls,
                                        function_responses=output.function_responses,
                                        thinking=output.thinking,
                                        images=output.images,
                                        reasoning_images=output.reasoning_images,
                                    ),
                                ),
                                finishReason="STOP" if not output.function_calls else "FUNCTION_CALL",
                                groundingMetadata=output.grounding_metadata or None,
                                urlContextMetadata=output.url_context_metadata or None,
                            )
                        ],
                        usageMetadata=to_gemini_usage_metadata(output.usage),
                    )
                except UsageLimitExceeded as exc:
                    runtime_state.record(model_path, "rate_limited")
                    last_error = exc
                    record_rotator_event("rate_limited", account_id)
                    if attempt < MAX_RETRIES - 1:
                        logger.info("Gemini 429 限流，改用其他可用账号重试 %d/%d", attempt + 1, MAX_RETRIES)
                        continue
                    raise HTTPException(429, detail={"message": str(exc), "type": "rate_limit_exceeded"}) from exc
                except AuthError as exc:
                    last_error = exc
                    mark_request_worker_unhealthy(execution.worker_id)
                    if attempt < MAX_RETRIES - 1:
                        logger.warning("Gemini 工作浏览器鉴权失败，重建后重试 %d/%d", attempt + 1, MAX_RETRIES)
                        continue
                    runtime_state.record(model_path, "errors")
                    record_rotator_event("error", account_id)
                    raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
                except AistudioError as exc:
                    runtime_state.record(model_path, "errors")
                    record_rotator_event("error", account_id)
                    raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
                except Exception as exc:
                    runtime_state.record(model_path, "errors")
                    record_rotator_event("error", account_id)
                    logger.error("Gemini error: %s", exc, exc_info=True)
                    raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
        raise HTTPException(429, detail={"message": str(last_error), "type": "rate_limit_exceeded"}) from last_error
    finally:
        if large_media_lock_acquired:
            large_media_lock.release()
        cleanup_files(normalized["cleanup_paths"])


def _gemini_stream_payload(event_type: str, value) -> str | None:
    parts = None
    if event_type == "body" and value:
        parts = [{"text": value}]
    elif event_type == "images" and value:
        parts = [part.model_dump(mode="json", exclude_none=True) for part in to_gemini_parts("", images=value)]
    elif event_type == "reasoning_images" and value:
        parts = [
            part.model_dump(mode="json", exclude_none=True)
            for part in to_gemini_parts("", reasoning_images=value)
        ]
    elif event_type == "tool_calls" and value:
        parts = [
            part.model_dump(mode="json", exclude_none=True)
            for part in to_gemini_parts("", function_calls=value)
        ]
    elif event_type == "thinking" and value:
        parts = [{"text": value, "thought": True}]
    elif event_type == "grounding_metadata" and value:
        return "data: " + json.dumps(
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": []},
                        "finishReason": None,
                        "groundingMetadata": value,
                    }
                ]
            },
            ensure_ascii=False,
        ) + "\n\n"
    elif event_type == "url_context_metadata" and value:
        return "data: " + json.dumps(
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": []},
                        "finishReason": None,
                        "urlContextMetadata": value,
                    }
                ]
            },
            ensure_ascii=False,
        ) + "\n\n"
    if parts is None:
        return None
    payload = {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "finishReason": None,
            }
        ]
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _build_gemini_streaming_response(*, client: AIStudioClient, normalized: dict) -> StreamingResponse:
    async def stream_response():
        final_usage = None
        account_id = None
        attempted_accounts: set[str] = set()
        large_media_lock = large_media_lock_for(normalized)
        large_media_lock_acquired = False
        try:
            if large_media_lock is not None:
                await large_media_lock.acquire()
                large_media_lock_acquired = True
                logger.info("Gemini 大媒体流请求进入专用队列，inline_bytes=%d", inline_media_bytes(normalized))
            for stream_attempt in range(MAX_RETRIES):
                async with acquire_request_client(
                    client,
                    attempt=stream_attempt,
                    exclude_account_ids=attempted_accounts,
                ) as execution:
                    stream_client = execution.client
                    account_id = execution.account_id
                    try:
                        has_yielded_data = False
                        async for event_type, value in stream_client.stream_generate_content(
                            model=normalized["model"],
                            capture_prompt=normalized["capture_prompt"],
                            capture_images=normalized["capture_images"],
                            contents=normalized["contents"],
                            system_instruction_content=normalized["system_instruction"],
                            tools=normalized["tools"],
                            safety_settings=normalized["safety_settings"],
                            temperature=normalized["temperature"],
                            top_p=normalized["top_p"],
                            top_k=normalized["top_k"],
                            max_tokens=normalized["max_tokens"],
                            generation_config_overrides=normalized["generation_config_overrides"],
                            sanitize_plain_text=False,
                            force_refresh_capture=stream_attempt > 0,
                        ):
                            if event_type == "usage":
                                final_usage = value if isinstance(value, dict) else None
                                continue
                            payload = _gemini_stream_payload(event_type, value)
                            if payload is not None:
                                has_yielded_data = True
                                yield payload
                        break
                    except UsageLimitExceeded:
                        runtime_state.record(normalized["model"], "rate_limited")
                        record_rotator_event("rate_limited", account_id)
                        if account_id:
                            attempted_accounts.add(account_id)
                        if not has_yielded_data and stream_attempt < MAX_RETRIES - 1:
                            logger.warning(
                                "Gemini stream 429 限流，改用其他可用账号重试 %d/%d",
                                stream_attempt + 1,
                                MAX_RETRIES,
                            )
                            continue
                        raise
                    except RequestError as exc:
                        retry_reason = capture_retry_reason(
                            exc,
                            attempt=stream_attempt,
                            has_yielded_data=has_yielded_data,
                        )
                        if retry_reason is not None:
                            logger.warning(
                                "Gemini stream 捕获模板失效（%s），清理缓存后重试 %d/%d",
                                retry_reason,
                                stream_attempt + 1,
                                MAX_RETRIES,
                            )
                            stream_client.clear_snapshot_cache()
                            if retry_reason == "ambiguous_service" and account_id:
                                attempted_accounts.add(account_id)
                            continue
                        raise
                    except AuthError as exc:
                        mark_request_worker_unhealthy(execution.worker_id)
                        if not has_yielded_data and stream_attempt < MAX_RETRIES - 1:
                            logger.warning("Gemini stream 工作浏览器鉴权异常，重建后重试 %d/%d: %s", stream_attempt + 1, MAX_RETRIES, exc)
                            stream_client.clear_snapshot_cache()
                            continue
                        raise

            execution.succeeded = True
            record_rotator_event("success", account_id)
            runtime_state.record(normalized["model"], "success", final_usage)
            if final_usage:
                yield "data: " + json.dumps(
                    {
                        "candidates": [],
                        "usageMetadata": to_gemini_usage_metadata(final_usage).model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                ) + "\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            logger.error("Gemini stream error: %s", exc, exc_info=True)
            if not isinstance(exc, UsageLimitExceeded):
                record_rotator_event("error", account_id)
            runtime_state.record(normalized["model"], "errors")
            yield "data: " + json.dumps({"error": {"message": str(exc)}}, ensure_ascii=False) + "\n\n"
        finally:
            if large_media_lock_acquired:
                large_media_lock.release()
            cleanup_files(normalized["cleanup_paths"])

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
