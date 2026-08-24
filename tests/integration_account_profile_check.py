"""Verify one saved account through its authoritative Chromium profile.

This probe never opens the login flow and never prints cookies. It is useful
for separating an invalid saved login from request-pool profile cloning bugs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.wire_codec import modify_body
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


def _shape(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        return f"object[{len(value)}]"
    if isinstance(value, str):
        return f"string[{len(value)}]"
    return type(value).__name__


async def inspect_wire(client: AIStudioClient, model: str) -> dict[str, Any]:
    contents = [AistudioContent(role="user", parts=[AistudioPart(text="只回复：WIRE")])]
    template_raw = await client._session.capture_template(model)
    captured = await client.capture_request(
        "只回复：WIRE",
        model=model,
        contents=contents,
    )
    if captured is None:
        raise RuntimeError("未捕获到请求")
    final_body = modify_body(
        captured.body,
        model=model,
        contents=contents,
        sanitize_plain_text=True,
    )
    template = json.loads(template_raw["body"])
    final = json.loads(final_body)
    size = max(len(template), len(final))
    changed = [
        index
        for index in range(size)
        if (template[index] if index < len(template) else object())
        != (final[index] if index < len(final) else object())
    ]
    generation_before = template[3] if len(template) > 3 and isinstance(template[3], list) else []
    generation_after = final[3] if len(final) > 3 and isinstance(final[3], list) else []
    generation_size = max(len(generation_before), len(generation_after))
    generation_changed = [
        index
        for index in range(generation_size)
        if (generation_before[index] if index < len(generation_before) else object())
        != (generation_after[index] if index < len(generation_after) else object())
    ]
    return {
        "template_root_length": len(template),
        "final_root_length": len(final),
        "changed_root_indexes": changed,
        "template_root_shapes": [_shape(value) for value in template],
        "final_root_shapes": [_shape(value) for value in final],
        "generation_changed_indexes": generation_changed,
        "template_response_mime_type": generation_before[7] if len(generation_before) > 7 else None,
        "final_response_mime_type": generation_after[7] if len(generation_after) > 7 else None,
        "template_thinking_config": generation_before[16] if len(generation_before) > 16 else None,
        "final_thinking_config": generation_after[16] if len(generation_after) > 16 else None,
        "template_model": template[0] if template else None,
        "final_model": final[0] if final else None,
        "url_path": template_raw["url"].split("?", 1)[0],
    }


async def probe_rewrite_variants(
    client: AIStudioClient,
    model: str,
    variant_only: str | None = None,
) -> dict[str, Any]:
    contents = [AistudioContent(role="user", parts=[AistudioPart(text="只回复：VARIANT")])]
    template_raw = await client._session.capture_template(model)
    template = json.loads(template_raw["body"])

    async def send_variant(name: str, *, mime: bool, thinking: bool) -> dict[str, Any]:
        body = json.loads(json.dumps(template))
        snapshot = await client._session.generate_snapshot(contents)
        body[1] = [content.to_wire() for content in contents]
        body[4] = snapshot
        generation = body[3]
        if mime:
            while len(generation) <= 7:
                generation.append(None)
            generation[7] = "text/plain"
        if thinking:
            while len(generation) <= 16:
                generation.append(None)
            generation[16] = [1, None, None, 3]
        started = time.perf_counter()
        status, raw = await client._session.send_hooked_request(
            body=json.dumps(body, separators=(",", ":"), ensure_ascii=False),
            timeout_ms=120000,
        )
        result: dict[str, Any] = {
            "name": name,
            "status": status,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
        if status != 200:
            result["error"] = raw.decode("utf-8", errors="replace")[:160]
        return result

    definitions = [
        ("content_snapshot_only", False, False),
        ("plus_mime", True, False),
        ("plus_thinking", False, True),
        ("plus_mime_and_thinking", True, True),
    ]
    if variant_only:
        definitions = [item for item in definitions if item[0] == variant_only]
        if not definitions:
            raise ValueError(f"未知请求变体: {variant_only}")
    variants = [
        await send_variant(name, mime=mime, thinking=thinking)
        for name, mime, thinking in definitions
    ]
    return {"variants": variants}


async def main(
    account_id: str | None,
    model: str,
    inspect: bool,
    variants: bool,
    variant_only: str | None,
) -> int:
    store = AccountStore()
    account = store.get_account(account_id) if account_id else store.get_active_account()
    if account is None:
        raise RuntimeError("没有可验证的账号")
    auth_path = store.get_auth_path_optional(account.id, require_exists=True)
    profile_path = store.get_profile_path(account.id)
    if auth_path is None or profile_path is None:
        raise RuntimeError(f"账号 {account.id} 缺少认证文件或 profile")

    client = AIStudioClient(
        auth_file=str(auth_path),
        profile_dir=str(profile_path),
        account_id=account.id,
    )
    started = time.perf_counter()
    warmup_elapsed = 0.0
    try:
        await client.warmup()
        warmup_elapsed = time.perf_counter() - started
        if inspect:
            result = await inspect_wire(client, model)
            result.update({
                "ok": True,
                "account_id": account.id,
                "warmup_seconds": round(warmup_elapsed, 3),
                "total_seconds": round(time.perf_counter() - started, 3),
            })
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if variants:
            result = await probe_rewrite_variants(client, model, variant_only)
            result.update({
                "ok": all(item["status"] == 200 for item in result["variants"]),
                "account_id": account.id,
                "warmup_seconds": round(warmup_elapsed, 3),
                "total_seconds": round(time.perf_counter() - started, 3),
            })
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["ok"] else 1
        await client.warmup()
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "account_id": account.id,
            "warmup_seconds": round(warmup_elapsed, 3),
            "total_seconds": round(time.perf_counter() - started, 3),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }, ensure_ascii=False))
        return 1
    finally:
        await client.close()

    print(json.dumps({
        "ok": True,
        "account_id": account.id,
        "warmup_seconds": round(warmup_elapsed, 3),
        "total_seconds": round(time.perf_counter() - started, 3),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id")
    parser.add_argument("--model", default="gemini-3.7-flash")
    parser.add_argument("--inspect-wire", action="store_true")
    parser.add_argument("--probe-variants", action="store_true")
    parser.add_argument("--variant-only")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(
        args.account_id,
        args.model,
        args.inspect_wire,
        args.probe_variants,
        args.variant_only,
    )))
