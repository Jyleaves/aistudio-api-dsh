"""Proxy selection for application and plugin updates."""

import urllib.request
from types import SimpleNamespace

from aistudio_api.api import routes_system
from aistudio_api.config import settings
from aistudio_api.infrastructure import update_network as network_module
from aistudio_api.infrastructure.update_network import (
    UpdateNetworkRoute,
    open_update_url,
    resolve_update_network,
    update_subprocess_environment,
)


def test_configured_proxy_has_priority_and_is_normalised():
    route = resolve_update_network(
        "127.0.0.1:7890",
        environ={"HTTPS_PROXY": "http://127.0.0.1:8888"},
        system_proxy="http://127.0.0.1:9999",
    )

    assert route == UpdateNetworkRoute("http://127.0.0.1:7890", "asteria")


def test_environment_proxy_precedes_windows_system_proxy():
    route = resolve_update_network(
        None,
        environ={"https_proxy": "http://127.0.0.1:8888"},
        system_proxy="http://127.0.0.1:9999",
    )

    assert route == UpdateNetworkRoute("http://127.0.0.1:8888", "environment")


def test_windows_system_proxy_is_used_when_no_other_proxy_exists():
    route = resolve_update_network(None, environ={}, system_proxy="127.0.0.1:7890")

    assert route == UpdateNetworkRoute("http://127.0.0.1:7890", "system")


def test_direct_route_is_reported_when_no_proxy_exists():
    assert resolve_update_network(None, environ={}, system_proxy="") == UpdateNetworkRoute(None, "direct")


def test_plugin_subprocess_receives_both_git_and_pnpm_proxy_variables():
    env = update_subprocess_environment(UpdateNetworkRoute("http://127.0.0.1:7890", "asteria"))

    assert env["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert env["npm_config_proxy"] == "http://127.0.0.1:7890"
    assert env["npm_config_https_proxy"] == "http://127.0.0.1:7890"


def test_explicit_asteria_proxy_builds_an_isolated_url_opener(monkeypatch):
    captured = {}
    response = object()

    class _Opener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return response

    def fake_build_opener(handler):
        captured["proxies"] = handler.proxies
        return _Opener()

    monkeypatch.setattr(network_module.urllib.request, "build_opener", fake_build_opener)
    request = urllib.request.Request("https://api.github.com/repos/example/releases/latest")
    route = UpdateNetworkRoute("http://127.0.0.1:7890", "asteria")

    assert open_update_url(request, 15, route) is response
    assert captured["proxies"] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }
    assert captured["request"] is request
    assert captured["timeout"] == 15


def test_source_git_update_inherits_resolved_proxy(monkeypatch):
    captured = {}
    monkeypatch.setattr(settings, "proxy_url", "http://127.0.0.1:7890")

    def fake_run(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(routes_system.subprocess, "run", fake_run)

    routes_system._run_git("fetch", "--prune")

    assert captured["arguments"] == ["git", "fetch", "--prune"]
    assert captured["kwargs"]["env"]["HTTPS_PROXY"] == "http://127.0.0.1:7890"
