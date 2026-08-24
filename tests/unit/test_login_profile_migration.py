"""Regression tests for preserving Google persistent login profiles."""

from pathlib import Path

from aistudio_api.infrastructure.account.login_service import _copy_login_profile_to_account


def test_completed_login_profile_is_copied_after_browser_closes(tmp_path):
    source = tmp_path / "login-profile"
    source.mkdir()
    (source / "Local State").write_text("state", encoding="utf-8")
    (source / "Default").mkdir()
    (source / "Default" / "Cookies").write_bytes(b"cookies")
    target = tmp_path / "accounts" / "acc_1" / "profile"

    class Store:
        def get_profile_path(self, account_id):
            assert account_id == "acc_1"
            return target

    _copy_login_profile_to_account(source, Store(), "acc_1")

    assert (target / "Local State").read_text(encoding="utf-8") == "state"
    assert (target / "Default" / "Cookies").read_bytes() == b"cookies"


def test_profile_copy_failure_does_not_raise(tmp_path):
    source = tmp_path / "missing-profile"

    class Store:
        def get_profile_path(self, account_id):
            return tmp_path / "target"

    _copy_login_profile_to_account(source, Store(), "acc_1")
