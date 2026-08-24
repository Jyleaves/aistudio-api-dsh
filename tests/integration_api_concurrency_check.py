r"""Live signed-in API concurrency check for a running Asteria instance.

This test never performs Google login. Start Asteria with an already signed-in
account, then run:

    .venv\Scripts\python.exe tests\integration_api_concurrency_check.py
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class Result:
    name: str
    status: int
    elapsed: float
    text: str
    raw: str


def _response_text(kind: str, payload: Any) -> str:
    try:
        if kind == "openai":
            return str(payload["choices"][0]["message"]["content"] or "")
        if kind == "gemini":
            return str(payload["candidates"][0]["content"]["parts"][0]["text"] or "")
        if kind == "anthropic":
            return str(payload["content"][0]["text"] or "")
    except (KeyError, IndexError, TypeError):
        return ""
    return ""


async def _send(
    client: httpx.AsyncClient,
    *,
    name: str,
    kind: str,
    model: str,
) -> Result:
    prompt = f"只回复：{name}"
    if kind == "openai":
        path = "/v1/chat/completions"
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
    elif kind == "gemini":
        path = f"/v1beta/models/{model}:generateContent"
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    elif kind == "anthropic":
        path = "/v1/messages"
        body = {
            "model": model,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": prompt}],
        }
    else:
        raise ValueError(f"unsupported kind: {kind}")

    started = time.perf_counter()
    response = await client.post(path, json=body)
    elapsed = time.perf_counter() - started
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    text = _response_text(kind, payload)
    if response.status_code != 200:
        text = response.text[:300]
    return Result(
        name=name,
        status=response.status_code,
        elapsed=elapsed,
        text=text,
        raw=response.text[:500],
    )


async def _monitor_pool(
    client: httpx.AsyncClient,
    stop: asyncio.Event,
) -> dict[str, int]:
    peak_active = 0
    peak_workers = 0
    peak_verified_workers = 0
    while not stop.is_set():
        response = await client.get("/runtime/request-pool")
        response.raise_for_status()
        status = response.json()
        peak_active = max(peak_active, int(status.get("active", 0)))
        peak_workers = max(peak_workers, int(status.get("workers", 0)))
        peak_verified_workers = max(
            peak_verified_workers,
            int(status.get("verified_workers", 0)),
        )
        await asyncio.sleep(0.2)
    return {
        "active": peak_active,
        "workers": peak_workers,
        "verified_workers": peak_verified_workers,
    }


async def _stage(
    client: httpx.AsyncClient,
    *,
    label: str,
    requests: list[tuple[str, str]],
    model: str,
) -> tuple[list[Result], dict[str, int], float]:
    stop = asyncio.Event()
    monitor = asyncio.create_task(_monitor_pool(client, stop))
    started = time.perf_counter()
    try:
        results = await asyncio.gather(*(
            _send(client, name=name, kind=kind, model=model)
            for name, kind in requests
        ))
    finally:
        wall = time.perf_counter() - started
        stop.set()
    peak = await monitor
    print(
        f"[{label}] wall={wall:.2f}s peak_active={peak['active']} "
        f"peak_workers={peak['workers']} "
        f"peak_verified_workers={peak['verified_workers']}"
    )
    for result in results:
        print(
            f"  {result.name}: status={result.status} elapsed={result.elapsed:.2f}s "
            f"reply={result.text[:80]!r}"
        )
        if not result.text.strip():
            print(f"    raw={result.raw!r}")
    return results, peak, wall


async def main(base_url: str, model: str, stage_only: str | None = None) -> int:
    timeout = httpx.Timeout(180.0, connect=10.0)
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        auth = await client.get("/auth/check")
        auth.raise_for_status()
        if not auth.json().get("local_session"):
            raise RuntimeError("local UI session was not granted")

        health = (await client.get("/health")).json()
        if not health.get("ready"):
            raise RuntimeError(f"Asteria is not ready: {health}")

        accounts = (await client.get("/accounts")).json()
        if not isinstance(accounts, list) or not accounts:
            raise RuntimeError("no signed-in account is available")
        initial_pool = (await client.get("/runtime/request-pool")).json()
        max_concurrency = int(initial_pool.get("max_concurrency", 1))
        ready_accounts = list(initial_pool.get("ready_accounts") or [])
        if not ready_accounts:
            raise RuntimeError(f"no healthy request account is ready: {initial_pool}")
        print(
            f"[capacity] signed_in={len(accounts)} ready={len(ready_accounts)} "
            f"max_concurrency={max_concurrency} "
            "per_account=1"
        )

        stages = [
            ("warmup", [("warmup", "openai")]),
            ("same-api-3", [(f"openai-{index}", "openai") for index in range(1, 4)]),
            ("multi-api-3", [
                ("multi-openai", "openai"),
                ("multi-gemini", "gemini"),
                ("multi-anthropic", "anthropic"),
            ]),
        ]
        if stage_only:
            stages = [stage for stage in stages if stage[0] == stage_only]
            if not stages:
                raise ValueError(f"unknown stage: {stage_only}")

        failures: list[str] = []
        required_workers = min(
            len(ready_accounts),
            max_concurrency,
            max(len(requests) for _label, requests in stages),
        )
        observed_peak = 0
        for stage_index, (label, requests) in enumerate(stages):
            if stage_index > 0:
                # Keep the live test below typical per-account request/minute
                # limits. Queue overflow behavior is covered with fake clients
                # in the unit suite and does not need extra Google requests.
                await asyncio.sleep(10)
            results, peak, _wall = await _stage(
                client,
                label=label,
                requests=requests,
                model=model,
            )
            failures.extend(
                result.name
                for result in results
                if result.status != 200 or not result.text.strip()
            )
            observed_peak = max(observed_peak, peak["active"])

        final_pool = (await client.get("/runtime/request-pool")).json()
        print(f"[final-pool] {final_pool}")
        if int(final_pool.get("active", -1)) != 0:
            failures.append("pool-not-released")
        if int(final_pool.get("verified_workers", 0)) < required_workers:
            failures.append(f"verified-workers-below-{required_workers}")
        if observed_peak < required_workers:
            failures.append(f"active-peak-below-{required_workers}")

        if failures:
            print("RESULT: FAIL", ", ".join(failures))
            return 1
        print("RESULT: PASS")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--model", default="gemini-3.7-flash")
    parser.add_argument("--stage", choices=["warmup", "same-api-3", "multi-api-3"])
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.base_url, args.model, args.stage)))
