# Windows 本地部署说明

本项目面向 dsh 提供 Google AI Studio 的 Gemini 反代能力。完整安装、dsh 字段填写和模型配置请直接阅读项目根目录的 [README.md](README.md)。

Windows 下建议使用虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

不需要手动编辑 `.env`，启动反代后访问 `http://127.0.0.1:8090`。首次启动会自动创建 `.env` 和空的 API Key 存储，不会自动生成 Key；进入“API Key 管理”页面创建 Key。点击“添加账号”会自动打开可交互的 Google 登录窗口（默认使用系统 Chrome/Edge），不需要调整无头模式；登录窗口使用持久化档案 `data\login-profile\`，首次登录之后再添加账号会显示 Google 账号列表，点一下即可授权。设置页中的无头模式只影响正常请求和后台浏览器。后台请求浏览器同样自动发现：项目内 CloakBrowser → 系统 Chrome → 系统 Edge，无需手动下载。

dsh 插件安装：

```powershell
dsh plugin --profile web add https://github.com/Jyleaves/dsh-gemini-aistudio.git
```

自定义提供方填写：Provider ID 为 `gemini-aistudio`，显示名称为 `Google AI Studio`，API 地址为 `http://127.0.0.1:8090/v1`，协议为 `openai-completions`，API 密钥填写反代 Key。模型 `gemini-3.7-flash` 使用上下文窗口 `1000000`、最大输出 `65536`，启用 reasoning，输入模态选择 text 和 image。反代其他可选配置统一在管理页面“设置”路由修改。

更新反代使用 `update-aistudio-api.ps1`，更新插件使用 `update-dsh-gemini-aistudio.ps1`。不要提交 `.env`、`data\accounts\`、Cookie、Key、日志和浏览器文件。
