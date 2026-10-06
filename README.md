# Gemini Dev Studio for Windows

一个独立开发的 Windows 桌面 Gemini 开发工作台。

主要功能：

- Gemini API 多轮对话与流式输出
- System Instruction
- 模型选择与 Temperature / Top-P / Top-K / 最大输出 Token
- 图片、PDF、源码等文件作为上下文上传
- 会话新建、保存、恢复
- 原始代码/文本输出查看与导出
- API Key 仅保存在本机（可选）
- GitHub Actions 自动生成 Windows EXE

> 本项目不是 Google 官方 Google AI Studio，也不使用 Google 官方产品名称作为软件品牌。

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

## Windows 打包

```powershell
pip install pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed --name GeminiDevStudio main.py
```

生成文件：

`dist/GeminiDevStudio.exe`

## GitHub Actions

推送到 `main` 后会自动运行 **Build Windows EXE**。构建完成后，在该 Actions 运行页面下载 `GeminiDevStudio-windows` Artifact。

## API Key

在 Google AI Studio / Google AI for Developers 创建 Gemini API Key。Key 不应提交到 GitHub。本项目的 `.gitignore` 已忽略 `.env`。
