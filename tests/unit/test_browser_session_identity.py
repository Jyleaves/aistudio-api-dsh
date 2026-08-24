from types import SimpleNamespace

import pytest

from aistudio_api.infrastructure.gateway.session import BrowserSession


def _session_fixture(tmp_path):
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    auth_file = account_dir / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    (account_dir / "meta.json").write_text(
        '{"id":"acc_test","email":"expected@example.com"}',
        encoding="utf-8",
    )
    profile_dir = account_dir / "profile"
    profile_dir.mkdir()
    session = BrowserSession.__new__(BrowserSession)
    session._auth_file = str(auth_file)
    session._profile_dir = str(profile_dir)
    return session, profile_dir


def test_identity_check_does_not_delete_profile_for_unrelated_page_email(tmp_path):
    session, profile_dir = _session_fixture(tmp_path)
    page = SimpleNamespace(
        url="https://aistudio.google.com/app/prompts/new_chat",
        content=lambda: "Some rendered page text other@example.com",
    )

    # AI Studio may render a stale/secondary email in the DOM while the
    # authenticated page is valid. This must not destroy the fresh profile.
    session._verify_account_identity_sync(page)
    assert profile_dir.exists()


def test_identity_check_rejects_google_signin_and_removes_profile(tmp_path):
    session, profile_dir = _session_fixture(tmp_path)
    page = SimpleNamespace(
        url="https://accounts.google.com/v3/signin/identifier",
        content=lambda: "Google sign in other@example.com",
    )

    with pytest.raises(RuntimeError, match="页面未登录"):
        session._verify_account_identity_sync(page)
    assert not profile_dir.exists()
