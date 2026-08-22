# AI Studio API for dsh

本项目将 Google AI Studio 转换为 dsh 可使用的 Gemini 接口，支持多账号轮询、图片、PDF、Google Search、思考输出、函数工具和流式响应。

本项目基于 [chrysoljq/aistudio-api](https://github.com/chrysoljq/aistudio-api)，上游采用 MIT License。再发布或修改时请保留上游版权声明和许可证。本项目不是 Google 官方项目。

## 1. 安装反代

要求 Windows 10/11、Python 3.11+、能访问 Google 的网络环境和可用的 Google 账号。

```powershell
git clone https://github.com/Jyleaves/aistudio-api-dsh.git
cd aistudio-api-dsh
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

双击 `start-aistudio-api.bat` 启动，或执行 `.\.venv\Scripts\python.exe main.py server --port 8090`。

管理页面：<http://127.0.0.1:8090> ——在“API Key 管理”新建 Key，点击“添加账号”完成 Google 登录。首次登录后账号会被记住，之后添加账号只需在弹出的账号列表里点一下即可。

## 2. 安装 dsh 插件

通过以下命令安装 dsh-gemini-aistudio 插件并重启 dsh：

```powershell
dsh plugin --profile web add https://github.com/Jyleaves/dsh-gemini-aistudio.git
```

## 3. 在 dsh 设置中添加提供方

如果 dsh 设置页面没有自动显示插件模型，进入“设置 → 模型 → 自定义提供方”，按下面填写：

| 界面字段 | 填写值 |
|---|---|
| Provider ID | `gemini-aistudio` |
| 显示名称 | `Google AI Studio` |
| API 地址 | `http://127.0.0.1:8090/v1` |
| API 协议 | `openai-completions` |
| API 密钥 | “API Key 管理”页面创建的 Key |

点击“获取可用模型”。

## 4. dsh 特性

- 插件上传按钮支持原始图片和 PDF，不经过 dsh 图片压缩流程。
- 用户消息中的本地 PDF 路径会被识别并以内联 PDF 发送。
- 普通 Gemini 请求默认启用 Google Search。
- 自定义函数工具请求不会隐式混入 Google Search，避免 Gemini 3 的内置工具配置冲突。
- PDF 默认限制为 20 MiB、300 页；原始上传按钮默认限制为 32 MiB。

## 5. 更新

```powershell
.\update-aistudio-api.ps1 -Restart
.\update-dsh-gemini-aistudio.ps1
```

## 6. 安全与许可证

不要公开 `.env`、`data\`、Cookie、API Key 或日志。公开部署时必须增加 HTTPS、网络访问控制和防火墙。

反代及插件保留各自的 MIT License；插件是独立项目，不代表 dsh 或 Google 官方。
