"""Isolated browser-backed client pool for concurrent API requests."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from aistudio_api.domain.errors import AuthError, UsageLimitExceeded
from aistudio_api.infrastructure.cache.snapshot_cache import SnapshotCache
from aistudio_api.infrastructure.gateway.client import AIStudioClient

logger = logging.getLogger("aistudio.request_pool")

_PROFILE_IGNORES = {
    "Cache",
    "Code Cache",
    "Crashpad",
    "DawnCache",
    "DevToolsActivePort",
    "GPUCache",
    "GrShaderCache",
    "ShaderCache",
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
}


def _is_terminal_permission_error(exc: AuthError) -> bool:
    """Return whether rebuilding the same saved login cannot repair the error."""
    message = str(exc).casefold()
    return "does not have permission" in message or "禁止访问" in message


@dataclass
class RequestClientLease:
    """A client exclusively owned by one API request until the lease exits."""

    client: Any
    account_id: str
    worker_id: str
    verified: bool


@dataclass
class _Worker:
    id: str
    account_id: str
    slot_name: str
    auth_file: Path
    profile_dir: Path
    owns_files: bool = True
    client: Any | None = None
    busy: bool = True
    initialized: bool = False
    verified: bool = False
    discard_on_release: bool = False
    last_used: float = 0.0
    seed_worker_id: str | None = None


class RequestClientPool:
    """Bounded pool with one authoritative Chromium session per account.

    A Playwright synchronous browser context cannot safely serve overlapping
    requests. AI Studio also rejects copied same-account browser identities, so
    requests for the same account queue on its single worker. Distinct accounts
    retain independent workers and can run concurrently up to the global limit.
    """

    def __init__(
        self,
        account_store: Any,
        *,
        max_concurrency: int,
        max_idle_browsers: int = 1,
        port: int,
        rotator: Any = None,
        control_client: Any = None,
        client_factory: Callable[[str, str, str], Any] | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if max_idle_browsers < 0:
            raise ValueError("max_idle_browsers must be at least 0")
        self._store = account_store
        self._max_concurrency = max_concurrency
        self._max_idle_browsers = min(max_idle_browsers, max_concurrency)
        self._port = port
        self._rotator = rotator
        self._control_client = control_client
        self._client_factory = client_factory or self._default_client_factory
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._condition = asyncio.Condition()
        self._workers: list[_Worker] = []
        self._active_leases = 0
        self._closed = False
        self._source_copy_lock = asyncio.Lock()
        self._verified_sync_lock = asyncio.Lock()
        self._account_init_locks: dict[str, asyncio.Lock] = {}
        self._preparing_account_ids: set[str] = set()
        self._failed_account_errors: dict[str, str] = {}

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def active_count(self) -> int:
        return self._active_leases

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    @property
    def verified_worker_count(self) -> int:
        return sum(worker.verified for worker in self._workers)

    @property
    def max_idle_browsers(self) -> int:
        return self._max_idle_browsers

    @property
    def ready_account_ids(self) -> set[str]:
        return {
            worker.account_id
            for worker in self._workers
            if worker.verified and self._account_is_available(worker.account_id)
        }

    @property
    def initializing_account_ids(self) -> set[str]:
        worker_accounts = {
            worker.account_id
            for worker in self._workers
            if worker.busy and not worker.verified
        }
        return worker_accounts | self._preparing_account_ids

    @property
    def failed_account_errors(self) -> dict[str, str]:
        return dict(self._failed_account_errors)

    @property
    def standby_account_ids(self) -> set[str]:
        known = {
            account.id
            for account in self._store.list_accounts()
            if self._store.get_auth_path_optional(account.id, require_exists=True) is not None
        }
        return known - self.ready_account_ids - self.initializing_account_ids - set(self._failed_account_errors)

    @property
    def saturated(self) -> bool:
        return self._active_leases >= self._max_concurrency

    def _default_client_factory(self, auth_file: str, profile_dir: str, account_id: str) -> AIStudioClient:
        cache = SnapshotCache()
        cache.set_namespace(account_id)
        return AIStudioClient(
            port=self._port,
            auth_file=auth_file,
            profile_dir=profile_dir,
            snapshot_cache=cache,
            account_id=account_id,
        )

    def _account_is_available(self, account_id: str) -> bool:
        if self._rotator is None or not hasattr(self._rotator, "get_all_stats"):
            return True
        stats = self._rotator.get_all_stats().get(account_id)
        return not stats or bool(stats.get("is_available", True))

    def _account_scheduling_key(self, account_id: str) -> tuple:
        if self._rotator is not None and hasattr(self._rotator, "scheduling_key"):
            return tuple(self._rotator.scheduling_key(account_id))
        return (0, 0.0, 0.0, 0)

    def _max_cached_workers(self) -> int:
        """Maximum browser processes retained after leases become idle."""
        return self._max_idle_browsers

    def _ordered_accounts(
        self,
        excluded: set[str],
        preferred_account_id: str | None = None,
    ) -> list[Any]:
        accounts = [
            account
            for account in self._store.list_accounts()
            if self._store.get_auth_path_optional(account.id, require_exists=True) is not None
        ]
        if not accounts:
            return []

        # Explicit preparation always targets the requested login. Ordinary API
        # leases use adaptive health/cooldown/load scheduling below.
        if preferred_account_id is not None:
            return [account for account in accounts if account.id == preferred_account_id]

        healthy = [account for account in accounts if self._account_is_available(account.id)]
        candidates = [account for account in healthy if account.id not in excluded]
        if not candidates:
            candidates = healthy
        if not candidates:
            # Every account is cooling. Prefer a non-attempted account and then
            # the account whose cooldown expires first, avoiding a fixed sleep
            # inside the shared scheduler.
            candidates = [account for account in accounts if account.id not in excluded] or accounts
        candidates.sort(key=lambda account: self._account_scheduling_key(account.id))
        return candidates

    def _pick_idle_worker(self, accounts: list[Any]) -> _Worker | None:
        order = {account.id: index for index, account in enumerate(accounts)}
        candidates = [
            worker
            for worker in self._workers
            if not worker.busy and worker.account_id in order
        ]
        if not candidates:
            return None
        reusable = candidates
        if self._rotator is not None and hasattr(self._rotator, "should_avoid_warm_reuse"):
            reusable = [
                worker
                for worker in candidates
                if not self._rotator.should_avoid_warm_reuse(worker.account_id)
            ]
            if not reusable:
                # Prefer launching an untried account. If every candidate
                # already owns a worker, reuse the best failed worker so a
                # single-account installation can recover instead of waiting
                # forever.
                worker_accounts = {worker.account_id for worker in self._workers}
                if any(account.id not in worker_accounts for account in accounts):
                    return None
                reusable = candidates
        return min(reusable, key=lambda worker: (order[worker.account_id], worker.last_used))

    async def _checkout(
        self,
        excluded: set[str],
        preferred_account_id: str | None = None,
    ) -> _Worker:
        victim: _Worker | None = None
        while True:
            async with self._condition:
                if self._closed:
                    raise RuntimeError("请求执行池已关闭")
                accounts = self._ordered_accounts(excluded, preferred_account_id)
                if not accounts:
                    raise RuntimeError("没有可用账号，请先添加账号并完成登录")

                # Reuse an eligible warm browser before starting another one.
                # With a one-browser idle cache this avoids a close/relaunch on
                # every round-robin request. Failover still starts another
                # account when the warm account is busy, excluded or cooling.
                worker = self._pick_idle_worker(accounts)
                if worker is not None:
                    worker.busy = True
                    self._active_leases += 1
                    return worker

                worker_account_ids = {item.account_id for item in self._workers}
                new_worker_accounts = [
                    account for account in accounts if account.id not in worker_account_ids
                ]

                # One authoritative profile is allowed per account. If every
                # eligible account already has a busy worker, queue until one
                # returns instead of cloning the login profile.
                if not new_worker_accounts:
                    await self._condition.wait()
                    continue

                # Active browsers never exceed request concurrency. If the pool
                # is full but an old worker is idle, replace it with the newly
                # scheduled account; otherwise wait for an active lease.
                if len(self._workers) >= self._max_concurrency:
                    idle_victims = [item for item in self._workers if not item.busy]
                    if idle_victims:
                        victim = min(idle_victims, key=lambda item: item.last_used)
                        self._workers.remove(victim)
                    else:
                        await self._condition.wait()
                        continue

                account = new_worker_accounts[0]
                auth_path = self._store.get_auth_path_optional(account.id, require_exists=True)
                if auth_path is None:
                    continue
                slot_name = "slot-1"
                worker_id = f"{account.id}:{slot_name}"
                account_root = Path(auth_path).parent
                worker = _Worker(
                    id=worker_id,
                    account_id=account.id,
                    slot_name=slot_name,
                    auth_file=account_root / "auth.json",
                    profile_dir=account_root / "profile",
                    owns_files=False,
                )
                self._workers.append(worker)
                self._active_leases += 1
                break

        if victim is not None:
            await self._close_worker(victim)
        try:
            await self._prepare_worker(worker)
            return worker
        except BaseException:
            async with self._condition:
                if worker in self._workers:
                    self._workers.remove(worker)
                self._active_leases -= 1
                self._condition.notify_all()
            raise

    @staticmethod
    def _profile_ignore(_directory: str, names: list[str]) -> list[str]:
        return [name for name in names if name in _PROFILE_IGNORES]

    @staticmethod
    def _source_fingerprint(auth_path: Path, profile_path: Path | None) -> str:
        digest = hashlib.sha256(auth_path.read_bytes())
        # Keep auth-only worker snapshots distinct from the legacy full-profile
        # copy.  Otherwise an old marker can preserve stale Google cookies in a
        # previously-created worker profile after an application update.
        digest.update(b"auth-only" if profile_path is None else b"profile-copy")
        if profile_path is not None:
            for relative in (Path("Local State"), Path("Default") / "Cookies"):
                candidate = profile_path / relative
                if candidate.exists():
                    stat = candidate.stat()
                    digest.update(str(relative).encode("utf-8"))
                    digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
        return digest.hexdigest()

    @staticmethod
    def _auth_fingerprint(auth_path: Path) -> str:
        return hashlib.sha256(auth_path.read_bytes()).hexdigest()

    @classmethod
    def _verified_marker_matches(cls, auth_path: Path, marker: Path) -> bool:
        try:
            return (
                auth_path.exists()
                and marker.exists()
                and marker.read_text(encoding="ascii", errors="ignore").strip()
                == cls._auth_fingerprint(auth_path)
            )
        except OSError:
            return False

    @classmethod
    def _copy_worker_files_sync(
        cls,
        source_auth: Path,
        source_profile: Path | None,
        worker: _Worker,
    ) -> None:
        root = worker.auth_file.parent
        marker = root / "source.sha256"
        fingerprint = cls._source_fingerprint(source_auth, source_profile)
        if (
            marker.exists()
            and marker.read_text(encoding="ascii", errors="ignore").strip() == fingerprint
            and worker.auth_file.exists()
            and worker.profile_dir.exists()
        ):
            return

        if root.exists():
            shutil.rmtree(root, ignore_errors=False)
        root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_auth, worker.auth_file)
        source_meta = source_auth.parent / "meta.json"
        if source_meta.exists():
            shutil.copy2(source_meta, root / "meta.json")
        copied_profile = False
        if source_profile is not None and source_profile.exists():
            try:
                shutil.copytree(
                    source_profile,
                    worker.profile_dir,
                    ignore=cls._profile_ignore,
                )
                copied_profile = True
            except OSError as exc:
                # Chromium may briefly keep a nonessential profile file open on
                # Windows. Keep the partial profile and auth.json fallback so a
                # transient cache lock does not reject the API request.
                logger.warning("复制账号 profile 不完整，将由 auth.json 补全: %s", exc)
                worker.profile_dir.mkdir(parents=True, exist_ok=True)
        else:
            worker.profile_dir.mkdir(parents=True, exist_ok=True)
        if copied_profile or source_profile is None or not source_profile.exists():
            marker.write_text(fingerprint, encoding="ascii")

    def _ready_seed_worker(self, worker: _Worker) -> _Worker | None:
        candidates = [
            item
            for item in self._workers
            if (
                item is not worker
                and item.account_id == worker.account_id
                and item.verified
                and item.auth_file.exists()
            )
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.last_used)

    async def _copy_worker_seed(self, worker: _Worker) -> None:
        if not worker.owns_files:
            # Slot 1 exclusively owns the authoritative profile created by the
            # completed manual login. AI Studio can bind project permission to
            # profile-local device state; copying that directory produced a
            # signed-in page whose native GenerateContent still returned 403.
            worker.seed_worker_id = None
            logger.info(
                "[pool] worker seed ready worker=%s account=%s "
                "source=account mode=authoritative-profile",
                worker.id,
                worker.account_id,
            )
            return
        ready_seed = self._ready_seed_worker(worker)
        if ready_seed is not None:
            # A ready worker has already completed ServiceLogin rotation and
            # carries the newest short-lived Google session cookies.  Copy only
            # that storage state; every concurrent worker must build its own
            # Chromium profile so SQLite/lock files are never shared.
            source_auth = ready_seed.auth_file
            source_profile = None
            worker.seed_worker_id = ready_seed.id
            seed_label = ready_seed.id
            seed_mode = "ready-auth"
        else:
            worker_verified_marker = worker.auth_file.parent / "verified.sha256"
            if (
                worker.profile_dir.exists()
                and self._verified_marker_matches(worker.auth_file, worker_verified_marker)
            ):
                # Reusing a previously successful worker keeps the exact
                # persistent browser identity that Google already accepted.
                # It is verified again in the current process before the UI
                # reports the account as ready.
                worker.seed_worker_id = None
                logger.info(
                    "[pool] worker seed ready worker=%s account=%s source=worker-cache mode=verified-profile",
                    worker.id,
                    worker.account_id,
                )
                return
            if (
                worker.auth_file.exists()
                and worker.profile_dir.exists()
                and (worker.auth_file.parent / "source.sha256").exists()
            ):
                # Migration path for workers created by older Asteria builds:
                # preserve the exact profile once and validate it with a real
                # request. If it is stale, mark_worker_unhealthy deletes it and
                # the next retry advances to another slot identity.
                worker.seed_worker_id = None
                logger.info(
                    "[pool] worker seed ready worker=%s account=%s source=worker-cache mode=unverified-profile",
                    worker.id,
                    worker.account_id,
                )
                return
            source_auth = self._store.get_auth_path_optional(
                worker.account_id,
                require_exists=True,
            )
            if source_auth is None:
                raise RuntimeError(f"账号 {worker.account_id} 缺少 auth.json")
            source_auth = Path(source_auth)
            # The account profile is the authoritative manual-login identity.
            # A copied auth.json can expire after Google rotates short-lived
            # session cookies, while the persistent login profile can still
            # renew itself. Use it for the first worker; only siblings clone a
            # currently verified worker's auth into fresh isolated profiles.
            source_profile = source_auth.parent / "profile"
            seed_mode = "profile"
            worker.seed_worker_id = None
            seed_label = "account"

        await asyncio.to_thread(
            self._copy_worker_files_sync,
            Path(source_auth),
            source_profile,
            worker,
        )
        logger.info(
            "[pool] worker seed ready worker=%s account=%s source=%s mode=%s",
            worker.id,
            worker.account_id,
            seed_label,
            seed_mode,
        )

    async def _release_control_profile(self) -> None:
        session = getattr(self._control_client, "_session", None)
        if session is not None and hasattr(session, "release_context"):
            await session.release_context()

    async def _prepare_worker(self, worker: _Worker) -> None:
        started = time.perf_counter()
        async with self._source_copy_lock:
            await self._release_control_profile()
        # Once the control profile is closed, worker copies are independent
        # read-only snapshots and can be prepared in parallel. Serializing the
        # disk copies made a cold three-request burst start one-by-one.
        await self._copy_worker_seed(worker)
        logger.info(
            "[pool] worker profile ready worker=%s account=%s elapsed=%.3fs",
            worker.id,
            worker.account_id,
            time.perf_counter() - started,
        )

    async def _ensure_initialized(self, worker: _Worker) -> None:
        if worker.initialized:
            return
        # Independent workers may execute requests concurrently, but Google can
        # reject overlapping ServiceLogin/BotGuard bootstrap attempts from the
        # same account. Serialize only this one-time warmup per account; once
        # initialized, leases remain fully concurrent.
        init_lock = self._account_init_locks.setdefault(
            worker.account_id,
            asyncio.Lock(),
        )
        async with init_lock:
            if worker.initialized:
                return
            # Several cold requests can copy the account snapshot before the
            # first worker finishes warmup. Re-evaluate the seed here, after the
            # per-account initialization lock is acquired, so workers 2..N are
            # rebuilt from the now-ready worker instead of stale account data.
            await self._copy_worker_seed(worker)
            worker.client = self._client_factory(
                str(worker.auth_file),
                str(worker.profile_dir),
                worker.account_id,
            )
            started = time.perf_counter()
            logger.info(
                "[pool] worker warmup start worker=%s account=%s",
                worker.id,
                worker.account_id,
            )
            await worker.client.warmup()
            worker.initialized = True
            logger.info(
                "[pool] worker ready worker=%s account=%s elapsed=%.3fs",
                worker.id,
                worker.account_id,
                time.perf_counter() - started,
            )

    async def _checkin(self, worker: _Worker) -> None:
        victims: list[_Worker] = []
        async with self._condition:
            if worker in self._workers:
                worker.busy = False
                worker.last_used = time.monotonic()
                idle_workers = sorted(
                    (item for item in self._workers if not item.busy),
                    key=lambda item: item.last_used,
                    reverse=True,
                )
                victims = idle_workers[self._max_cached_workers():]
                for victim in victims:
                    self._workers.remove(victim)
            self._active_leases = max(0, self._active_leases - 1)
            self._condition.notify_all()
        if victims:
            await asyncio.gather(*(self._close_worker(victim) for victim in victims))
            logger.info(
                "[pool] trimmed idle browsers closed=%d retained=%d",
                len(victims),
                self._max_idle_browsers,
            )

    @classmethod
    def _persist_verified_worker_sync(cls, worker: _Worker, account_auth: Path) -> None:
        fingerprint = cls._auth_fingerprint(worker.auth_file)
        (worker.auth_file.parent / "verified.sha256").write_text(
            fingerprint,
            encoding="ascii",
        )
        if worker.auth_file.resolve() != account_auth.resolve():
            temporary = account_auth.with_suffix(account_auth.suffix + ".request-worker.tmp")
            shutil.copy2(worker.auth_file, temporary)
            temporary.replace(account_auth)
        (account_auth.parent / "auth.verified.sha256").write_text(
            cls._auth_fingerprint(account_auth),
            encoding="ascii",
        )

    async def mark_worker_verified(self, worker_id: str) -> None:
        worker = next((item for item in self._workers if item.id == worker_id), None)
        if worker is None or worker.discard_on_release or not worker.auth_file.exists():
            return
        worker.verified = True
        self._failed_account_errors.pop(worker.account_id, None)
        account_auth = self._store.get_auth_path_optional(
            worker.account_id,
            require_exists=False,
        )
        if account_auth is None:
            return
        try:
            async with self._verified_sync_lock:
                await asyncio.to_thread(
                    self._persist_verified_worker_sync,
                    worker,
                    Path(account_auth),
                )
        except OSError as exc:
            # The request itself succeeded. A transient antivirus/profile lock
            # must not turn that success into an API 500; the live worker stays
            # verified and persistence is retried after the next success.
            logger.warning("持久化已验证 worker 会话失败 worker=%s: %s", worker.id, exc)
        logger.info(
            "[pool] worker verified worker=%s account=%s",
            worker.id,
            worker.account_id,
        )

    def mark_worker_unhealthy(self, worker_id: str) -> None:
        worker = next((item for item in self._workers if item.id == worker_id), None)
        if worker is None:
            return
        worker.verified = False
        worker.discard_on_release = True
        logger.warning(
            "[pool] worker marked unhealthy worker=%s account=%s",
            worker.id,
            worker.account_id,
        )

    @staticmethod
    def _remove_worker_files_sync(worker: _Worker) -> None:
        if not worker.owns_files:
            return
        root = worker.auth_file.parent
        for attempt in range(10):
            try:
                if root.exists():
                    shutil.rmtree(root, ignore_errors=False)
                return
            except OSError:
                if attempt == 9:
                    logger.warning("清理失效请求 worker 目录失败: %s", root)
                    return
                time.sleep(0.2)

    async def _discard_worker(self, worker: _Worker) -> None:
        await self._close_worker(worker)
        await asyncio.to_thread(self._remove_worker_files_sync, worker)
        async with self._condition:
            if worker in self._workers:
                self._workers.remove(worker)
            self._active_leases = max(0, self._active_leases - 1)
            self._condition.notify_all()
        logger.info(
            "[pool] worker discarded worker=%s account=%s active=%d/%d",
            worker.id,
            worker.account_id,
            self._active_leases,
            self._max_concurrency,
        )

    @asynccontextmanager
    async def lease(
        self,
        *,
        exclude_account_ids: set[str] | None = None,
        preferred_account_id: str | None = None,
    ):
        """Wait for an isolated request client and always return it to the pool."""
        queued_at = time.perf_counter()
        await self._semaphore.acquire()
        worker: _Worker | None = None
        try:
            worker = await self._checkout(
                set(exclude_account_ids or ()),
                preferred_account_id,
            )
            try:
                await self._ensure_initialized(worker)
            except BaseException:
                failed_worker = worker
                worker = None
                async with self._condition:
                    if failed_worker in self._workers:
                        self._workers.remove(failed_worker)
                    self._active_leases = max(0, self._active_leases - 1)
                    self._condition.notify_all()
                await asyncio.shield(self._close_worker(failed_worker))
                raise
            logger.info(
                "[pool] lease acquired worker=%s account=%s active=%d/%d wait=%.3fs",
                worker.id,
                worker.account_id,
                self._active_leases,
                self._max_concurrency,
                time.perf_counter() - queued_at,
            )
            yield RequestClientLease(
                client=worker.client,
                account_id=worker.account_id,
                worker_id=worker.id,
                verified=worker.verified,
            )
        finally:
            try:
                if worker is not None:
                    if worker.discard_on_release:
                        await asyncio.shield(self._discard_worker(worker))
                    else:
                        await asyncio.shield(self._checkin(worker))
                    logger.info(
                        "[pool] lease released worker=%s account=%s active=%d/%d",
                        worker.id,
                        worker.account_id,
                        self._active_leases,
                        self._max_concurrency,
                    )
            finally:
                self._semaphore.release()

    async def _prepare_account(self, account_id: str) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with self.lease(preferred_account_id=account_id) as lease:
                    # Browser warmup already validates the saved Google login.
                    # A synthetic GenerateContent probe polluted AI Studio
                    # history and could consume the account's short-window
                    # quota before the user's first real request.
                    await self.mark_worker_verified(lease.worker_id)
                    return
            except AuthError as exc:
                last_error = exc
                if _is_terminal_permission_error(exc):
                    logger.warning(
                        "账号请求权限验证被明确拒绝，不再重复重建 account=%s: %s",
                        account_id,
                        exc,
                    )
                    break
                logger.warning(
                    "账号请求权限验证失败，重建工作浏览器 %d/3 account=%s: %s",
                    attempt + 1,
                    account_id,
                    exc,
                )
        if last_error is not None:
            raise last_error

    async def prepare_account(self, account_id: str) -> None:
        """Warm and permission-check one real worker for an explicit account."""
        self._preparing_account_ids.add(account_id)
        self._failed_account_errors.pop(account_id, None)
        try:
            await self._prepare_account(account_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed_account_errors[account_id] = str(exc) or type(exc).__name__
            if self._rotator is not None:
                if isinstance(exc, UsageLimitExceeded):
                    self._rotator.record_rate_limited(account_id)
                else:
                    self._rotator.record_error(account_id)
            raise
        finally:
            self._preparing_account_ids.discard(account_id)

    async def prepare_all_accounts(self) -> dict[str, str]:
        """Warm every logged-in account concurrently, isolating failures."""
        # Include damaged/incomplete account records as well so they end in an
        # explicit failed state instead of remaining "initializing" forever.
        account_ids = [account.id for account in self._store.list_accounts()]

        async def prepare_one(account_id: str) -> tuple[str, str | None]:
            try:
                await self.prepare_account(account_id)
                return account_id, None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("账号后台初始化失败 account=%s: %s", account_id, exc)
                return account_id, str(exc) or type(exc).__name__

        results = await asyncio.gather(*(prepare_one(account_id) for account_id in account_ids))
        return {account_id: error for account_id, error in results if error is not None}

    async def _close_worker(self, worker: _Worker) -> None:
        client = worker.client
        if client is None:
            return
        try:
            await client.close()
        except Exception:
            logger.exception("关闭请求 worker 失败: %s", worker.id)

    async def close_account(self, account_id: str) -> None:
        """Drain and close every cached worker for an account before deletion."""
        logger.info("[pool] account drain start account=%s", account_id)
        async with self._condition:
            while any(worker.busy and worker.account_id == account_id for worker in self._workers):
                await self._condition.wait()
            workers = [worker for worker in self._workers if worker.account_id == account_id]
            self._workers = [worker for worker in self._workers if worker.account_id != account_id]
            self._condition.notify_all()
        await asyncio.gather(*(self._close_worker(worker) for worker in workers))
        self._account_init_locks.pop(account_id, None)
        self._preparing_account_ids.discard(account_id)
        self._failed_account_errors.pop(account_id, None)
        logger.info("[pool] account drain done account=%s workers=%d", account_id, len(workers))

    async def close(self) -> None:
        """Stop accepting work, drain active leases, and close every browser."""
        async with self._condition:
            self._closed = True
            self._condition.notify_all()
            while any(worker.busy for worker in self._workers):
                await self._condition.wait()
            workers = list(self._workers)
            self._workers.clear()
        await asyncio.gather(*(self._close_worker(worker) for worker in workers))
        self._account_init_locks.clear()
        self._preparing_account_ids.clear()
        self._failed_account_errors.clear()


__all__ = ["RequestClientLease", "RequestClientPool"]
