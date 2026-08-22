"""Persistent API key management for the local proxy."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aistudio_api.config import settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return not lowered or lowered.startswith(("replace-with-", "change-this-"))


class ApiKeyStore:
    """Stores only API key hashes and metadata; secrets are returned once."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or settings.api_key_store_path)
        self._lock = threading.RLock()
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("keys"), list):
                    self._data = raw
            except (OSError, json.JSONDecodeError):
                pass
        if self._data is None:
            self._data = {"version": 1, "keys": []}
        return self._data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def _record(secret: str, name: str) -> dict[str, Any]:
        return {
            "id": f"key_{secrets.token_hex(6)}",
            "name": name.strip() or "未命名 Key",
            "prefix": secret[:16],
            "hash": _hash_key(secret),
            "created_at": _now(),
            "last_used": None,
            "revoked_at": None,
        }

    def ensure(self) -> str | None:
        """Initialize the store and import explicitly configured legacy keys.

        A first run intentionally creates no API secret. Users create one in
        the web UI, where the plaintext is shown exactly once.
        """
        with self._lock:
            data = self._load()
            changed = False
            known_hashes = {item.get("hash") for item in data["keys"]}
            existing = [key for key in settings.api_keys if not _is_placeholder(key)]
            if existing:
                for key in sorted(existing):
                    if _hash_key(key) not in known_hashes:
                        data["keys"].append(self._record(key, "环境变量 API Key"))
                        known_hashes.add(_hash_key(key))
                        changed = True
            if changed or not self.path.exists():
                self._save()
            return None

    def verify(self, secret: str | None) -> bool:
        if not secret:
            return False
        with self._lock:
            self._load()
            digest = _hash_key(secret.strip())
            return any(
                not item.get("revoked_at") and hmac.compare_digest(item.get("hash", ""), digest)
                for item in self._load()["keys"]
            )

    def list_public(self) -> list[dict[str, Any]]:
        with self._lock:
            self.ensure()
            return [
                {key: item.get(key) for key in ("id", "name", "prefix", "created_at", "last_used", "revoked_at")}
                | {"active": not bool(item.get("revoked_at"))}
                for item in self._load()["keys"]
            ]

    def create(self, name: str | None = None) -> tuple[dict[str, Any], str]:
        with self._lock:
            self.ensure()
            secret = "sk-aistudio-" + secrets.token_urlsafe(32)
            item = self._record(secret, name or "API Key")
            self._load()["keys"].append(item)
            self._save()
            return (
                {key: item.get(key) for key in ("id", "name", "prefix", "created_at", "last_used", "revoked_at")}
                | {"active": True},
                secret,
            )

    def revoke(self, key_id: str) -> dict[str, Any]:
        with self._lock:
            self.ensure()
            items = self._load()["keys"]
            target = next((item for item in items if item.get("id") == key_id), None)
            if target is None:
                raise KeyError(key_id)
            if target.get("revoked_at"):
                return target
            active_count = sum(1 for item in items if not item.get("revoked_at"))
            if active_count <= 1:
                raise ValueError("不能撤销最后一个有效 API Key，请先创建新 Key")
            target["revoked_at"] = _now()
            self._save()
            return target

    def rotate(self, name: str | None = None) -> tuple[dict[str, Any], str]:
        with self._lock:
            self.ensure()
            created, secret = self.create(name or "轮换 API Key")
            return created, secret


_store = ApiKeyStore()


def get_api_key_store() -> ApiKeyStore:
    return _store


_local_sessions: dict[str, float] = {}
_local_session_lock = threading.RLock()


def create_local_session(ttl_seconds: int = 86400) -> str:
    token = secrets.token_urlsafe(32)
    with _local_session_lock:
        _local_sessions[token] = datetime.now(timezone.utc).timestamp() + ttl_seconds
    return token


def is_local_session_valid(token: str | None) -> bool:
    if not token:
        return False
    with _local_session_lock:
        expires_at = _local_sessions.get(token)
        if expires_at is None:
            return False
        if expires_at <= datetime.now(timezone.utc).timestamp():
            _local_sessions.pop(token, None)
            return False
        return True


def revoke_local_session(token: str | None) -> None:
    if token:
        with _local_session_lock:
            _local_sessions.pop(token, None)
