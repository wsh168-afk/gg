from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sys
import tempfile
import urllib.error
import uuid
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, Signal
from PySide6.QtGui import QAction, QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox, QPushButton,
    QSlider, QSpinBox, QSplitter, QTabWidget, QTextBrowser, QTextEdit,
    QToolBar, QVBoxLayout, QWidget,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from google import genai
from google.genai import types

APP_NAME = "Gemini Dev Studio"
ORG_NAME = "IndependentDevTools"
DEFAULT_MODEL = "gemini-2.5-flash"

@dataclass
class ChatMessage:
    role: str
    text: str

class SendTextEdit(QTextEdit):
    send_requested = Signal()
    files_attached = Signal(list)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
            self.send_requested.emit()
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source):
        paths = []
        if source.hasUrls():
            for url in source.urls():
                if url.isLocalFile():
                    paths.append(url.toLocalFile())
            if paths:
                self.files_attached.emit(paths)
                return

        if source.hasImage():
            image = source.imageData()
            if image is not None:
                target = Path(tempfile.gettempdir()) / f"gemini-paste-{uuid.uuid4().hex}.png"
                if image.save(str(target), "PNG"):
                    self.files_attached.emit([str(target)])
                    return

        super().insertFromMimeData(source)


class DropFileList(QListWidget):
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DropOnly)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class GeminiWorker(QThread):
    chunk = Signal(str)
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, api_key, model, api_base, history, prompt, files, system_instruction,
                 temperature, top_p, top_k, max_tokens):
        super().__init__()
        self.api_key = api_key
        self.model = model
        self.api_base = (api_base or "").strip().rstrip("/")
        self.history = history
        self.prompt = prompt
        self.files = files
        self.system_instruction = system_instruction
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.max_tokens = max_tokens

    def run(self):
        if self.api_base:
            self.run_openai_compatible()
            return

        uploaded = []
        try:
            client = genai.Client(api_key=self.api_key)
            contents = []
            for m in self.history:
                role = "model" if m.role == "assistant" else "user"
                contents.append(types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=m.text)]
                ))

            parts = [types.Part.from_text(
                text=self.prompt or "请分析附件并给出结果。"
            )]
            for f in self.files:
                obj = client.files.upload(file=f)
                uploaded.append(obj)
                parts.append(obj)

            contents.append(types.Content(role="user", parts=parts))
            config = types.GenerateContentConfig(
                system_instruction=self.system_instruction or None,
                temperature=self.temperature,
                top_p=self.top_p,
                top_k=self.top_k,
                max_output_tokens=self.max_tokens,
            )

            result = []
            for event in client.models.generate_content_stream(
                model=self.model,
                contents=contents,
                config=config,
            ):
                text = getattr(event, "text", None)
                if text:
                    result.append(text)
                    self.chunk.emit(text)
            self.completed.emit("".join(result))
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            try:
                for obj in uploaded:
                    if getattr(obj, "name", None):
                        client.files.delete(name=obj.name)
            except Exception:
                pass

    def run_openai_compatible(self):
        try:
            messages = []
            if self.system_instruction:
                messages.append({"role": "system", "content": self.system_instruction})

            for m in self.history:
                role = "assistant" if m.role == "assistant" else "user"
                messages.append({"role": role, "content": m.text})

            prompt_text = self.prompt or "请分析附件并给出结果。"
            content = [{"type": "text", "text": prompt_text}]

            for path in self.files:
                file_path = Path(path)
                mime, _ = mimetypes.guess_type(path)
                mime = mime or "application/octet-stream"
                raw = file_path.read_bytes()
                b64 = base64.b64encode(raw).decode("ascii")
                data_url = f"data:{mime};base64,{b64}"

                if mime.startswith("image/"):
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": data_url}
                    })
                elif mime.startswith("video/"):
                    content.append({
                        "type": "video_url",
                        "video_url": {"url": data_url}
                    })
                elif mime.startswith("audio/"):
                    content.append({
                        "type": "input_audio",
                        "input_audio": {
                            "data": b64,
                            "format": file_path.suffix.lower().lstrip(".") or "wav"
                        }
                    })
                elif mime.startswith("text/") or file_path.suffix.lower() in {
                    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".cs",
                    ".cpp", ".c", ".h", ".hpp", ".html", ".css", ".json", ".xml",
                    ".yaml", ".yml", ".md", ".txt", ".sql", ".sh", ".ps1", ".bat"
                }:
                    text = raw.decode("utf-8", errors="replace")
                    content.append({
                        "type": "text",
                        "text": f"\n\n--- 附件: {file_path.name} ---\n{text[:500000]}"
                    })
                else:
                    content.append({
                        "type": "file",
                        "file": {
                            "filename": file_path.name,
                            "file_data": data_url
                        }
                    })

            messages.append({"role": "user", "content": content})

            api_model = self.model
            if self.model.strip().lower() == "gemini-3.8-flash":
                api_model = "gemini-3.8-flash"

            payload = {
                "model": api_model,
                "messages": messages,
                "temperature": self.temperature,
                "top_p": self.top_p,
                "max_tokens": self.max_tokens,
                "stream": False,
            }

            url = self.api_base
            if not url.endswith("/v1/chat/completions"):
                url += "/v1/chat/completions"

            req = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=300) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            text = data["choices"][0]["message"]["content"]
            if isinstance(text, list):
                text = "".join(
                    item.get("text", "") if isinstance(item, dict) else str(item)
                    for item in text
                )
            text = str(text)
            self.chunk.emit(text)
            self.completed.emit(text)

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            self.failed.emit(f"第三方 API 请求失败 HTTP {e.code}:\n{body}")
        except Exception as e:
            self.failed.emit(f"第三方 API 请求失败：\n{e}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.history = []
        self.files = []
        self.worker = None
        self.current_response = ""

        self.setWindowTitle(f"{APP_NAME} · Windows")
        self.resize(1360, 850)
        self.setMinimumSize(980, 640)
        self.build_ui()
        self.load_settings()
        self.refresh_chat()

    def build_ui(self):
        bar = QToolBar("主工具栏")
        bar.setMovable(False)
        self.addToolBar(bar)

        for name, fn in [
            ("新建会话", self.new_chat),
            ("保存会话", self.save_session),
            ("打开会话", self.load_session),
            ("导出回答", self.export_answer),
        ]:
            action = QAction(name, self)
            action.triggered.connect(fn)
            bar.addAction(action)

        root = QSplitter(Qt.Horizontal)
        self.setCentralWidget(root)

        left = QWidget()
        left.setMinimumWidth(320)
        left.setMaximumWidth(420)
        ll = QVBoxLayout(left)

        title = QLabel("模型与运行参数")
        title.setFont(QFont("Microsoft YaHei UI", 14, QFont.Bold))
        ll.addWidget(title)

        form = QFormLayout()
        api_key_row = QHBoxLayout()
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("输入 API Key")
        api_key_row.addWidget(self.api_key, 1)

        self.show_key_btn = QPushButton("显示")
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.setFixedWidth(58)
        self.show_key_btn.toggled.connect(self.toggle_api_key_visibility)
        api_key_row.addWidget(self.show_key_btn)

        self.test_key_btn = QPushButton("测试")
        self.test_key_btn.setFixedWidth(58)
        self.test_key_btn.clicked.connect(self.test_api_key)
        api_key_row.addWidget(self.test_key_btn)

        form.addRow("API Key", api_key_row)

        self.remember = QCheckBox("仅保存在本机设置中")
        form.addRow("", self.remember)

        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItems([
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
            "gemini-3.8-flash",
        ])
        self.model.currentTextChanged.connect(self.on_model_changed)
        form.addRow("模型", self.model)

        self.api_base = QLineEdit()
        self.api_base.setPlaceholderText("Google 官方请留空；自定义接口可填写完整域名")
        form.addRow("接口地址", self.api_base)

        self.temperature = QSlider(Qt.Horizontal)
        self.temperature.setRange(0, 200)
        self.temperature.setValue(70)
        form.addRow("Temperature", self.temperature)

        self.top_p = QSlider(Qt.Horizontal)
        self.top_p.setRange(1, 100)
        self.top_p.setValue(95)
        form.addRow("Top-P", self.top_p)

        self.top_k = QSpinBox()
        self.top_k.setRange(1, 200)
        self.top_k.setValue(40)
        form.addRow("Top-K", self.top_k)

        self.max_tokens = QSpinBox()
        self.max_tokens.setRange(256, 65536)
        self.max_tokens.setSingleStep(256)
        self.max_tokens.setValue(8192)
        form.addRow("最大输出 Token", self.max_tokens)
        ll.addLayout(form)

        ll.addWidget(QLabel("System Instruction"))
        self.system_prompt = QTextEdit()
        self.system_prompt.setMaximumHeight(170)
        self.system_prompt.setPlaceholderText(
            "例如：你是一名高级软件工程师。输出可运行代码，并解释文件结构。"
        )
        ll.addWidget(self.system_prompt)

        ll.addWidget(QLabel("附件与项目上下文"))
        file_help = QLabel("可拖入文件，也可在输入框直接 Ctrl+V 粘贴图片/文件")
        file_help.setWordWrap(True)
        file_help.setObjectName("helperText")
        ll.addWidget(file_help)
        self.file_list = DropFileList()
        self.file_list.setMaximumHeight(190)
        self.file_list.setToolTip("可拖入图片、视频、音频、PDF、源码、压缩包等文件")
        self.file_list.files_dropped.connect(self.attach_paths)
        ll.addWidget(self.file_list)

        row = QHBoxLayout()
        add_btn = QPushButton("添加文件")
        add_btn.clicked.connect(self.add_files)
        paste_btn = QPushButton("粘贴")
        paste_btn.clicked.connect(self.paste_clipboard)
        rm_btn = QPushButton("移除")
        rm_btn.clicked.connect(self.remove_files)
        row.addWidget(add_btn)
        row.addWidget(paste_btn)
        row.addWidget(rm_btn)
        ll.addLayout(row)
        ll.addStretch()
        root.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)

        head = QHBoxLayout()
        h1 = QLabel(APP_NAME)
        h1.setFont(QFont("Microsoft YaHei UI", 16, QFont.Bold))
        head.addWidget(h1)
        head.addStretch()
        self.context_label = QLabel("0 条消息 · 0 个附件")
        head.addWidget(self.context_label)
        rl.addLayout(head)

        self.tabs = QTabWidget()
        self.chat = QTextBrowser()

        code_page = QWidget()
        code_layout = QVBoxLayout(code_page)
        code_bar = QHBoxLayout()
        code_hint = QLabel("代码可直接编辑；HTML/CSS/JS 可刷新到“可视化”预览")
        code_bar.addWidget(code_hint)
        code_bar.addStretch()
        preview_btn = QPushButton("刷新可视化")
        preview_btn.clicked.connect(self.refresh_visual_preview)
        code_bar.addWidget(preview_btn)
        code_layout.addLayout(code_bar)

        self.raw = QTextEdit()
        self.raw.setReadOnly(False)
        self.raw.setFont(QFont("Consolas", 10))
        code_layout.addWidget(self.raw)

        visual_page = QWidget()
        visual_layout = QVBoxLayout(visual_page)
        visual_bar = QHBoxLayout()
        self.visual_status = QLabel("等待 HTML 内容")
        visual_bar.addWidget(self.visual_status)
        visual_bar.addStretch()
        visual_refresh = QPushButton("刷新预览")
        visual_refresh.clicked.connect(self.refresh_visual_preview)
        visual_bar.addWidget(visual_refresh)
        visual_layout.addLayout(visual_bar)
        self.web_preview = QWebEngineView()
        self.web_preview.setHtml(
            "<html><body style='font-family:Segoe UI;padding:32px;color:#666'>"
            "<h3>可视化预览</h3><p>AI 返回 HTML 后，这里会显示实时界面效果。</p>"
            "<p>也可以在“代码”页修改 HTML，再点击“刷新可视化”。</p>"
            "</body></html>"
        )
        visual_layout.addWidget(self.web_preview)

        self.tabs.addTab(self.chat, "对话")
        self.tabs.addTab(code_page, "代码")
        self.tabs.addTab(visual_page, "可视化")
        rl.addWidget(self.tabs, 1)

        self.prompt = SendTextEdit()
        self.prompt.setPlaceholderText(
            "输入开发需求。Enter 发送，Shift+Enter 换行。"
        )
        self.prompt.send_requested.connect(self.send_prompt)
        self.prompt.files_attached.connect(self.attach_paths)
        self.prompt.setAcceptDrops(True)
        self.prompt.setMaximumHeight(150)
        rl.addWidget(self.prompt)

        send_row = QHBoxLayout()
        clear_btn = QPushButton("清空上下文")
        clear_btn.clicked.connect(self.clear_history)
        self.send_btn = QPushButton("发送")
        self.send_btn.clicked.connect(self.send_prompt)
        self.send_btn.setMinimumHeight(40)
        self.send_btn.setStyleSheet(
            "QPushButton { background:#2563eb; color:white; border:none; font-weight:600; padding:7px 20px; }"
            "QPushButton:hover { background:#1d4ed8; }"
            "QPushButton:disabled { background:#9ca3af; }"
        )
        send_row.addWidget(clear_btn)
        send_row.addStretch()
        send_row.addWidget(self.send_btn)
        rl.addLayout(send_row)

        root.addWidget(right)
        root.setStretchFactor(1, 1)
        self.statusBar().showMessage("就绪")

    def toggle_api_key_visibility(self, checked):
        self.api_key.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        self.show_key_btn.setText("隐藏" if checked else "显示")

    def test_api_key(self):
        key = self.api_key.text().strip()
        if not key:
            QMessageBox.warning(self, "缺少 API Key", "请先输入 API Key。")
            return

        model = self.model.currentText().strip() or DEFAULT_MODEL
        api_base = self.api_base.text().strip()

        self.test_key_btn.setEnabled(False)
        self.test_key_btn.setText("测试中")
        self.statusBar().showMessage("正在测试 API 连接…")

        history = []
        worker = GeminiWorker(
            key,
            model,
            api_base,
            history,
            "只回复：OK",
            [],
            "",
            0.1,
            0.9,
            20,
            64,
        )

        def ok(_):
            self.test_key_btn.setEnabled(True)
            self.test_key_btn.setText("测试")
            self.statusBar().showMessage("API 连接成功", 5000)
            QMessageBox.information(self, "连接成功", "API Key 与当前模型连接正常。")

        def fail(msg):
            self.test_key_btn.setEnabled(True)
            self.test_key_btn.setText("测试")
            self.statusBar().showMessage("API 连接失败", 5000)
            QMessageBox.warning(self, "连接失败", msg)

        worker.completed.connect(ok)
        worker.failed.connect(fail)
        self._test_worker = worker
        worker.start()

    def on_model_changed(self, text):
        if text.strip().lower() == "gemini-3.8-flash":
            self.api_base.setText("https://api.cxhao.com")
        elif self.api_base.text().strip().rstrip("/") == "https://api.cxhao.com":
            self.api_base.clear()

    def load_settings(self):
        self.model.setCurrentText(self.settings.value("model", DEFAULT_MODEL))
        self.system_prompt.setPlainText(self.settings.value("system_prompt", ""))
        self.api_base.setText(self.settings.value("api_base", ""))
        if self.model.currentText().strip().lower() == "gemini-3.8-flash" and not self.api_base.text().strip():
            self.api_base.setText("https://api.cxhao.com")
        self.temperature.setValue(int(self.settings.value("temperature", 70)))
        self.top_p.setValue(int(self.settings.value("top_p", 95)))
        self.top_k.setValue(int(self.settings.value("top_k", 40)))
        self.max_tokens.setValue(int(self.settings.value("max_tokens", 8192)))
        remember = self.settings.value("remember_key", False, type=bool)
        self.remember.setChecked(remember)
        if remember:
            self.api_key.setText(self.settings.value("api_key", ""))
        elif os.getenv("GEMINI_API_KEY"):
            self.api_key.setText(os.getenv("GEMINI_API_KEY", ""))

    def save_settings(self):
        self.settings.setValue("model", self.model.currentText().strip())
        self.settings.setValue("api_base", self.api_base.text().strip())
        self.settings.setValue("system_prompt", self.system_prompt.toPlainText())
        self.settings.setValue("temperature", self.temperature.value())
        self.settings.setValue("top_p", self.top_p.value())
        self.settings.setValue("top_k", self.top_k.value())
        self.settings.setValue("max_tokens", self.max_tokens.value())
        self.settings.setValue("remember_key", self.remember.isChecked())
        if self.remember.isChecked():
            self.settings.setValue("api_key", self.api_key.text().strip())
        else:
            self.settings.remove("api_key")

    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "添加上下文文件",
            "",
            "所有支持文件 (*.*);;图片 (*.png *.jpg *.jpeg *.webp *.gif *.bmp);;"
            "视频 (*.mp4 *.mov *.avi *.mkv *.webm);;音频 (*.mp3 *.wav *.m4a *.aac *.flac);;"
            "文档 (*.pdf *.doc *.docx *.xls *.xlsx *.ppt *.pptx *.txt *.md);;"
            "源码 (*.py *.js *.ts *.tsx *.jsx *.html *.css *.json *.java *.kt *.cs *.cpp *.c *.h)"
        )
        self.attach_paths(paths)

    def attach_paths(self, paths):
        for p in paths:
            if p and Path(p).exists() and p not in self.files:
                self.files.append(p)
                mime, _ = mimetypes.guess_type(p)
                label = Path(p).name
                if mime:
                    label += f"   [{mime}]"
                self.file_list.addItem(label)
        self.update_context()

    def paste_clipboard(self):
        mime = QApplication.clipboard().mimeData()
        paths = []
        if mime.hasUrls():
            paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        if paths:
            self.attach_paths(paths)
            return
        if mime.hasImage():
            image = QApplication.clipboard().image()
            if not image.isNull():
                target = Path(tempfile.gettempdir()) / f"gemini-paste-{uuid.uuid4().hex}.png"
                if image.save(str(target), "PNG"):
                    self.attach_paths([str(target)])
                    self.statusBar().showMessage("已粘贴图片到附件", 3000)
                    return
        QMessageBox.information(self, "剪贴板", "剪贴板中没有图片或本地文件。")

    def remove_files(self):
        rows = sorted({self.file_list.row(i) for i in self.file_list.selectedItems()}, reverse=True)
        for r in rows:
            self.file_list.takeItem(r)
            self.files.pop(r)
        self.update_context()

    def update_context(self):
        self.context_label.setText(f"{len(self.history)} 条消息 · {len(self.files)} 个附件")

    def send_prompt(self):
        key = self.api_key.text().strip()
        prompt = self.prompt.toPlainText().strip()
        if not key:
            QMessageBox.warning(self, "缺少 API Key", "请先输入 Gemini API Key。")
            return
        if not prompt and not self.files:
            QMessageBox.warning(self, "没有内容", "请输入提示词或添加附件。")
            return
        if self.worker and self.worker.isRunning():
            return

        self.save_settings()
        self.current_response = ""
        self.raw.clear()
        previous = list(self.history)
        self.history.append(ChatMessage("user", prompt or "[附件分析]"))
        self.prompt.clear()
        self.refresh_chat(streaming="正在生成…")
        self.send_btn.setEnabled(False)
        self.send_btn.setText("生成中…")

        self.worker = GeminiWorker(
            key,
            self.model.currentText().strip() or DEFAULT_MODEL,
            self.api_base.text().strip(),
            previous,
            prompt,
            list(self.files),
            self.system_prompt.toPlainText().strip(),
            self.temperature.value() / 100,
            self.top_p.value() / 100,
            self.top_k.value(),
            self.max_tokens.value(),
        )
        self.worker.chunk.connect(self.on_chunk)
        self.worker.completed.connect(self.on_done)
        self.worker.failed.connect(self.on_error)
        self.worker.start()

    def on_chunk(self, text):
        self.current_response += text
        self.raw.moveCursor(QTextCursor.End)
        self.raw.insertPlainText(text)
        self.refresh_chat(streaming=self.current_response)

    def on_done(self, text):
        self.history.append(ChatMessage("assistant", text or self.current_response))
        self.current_response = ""
        self.files.clear()
        self.file_list.clear()
        self.send_btn.setEnabled(True)
        self.send_btn.setText("发送")
        self.statusBar().showMessage("生成完成", 4000)
        self.refresh_chat()
        self.refresh_visual_preview()

    def extract_html(self, text):
        if not text:
            return ""
        matches = re.findall(r"\`\`\`(?:html)?\s*(.*?)\`\`\`", text, flags=re.I | re.S)
        for block in matches:
            candidate = block.strip()
            if "<html" in candidate.lower() or "<body" in candidate.lower() or "<div" in candidate.lower():
                return candidate

        stripped = text.strip()
        lower = stripped.lower()
        if lower.startswith("<!doctype html") or "<html" in lower:
            return stripped
        return ""

    def refresh_visual_preview(self):
        code = self.raw.toPlainText().strip()
        html = self.extract_html(code)
        if not html:
            self.visual_status.setText("未检测到可预览的 HTML")
            self.web_preview.setHtml(
                "<html><body style='font-family:Segoe UI;padding:32px;color:#666'>"
                "<h3>暂无可视化内容</h3>"
                "<p>“可视化”目前直接渲染 HTML/CSS/JavaScript。</p>"
                "<p>让 AI 生成网页/UI 代码，或在“代码”页粘贴完整 HTML 后点击刷新。</p>"
                "</body></html>"
            )
            return
        self.web_preview.setHtml(html)
        self.visual_status.setText("HTML 可视化预览已更新")

    def on_error(self, text):
        if self.history and self.history[-1].role == "user":
            self.history.pop()
        self.send_btn.setEnabled(True)
        self.send_btn.setText("发送给 Gemini")
        self.raw.setPlainText(text)
        QMessageBox.critical(self, "Gemini API 请求失败", text)
        self.refresh_chat()

    @staticmethod
    def esc(text):
        return (text.replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;").replace("\n", "<br>"))

    def refresh_chat(self, streaming=""):
        blocks = []
        for m in self.history:
            who = "你" if m.role == "user" else "Gemini"
            bg = "#eef3fd" if m.role == "user" else "#ffffff"
            blocks.append(
                f"<div style='background:{bg};border:1px solid #ddd;border-radius:10px;"
                f"padding:12px;margin:8px'><b>{who}</b><br><br>{self.esc(m.text)}</div>"
            )
        if streaming:
            blocks.append(
                "<div style='background:#fff;border:1px solid #ddd;border-radius:10px;"
                f"padding:12px;margin:8px'><b>Gemini</b><br><br>{self.esc(streaming)}</div>"
            )
        self.chat.setHtml("".join(blocks))
        self.chat.verticalScrollBar().setValue(self.chat.verticalScrollBar().maximum())
        self.update_context()

    def new_chat(self):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "正在生成", "请等待当前回答完成。")
            return
        self.history.clear()
        self.files.clear()
        self.file_list.clear()
        self.raw.clear()
        self.refresh_chat()

    def clear_history(self):
        self.history.clear()
        self.refresh_chat()

    def save_session(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存会话", "gemini-session.json", "JSON (*.json)")
        if not path:
            return
        data = {
            "app": APP_NAME,
            "model": self.model.currentText(),
            "system_instruction": self.system_prompt.toPlainText(),
            "history": [asdict(m) for m in self.history],
        }
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_session(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开会话", "", "JSON (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.history = [ChatMessage(**m) for m in data.get("history", [])]
            self.model.setCurrentText(data.get("model", DEFAULT_MODEL))
            self.system_prompt.setPlainText(data.get("system_instruction", ""))
            self.refresh_chat()
        except Exception as e:
            QMessageBox.critical(self, "打开失败", str(e))

    def export_answer(self):
        answer = next((m.text for m in reversed(self.history) if m.role == "assistant"), "")
        if not answer:
            answer = self.current_response or self.raw.toPlainText()
        if not answer:
            QMessageBox.information(self, "没有回答", "当前没有可保存的回答。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存最后回答", "gemini-output.md",
            "Markdown (*.md);;Text (*.txt);;All files (*.*)"
        )
        if path:
            Path(path).write_text(answer, encoding="utf-8")

def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QMainWindow, QWidget {
            font-family: "Microsoft YaHei UI", "Segoe UI";
            font-size: 13px;
        }
        QMainWindow { background: #f6f7fb; }
        QToolBar {
            spacing: 8px;
            padding: 7px 10px;
            border: none;
            border-bottom: 1px solid #dfe3ea;
            background: #ffffff;
        }
        QToolButton {
            padding: 7px 10px;
            border-radius: 6px;
        }
        QToolButton:hover { background: #eef3ff; }
        QLabel#helperText { color: #6b7280; font-size: 12px; }
        QLineEdit, QTextEdit, QTextBrowser, QListWidget, QComboBox, QSpinBox {
            background: #ffffff;
            border: 1px solid #d6dbe5;
            border-radius: 7px;
            padding: 6px;
            selection-background-color: #2563eb;
        }
        QLineEdit:focus, QTextEdit:focus, QListWidget:focus, QComboBox:focus {
            border: 1px solid #2563eb;
        }
        QPushButton {
            min-height: 30px;
            padding: 4px 12px;
            border-radius: 7px;
            border: 1px solid #cfd5df;
            background: #ffffff;
        }
        QPushButton:hover { background: #f2f5fa; }
        QPushButton:pressed { background: #e8edf5; }
        QTabWidget::pane {
            border: 1px solid #d7dce5;
            background: #ffffff;
            border-radius: 8px;
        }
        QTabBar::tab {
            padding: 8px 16px;
            margin-right: 3px;
            border-top-left-radius: 7px;
            border-top-right-radius: 7px;
            background: #e9edf4;
        }
        QTabBar::tab:selected {
            color: #1557d6;
            background: #ffffff;
            font-weight: 600;
        }
        QStatusBar {
            background: #ffffff;
            border-top: 1px solid #e2e6ed;
        }
    """)
    window = MainWindow()
    window.show()
    return app.exec()

if __name__ == "__main__":
    raise SystemExit(main())
