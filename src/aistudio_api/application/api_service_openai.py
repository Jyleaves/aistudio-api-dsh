"""OpenAI-compatible application service handlers."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from aistudio_api.api.responses import (
    chat_completion_response,
    new_chat_id,
    sse_chunk,
    sse_error,
    sse_usage_chunk,
    to_openai_tool_calls,
)
from aistudio_api.api.schemas import ChatRequest, ImageRequest
from aistudio_api.api.state import runtime_state
from aistudio_api.application.api_service_common import (
    MAX_RETRIES,
    acquire_request_client,
    build_inline_image_parts,
    capture_retry_reason,
    image_response,
    logger,
    mark_request_worker_unhealthy,
    record_rotator_event,
    validate_image_request_options,
)
from aistudio_api.application.chat_service import (
    cleanup_files,
    normalize_chat_request,
    normalize_openai_reasoning_effort,
    normalize_openai_tools,
)
from aistudio_api.domain.errors import AistudioError, AuthError, RequestError, UsageLimitExceeded
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.model_defaults import resolve_model_defaults
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


async def handle_chat(req: ChatRequest, client: AIStudioClient):
    last_error = None
    attempted_accounts: set[str] = set()
    normalized = normalize_chat_request(req.messages, req.model, tmp_dir=settings.tmp_dir)
    model = normalized["model"]
    tmp_files = list(normalized["cleanup_paths"])
    tools = None if req.tools is None else (normalize_openai_tools(req.tools) or [])
    thinking_config = normalize_openai_reasoning_effort(req.reasoning_effort)
    generation_config_overrides = (
        {"thinking_config": thinking_config} if thinking_config is not None else None
    )

    # Google AI Studio requires an additional wire-level tool_config flag when
    # built-in tools are combined with client-side function declarations.
    if req.tools is None:
        from aistudio_api.infrastructure.gateway.request_rewriter import build_tools_from_names

        model_defaults = resolve_model_defaults(model)
        if model_defaults.default_tools:
            tools = build_tools_from_names(
                model_defaults.default_tools,
                model=model,
                is_image_model=model_defaults.is_image_model,
            )

    if req.stream:
        include_usage = True
        if req.stream_options is not None:
            include_usage = req.stream_options.include_usage
        return _build_streaming_response(
            client=client,
            capture_prompt=normalized["capture_prompt"],
            model=model,
            capture_images=normalized["capture_images"] if normalized["capture_images"] else None,
            contents=normalized["contents"],
            system_instruction=normalized["system_instruction"],
            cleanup_paths=tmp_files,
            include_usage=include_usage,
            temperature=req.temperature,
            top_p=req.top_p,
            top_k=req.top_k,
            max_tokens=req.max_tokens,
            generation_config_overrides=generation_config_overrides,
            tools=tools,
        )

    try:
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
                        "Chat: model=%s, contents=%s, capture_prompt=%s..., images=%s, stream=%s, attempt=%d, worker=%s",
                        model,
                        len(normalized["contents"]),
                        normalized["capture_prompt"][:50],
                        len(normalized["capture_images"]),
                        req.stream,
                        attempt + 1,
                        execution.worker_id,
                    )
                    generation_kwargs = dict(
                        model=model,
                        capture_prompt=normalized["capture_prompt"],
                        capture_images=normalized["capture_images"] if normalized["capture_images"] else None,
                        contents=normalized["contents"],
                        system_instruction_content=(
                            AistudioContent(role="user", parts=[AistudioPart(text=normalized["system_instruction"])])
                            if normalized["system_instruction"]
                            else None
                        ),
                        temperature=req.temperature,
                        top_p=req.top_p,
                        top_k=req.top_k,
                        max_tokens=req.max_tokens,
                        generation_config_overrides=generation_config_overrides,
                        tools=tools,
                        sanitize_plain_text=True,
                    )
                    output = await request_client.generate_content(**generation_kwargs)
                    if (
                        not output.text
                        and not output.function_calls
                        and output.thinking
                        and (req.max_tokens or 0) < 512
                    ):
                        logger.info(
                            "OpenAI-compatible response used small token budget on hidden reasoning; "
                            "retrying with visible-output reserve"
                        )
                        output = await request_client.generate_content(
                            **{**generation_kwargs, "max_tokens": 512}
                        )

                    execution.succeeded = True
                    record_rotator_event("success", account_id)
                    runtime_state.record(model, "success", output.usage)
                    return chat_completion_response(
                        model=model,
                        content=output.text,
                        thinking=output.thinking,
                        usage=output.usage,
                        function_calls=output.function_calls,
                    )
                except UsageLimitExceeded as exc:
                    runtime_state.record(model, "rate_limited")
                    last_error = exc
                    record_rotator_event("rate_limited", account_id)
                    if attempt < MAX_RETRIES - 1:
                        logger.info("429 限流，改用其他可用账号重试 %d/%d", attempt + 1, MAX_RETRIES)
                        continue
                    raise HTTPException(429, detail={"message": str(exc), "type": "rate_limit_exceeded"}) from exc
                except AuthError as exc:
                    last_error = exc
                    mark_request_worker_unhealthy(execution.worker_id)
                    if attempt < MAX_RETRIES - 1:
                        logger.warning("工作浏览器鉴权失败，重建后重试 %d/%d", attempt + 1, MAX_RETRIES)
                        continue
                    runtime_state.record(model, "errors")
                    record_rotator_event("error", account_id)
                    raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
                except AistudioError as exc:
                    runtime_state.record(model, "errors")
                    record_rotator_event("error", account_id)
                    raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
                except Exception as exc:
                    runtime_state.record(model, "errors")
                    record_rotator_event("error", account_id)
                    logger.error("Chat error: %s", exc, exc_info=True)
                    raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
        raise HTTPException(429, detail={"message": str(last_error), "type": "rate_limit_exceeded"}) from last_error
    finally:
        cleanup_files(tmp_files)


async def handle_image_generation(req: ImageRequest, client: AIStudioClient):
    validate_image_request_options(size=req.size, n=req.n)

    last_error = None
    attempted_accounts: set[str] = set()

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
                logger.info("Image: model=%s, prompt=%s..., attempt=%d", req.model, req.prompt[:50], attempt + 1)
                output = await request_client.generate_image(
                    prompt=req.prompt,
                    model=req.model,
                    size=req.size,
                    google_search=req.google_search,
                    image_search=req.image_search,
                    use_default_tools=not bool({"google_search", "image_search"} & req.model_fields_set),
                )
                execution.succeeded = True
                record_rotator_event("success", account_id)
                runtime_state.record(req.model, "success", output.usage)
                return image_response(output)
            except UsageLimitExceeded as exc:
                runtime_state.record(req.model, "rate_limited")
                last_error = exc

                record_rotator_event("rate_limited", account_id)
                if attempt < MAX_RETRIES - 1:
                    logger.info("Image 429 限流，改用其他可用账号重试 %d/%d", attempt + 1, MAX_RETRIES)
                    continue
                raise HTTPException(429, detail={"message": str(exc), "type": "rate_limit_exceeded"}) from exc
            except AuthError as exc:
                last_error = exc
                mark_request_worker_unhealthy(execution.worker_id)
                if attempt < MAX_RETRIES - 1:
                    logger.warning("图片工作浏览器鉴权失败，重建后重试 %d/%d", attempt + 1, MAX_RETRIES)
                    continue
                runtime_state.record(req.model, "errors")
                record_rotator_event("error", account_id)
                raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
            except AistudioError as exc:
                runtime_state.record(req.model, "errors")
                record_rotator_event("error", account_id)
                raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
            except Exception as exc:
                runtime_state.record(req.model, "errors")
                record_rotator_event("error", account_id)
                logger.error("Image error: %s", exc, exc_info=True)
                raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc

    raise HTTPException(429, detail={"message": str(last_error), "type": "rate_limit_exceeded"}) from last_error


async def handle_image_edit(
    prompt: str,
    image_files: list,
    mask_file,
    model: str,
    n: int,
    size: str,
    client: AIStudioClient,
):
    validate_image_request_options(size=size, n=n)

    image_parts = await build_inline_image_parts(image_files)
    request_contents = [AistudioContent(role="user", parts=[*image_parts, AistudioPart(text=prompt)])]

    last_error = None
    attempted_accounts: set[str] = set()

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
                    "Image Edit: model=%s, prompt=%s..., images=%d, attempt=%d",
                    model,
                    prompt[:50],
                    len(image_parts),
                    attempt + 1,
                )
                output = await request_client.generate_image(
                    prompt=prompt,
                    model=model,
                    size=size,
                    contents=request_contents,
                )
                execution.succeeded = True
                record_rotator_event("success", account_id)
                runtime_state.record(model, "success", output.usage)
                return image_response(output)
            except UsageLimitExceeded as exc:
                runtime_state.record(model, "rate_limited")
                last_error = exc

                record_rotator_event("rate_limited", account_id)
                if attempt < MAX_RETRIES - 1:
                    logger.info("Image Edit 429 限流，改用其他可用账号重试 %d/%d", attempt + 1, MAX_RETRIES)
                    continue
                raise HTTPException(429, detail={"message": str(exc), "type": "rate_limit_exceeded"}) from exc
            except AuthError as exc:
                last_error = exc
                mark_request_worker_unhealthy(execution.worker_id)
                if attempt < MAX_RETRIES - 1:
                    logger.warning("图片编辑工作浏览器鉴权失败，重建后重试 %d/%d", attempt + 1, MAX_RETRIES)
                    continue
                runtime_state.record(model, "errors")
                record_rotator_event("error", account_id)
                raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
            except AistudioError as exc:
                runtime_state.record(model, "errors")
                record_rotator_event("error", account_id)
                raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
            except Exception as exc:
                runtime_state.record(model, "errors")
                record_rotator_event("error", account_id)
                logger.error("Image Edit error: %s", exc, exc_info=True)
                raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc

    raise HTTPException(429, detail={"message": str(last_error), "type": "rate_limit_exceeded"}) from last_error


def _build_streaming_response(
    *,
    client: AIStudioClient,
    capture_prompt: str,
    model: str,
    capture_images: list[str] | None,
    contents: list[AistudioContent],
    system_instruction: str | None,
    cleanup_paths: list[str],
    include_usage: bool = False,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    max_tokens: int | None = None,
    generation_config_overrides: dict | None = None,
    tools: list[list] | None = None,
) -> StreamingResponse:
    async def stream_response():
        account_id = None
        attempted_accounts: set[str] = set()
        try:
            chat_id = new_chat_id()
            final_usage = None
            saw_tool_calls = False
            next_tool_call_index = 0
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
                        async for event_type, text in stream_client.stream_generate_content(
                            model=model,
                            capture_prompt=capture_prompt,
                            capture_images=capture_images,
                            contents=contents,
                            system_instruction_content=(
                                AistudioContent(role="user", parts=[AistudioPart(text=system_instruction)])
                                if system_instruction
                                else None
                            ),
                            temperature=temperature,
                            top_p=top_p,
                            top_k=top_k,
                            max_tokens=max_tokens,
                            generation_config_overrides=generation_config_overrides,
                            tools=tools,
                            force_refresh_capture=stream_attempt > 0,
                        ):
                            if event_type == "body" and text:
                                has_yielded_data = True
                                yield sse_chunk(chat_id, model, text, include_usage=include_usage)
                            elif event_type == "thinking" and text:
                                has_yielded_data = True
                                yield sse_chunk(chat_id, model, "", thinking=text, include_usage=include_usage)
                            elif event_type == "tool_calls" and text:
                                has_yielded_data = True
                                saw_tool_calls = True
                                openai_tool_calls = to_openai_tool_calls(
                                    text if isinstance(text, list) else [],
                                    start_index=next_tool_call_index,
                                )
                                next_tool_call_index += len(openai_tool_calls)
                                yield sse_chunk(
                                    chat_id,
                                    model,
                                    "",
                                    tool_calls=openai_tool_calls,
                                    include_usage=include_usage,
                                )
                            elif event_type == "usage":
                                final_usage = text if isinstance(text, dict) else None
                        # The pool observes this flag while __aexit__ returns
                        # the worker. Mark success before leaving the lease so
                        # streamed accounts become verified and reusable.
                        execution.succeeded = True
                        break
                    except UsageLimitExceeded:
                        runtime_state.record(model, "rate_limited")
                        record_rotator_event("rate_limited", account_id)
                        if account_id:
                            attempted_accounts.add(account_id)
                        if not has_yielded_data and stream_attempt < MAX_RETRIES - 1:
                            logger.warning("Stream 429 限流，改用其他可用账号重试 %d/%d", stream_attempt + 1, MAX_RETRIES)
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
                                "Stream 请求可恢复失败（%s），清理缓存后重试 %d/%d",
                                retry_reason,
                                stream_attempt + 1,
                                MAX_RETRIES,
                            )
                            stream_client.clear_snapshot_cache()
                            if retry_reason == "transport":
                                mark_request_worker_unhealthy(execution.worker_id)
                            if retry_reason in {"ambiguous_service", "transport"} and account_id:
                                attempted_accounts.add(account_id)
                            continue
                        raise
                    except AuthError as exc:
                        mark_request_worker_unhealthy(execution.worker_id)
                        if not has_yielded_data and stream_attempt < MAX_RETRIES - 1:
                            logger.warning("Stream 工作浏览器鉴权异常，重建后重试 %d/%d: %s", stream_attempt + 1, MAX_RETRIES, exc)
                            stream_client.clear_snapshot_cache()
                            continue
                        raise

            record_rotator_event("success", account_id)
            runtime_state.record(model, "success", final_usage)
            yield sse_chunk(chat_id, model, "", finish="tool_calls" if saw_tool_calls else "stop", include_usage=include_usage)
            if include_usage:
                yield sse_usage_chunk(chat_id, model, final_usage)
            yield "data: [DONE]\n\n"
        except Exception as exc:
            logger.error("Stream error: %s", exc, exc_info=True)
            if not isinstance(exc, UsageLimitExceeded):
                record_rotator_event("error", account_id)
            runtime_state.record(model, "errors")
            yield sse_error(str(exc))
        finally:
            cleanup_files(cleanup_paths)

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
