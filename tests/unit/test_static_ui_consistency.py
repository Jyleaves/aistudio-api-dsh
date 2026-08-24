"""Static UI contracts for the packaged desktop application."""

from pathlib import Path


STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "aistudio_api" / "static"


def test_destructive_actions_use_the_shared_confirm_dialog():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "confirm(" not in script
    assert "await this.askConfirm('清理本地缓存'" in script
    assert "await this.askConfirm('删除 API Key'" in script
    assert "await this.askConfirm('安装更新'" in script
    assert "targetLabel" in script
    assert "hint" in script


def test_primary_pages_use_shared_visual_components():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "AI Studio Proxy" not in html
    assert html.count('class="page-header"') >= 5
    assert 'class="settings-grid"' in html
    assert 'class="check-row"' in html
    assert 'class="empty-state"' in html
    assert "confirmDialog.targetLabel" in html
    assert "confirmDialog.hint" in html


def test_integration_guide_covers_all_supported_protocols_and_current_origin():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "接入指南" in html
    assert "OpenAI 兼容协议" in html
    assert "Gemini 原生协议" in html
    assert "Anthropic 兼容协议" in html
    assert "x-goog-api-key" in html
    assert "Authorization: Bearer" in html
    assert "x-api-key" in html
    assert "gemini-3.7-flash" in html
    assert "return window.location.origin" in script
    assert "navigator.clipboard.writeText" in script


def test_login_polling_keeps_existing_active_account_and_recovers_transient_errors():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "this.loginPreviousActiveId = this.activeId" in script
    assert "transientFailures < 6" in script
    assert "this.prepareAccount(d.account_id, false)" in script
    assert "`/accounts/${id}/prepare`" in script
    assert "当前账号未切换" not in script


def test_account_pool_ui_has_no_manual_activation_or_standby_state():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "手动激活" not in html
    assert ">待命<" not in html
    assert "当前激活账号" not in html
    assert "登录后会自动加入可用账号池" in html
    assert "(this.requestPoolStatus.ready_accounts || []).includes(a.id) ? 'available' : 'initializing'" in script
    assert "get hasAccountPreparations()" in script
    assert "get hasInitializingAccounts()" in script
    assert "failed_accounts" in script


def test_account_scheduling_is_automatic_and_hidden_from_users():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "轮询模式" not in html
    assert "轮询设置" not in html
    assert "账号使用顺序" not in html
    assert "账号冷却时间" not in html
    assert "最大重试次数" not in html
    assert "最大并发数" not in html
    assert "/rotation/mode" not in script
    assert "loadAccountMetrics" in script
    assert "可用账号" in html


def test_logout_hides_account_immediately_and_restores_state_on_failure():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    logout_start = script.index("async logoutAccount(account)")
    logout_end = script.index("openAccountEdit(account)", logout_start)
    logout = script[logout_start:logout_end]

    assert logout.index("this.accounts = this.accounts.filter") < logout.index("this.apiFetch(")
    assert "正在退出登录并清理本地数据" in logout
    assert logout.count("await this.refreshAccountData()") >= 2


def test_all_accounts_are_monitored_while_streaming_has_visible_feedback():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "this.monitorAccountWarmup()" in script
    assert "ensureActiveAccountReady" not in script
    assert "ready_accounts" in script
    assert "正在准备账号和模型…" in script
    assert "正在等待模型回复…" in script
    assert "this.msgs[idx] = {" in script
    assert "重试初始化" in html
