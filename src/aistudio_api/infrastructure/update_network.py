"""Network routing shared by Asteria and companion-component updates."""

from __future__ import annotations

import os
import urllib.request
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse


_STANDARD_PROXY_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


@dataclass(frozen=True, slots=True)
class UpdateNetworkRoute:
    proxy_url: str | None
    proxy_mode: str


def _normalise_proxy_url(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.hostname:
        return None
    return raw


def _windows_system_proxy() -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return None
            value, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None

    server = str(value or "").strip()
    if not server:
        return None
    if "=" in server:
        entries: dict[str, str] = {}
        for item in server.split(";"):
            protocol, separator, address = item.partition("=")
            if separator and address.strip():
                entries[protocol.strip().lower()] = address.strip()
        server = entries.get("https") or entries.get("http") or ""
    return _normalise_proxy_url(server)


def resolve_update_network(
    configured_proxy: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    system_proxy: str | None = None,
) -> UpdateNetworkRoute:
    """Resolve a download route without exposing the selected proxy address."""

    env = os.environ if environ is None else environ
    configured = _normalise_proxy_url(configured_proxy)
    if configured:
        app_proxy = _normalise_proxy_url(env.get("AISTUDIO_PROXY"))
        standard_values = {_normalise_proxy_url(env.get(key)) for key in _STANDARD_PROXY_KEYS}
        mode = "environment" if configured in standard_values and configured != app_proxy else "asteria"
        return UpdateNetworkRoute(configured, mode)

    app_proxy = _normalise_proxy_url(env.get("AISTUDIO_PROXY"))
    if app_proxy:
        return UpdateNetworkRoute(app_proxy, "asteria")
    for key in _STANDARD_PROXY_KEYS:
        value = _normalise_proxy_url(env.get(key))
        if value:
            return UpdateNetworkRoute(value, "environment")

    windows_proxy = _normalise_proxy_url(system_proxy) if system_proxy is not None else _windows_system_proxy()
    if windows_proxy:
        return UpdateNetworkRoute(windows_proxy, "system")
    return UpdateNetworkRoute(None, "direct")


def open_update_url(request: urllib.request.Request, timeout: int, route: UpdateNetworkRoute):
    if not route.proxy_url or route.proxy_mode in {"environment", "system"}:
        return urllib.request.urlopen(request, timeout=timeout)
    handler = urllib.request.ProxyHandler({"http": route.proxy_url, "https": route.proxy_url})
    return urllib.request.build_opener(handler).open(request, timeout=timeout)


def update_subprocess_environment(route: UpdateNetworkRoute) -> dict[str, str]:
    env = os.environ.copy()
    if not route.proxy_url:
        return env
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "npm_config_proxy",
        "npm_config_https_proxy",
    ):
        env[key] = route.proxy_url
    return env


def proxy_mode_label(mode: str) -> str:
    return {
        "asteria": "Asteria 代理",
        "environment": "环境代理",
        "system": "系统代理",
        "direct": "直连",
    }.get(mode, "自动")
