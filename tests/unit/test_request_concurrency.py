"""Concurrency safety tests for API request browser isolation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aistudio_api.api.schemas import (
    AnthropicMessageRequest,
    ChatRequest,
    GeminiContent,
    GeminiGenerateContentRequest,
    GeminiPart,
)
from aistudio_api.api.state import runtime_state
from aistudio_api.api.routes_system import request_pool_status
from aistudio_api.application.api_service_anthropic import (
    _MAX_ANTHROPIC_TOOL_CONTEXTS,
    _remember_anthropic_tool_context,
    handle_anthropic_messages,
)
from aistudio_api.application.api_service_gemini import handle_gemini_generate_content
from aistudio_api.application.api_service_openai import handle_chat
from aistudio_api.application.account_rotator import AccountRotator
from aistudio_api.application.request_client_pool import RequestClientPool
from aistudio_api.domain.errors import AuthError, RequestError, UsageLimitExceeded
from aistudio_api.domain.models import Candidate, ModelOutput
from aistudio_api.infrastructure.account.account_store import AccountStore


class _Tracker:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    async def enter(self) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)

    async def leave(self) -> None:
        self.active -= 1


class _FakeClient:
    def __init__(self, account_id: str, tracker: _Tracker, calls: list[str]) -> None:
        self.account_id = account_id
        self.tracker = tracker
        self.calls = calls
        self.closed = 0

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        self.closed += 1

    async def generate_content(self, **kwargs) -> ModelOutput:
        self.calls.append(self.account_id)
        await self.tracker.enter()
        try:
            await asyncio.sleep(0.04)
            text = f"{self.account_id}:{kwargs.get('capture_prompt', '')}"
            return ModelOutput(
                candidates=[Candidate(text=text)],
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )
        finally:
            await self.tracker.leave()

    def clear_snapshot_cache(self) -> None:
        return None


def _create_store(tmp_path: Path, count: int = 1) -> AccountStore:
    store = AccountStore(accounts_dir=tmp_path / "accounts")
    for index in range(count):
        meta = store.save_account(
            name=f"Account {index + 1}",
            email=f"user{index + 1}@example.com",
            storage_state={"cookies": [], "origins": []},
        )
        profile = store.get_profile_path(meta.id)
        assert profile is not None
        (profile / "Default").mkdir(parents=True)
        (profile / "Local State").write_text("{}", encoding="utf-8")
        (profile / "Default" / "Cookies").write_bytes(b"cookie-db")
        (profile / "Default" / "Cache").mkdir()
        (profile / "Default" / "Cache" / "large.bin").write_bytes(b"cache")
    return store


def _pool(
    store: AccountStore,
    *,
    maximum: int,
    tracker: _Tracker,
    calls: list[str],
    clients: list[_FakeClient] | None = None,
) -> RequestClientPool:
    def factory(_auth_file: str, _profile_dir: str, account_id: str):
        client = _FakeClient(account_id, tracker, calls)
        if clients is not None:
            clients.append(client)
        return client

    return RequestClientPool(
        store,
        max_concurrency=maximum,
        port=9222,
        client_factory=factory,
    )


async def _with_runtime_pool(pool: RequestClientPool, operation):
    original = (
        runtime_state.request_pool,
        runtime_state.busy_lock,
        runtime_state.ready,
        runtime_state.account_switching,
        runtime_state.login_in_progress,
        runtime_state.rotator,
    )
    runtime_state.request_pool = pool
    runtime_state.busy_lock = asyncio.Semaphore(pool.max_concurrency)
    runtime_state.ready = True
    runtime_state.account_switching = False
    runtime_state.login_in_progress = False
    runtime_state.rotator = getattr(pool, "_rotator", None)
    try:
        return await operation()
    finally:
        await pool.close()
        (
            runtime_state.request_pool,
            runtime_state.busy_lock,
            runtime_state.ready,
            runtime_state.account_switching,
            runtime_state.login_in_progress,
            runtime_state.rotator,
        ) = original


def test_single_account_parallel_requests_queue_on_one_authoritative_profile(tmp_path):
    async def scenario():
        store = _create_store(tmp_path)
        tracker = _Tracker()
        calls: list[str] = []
        profiles: list[str] = []

        def factory(_auth_file: str, profile_dir: str, account_id: str):
            profiles.append(profile_dir)
            return _FakeClient(account_id, tracker, calls)

        pool = RequestClientPool(
            store,
            max_concurrency=3,
            port=9222,
            client_factory=factory,
        )

        async def use_one():
            async with pool.lease() as lease:
                await lease.client.generate_content(capture_prompt="same-api")

        await asyncio.gather(*(use_one() for _ in range(3)))
        assert tracker.peak == 1
        assert len(profiles) == 1
        authoritative_profile = str(store.get_profile_path(store.get_active_account().id))
        assert profiles == [authoritative_profile]
        assert pool.worker_count == 1
        assert not (Path(authoritative_profile).parent / ".request-workers").exists()
        assert pool.active_count == 0
        await pool.close()

    asyncio.run(scenario())


def test_requests_above_limit_queue_and_exception_releases_capacity(tmp_path):
    async def scenario():
        store = _create_store(tmp_path, count=2)
        tracker = _Tracker()
        pool = _pool(store, maximum=2, tracker=tracker, calls=[])

        async def use_one():
            async with pool.lease() as lease:
                await lease.client.generate_content(capture_prompt="queued")

        await asyncio.gather(*(use_one() for _ in range(5)))
        assert tracker.peak == 2
        assert pool.active_count == 0

        with pytest.raises(RuntimeError, match="boom"):
            async with pool.lease():
                raise RuntimeError("boom")
        async with pool.lease():
            assert pool.active_count == 1
        assert pool.active_count == 0
        await pool.close()

    asyncio.run(scenario())


def test_distinct_account_workers_warm_up_and_run_in_parallel(tmp_path):
    async def scenario():
        store = _create_store(tmp_path, count=3)
        warmups = _Tracker()
        requests = _Tracker()
        all_leased = asyncio.Event()
        leased = 0

        class Client(_FakeClient):
            async def warmup(self):
                await warmups.enter()
                try:
                    await asyncio.sleep(0.03)
                finally:
                    await warmups.leave()

            async def generate_content(self, **kwargs):
                await all_leased.wait()
                return await super().generate_content(**kwargs)

        def factory(_auth_file: str, _profile_dir: str, account_id: str):
            return Client(account_id, requests, [])

        pool = RequestClientPool(
            store,
            max_concurrency=3,
            port=9222,
            client_factory=factory,
        )

        async def use_one():
            nonlocal leased
            async with pool.lease() as lease:
                leased += 1
                if leased == 3:
                    all_leased.set()
                await lease.client.generate_content(capture_prompt="parallel")

        await asyncio.gather(*(use_one() for _ in range(3)))
        assert warmups.peak == 3
        assert requests.peak == 3
        await pool.close()

    asyncio.run(scenario())


def test_same_account_never_creates_a_cloned_worker_profile(tmp_path):
    async def scenario():
        store = _create_store(tmp_path)
        account_id = store.list_accounts()[0].id
        tracker = _Tracker()
        created: list[tuple[Path, Path]] = []

        def factory(auth_file: str, profile_dir: str, _account_id: str):
            created.append((Path(auth_file), Path(profile_dir)))
            return _FakeClient(account_id, tracker, [])

        pool = RequestClientPool(
            store,
            max_concurrency=2,
            port=9222,
            client_factory=factory,
        )

        async def use_one():
            async with pool.lease(preferred_account_id=account_id) as lease:
                await lease.client.generate_content(capture_prompt="queued")

        await asyncio.gather(use_one(), use_one())
        assert len(created) == 1
        auth_file, profile_dir = created[0]
        assert auth_file == store.get_auth_path_optional(account_id, require_exists=True)
        assert profile_dir == store.get_profile_path(account_id)
        assert not (auth_file.parent / ".request-workers").exists()
        await pool.close()

    asyncio.run(scenario())


def test_sequential_request_returns_to_active_account_after_parallel_burst(tmp_path):
    async def scenario():
        store = _create_store(tmp_path, count=3)
        active_id = store.get_active_account().id
        tracker = _Tracker()
        pool = _pool(store, maximum=3, tracker=tracker, calls=[])
        release = asyncio.Event()
        entered = 0
        all_entered = asyncio.Event()

        async def parallel_lease():
            nonlocal entered
            async with pool.lease():
                entered += 1
                if entered == 3:
                    all_entered.set()
                await release.wait()

        tasks = [asyncio.create_task(parallel_lease()) for _ in range(3)]
        await all_entered.wait()
        release.set()
        await asyncio.gather(*tasks)

        async with pool.lease() as lease:
            assert lease.account_id == active_id
        await pool.close()

    asyncio.run(scenario())


def test_prepare_account_warms_the_explicit_account_without_changing_rotation(tmp_path):
    async def scenario():
        store = _create_store(tmp_path, count=2)
        account_ids = [account.id for account in store.list_accounts()]
        tracker = _Tracker()
        created_for: list[str] = []

        def factory(_auth_file: str, _profile_dir: str, account_id: str):
            created_for.append(account_id)
            return _FakeClient(account_id, tracker, [])

        pool = RequestClientPool(
            store,
            max_concurrency=2,
            port=9222,
            client_factory=factory,
        )
        await pool.prepare_account(account_ids[1])
        assert created_for == [account_ids[1]]
        assert pool.active_count == 0
        assert store.get_active_account().id == account_ids[0]
        await pool.close()

    asyncio.run(scenario())


def test_prepare_account_never_calls_quota_consuming_generation_probe(tmp_path):
    async def scenario():
        store = _create_store(tmp_path)
        account_id = store.list_accounts()[0].id
        verify_calls = 0

        class NoSyntheticProbeClient(_FakeClient):
            async def verify_access(self):
                nonlocal verify_calls
                verify_calls += 1
                raise AssertionError("synthetic generation probe must not run")

        pool = RequestClientPool(
            store,
            max_concurrency=1,
            port=9222,
            client_factory=lambda _auth, _profile, account: NoSyntheticProbeClient(
                account, _Tracker(), []
            ),
        )

        await pool.prepare_account(account_id)

        assert verify_calls == 0
        assert pool.ready_account_ids == {account_id}
        await pool.close()

    asyncio.run(scenario())


def test_low_resource_pool_reuses_the_single_warm_account(tmp_path):
    async def scenario():
        store = _create_store(tmp_path, count=3)
        account_ids = [account.id for account in store.list_accounts()]
        tracker = _Tracker()
        rotator = AccountRotator(store, cooldown_seconds=60)
        pool = RequestClientPool(
            store,
            max_concurrency=3,
            port=9222,
            rotator=rotator,
            client_factory=lambda _auth, _profile, account_id: _FakeClient(
                account_id, tracker, []
            ),
        )
        await pool.prepare_all_accounts()

        selected: list[str] = []
        for _ in range(6):
            async with pool.lease() as lease:
                selected.append(lease.account_id)
                rotator.record_success(lease.account_id)

        assert len(set(selected)) == 1
        assert selected[0] in account_ids
        await pool.close()

    asyncio.run(scenario())


def test_adaptive_scheduler_skips_cooling_and_recently_failed_accounts(tmp_path):
    async def scenario():
        store = _create_store(tmp_path, count=3)
        account_ids = [account.id for account in store.list_accounts()]
        tracker = _Tracker()
        rotator = AccountRotator(store, cooldown_seconds=60)
        pool = RequestClientPool(
            store,
            max_concurrency=3,
            port=9222,
            rotator=rotator,
            client_factory=lambda _auth, _profile, account_id: _FakeClient(
                account_id, tracker, []
            ),
        )
        await pool.prepare_all_accounts()
        rotator.record_rate_limited(account_ids[0])
        rotator.record_error(account_ids[1])

        async with pool.lease() as lease:
            assert lease.account_id == account_ids[2]
        assert rotator.get_all_stats()[account_ids[0]]["cooldown_remaining"] > 0
        await pool.close()

    asyncio.run(scenario())


def test_adaptive_scheduler_uses_exponential_429_backoff_and_resets_after_success(tmp_path):
    store = _create_store(tmp_path)
    account_id = store.list_accounts()[0].id
    rotator = AccountRotator(store, cooldown_seconds=60)

    rotator.record_rate_limited(account_id)
    first = rotator._stats[account_id]
    first_remaining = first.cooldown_until - first.last_rate_limited
    rotator.record_rate_limited(account_id)
    second = rotator._stats[account_id]
    second_remaining = second.cooldown_until - second.last_rate_limited

    assert 59 <= first_remaining <= 61
    assert 119 <= second_remaining <= 121
    assert second.consecutive_rate_limits == 2

    rotator.record_success(account_id)
    rotator.record_rate_limited(account_id)
    recovered = rotator._stats[account_id]
    recovered_remaining = recovered.cooldown_until - recovered.last_rate_limited
    assert 59 <= recovered_remaining <= 61
    assert recovered.consecutive_rate_limits == 1


def test_prepare_account_rate_limit_immediately_enters_scheduler_cooldown(tmp_path):
    async def scenario():
        store = _create_store(tmp_path)
        account_id = store.list_accounts()[0].id
        tracker = _Tracker()
        rotator = AccountRotator(store, cooldown_seconds=60)

        class QuotaClient(_FakeClient):
            async def warmup(self):
                raise UsageLimitExceeded("quota exhausted")

        pool = RequestClientPool(
            store,
            max_concurrency=1,
            port=9222,
            rotator=rotator,
            client_factory=lambda _auth, _profile, account: QuotaClient(
                account, tracker, []
            ),
        )

        with pytest.raises(UsageLimitExceeded, match="quota exhausted"):
            await pool.prepare_account(account_id)

        stats = rotator.get_all_stats()[account_id]
        assert stats["rate_limited"] == 1
        assert stats["cooldown_remaining"] > 0
        assert account_id in pool.failed_account_errors
        await pool.close()

    asyncio.run(scenario())


def test_prepare_all_accounts_starts_every_account_and_isolates_failure(tmp_path):
    async def scenario():
        store = _create_store(tmp_path, count=4)
        account_ids = [account.id for account in store.list_accounts()]
        tracker = _Tracker()
        started: set[str] = set()
        all_started = asyncio.Event()
        release = asyncio.Event()

        class ProbeClient(_FakeClient):
            async def warmup(self):
                started.add(self.account_id)
                if len(started) == 2:
                    all_started.set()
                await release.wait()
                if self.account_id == account_ids[1]:
                    raise AuthError('禁止访问: [7,"The caller does not have permission"]')

        def factory(_auth_file: str, _profile_dir: str, account_id: str):
            return ProbeClient(account_id, tracker, [])

        pool = RequestClientPool(
            store,
            max_concurrency=2,
            port=9222,
            client_factory=factory,
        )
        warmup = asyncio.create_task(pool.prepare_all_accounts())
        await asyncio.wait_for(all_started.wait(), timeout=1)
        assert pool.initializing_account_ids == set(account_ids)

        release.set()
        failures = await warmup
        assert set(failures) == {account_ids[1]}
        successful = {account_ids[0], account_ids[2], account_ids[3]}
        assert len(pool.ready_account_ids) == 1
        assert pool.ready_account_ids < successful
        assert pool.standby_account_ids == successful - pool.ready_account_ids
        assert pool.worker_count == 1
        assert pool.max_concurrency == 2
        assert set(pool.failed_account_errors) == {account_ids[1]}
        assert pool.initializing_account_ids == set()
        await pool.close()

    asyncio.run(scenario())


def test_default_pool_clients_do_not_override_login_device_identity(monkeypatch, tmp_path):
    store = _create_store(tmp_path)
    captured: list[dict] = []

    class Client:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(
        "aistudio_api.application.request_client_pool.AIStudioClient",
        Client,
    )
    pool = RequestClientPool(store, max_concurrency=3, port=9222)
    worker_root = tmp_path / "accounts" / "acc" / ".request-workers"
    pool._default_client_factory(
        str(worker_root / "slot-1" / "auth.json"),
        str(worker_root / "slot-1" / "profile"),
        "acc_test",
    )
    pool._default_client_factory(
        str(worker_root / "slot-2" / "auth.json"),
        str(worker_root / "slot-2" / "profile"),
        "acc_test",
    )
    assert all("browser_identity_key" not in item for item in captured)


def test_openai_same_endpoint_burst_uses_configured_concurrency(tmp_path):
    async def scenario():
        store = _create_store(tmp_path, count=3)
        tracker = _Tracker()
        pool = _pool(store, maximum=3, tracker=tracker, calls=[])
        request = ChatRequest(
            model="gemini-3.7-flash",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )

        async def operation():
            return await asyncio.gather(*(handle_chat(request, object()) for _ in range(5)))

        responses = await _with_runtime_pool(pool, operation)
        assert len(responses) == 5
        assert tracker.peak == 3
        assert all(response.choices[0].message.content for response in responses)

    asyncio.run(scenario())


def test_openai_gemini_and_anthropic_requests_run_concurrently(tmp_path):
    async def scenario():
        store = _create_store(tmp_path, count=3)
        tracker = _Tracker()
        calls: list[str] = []
        pool = _pool(store, maximum=3, tracker=tracker, calls=calls)
        openai_request = ChatRequest(
            model="gemini-3.7-flash",
            messages=[{"role": "user", "content": "openai"}],
            tools=[],
        )
        gemini_request = GeminiGenerateContentRequest(
            contents=[GeminiContent(role="user", parts=[GeminiPart(text="gemini")])]
        )
        anthropic_request = AnthropicMessageRequest(
            model="gemini-3.7-flash",
            messages=[{"role": "user", "content": "anthropic"}],
        )

        async def operation():
            return await asyncio.gather(
                handle_chat(openai_request, object()),
                handle_gemini_generate_content(
                    "gemini-3.7-flash",
                    gemini_request,
                    object(),
                    stream=False,
                ),
                handle_anthropic_messages(anthropic_request, object()),
            )

        results = await _with_runtime_pool(pool, operation)
        assert len(results) == 3
        assert tracker.peak == 3
        assert len(calls) == 3
        assert len(set(calls)) == 3

    asyncio.run(scenario())


def test_anthropic_small_budget_empty_reasoning_retries_once_on_same_worker(tmp_path):
    async def scenario():
        store = _create_store(tmp_path)
        tracker = _Tracker()
        max_tokens_seen: list[int] = []

        class EmptyReasoningClient(_FakeClient):
            async def generate_content(self, **kwargs):
                max_tokens_seen.append(kwargs["max_tokens"])
                if len(max_tokens_seen) == 1:
                    return ModelOutput(
                        candidates=[Candidate(text="", thinking="hidden")],
                        usage={"prompt_tokens": 1, "completion_tokens": 64, "total_tokens": 65},
                    )
                return ModelOutput(
                    candidates=[Candidate(text="visible")],
                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                )

        def factory(_auth_file: str, _profile_dir: str, account_id: str):
            return EmptyReasoningClient(account_id, tracker, [])

        pool = RequestClientPool(
            store,
            max_concurrency=1,
            port=9222,
            client_factory=factory,
        )
        request = AnthropicMessageRequest(
            model="gemini-3.7-flash",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=64,
        )

        async def operation():
            return await handle_anthropic_messages(request, object())

        response = await _with_runtime_pool(pool, operation)
        assert response.content[0].text == "visible"
        assert max_tokens_seen == [64, 512]

    asyncio.run(scenario())


def test_rate_limit_retry_moves_only_current_request_to_another_account(tmp_path):
    async def scenario():
        store = _create_store(tmp_path, count=2)
        account_ids = [account.id for account in store.list_accounts()]
        calls: list[str] = []
        tracker = _Tracker()

        class RateLimitClient(_FakeClient):
            async def generate_content(self, **kwargs):
                calls.append(self.account_id)
                if self.account_id == account_ids[0]:
                    raise UsageLimitExceeded("quota")
                return await super().generate_content(**kwargs)

        def factory(_auth_file: str, _profile_dir: str, account_id: str):
            return RateLimitClient(account_id, tracker, [])

        pool = RequestClientPool(
            store,
            max_concurrency=2,
            port=9222,
            client_factory=factory,
        )
        request = ChatRequest(
            model="gemini-3.7-flash",
            messages=[{"role": "user", "content": "retry"}],
            tools=[],
        )

        async def operation():
            result = await handle_chat(request, object())
            return result, set(pool.ready_account_ids)

        response, ready_accounts = await _with_runtime_pool(pool, operation)
        assert response.choices[0].message.content.startswith(account_ids[1])
        assert calls[:2] == account_ids
        assert ready_accounts == {account_ids[1]}

    asyncio.run(scenario())


def test_navigation_context_failure_discards_worker_and_switches_account(tmp_path):
    async def scenario():
        store = _create_store(tmp_path, count=2)
        account_ids = [account.id for account in store.list_accounts()]
        calls: list[str] = []
        tracker = _Tracker()

        class NavigationClient(_FakeClient):
            async def stream_generate_content(self, **_kwargs):
                calls.append(self.account_id)
                if self.account_id == account_ids[0]:
                    raise RequestError(
                        0,
                        "Execution context was destroyed, most likely because of a navigation",
                    )
                yield "body", f"recovered:{self.account_id}"

        pool = RequestClientPool(
            store,
            max_concurrency=2,
            port=9222,
            client_factory=lambda _auth, _profile, account_id: NavigationClient(
                account_id, tracker, []
            ),
        )
        request = ChatRequest(
            model="gemini-3.7-flash",
            messages=[{"role": "user", "content": "retry after navigation"}],
            stream=True,
            tools=[],
        )

        async def operation():
            response = await handle_chat(request, object())
            body = "".join([chunk async for chunk in response.body_iterator])
            return body, set(pool.ready_account_ids)

        body, ready_accounts = await _with_runtime_pool(pool, operation)
        assert f"recovered:{account_ids[1]}" in body
        assert calls[:2] == account_ids
        assert ready_accounts == {account_ids[1]}

    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", ["gemini", "anthropic"])
def test_rate_limit_retry_switches_accounts_for_all_compat_protocols(tmp_path, protocol):
    async def scenario():
        store = _create_store(tmp_path, count=2)
        account_ids = [account.id for account in store.list_accounts()]
        calls: list[str] = []
        tracker = _Tracker()
        rotator = AccountRotator(store, cooldown_seconds=60)

        class RateLimitClient(_FakeClient):
            async def generate_content(self, **kwargs):
                calls.append(self.account_id)
                if self.account_id == account_ids[0]:
                    raise UsageLimitExceeded("quota")
                return await super().generate_content(**kwargs)

        pool = RequestClientPool(
            store,
            max_concurrency=2,
            port=9222,
            rotator=rotator,
            client_factory=lambda _auth, _profile, account_id: RateLimitClient(
                account_id, tracker, []
            ),
        )
        if protocol == "gemini":
            request = GeminiGenerateContentRequest(
                contents=[GeminiContent(role="user", parts=[GeminiPart(text="retry")])]
            )

            async def operation():
                return await handle_gemini_generate_content(
                    "gemini-3.7-flash",
                    request,
                    object(),
                    stream=False,
                )
        else:
            request = AnthropicMessageRequest(
                model="gemini-3.7-flash",
                messages=[{"role": "user", "content": "retry"}],
            )

            async def operation():
                return await handle_anthropic_messages(request, object())

        protocol_operation = operation

        async def operation_with_status():
            result = await protocol_operation()
            return result, set(pool.ready_account_ids)

        _response, ready_accounts = await _with_runtime_pool(pool, operation_with_status)
        assert calls[:2] == account_ids
        assert ready_accounts == {account_ids[1]}
        assert rotator.get_all_stats()[account_ids[0]]["cooldown_remaining"] > 0

    asyncio.run(scenario())


def test_auth_failure_discards_worker_and_retries_until_verified(tmp_path):
    async def scenario():
        store = _create_store(tmp_path)
        tracker = _Tracker()
        created: list[_FakeClient] = []

        class RecoveringClient(_FakeClient):
            async def generate_content(self, **kwargs):
                if created.index(self) < 2:
                    raise AuthError("permission denied")
                return await super().generate_content(**kwargs)

        def factory(_auth_file: str, _profile_dir: str, account_id: str):
            client = RecoveringClient(account_id, tracker, [])
            created.append(client)
            return client

        pool = RequestClientPool(
            store,
            max_concurrency=3,
            port=9222,
            client_factory=factory,
        )
        request = ChatRequest(
            model="gemini-3.7-flash",
            messages=[{"role": "user", "content": "recover"}],
            tools=[],
        )

        async def operation():
            return await handle_chat(request, object())

        response = await _with_runtime_pool(pool, operation)
        assert response.choices[0].message.content
        assert len(created) == 3
        assert created[0].closed == 1
        assert created[1].closed == 1

    asyncio.run(scenario())


def test_prepare_account_retries_real_permission_probe(tmp_path):
    async def scenario():
        store = _create_store(tmp_path)
        account_id = store.list_accounts()[0].id
        tracker = _Tracker()
        created: list[_FakeClient] = []

        class ProbeClient(_FakeClient):
            async def warmup(self):
                if created.index(self) == 0:
                    raise AuthError("stale cookies")

        def factory(_auth_file: str, _profile_dir: str, client_account_id: str):
            client = ProbeClient(client_account_id, tracker, [])
            created.append(client)
            return client

        pool = RequestClientPool(
            store,
            max_concurrency=1,
            port=9222,
            client_factory=factory,
        )
        await pool.prepare_account(account_id)
        assert len(created) == 2
        assert created[0].closed == 1
        assert pool.ready_account_ids == {account_id}
        account_auth = store.get_auth_path_optional(account_id, require_exists=True)
        assert account_auth is not None
        assert (Path(account_auth).parent / "auth.verified.sha256").exists()
        await pool.close()

    asyncio.run(scenario())


def test_prepare_account_initialization_does_not_call_legacy_native_probe(tmp_path):
    async def scenario():
        store = _create_store(tmp_path)
        account_id = store.list_accounts()[0].id
        tracker = _Tracker()
        created: list[_FakeClient] = []
        verify_calls = 0

        class NoProbeClient(_FakeClient):
            async def verify_access(self):
                nonlocal verify_calls
                verify_calls += 1
                raise AssertionError("legacy native probe was invoked")

        def factory(_auth_file: str, _profile_dir: str, client_account_id: str):
            client = NoProbeClient(client_account_id, tracker, [])
            created.append(client)
            return client

        pool = RequestClientPool(
            store,
            max_concurrency=3,
            port=9222,
            client_factory=factory,
        )
        await pool.prepare_account(account_id)
        assert verify_calls == 0
        assert len(created) == 1
        assert pool.verified_worker_count == 1
        assert pool.ready_account_ids == {account_id}
        await pool.close()

    asyncio.run(scenario())


def test_prepare_account_does_not_repeat_terminal_permission_denial(tmp_path):
    async def scenario():
        store = _create_store(tmp_path)
        account_id = store.list_accounts()[0].id
        authoritative_profile = store.get_profile_path(account_id)
        authoritative_profile.mkdir(parents=True, exist_ok=True)
        sentinel = authoritative_profile / "manual-login-state"
        sentinel.write_text("keep", encoding="utf-8")
        tracker = _Tracker()
        created: list[_FakeClient] = []

        class ForbiddenClient(_FakeClient):
            async def warmup(self):
                raise AuthError('禁止访问: [7,"The caller does not have permission"]')

        def factory(_auth_file: str, _profile_dir: str, client_account_id: str):
            client = ForbiddenClient(client_account_id, tracker, [])
            created.append(client)
            return client

        pool = RequestClientPool(
            store,
            max_concurrency=3,
            port=9222,
            client_factory=factory,
        )
        with pytest.raises(AuthError, match="does not have permission"):
            await pool.prepare_account(account_id)
        assert len(created) == 1
        assert created[0].closed == 1
        assert pool.active_count == 0
        assert pool.worker_count == 0
        assert pool.verified_worker_count == 0
        assert sentinel.read_text(encoding="utf-8") == "keep"
        assert store.get_auth_path_optional(account_id, require_exists=True) is not None
        await pool.close()

    asyncio.run(scenario())


def test_stream_close_and_task_cancellation_release_worker(tmp_path):
    async def scenario():
        store = _create_store(tmp_path)
        tracker = _Tracker()
        calls: list[str] = []
        release_stream = asyncio.Event()

        class StreamingClient(_FakeClient):
            async def stream_generate_content(self, **_kwargs):
                await self.tracker.enter()
                try:
                    yield "body", "first"
                    await release_stream.wait()
                finally:
                    await self.tracker.leave()

        def factory(_auth_file: str, _profile_dir: str, account_id: str):
            return StreamingClient(account_id, tracker, calls)

        pool = RequestClientPool(store, max_concurrency=1, port=9222, client_factory=factory)
        request = ChatRequest(
            model="gemini-3.7-flash",
            messages=[{"role": "user", "content": "stream"}],
            stream=True,
            tools=[],
        )

        async def operation():
            response = await handle_chat(request, object())
            iterator = response.body_iterator
            first = await anext(iterator)
            assert "first" in first
            await iterator.aclose()
            await asyncio.sleep(0)
            assert pool.active_count == 0
            async with pool.lease():
                assert pool.active_count == 1

        await _with_runtime_pool(pool, operation)
        assert tracker.active == 0

    asyncio.run(scenario())


def test_account_close_waits_for_inflight_request_and_closes_its_clients(tmp_path):
    async def scenario():
        store = _create_store(tmp_path)
        account_id = store.list_accounts()[0].id
        tracker = _Tracker()
        clients: list[_FakeClient] = []
        pool = _pool(store, maximum=1, tracker=tracker, calls=[], clients=clients)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_lease():
            async with pool.lease():
                entered.set()
                await release.wait()

        holder = asyncio.create_task(hold_lease())
        await entered.wait()
        closer = asyncio.create_task(pool.close_account(account_id))
        await asyncio.sleep(0.02)
        assert not closer.done()
        release.set()
        await holder
        await closer
        assert clients and clients[0].closed == 1
        assert pool.worker_count == 0
        await pool.close()

    asyncio.run(scenario())


def test_cancelled_active_request_releases_capacity(tmp_path):
    async def scenario():
        store = _create_store(tmp_path)
        tracker = _Tracker()
        pool = _pool(store, maximum=1, tracker=tracker, calls=[])
        entered = asyncio.Event()

        async def hold_forever():
            async with pool.lease():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(hold_forever())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.01)
        assert pool.active_count == 0
        async with pool.lease():
            assert pool.active_count == 1
        await pool.close()

    asyncio.run(scenario())


def test_failed_worker_warmup_is_closed_and_does_not_leak_slot(tmp_path):
    async def scenario():
        store = _create_store(tmp_path)
        tracker = _Tracker()
        created: list[_FakeClient] = []

        class FirstWarmupFails(_FakeClient):
            async def warmup(self):
                if len(created) == 1:
                    raise RuntimeError("warmup failed")

        def factory(_auth_file: str, _profile_dir: str, account_id: str):
            client = FirstWarmupFails(account_id, tracker, [])
            created.append(client)
            return client

        pool = RequestClientPool(store, max_concurrency=1, port=9222, client_factory=factory)
        with pytest.raises(RuntimeError, match="warmup failed"):
            async with pool.lease():
                pass
        assert pool.active_count == 0
        assert pool.worker_count == 0
        assert created[0].closed == 1

        async with pool.lease() as lease:
            assert lease.client is created[1]
        assert pool.active_count == 0
        await pool.close()

    asyncio.run(scenario())


def test_request_pool_probe_reports_live_capacity(tmp_path):
    store = _create_store(tmp_path)
    tracker = _Tracker()
    pool = _pool(store, maximum=3, tracker=tracker, calls=[])

    async def scenario():
        async with pool.lease():
            status = await request_pool_status(
                type("Runtime", (), {"request_pool": pool})()
            )
            assert status == {
                "enabled": True,
                "max_concurrency": 3,
                "max_idle_browsers": 1,
                "active": 1,
                "workers": 1,
                "verified_workers": 0,
                "saturated": False,
                "ready_accounts": [],
                "standby_accounts": [],
                "initializing_accounts": [store.get_active_account().id],
                "failed_accounts": {},
            }
        await pool.close()

    asyncio.run(scenario())


def test_parallel_burst_releases_extra_idle_browsers(tmp_path):
    async def scenario():
        store = _create_store(tmp_path, count=3)
        tracker = _Tracker()
        clients: list[_FakeClient] = []
        pool = _pool(
            store,
            maximum=3,
            tracker=tracker,
            calls=[],
            clients=clients,
        )
        release = asyncio.Event()
        entered = 0
        all_entered = asyncio.Event()

        async def use_one():
            nonlocal entered
            async with pool.lease():
                entered += 1
                if entered == 3:
                    all_entered.set()
                await release.wait()

        tasks = [asyncio.create_task(use_one()) for _ in range(3)]
        await asyncio.wait_for(all_entered.wait(), timeout=1)
        assert pool.worker_count == 3
        release.set()
        await asyncio.gather(*tasks)

        assert pool.worker_count == 1
        assert sum(client.closed for client in clients) == 2
        assert len(pool.standby_account_ids) == 3
        await pool.close()

    asyncio.run(scenario())


def test_zero_idle_browser_mode_closes_every_released_worker(tmp_path):
    async def scenario():
        store = _create_store(tmp_path)
        tracker = _Tracker()
        clients: list[_FakeClient] = []

        def factory(_auth_file: str, _profile_dir: str, account_id: str):
            client = _FakeClient(account_id, tracker, [])
            clients.append(client)
            return client

        pool = RequestClientPool(
            store,
            max_concurrency=1,
            max_idle_browsers=0,
            port=9222,
            client_factory=factory,
        )
        async with pool.lease():
            assert pool.worker_count == 1
        assert pool.worker_count == 0
        assert clients[0].closed == 1
        await pool.close()

    asyncio.run(scenario())


def test_anthropic_tool_context_cache_is_bounded():
    original = runtime_state.anthropic_tool_context
    runtime_state.anthropic_tool_context = {}
    try:
        for index in range(_MAX_ANTHROPIC_TOOL_CONTEXTS + 20):
            _remember_anthropic_tool_context(
                f"toolu_{index}",
                {"call_id": str(index)},
            )
        assert len(runtime_state.anthropic_tool_context) == _MAX_ANTHROPIC_TOOL_CONTEXTS
        assert "toolu_0" not in runtime_state.anthropic_tool_context
        assert f"toolu_{_MAX_ANTHROPIC_TOOL_CONTEXTS + 19}" in runtime_state.anthropic_tool_context
    finally:
        runtime_state.anthropic_tool_context = original
