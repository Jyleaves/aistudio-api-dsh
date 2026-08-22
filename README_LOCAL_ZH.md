# Windows 本地部署说明

本文件说明如何在 Windows 上使用本项目，并将它作为 `pi-web` 的 Gemini 反代。

## 项目来源与许可

本项目基于 [chrysoljq/aistudio-api](https://github.com/chrysoljq/aistudio-api)，上游项目采用 MIT License。本仓库保留上游许可证和项目结构，并额外包含本地部署所需的 Windows 兼容修复、`gemini-3.7-flash` 模型支持、Windows 图片临时目录修复和启动脚本。

使用、再发布或二次修改时，请保留上游版权声明和 MIT License。这里的本地修改不代表 Google 官方，也不代表上游作者。

## 一、准备环境

建议使用 Python 3.11 或更高版本，并使用虚拟环境，不要把依赖安装到系统 Python 或 base 环境：

```powershell
# 在项目根目录打开 PowerShell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install playwright cloakbrowser
```

`cloakbrowser` 需要一个专用的 Chromium 浏览器。不要把这个浏览器文件提交到 GitHub；请从其发布页下载对应版本并解压到项目目录，例如：

```text
cloakbrowser-chromium\chrome.exe
```

下载地址：

<https://github.com/CloakHQ/cloakbrowser/releases>

## 二、配置环境变量

复制 `.env.example` 为 `.env`，再按本机路径修改。Windows 本地配置示例：

```dotenv
AISTUDIO_PORT=8090
AISTUDIO_BROWSER=chromium
AISTUDIO_BROWSER_HEADLESS=1
AISTUDIO_BROWSER_EXECUTABLE=cloakbrowser-chromium\chrome.exe
AISTUDIO_API_KEY=change-this-to-a-random-token
AISTUDIO_ACCOUNTS_DIR=data\accounts
AISTUDIO_TMP_DIR=data\tmp
```

`AISTUDIO_API_KEY` 是本地反代访问 Token，不是 Google Gemini API Key。请改成自己的随机字符串，不要把 `.env` 提交到公开仓库。

首次登录时，如需看到浏览器窗口，可临时设置：

```dotenv
AISTUDIO_BROWSER_HEADLESS=0
```

登录完成后建议改回 `1`，这样服务运行时不会重复弹出浏览器窗口。

## 三、启动反代

命令行启动：

```powershell
# 在项目根目录执行
.\.venv\Scripts\python.exe main.py server --port 8090
```

也可以直接双击项目内的 `start-aistudio-api.bat`。服务启动后访问：

```text
http://127.0.0.1:8090
```

本机打开管理页会自动建立本地管理会话，不需要每次输入 API Key；从其他设备访问时输入 `.env` 中配置的 `AISTUDIO_API_KEY`。然后添加并登录 Google 账号。账号 Cookie 会保存在 `data/accounts/`，该目录已被 Git 忽略。

“API Key 管理”页面支持创建和撤销 Key。新 Key 明文只显示一次，撤销最后一个有效 Key 会被阻止。

可以同时保留多个有效 API Key。创建新 Key 不会自动使旧 Key 失效；需要失效某个 Key 时，在列表中单独撤销即可。

## 四、配置 pi-web

在 `~/.pi/agent/models.json` 中添加一个 OpenAI 兼容供应商：

```json
{
  "providers": {
    "gemini-aistudio": {
      "api": "openai-completions",
      "apiKey": "change-this-to-a-random-token",
      "baseUrl": "http://127.0.0.1:8090/v1",
      "models": [
        {
          "id": "gemini-3.7-flash",
          "name": "Gemini 3.7 Flash (AI Studio)",
          "reasoning": true,
          "input": ["text", "image"],
          "contextWindow": 1000000,
          "maxTokens": 65536
        }
      ]
    }
  }
}
```

保存后重启或刷新 pi-web，在模型选择器中选择 `gemini-aistudio` 下的模型。图片可以直接从 pi-web 上传，反代会转换为 Gemini 请求格式。

pi-web 的模型配置面板支持“发现模型”，它会读取反代的 `/v1/models`。模型发现是手动触发的，不会持续自动修改本地 `models.json`。

## 五、配置 dsh

安装独立的 `dsh-gemini-aistudio` 插件。以下命令中的路径替换为插件实际所在目录：

```powershell
dsh plugin --profile web add E:\Project\Codex\dsh-gemini-aistudio
$env:AISTUDIO_API_KEY = "与反代中某个有效 API Key 相同"
```

重启 dsh 后选择 provider `aistudio-gemini`。插件会从反代 `/v1/models` 自动获取 Gemini 模型，并自动读取上下文窗口、最大输出 Token、图片输入和 reasoning 能力。反代更新模型后，重启 dsh 或重新加载 provider 即可刷新。

插件支持原图/PDF 上传、PDF 路径识别和 Google Search。dsh 的自定义工具请求会走反代的 OpenAI 兼容接口，普通 Gemini 请求走原生 Gemini 接口。

## 五、测试接口

查看模型列表：

```powershell
$headers = @{ Authorization = "Bearer change-this-to-a-random-token" }
Invoke-RestMethod http://127.0.0.1:8090/v1/models -Headers $headers
```

测试文本请求：

```powershell
$headers = @{
  Authorization = "Bearer change-this-to-a-random-token"
  "Content-Type" = "application/json"
}
$body = @{
  model = "gemini-3.7-flash"
  messages = @(@{ role = "user"; content = "你好，只回复 OK" })
  stream = $false
} | ConvertTo-Json -Depth 8

Invoke-RestMethod http://127.0.0.1:8090/v1/chat/completions `
  -Method Post -Headers $headers -Body $body
```

## 六、更新项目

双击 `update-aistudio-api.bat`，或在项目根目录执行：

```powershell
.\update-aistudio-api.ps1 -Restart
```

脚本会检查未提交修改、拉取当前分支的上游提交，并在 `.venv` 中更新依赖；`.env`、`data\`、`.venv\` 和浏览器文件不需要重新配置。存在未提交代码修改时会停止，避免覆盖本地修复。

## 安全注意事项

- 不要公开 `.env`、`data/accounts/`、Cookie、API Token 或日志。
- 不要把 `cloakbrowser-windows-x64.zip` 或解压后的浏览器提交到 GitHub。
- 如果公开部署服务，必须使用 HTTPS、访问控制和防火墙；本地 Token 不能替代网络安全措施。
- Google AI Studio 的可用性、账号权限、限额和服务条款由 Google 决定；本项目不是 Google 官方项目。
