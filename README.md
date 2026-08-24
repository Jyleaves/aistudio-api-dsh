# Asteria

Asteria 将 Google AI Studio 作为本地 API 服务使用，支持多账号、流式响应、图片、PDF、Google Search 和函数工具。

## 安装与使用

1. 从 [GitHub Releases](https://github.com/Jyleaves/aistudio-api-dsh/releases) 下载最新的 `Asteria-setup-*.exe`。
2. 运行安装程序并选择安装位置，然后启动 Asteria。
3. 在“账号管理”中完成 Google 登录，等待账号状态变为“正常”或“待命”。
4. 在“API Key 管理”中创建密钥。
5. 打开应用内的“接入指南”，按客户端支持的协议填写地址、密钥和模型。

默认模型为 `gemini-3.7-flash`。Asteria 支持 OpenAI、Gemini 和 Anthropic 三种接口协议，接入地址会在应用中根据当前端口自动生成。

PDF 当前通过 Gemini `inlineData` 传输。为保证长期运行稳定性，8 MiB 以上的内联媒体默认串行进入 Chromium，HTTP 请求体默认限制为 40 MiB；可通过 `AISTUDIO_LARGE_MEDIA_THRESHOLD_BYTES`、`AISTUDIO_LARGE_MEDIA_MAX_CONCURRENCY` 和 `AISTUDIO_MAX_REQUEST_BYTES` 调整。建议普通用户保持默认值，并将单个 PDF 控制在 dsh 插件默认的 20 MiB / 300 页以内。

最高思考强度会占用 `maxOutputTokens` 的同一输出预算。若客户端手动覆盖该值，不要设成几十或几百；预算过小时可能只剩一行可见正文。dsh 插件默认使用模型的正常大输出上限，无需手动缩小。

## dsh 接入

安装插件并重启 dsh：

```powershell
dsh plugin --profile web add https://github.com/Jyleaves/dsh-gemini-aistudio.git
```

插件会注册 `Google AI Studio (native)` 提供方。优先直接选择这个提供方；
它会明确声明图片输入和 `Minimal`、`Low`、`Medium`、`High` 思考强度，默认
为 `High`，无需再手工新建同名提供方。

仅在需要兼容旧配置时，才在 dsh 的自定义提供方中填写：

| 字段 | 值 |
|---|---|
| Provider ID | `gemini-aistudio` |
| API 协议 | `openai-completions` |
| API 地址 | 应用“接入指南”中的 OpenAI Base URL |
| API 密钥 | 在 Asteria 中创建的 API Key |

随后点击“获取可用模型”。当前 dsh 的通用 OpenAI 模型发现可能只读取模型
名称和上下文限制，不一定导入图片与思考强度元数据，因此推荐使用插件注册的
原生提供方。

## 更新

在 Asteria 左下角打开“更新”。账号、设置和内置浏览器不会因普通应用更新而重复下载或被清除。

## 开发者构建

项目贡献者可在项目本地虚拟环境中执行：

```powershell
.\build-windows.ps1
```

## 安全与许可证

不要公开账号数据、Cookie、API Key 或日志。若允许其他设备访问，请同时配置 HTTPS、网络访问控制和防火墙。

本项目基于 [chrysoljq/aistudio-api](https://github.com/chrysoljq/aistudio-api)，按 MIT License 发布；不是 Google 官方项目。
