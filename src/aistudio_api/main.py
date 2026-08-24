"""Unified CLI entrypoint for local development and installed usage."""

from __future__ import annotations

import argparse
import asyncio


def build_parser() -> argparse.ArgumentParser:
    from aistudio_api.config import settings

    parser = argparse.ArgumentParser(description="AI Studio unified entrypoint")
    subparsers = parser.add_subparsers(dest="command")

    # 无参数 = 桌面应用模式（双击 exe 的默认行为）
    app_parser = subparsers.add_parser("app", help="桌面应用模式（内嵌管理页窗口）")
    app_parser.add_argument("--port", type=int, default=settings.port)

    server_parser = subparsers.add_parser("server", help="启动 OpenAI 兼容 API 服务（控制台模式）")
    server_parser.add_argument("--port", type=int, default=settings.port)
    server_parser.add_argument("--browser-port", type=int, default=settings.browser_port)
    server_parser.add_argument("--camoufox-port", type=int, dest="browser_port", help=argparse.SUPPRESS)

    client_parser = subparsers.add_parser("client", help="发送一次客户端请求")
    client_parser.add_argument("prompt", nargs="?", default="你好", help="用户消息")
    client_parser.add_argument("--model", "-m", help="模型名称")
    client_parser.add_argument("--system", "-s", help="系统指令")
    client_parser.add_argument("--search", action="store_true", help="启用 Google Search")
    client_parser.add_argument("--code", action="store_true", help="启用 Code Execution")
    client_parser.add_argument("--image", action="store_true", help="生图模式")
    client_parser.add_argument("--save", help="图片保存路径")
    client_parser.add_argument("--attach", "-a", nargs="+", help="附加图片（文件路径）")
    client_parser.add_argument("--port", type=int, default=settings.browser_port, help="浏览器调试端口（仅 Camoufox 后端使用）")

    snapshot_parser = subparsers.add_parser("snapshot", help="抓取 snapshot")
    snapshot_parser.add_argument("prompt", nargs="?", default="你好，测试snapshot提取", help="触发用 prompt")

    login_parser = subparsers.add_parser("login", help="通过终端完成 Google 账号登录")
    login_parser.add_argument("--name", help="保存账号时使用的名称")
    login_parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    login_parser.add_argument("--hl", default="en-US", help="Google 登录页语言，默认 en-US")
    login_parser.add_argument("--browser-port", type=int, default=9223, help="登录浏览器调试端口（仅 camoufox 后端使用）")
    login_parser.add_argument("--camoufox-port", type=int, dest="browser_port", help=argparse.SUPPRESS)

    subparsers.add_parser("install-browser", help="下载后台浏览器（稳定版 Chromium，自动使用系统代理）")
    subparsers.add_parser("_packaging-smoke", help=argparse.SUPPRESS)

    return parser


def _windows_system_proxy() -> str | None:
    """读取 Windows 系统代理，供下载浏览器时使用。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return None
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            if not server:
                return None
            if "=" in server:  # "http=...;https=..." 形式取 http 条目
                for entry in server.split(";"):
                    if entry.startswith("http="):
                        server = entry.split("=", 1)[1]
                        break
            if not server.startswith("http"):
                server = f"http://{server}"
            return server
    except Exception:
        return None


def _run_install_browser() -> int:
    """下载稳定版 Chromium；国内环境自动带上系统代理。"""
    import os
    import subprocess
    import sys

    proxy = _windows_system_proxy()
    env = dict(os.environ)
    if proxy:
        print(f"使用系统代理: {proxy}")
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy

    if getattr(sys, "frozen", False):
        # 打包版：没有 -m playwright，改用 playwright 包 API
        from playwright.__main__ import main as playwright_main
        sys.argv = ["playwright", "install", "chromium"]
        try:
            playwright_main()
            return 0
        except SystemExit as exc:
            return int(exc.code or 0)

    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        env=env,
    )
    return result.returncode


def _run_app(port: int) -> None:
    """桌面应用模式：后台启动 API 服务，原生窗口内嵌管理页面。"""
    import logging
    import os
    import socket
    import threading

    # 打包版无控制台，日志落盘方便排查
    from aistudio_api.config import USER_DATA_ROOT

    log_dir = USER_DATA_ROOT / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(log_dir / "app.log", encoding="utf-8")],
        force=True,
    )

    # A desktop instance owns the local API port and its browser session.
    # Starting a second copy on a random fallback port makes the visible UI
    # talk to a different, partially initialized server. Keep a process-wide
    # Windows lock so the second copy exits cleanly instead.
    instance_lock = None
    try:
        lock_path = log_dir / "Asteria.instance.lock"
        instance_lock = open(lock_path, "a+b")
        if os.name == "nt":
            import msvcrt

            instance_lock.seek(0)
            try:
                msvcrt.locking(instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                logging.getLogger("aistudio.server").warning("已有 Asteria 实例正在运行，当前实例退出")
                instance_lock.close()
                return
        logging.getLogger("aistudio.server").info("Asteria 单实例锁已取得")
    except Exception:
        if instance_lock is not None:
            instance_lock.close()
        raise

    def _pick_port(preferred: int) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                raise RuntimeError(f"Asteria 端口 {preferred} 已被占用，请关闭已有实例后重试")

    try:
        actual_port = _pick_port(port)
    except Exception:
        if instance_lock is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    instance_lock.seek(0)
                    msvcrt.locking(instance_lock.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                instance_lock.close()
        raise

    import uvicorn

    from aistudio_api.api.app import app

    # Windowed PyInstaller builds have no console streams. Uvicorn's default
    # logging config calls stderr.isatty(), which crashes when stderr is None.
    # The application logging configured above already writes to data/app.log.
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=actual_port,
        log_level="warning",
        log_config=None,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="aistudio-server")
    thread.start()

    import webview

    window = webview.create_window(
        "Asteria",
        f"http://127.0.0.1:{actual_port}/",
        width=1240,
        height=840,
        min_size=(960, 640),
    )
    from aistudio_api.api.state import runtime_state

    runtime_state.desktop_shutdown = window.destroy
    try:
        webview.start()
    finally:
        runtime_state.desktop_shutdown = None
        server.should_exit = True
        # FastAPI lifespan 会在这里取消登录任务并关闭后台浏览器。给 Chromium
        # 足够时间释放 Windows profile 文件锁，再解除单实例锁并退出进程。
        thread.join(timeout=30)
        if thread.is_alive():
            logging.getLogger("aistudio").error("应用关闭超时：本地服务线程仍未退出")
        if instance_lock is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    instance_lock.seek(0)
                    msvcrt.locking(instance_lock.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                instance_lock.close()


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command in (None, "app"):
        from aistudio_api.config import settings

        _run_app(getattr(args, "port", None) or settings.port)
        return

    if args.command == "_packaging-smoke":
        import webview

        for api_name in ("create_window", "start"):
            if not callable(getattr(webview, api_name, None)):
                raise RuntimeError(f"packaged pywebview is missing {api_name}")
        return

    if args.command == "server":
        from aistudio_api.api.app import main as server_main
        import sys

        sys.argv = ["aistudio-api-server", "--port", str(args.port), "--browser-port", str(args.browser_port)]
        server_main()
        return

    if args.command == "install-browser":
        raise SystemExit(_run_install_browser())

    if args.command == "client":
        from aistudio_api.config import DEFAULT_TEXT_MODEL
        from aistudio_api.infrastructure.gateway.cli import _run_cli

        if not args.model:
            args.model = DEFAULT_TEXT_MODEL
        asyncio.run(_run_cli(args))
        return

    if args.command == "snapshot":
        from aistudio_api.infrastructure.browser.snapshot_extractor import SnapshotExtractor

        async def _run_snapshot():
            extractor = SnapshotExtractor()
            snap = await extractor.extract(args.prompt)
            cookies = extractor.get_cookies()
            print(f"snapshot: {len(snap)} 字符")
            print(f"cookies: {len(cookies or {})} 个")

        asyncio.run(_run_snapshot())
        return

    if args.command == "login":
        from aistudio_api.application.account_service import AccountService
        from aistudio_api.infrastructure.account.account_store import AccountStore
        from aistudio_api.infrastructure.account.login_service import LoginService, LoginStatus

        async def _run_login():
            store = AccountStore()
            service = AccountService(
                account_store=store,
                login_service=LoginService(port=args.browser_port),
            )
            session_id = await service.start_login(
                args.name,
                headless=not args.headed,
                ui_locale=args.hl,
            )
            print(f"session_id: {session_id}")
            print(f"mode: {'headed' if args.headed else 'headless'}")

            last_status = None
            while True:
                session = service.get_login_status(session_id)
                if session is None:
                    raise RuntimeError("登录会话丢失")
                if session.status != last_status:
                    print(f"status: {session.status.value}")
                    last_status = session.status
                if session.status == LoginStatus.COMPLETED:
                    print(f"account_id: {session.account_id}")
                    if session.email:
                        print(f"email: {session.email}")
                    return
                if session.status == LoginStatus.FAILED:
                    raise RuntimeError(session.error or "登录失败")
                await asyncio.sleep(1)

        asyncio.run(_run_login())
        return


if __name__ == "__main__":
    main()
