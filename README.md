# AI Studio API 本地反代

本项目将 Google AI Studio 转换为 OpenAI 兼容接口，支持 Gemini/Gemma、多账号轮询、图片输入、PDF 输入、Google Search、流式输出和图片生成。

本仓库基于 [chrysoljq/aistudio-api](https://github.com/chrysoljq/aistudio-api)，保留上游 MIT License 和版权声明。本地修改不代表 Google 或上游作者。

## 一、环境要求

- Windows 10/11
- Python 3.11 或更高版本
- Node.js/npm（仅在使用 pi-web 补丁脚本时需要）
- 可用的 Google 账号

所有 Python 依赖都安装到项目虚拟环境，不要安装到系统 Python 或 base 环境。

## 二、安装

在仓库根目录打开 PowerShell。下面的命令都使用项目相对路径，不要求项目位于固定目录。

创建虚拟环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

项目使用 CloakBrowser Chromium。将对应的 Windows 浏览器文件放到：

```text
cloakbrowser-chromium\chrome.exe
```

浏览器文件从 [CloakBrowser Releases](https://github.com/CloakHQ/cloakbrowser/releases) 获取。不要将浏览器压缩包、浏览器目录、Cookie 或日志提交到公开仓库。

## 三、配置 `.env`

复制配置模板：

```powershell
Copy-Item .env.example .env
```

至少设置以下内容：

```dotenv
AISTUDIO_PORT=8090
AISTUDIO_BROWSER=chromium
AISTUDIO_BROWSER_HEADLESS=1
AISTUDIO_BROWSER_EXECUTABLE=cloakbrowser-chromium\chrome.exe
AISTUDIO_API_KEY=留空则首次启动自动生成
AISTUDIO_ACCOUNTS_DIR=data\accounts
AISTUDIO_TMP_DIR=data\tmp
```

`AISTUDIO_API_KEY` 是访问本地反代的令牌，不是 Google API Key。公开部署时必须使用强随机值；本地管理页和 API 请求都需要使用它。

如果是首次安装，可以不填写 `AISTUDIO_API_KEY`。首次启动会自动生成随机 Key 并写入项目根目录的 `.env`，同时保存到 `data\api_keys.json`。本机打开管理页时会自动建立本地管理会话，不会每次重复要求输入；远程访问仍必须提供 API Key。

首次添加账号时，如果需要看到登录浏览器，将 `AISTUDIO_BROWSER_HEADLESS` 临时改为 `0`。登录完成后改回 `1`。

## 四、启动与停止

推荐双击项目根目录的：

```text
start-aistudio-api.bat
```

脚本会先检查 `127.0.0.1:8090`：

- 如果没有本项目进程，直接启动；
- 如果已经是本项目进程，会询问是否关闭旧进程；选择是后再启动；
- 如果端口被其他程序占用，会提示并退出，不会强行结束其他程序。

也可以在 PowerShell 中启动：

```powershell
.\.venv\Scripts\python.exe main.py server --port 8090
```

管理页地址：

```text
http://127.0.0.1:8090
```

结束服务时可以关闭启动窗口，或再次运行启动脚本并确认关闭已运行实例。

## 五、添加和管理 Google 账号

1. 本机打开管理页会自动建立本地管理会话，无需重复输入 API Key；从其他设备访问时输入 `.env` 中的 `AISTUDIO_API_KEY`。
2. 进入“账号管理”，点击添加账号。
3. 在弹出的 Google 登录浏览器中完成登录。
4. 登录完成后，账号 Cookie 会保存到项目内的 `data\accounts\acc_xxx\`。
5. 账号列表支持轮询、手动激活和修改账号显示名称。

管理页的“API Key 管理”支持查看脱敏 Key、创建、轮换和撤销。新 Key 的明文只在创建完成时显示一次；撤销最后一个有效 Key 会被阻止，避免把服务锁死。

账号 Cookie 会持久化保存。通常不需要定期重新登录，只有 Google 会话失效、修改密码、触发安全验证或账号被撤销时才需要重新登录。

不要公开以下内容：

- `.env`
- `data/accounts/`
- Google Cookie
- API Token
- `*.log`

## 六、配置 pi-web

在 pi 的模型配置中添加 OpenAI 兼容供应商：

```json
{
  "providers": {
    "gemini-aistudio": {
      "api": "openai-completions",
      "apiKey": "与 .env 相同的 AISTUDIO_API_KEY",
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

保存后重启或刷新 pi-web。图片可以直接从 pi-web 上传；PDF 等文件由 pi-web 扩展和反代共同处理，具体能力取决于所选模型。

## 七、模型和配置

模型列表由反代的 `/v1/models` 提供。pi-web 的模型发现需要手动触发，不会持续自动修改本地配置。

项目根目录的 `config.yaml` 用于模型默认参数、Google Search、Thinking、安全设置和图片模型配置。修改后需要重启反代。

常用环境变量：

| 变量 | 作用 |
|---|---|
| `AISTUDIO_PORT` | 反代端口，当前为 `8090` |
| `AISTUDIO_API_KEY` | 本地 API 鉴权令牌 |
| `AISTUDIO_BROWSER_EXECUTABLE` | Chromium 可执行文件路径 |
| `AISTUDIO_BROWSER_HEADLESS` | 是否隐藏浏览器，`1` 为隐藏 |
| `AISTUDIO_ACCOUNT_ROTATION_MODE` | 账号轮询：`round_robin`、`lru`、`least_rl` |
| `AISTUDIO_ACCOUNT_COOLDOWN_SECONDS` | 账号限流后的冷却时间 |
| `AISTUDIO_TIMEOUT_REPLAY` | 非流式请求超时时间 |
| `AISTUDIO_TIMEOUT_STREAM` | 流式请求超时时间 |

## 八、更新项目

双击项目根目录的 `update-aistudio-api.bat`。更新脚本会：

1. 检查 Git 工作区，发现未提交代码修改时停止，避免覆盖本地补丁；
2. 使用当前分支配置的上游分支执行 `git pull --ff-only`；
3. 在项目虚拟环境中同步 `requirements.txt`；
4. 保留 `.env`、`data\`、`.venv\` 和本地浏览器文件，不需要重新配置账号。

反代运行中时，使用：

```powershell
.\update-aistudio-api.ps1 -Restart
```

如果只想检查是否满足自动更新条件：

```powershell
.\update-aistudio-api.ps1 -CheckOnly
```

更新完成后建议验证管理页、账号列表、`/v1/models` 和一次文本请求。若 GitHub 仓库中的本地修改尚未提交，先提交或备份后再更新。

## 九、许可证

本项目及上游项目采用 MIT License。再发布或二次修改时，请保留上游版权声明和许可证文件。
