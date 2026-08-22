# Windows 本地部署说明

完整安装步骤、dsh 字段填写和模型配置见项目根目录的 [README.md](README.md)。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

双击 `start-aistudio-api.bat` 启动，访问 `http://127.0.0.1:8090`：在“API Key 管理”创建 Key，点击“添加账号”完成 Google 登录。

dsh 插件安装并重启 dsh：

```powershell
dsh plugin --profile web add https://github.com/Jyleaves/dsh-gemini-aistudio.git
```

更新：`update-aistudio-api.ps1 -Restart`（反代）、`update-dsh-gemini-aistudio.ps1`（插件）。

## Windows v1.0.0 安装包

本地构建需要 Python 虚拟环境、PyInstaller 和 Inno Setup 7：

```powershell
.\build-windows.ps1
```

产物为 `dist\Asteria-setup-1.0.0.exe`。安装器支持选择安装目录，安装完成后可从桌面或开始菜单双击启动；管理网页会直接显示在应用窗口中，不需要手动打开浏览器或运行 Python。安装包内置浏览器组件，支持离线安装，当前安装包约 187 MB。推送 `v1.0.0` 标签后，GitHub Actions 会自动构建并将安装包上传到 Release。
