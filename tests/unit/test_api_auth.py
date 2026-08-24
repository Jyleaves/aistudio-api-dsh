from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from aistudio_api.api.dependencies import require_api_key
from aistudio_api.config import settings


def _build_client() -> TestClient:
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    async def protected():
        return {"ok": True}

    return TestClient(app)


def _use_empty_key_store(monkeypatch, tmp_path):
    """把全局 ApiKeyStore 换成指向临时路径的空 store。"""
    from aistudio_api.infrastructure.auth.api_key_store import ApiKeyStore

    store = ApiKeyStore(tmp_path / "api_keys.json")
    monkeypatch.setattr(settings, "api_key_store_path", str(tmp_path / "api_keys.json"))
    # require_api_key 通过 dependencies 模块引用 get_api_key_store，
    # 因此 patch 需要落在 dependencies 的命名空间。
    monkeypatch.setattr(
        "aistudio_api.api.dependencies.get_api_key_store",
        lambda: store,
    )
    return store


def test_auth_is_disabled_when_no_api_key_is_configured(monkeypatch, tmp_path):
    _use_empty_key_store(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "api_keys", frozenset())
    monkeypatch.setattr(settings, "local_ui_auto_login", False)
    # auth_enabled 还会检查 key store 文件是否存在，指向临时路径隔离
    monkeypatch.setattr(settings, "api_key_store_path", str(tmp_path / "api_keys.json"))
    client = _build_client()

    response = client.get("/protected")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_bearer_token_is_accepted(monkeypatch, tmp_path):
    store = _use_empty_key_store(monkeypatch, tmp_path)
    _, secret = store.create("test")
    client = _build_client()

    response = client.get("/protected", headers={"Authorization": f"Bearer {secret}"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_x_api_key_is_accepted(monkeypatch, tmp_path):
    store = _use_empty_key_store(monkeypatch, tmp_path)
    _, secret = store.create("test")
    client = _build_client()

    response = client.get("/protected", headers={"X-API-Key": secret})

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_google_api_key_header_is_accepted(monkeypatch, tmp_path):
    store = _use_empty_key_store(monkeypatch, tmp_path)
    _, secret = store.create("test")
    client = _build_client()

    response = client.get("/protected", headers={"X-Goog-Api-Key": secret})

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_missing_or_invalid_api_key_returns_401(monkeypatch, tmp_path):
    store = _use_empty_key_store(monkeypatch, tmp_path)
    store.create("test")
    monkeypatch.setattr(settings, "local_ui_auto_login", False)
    client = _build_client()

    response = client.get("/protected")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"]["type"] == "authentication_error"
