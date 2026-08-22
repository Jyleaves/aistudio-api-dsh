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
