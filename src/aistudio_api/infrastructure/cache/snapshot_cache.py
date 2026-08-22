"""In-memory snapshot cache."""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from typing import Optional

from aistudio_api.config import settings

logger = logging.getLogger("aistudio")


class SnapshotCache:
    def __init__(self, ttl: int | None = None, max_size: int | None = None):
        self._cache: OrderedDict[str, tuple] = OrderedDict()
        self._namespace = "default"
        self.ttl = ttl or settings.snapshot_cache_ttl
        self.max_size = max_size or settings.snapshot_cache_max

    def set_namespace(self, namespace: str | None) -> None:
        """隔离不同 Google 账号的 snapshot，避免切号后复用错误会话。"""
        self._namespace = namespace or "default"

    def _hash(self, prompt: str) -> str:
        value = f"{self._namespace}\n{prompt}"
        return hashlib.sha256(value.encode()).hexdigest()

    def get(self, prompt: str) -> Optional[tuple]:
        key = self._hash(prompt)
        if key not in self._cache:
            return None

        snapshot, url, headers, body, ts = self._cache[key]
        age = time.time() - ts
        if age >= self.ttl:
            del self._cache[key]
            logger.info("Snapshot 缓存过期: %s...", key[:8])
            return None

        # LRU: move to end
        self._cache.move_to_end(key)
        logger.info("Snapshot 缓存命中: %s... (%ss ago)", key[:8], int(age))
        return snapshot, url, headers, body

    def put(self, prompt: str, snapshot: str, url: str, headers: dict, body: str):
        key = self._hash(prompt)
        # Evict oldest if at capacity
        while len(self._cache) >= self.max_size:
            evicted_key, _ = self._cache.popitem(last=False)
            logger.info("Snapshot 缓存淘汰: %s...", evicted_key[:8])
        self._cache[key] = (snapshot, url, headers, body, time.time())
        logger.info("Snapshot 已缓存: %s...", key[:8])

    def clear(self):
        self._cache.clear()
