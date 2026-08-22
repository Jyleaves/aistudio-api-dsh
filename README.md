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

下载 CloakBrowser 的 Windows 版本并解压为项目根目录下的 `cloakbrowser-chromium\chrome.exe`：

<https://github.com/CloakHQ/cloakbrowser/releases>

双击 `start-aistudio-api.bat` 启动，或执行：

```powershell
.\.venv\Scripts\python.exe main.py server --port 8090
```

管理页面：<http://127.0.0.1:8090>

打开管理页面后，进入“API Key 管理 → 新建 Key”，明文只显示一次；dsh 或其他客户端使用这里创建的 Key。点击“添加账号”时会自动打开可交互的 Google 登录窗口，不需要修改无头模式。设置页中的无头模式只影响正常请求和后台浏览器。账号 Cookie 保存在 `data\accounts\`，不要提交到 GitHub。

API Key 管理支持同时保留多个有效 Key。创建新 Key 不会使旧 Key 失效；需要使某个 Key 失效时，在列表中单独删除即可。反代的浏览器、代理、账号轮询、并发、超时、缓存、模型默认值和运行目录等可选配置统一在“设置”页面修改，保存到 `.env`；部分配置需要重启反代后生效。高级用户仍可手动编辑 `.env`，但不是必需操作。

## 2. 安装 dsh 插件

在 dsh web profile 中直接从 GitHub 安装：

```powershell
dsh plugin --profile web add https://github.com/Jyleaves/dsh-gemini-aistudio.git
```

如果使用本地开发副本，也可以把命令中的 GitHub 地址替换为本地插件目录。将“API Key 管理”页面创建的 Key 设置为 dsh 进程可见的环境变量：

```powershell
$env:AISTUDIO_API_KEY = "在 API Key 管理页面创建的 Key"
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

点击“获取可用模型”。反代只返回 Gemini 开头的模型。如果自动获取失败，确认反代已启动、API 地址末尾为 `/v1`，并确认 API 密钥没有多余空格。

## 4. 在 dsh 中添加模型

点击“添加模型”，至少添加以下模型条目：

| 模型字段 | `gemini-3.7-flash` 的值 |
|---|---|
| 模型 ID | `gemini-3.7-flash` |
| 显示名称 | `Gemini 3.7 Flash (AI Studio)` |
| 支持推理 / Reasoning | 开启 |
| 输入模态 | `text`、`image` |
| 上下文窗口 | `1000000` |
| 最大输出 Token | `65536` |

其他 Gemini 模型使用其实际 ID，例如 `gemini-3.5-flash`。反代 `/v1/models` 会同时返回上下文窗口、最大输出 Token、图片输入和 reasoning 元数据；如果 dsh 当前版本的自定义提供方仍不自动填充这些字段，按上表手动填写即可。

插件原生 provider 的模型发现由 dsh 重启或重新加载 provider 触发；自定义提供方页面的“获取可用模型”则读取反代的 `/v1/models`。两者是 dsh 中不同的配置入口。

## 5. dsh 特性

- 插件上传按钮支持原始图片和 PDF，不经过 dsh 图片压缩流程。
- 用户消息中的本地 PDF 路径会被识别并以内联 PDF 发送。
- 普通 Gemini 请求默认启用 Google Search。
- 自定义函数工具请求不会隐式混入 Google Search，避免 Gemini 3 的内置工具配置冲突。
- PDF 默认限制为 20 MiB、300 页；原始上传按钮默认限制为 32 MiB。
- PDF 会缓存本地文件内容，账号轮换重试时不会重复读取和编码文件。

## 6. 更新

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

## 7. 安全与许可证

不要公开 `.env`、`data\accounts\`、Cookie、API Key、日志或浏览器文件。公开部署时必须增加 HTTPS、网络访问控制和防火墙。

反代及插件保留各自的 MIT License；插件是独立项目，不代表 dsh 或 Google 官方。
