# AI Studio API for dsh

本项目将 Google AI Studio 转换为 dsh 可使用的 Gemini 接口，支持多账号轮询、图片、PDF、Google Search、思考输出、函数工具和流式响应。

本项目基于 [chrysoljq/aistudio-api](https://github.com/chrysoljq/aistudio-api)，上游采用 MIT License。再发布或修改时请保留上游版权声明和许可证。本项目不是 Google 官方项目。

## 1. 安装反代

要求 Windows 10/11、Python 3.11+ 和可用的 Google 账号。Python 依赖安装在虚拟环境中：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

要求 Windows 10/11、Python 3.11+ 和可用的 Google 账号。Python 依赖安装在虚拟环境中：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

下载稳定版 Chromium 作为反代的后台浏览器（约 130MB，自动安装到 Playwright 目录，需要能访问 Google 的网络环境）：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

反代启动时会自动发现可用的浏览器：上面的 Playwright Chromium → 项目内解压的 CloakBrowser（`cloakbrowser-chromium\chrome.exe`，反检测增强版，可选）→ 系统 Chrome → 系统 Edge。新用户执行了那条命令即可；一台浏览器都没有时，启动日志会给出安装指引。也可以在 `.env` 中用 `AISTUDIO_BROWSER_EXECUTABLE` 指定浏览器路径，或设置 `AISTUDIO_BROWSER=cloakbrowser` 让程序自动下载 CloakBrowser：

<https://github.com/CloakHQ/cloakbrowser/releases>

双击 `start-aistudio-api.bat` 启动，或执行：

```powershell
.\.venv\Scripts\python.exe main.py server --port 8090
```

管理页面：<http://127.0.0.1:8090>

打开管理页面后，进入“API Key 管理 → 新建 Key”，明文只显示一次；dsh 或其他客户端使用这里创建的 Key。点击“添加账号”会自动打开可交互的 Google 登录窗口（默认使用系统 Chrome/Edge，找不到时回退 CloakBrowser），不需要修改无头模式。登录窗口使用持久化登录档案 `data\login-profile\`：首次登录后 Google 会记住账号，之后再添加账号会直接显示账号列表，点一下账号即可完成授权，无需重复输入邮箱、密码和验证码。账号列表中的“退出登录”会删除该账号；最后一个账号退出时同时清除登录档案，“清除登录档案”按钮可单独清空添加账号窗口记住的账号列表。设置页中的无头模式只影响正常请求和后台浏览器。账号 Cookie 保存在 `data\accounts\`，不要提交到 GitHub。

API Key 管理支持同时保留多个有效 Key。创建新 Key 不会使旧 Key 失效；需要使某个 Key 失效时，在列表中单独删除即可。反代的浏览器、代理、账号轮询、并发、超时、缓存、模型默认值和运行目录等可选配置统一在“设置”页面修改，保存到 `.env`；部分配置需要重启反代后生效。高级用户仍可手动编辑 `.env`，但不是必需操作。

## 2. 安装 dsh 插件

在 dsh web profile 中直接从 GitHub 安装：

```powershell
dsh plugin --profile web add https://github.com/Jyleaves/dsh-gemini-aistudio.git
```

重启 dsh。插件 provider ID 是 `aistudio-gemini`，普通 Gemini 请求走原生 Gemini 接口；带有 dsh 函数工具的请求走反代 OpenAI 兼容接口，以保证 `read`、`edit`、`bash` 等工具调用稳定完成。

## 3. 在 dsh 设置中添加提供方

如果 dsh 设置页面没有自动显示插件模型，进入“设置 → 模型 → 自定义提供方”，按下面填写。截图中的字段对应关系如下：

| 界面字段 | 填写值 |
|---|---|
| Provider ID | `gemini-aistudio` |
| 显示名称 | `Google AI Studio` |
| API 地址 | `http://127.0.0.1:8090/v1` |
| API 协议 | `openai-completions` |
| API 密钥 | “API Key 管理”页面创建的有效 API Key |

点击“获取可用模型”。反代只返回 Gemini 开头的模型，`/v1/models` 会同时返回上下文窗口、最大输出 Token、图片输入和 reasoning 元数据，dsh 会自动填充。如果自动获取失败，确认反代已启动、API 地址末尾为 `/v1`，并确认 API 密钥没有多余空格。

插件原生 provider 的模型发现由 dsh 重启或重新加载 provider 触发；自定义提供方页面的“获取可用模型”则读取反代的 `/v1/models`。两者是 dsh 中不同的配置入口。

## 4. dsh 特性

- 插件上传按钮支持原始图片和 PDF，不经过 dsh 图片压缩流程。
- 用户消息中的本地 PDF 路径会被识别并以内联 PDF 发送。
- 普通 Gemini 请求默认启用 Google Search。
- 自定义函数工具请求不会隐式混入 Google Search，避免 Gemini 3 的内置工具配置冲突。
- PDF 默认限制为 20 MiB、300 页；原始上传按钮默认限制为 32 MiB。
- PDF 会缓存本地文件内容，账号轮换重试时不会重复读取和编码文件。

## 5. 更新

反代项目更新：

```powershell
.\update-aistudio-api.ps1 -CheckOnly
.\update-aistudio-api.ps1 -Restart
```

dsh 插件更新：

```powershell
.\update-dsh-gemini-aistudio.ps1 -CheckOnly
.\update-dsh-gemini-aistudio.ps1
```

更新脚本要求 Git 工作区没有未提交修改，然后执行快进更新并重新安装插件依赖。`.env`、账号 Cookie 和本地运行数据不会被更新覆盖。

## 6. 安全与许可证

不要公开 `.env`、`data\accounts\`、Cookie、API Key、日志或浏览器文件。公开部署时必须增加 HTTPS、网络访问控制和防火墙。

反代及插件保留各自的 MIT License；插件是独立项目，不代表 dsh 或 Google 官方。
