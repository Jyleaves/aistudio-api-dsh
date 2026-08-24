"""Runtime configuration helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _user_data_root() -> Path:
    local_appdata = os.getenv("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "Asteria"
    return Path.home() / ".asteria"


USER_DATA_ROOT = _user_data_root()

def ensure_env_file() -> None:
    """Create a minimal local .env on first startup without creating a secret."""
    env_path = USER_DATA_ROOT / ".env"
    if env_path.exists():
        return
    content = (
        "# AI Studio API local configuration\n"
        "# Runtime settings are managed from the web UI.\n"
    )
    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(content, encoding="utf-8")
    except OSError:
        # Read-only deployments can still run with environment defaults.
        pass


# Make first-run configuration self-contained. Never generate an API secret here.
ensure_env_file()
load_dotenv(USER_DATA_ROOT / ".env")


def _repair_moved_project_paths() -> None:
    """Rebase stale absolute paths left by moving or renaming the checkout."""
    candidates = {
        "AISTUDIO_BROWSER_EXECUTABLE": PROJECT_ROOT / "cloakbrowser-chromium" / "chrome.exe",
        "AISTUDIO_ACCOUNTS_DIR": USER_DATA_ROOT / "data" / "accounts",
        "AISTUDIO_TMP_DIR": USER_DATA_ROOT / "data" / "tmp",
        "CLOAKBROWSER_BINARY_PATH": PROJECT_ROOT / "cloakbrowser-chromium" / "chrome.exe",
    }
    browser_path = candidates["AISTUDIO_BROWSER_EXECUTABLE"]
    for name, current in candidates.items():
        raw = os.getenv(name)
        if not raw:
            continue
        configured = Path(raw).expanduser()
        if configured.is_absolute() and not configured.exists() and current.exists():
            os.environ[name] = str(current)

    # CloakBrowser reads its own override directly from the environment. Keep
    # it aligned with the proxy setting after a project folder move.
    executable = os.getenv("AISTUDIO_BROWSER_EXECUTABLE")
    if executable:
        executable_path = Path(executable).expanduser()
        if not executable_path.is_absolute():
            executable_path = PROJECT_ROOT / executable_path
        if executable_path.exists():
            os.environ["CLOAKBROWSER_BINARY_PATH"] = str(executable_path.resolve())


_repair_moved_project_paths()


def resolve_project_path(value: str | None, default: str) -> str:
    """Resolve relative paths against the repository, not the launch directory."""
    candidate = Path(value or default).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return str(candidate.resolve())


def resolve_runtime_path(value: str | None, default: str) -> str:
    """Resolve writable runtime paths under the per-user Asteria data root."""
    candidate = Path(value or default).expanduser()
    if not candidate.is_absolute():
        candidate = USER_DATA_ROOT / candidate
    return str(candidate.resolve())

DEFAULT_TEXT_MODEL = os.getenv("AISTUDIO_DEFAULT_TEXT_MODEL", "gemini-3.7-flash")
DEFAULT_IMAGE_MODEL = os.getenv("AISTUDIO_DEFAULT_IMAGE_MODEL", "gemini-3.1-flash-image-preview")
DEFAULT_BROWSER_PORT = 9222


def _load_browser_engine() -> str:
    """Load browser engine name.

    Supported values:
    - chromium: discover an installed browser (CloakBrowser bundle, then
      system Chrome/Edge); no download required (default)
    - cloakbrowser: always use the cloakbrowser package (auto-download)
    - camoufox: Camoufox Firefox-based backend
    """
    value = (os.getenv("AISTUDIO_BROWSER", "chromium") or "chromium").strip().lower()
    if value not in {"camoufox", "chromium", "cloakbrowser"}:
        return "chromium"
    return value


def _load_login_browser() -> str:
    """Pick which browser the interactive Google login window uses.

    Supported values:
    - chromium: use the Playwright Chromium bundled with the desktop app (default)
    - auto: system Chrome/Edge first, retained for existing configurations
    - system: system Chrome/Edge only
    - cloakbrowser: stealth Chromium via cloakbrowser
    """
    value = (os.getenv("AISTUDIO_LOGIN_BROWSER", "chromium") or "chromium").strip().lower()
    if value not in {"auto", "chromium", "system", "cloakbrowser"}:
        return "chromium"
    return value


def _load_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    return None


def _load_bool_env(*names: str, default: bool) -> bool:
    value = _load_env(*names)
    if value is None:
        return default
    return value not in ("0", "false", "False")


def _load_int_env(*names: str, default: int) -> int:
    value = _load_env(*names)
    if value is None:
        return default
    return int(value)


def _parse_api_keys(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()

    keys: list[str] = []
    for line in raw.splitlines():
        for part in line.split(","):
            key = part.strip()
            if key and not key.lower().startswith(("replace-with-", "change-this-")) and key not in keys:
                keys.append(key)
    return tuple(keys)


def _load_api_keys() -> frozenset[str]:
    values: list[str] = []
    for name in ("AISTUDIO_API_KEY", "AISTUDIO_API_KEYS"):
        for key in _parse_api_keys(os.getenv(name)):
            if key not in values:
                values.append(key)
    return frozenset(values)


def _default_chromium_sandbox() -> bool:
    # On many recent Linux distros (including Ubuntu with AppArmor userns
    # restrictions), Chromium sandboxing is unavailable unless the host has been
    # explicitly configured for it. Default to disabled there so the browser can
    # actually start; keep enabled elsewhere.
    return os.name != "posix" or os.uname().sysname != "Linux"

_AUTH_SEARCH_ROOTS = [
        USER_DATA_ROOT / "data",
]


def discover_auth_file() -> str | None:
    override = os.getenv("AISTUDIO_AUTH_FILE")
    if override:
        return resolve_runtime_path(override, "data/auth.json")

    for root in _AUTH_SEARCH_ROOTS:
        if not root.is_dir():
            continue
        # 优先从 registry.json 读取活跃账号
        registry_path = root / "accounts" / "registry.json"
        if registry_path.exists():
            try:
                import json
                registry = json.loads(registry_path.read_text())
                active_id = registry.get("active_account_id")
                if active_id:
                    auth_path = root / "accounts" / active_id / "auth.json"
                    if auth_path.exists():
                        return str(auth_path)
            except (json.JSONDecodeError, KeyError):
                pass
        # 回退：扫描 data/ 目录下的 .json 文件
        for file in root.iterdir():
            if file.suffix == ".json":
                return str(file)
    return None


def discover_proxy_url() -> str | None:
    return (
        os.getenv("AISTUDIO_PROXY")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("http_proxy")
    )


def build_browser_proxy(proxy_url: str | None) -> dict[str, str] | None:
    if not proxy_url:
        return None

    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        return None

    proxy: dict[str, str] = {
        "server": f"{parsed.scheme}://{parsed.hostname}",
    }
    if parsed.port:
        proxy["server"] += f":{parsed.port}"
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


def build_camoufox_proxy(proxy_url: str | None) -> dict[str, str] | None:
    """Backward-compatible alias for old imports."""
    return build_browser_proxy(proxy_url)


@dataclass(slots=True)
class Settings:
    port: int = int(os.getenv("AISTUDIO_PORT", "8080"))
    browser_engine: str = _load_browser_engine()
    browser_port: int = _load_int_env("AISTUDIO_BROWSER_PORT", "AISTUDIO_CAMOUFOX_PORT", default=DEFAULT_BROWSER_PORT)
    browser_headless: bool = _load_bool_env("AISTUDIO_BROWSER_HEADLESS", "AISTUDIO_CAMOUFOX_HEADLESS", default=True)
    browser_channel: str | None = os.getenv("AISTUDIO_BROWSER_CHANNEL")
    browser_executable_path: str | None = (
        resolve_project_path(os.getenv("AISTUDIO_BROWSER_EXECUTABLE"), "")
        if os.getenv("AISTUDIO_BROWSER_EXECUTABLE") else None
    )
    browser_chromium_sandbox: bool = _load_bool_env(
        "AISTUDIO_CHROMIUM_SANDBOX",
        default=_default_chromium_sandbox(),
    )
    browser_python: str | None = _load_env("AISTUDIO_BROWSER_PYTHON", "AISTUDIO_CAMOUFOX_PYTHON")
    login_browser: str = _load_login_browser()
    login_browser_channel: str | None = os.getenv("AISTUDIO_LOGIN_BROWSER_CHANNEL") or None
    login_profile_dir: str = resolve_runtime_path(
        os.getenv("AISTUDIO_LOGIN_PROFILE_DIR"), "data/login-profile"
    )
    auth_file: str | None = discover_auth_file()
    tmp_dir: str = resolve_runtime_path(os.getenv("AISTUDIO_TMP_DIR"), "data/tmp")
    proxy_url: str | None = discover_proxy_url()
    api_keys: frozenset[str] = _load_api_keys()
    api_key_store_path: str = resolve_runtime_path(
        os.getenv("AISTUDIO_API_KEY_STORE"), "data/api_keys.json"
    )
    local_ui_auto_login: bool = _load_bool_env(
        "AISTUDIO_LOCAL_UI_AUTO_LOGIN", default=True
    )
    timeout_replay: int = int(os.getenv("AISTUDIO_TIMEOUT_REPLAY", "120"))
    timeout_stream: int = int(os.getenv("AISTUDIO_TIMEOUT_STREAM", "120"))
    timeout_capture: int = int(os.getenv("AISTUDIO_TIMEOUT_CAPTURE", "30"))
    snapshot_cache_ttl: int = int(os.getenv("AISTUDIO_SNAPSHOT_CACHE_TTL", "3600"))
    snapshot_cache_max: int = int(os.getenv("AISTUDIO_SNAPSHOT_CACHE_MAX", "100"))
    dump_raw_response: bool = os.getenv("AISTUDIO_DUMP_RAW_RESPONSE", "0") in ("1", "true", "True")
    dump_raw_response_dir: str = resolve_runtime_path(
        os.getenv("AISTUDIO_DUMP_RAW_RESPONSE_DIR"), "data/raw"
    )
    accounts_dir: str = resolve_runtime_path(
        os.getenv("AISTUDIO_ACCOUNTS_DIR"), "data/accounts"
    )
    # 账号轮询配置
    account_rotation_mode: str = os.getenv("AISTUDIO_ACCOUNT_ROTATION_MODE", "round_robin")  # round_robin, lru, least_rl
    account_cooldown_seconds: int = int(os.getenv("AISTUDIO_ACCOUNT_COOLDOWN_SECONDS", "60"))
    account_max_retries: int = int(os.getenv("AISTUDIO_ACCOUNT_MAX_RETRIES", "3"))
    max_concurrency: int = int(os.getenv("AISTUDIO_MAX_CONCURRENCY", "3"))
    default_text_model: str = os.getenv("AISTUDIO_DEFAULT_TEXT_MODEL", DEFAULT_TEXT_MODEL)
    default_image_model: str = os.getenv("AISTUDIO_DEFAULT_IMAGE_MODEL", DEFAULT_IMAGE_MODEL)

    @property
    def camoufox_port(self) -> int:
        return self.browser_port

    @property
    def camoufox_headless(self) -> bool:
        return self.browser_headless

    @property
    def camoufox_python(self) -> str | None:
        return self.browser_python

    @property
    def auth_enabled(self) -> bool:
        # The local management UI must remain reachable before the first Key is
        # created. Protected API routes still reject remote requests without a
        # valid Key (see api.dependencies.require_api_key).
        return self.local_ui_auto_login or bool(self.api_keys) or Path(self.api_key_store_path).exists()


settings = Settings()
