from __future__ import annotations

import json
import os
import sys
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
from google import genai
from google.genai import types

APP_NAME = "Gemini Dev Studio"
ORG_NAME = "IndependentDevTools"
DEFAULT_MODEL = "gemini-2.5-flash"

@dataclass
class ChatMessage:
    role: str
    text: str

class GeminiWorker(QThread):
    chunk = Signal(str)
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, api_key, model, history, prompt, files, system_instruction,
                 temperature, top_p, top_k, max_tokens):
        super().__init__()
        self.api_key = api_key
        self.model = model
        self.history = history
        self.prompt = prompt
        self.files = files
        self.system_instruction = system_instruction
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.max_tokens = max_tokens

    def run(self):
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
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("输入 Gemini API Key")
        form.addRow("API Key", self.api_key)

        self.remember = QCheckBox("仅在本机记住 API Key")
        form.addRow("", self.remember)

        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItems([
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
        ])
        form.addRow("模型", self.model)

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

        ll.addWidget(QLabel("本次附件 / 项目上下文"))
        self.file_list = QListWidget()
        self.file_list.setMaximumHeight(170)
        ll.addWidget(self.file_list)

        row = QHBoxLayout()
        add_btn = QPushButton("添加文件")
        add_btn.clicked.connect(self.add_files)
        rm_btn = QPushButton("移除")
        rm_btn.clicked.connect(self.remove_files)
        row.addWidget(add_btn)
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
        self.raw = QTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setFont(QFont("Consolas", 10))
        self.tabs.addTab(self.chat, "对话")
        self.tabs.addTab(self.raw, "原始输出 / 代码")
        rl.addWidget(self.tabs, 1)

        self.prompt = QTextEdit()
        self.prompt.setPlaceholderText(
            "输入开发需求，例如：生成一个 FastAPI 登录模块，或分析左侧附件。"
        )
        self.prompt.setMaximumHeight(150)
        rl.addWidget(self.prompt)

        send_row = QHBoxLayout()
        clear_btn = QPushButton("清空上下文")
        clear_btn.clicked.connect(self.clear_history)
        self.send_btn = QPushButton("发送给 Gemini")
        self.send_btn.clicked.connect(self.send_prompt)
        self.send_btn.setMinimumHeight(38)
        send_row.addWidget(clear_btn)
        send_row.addStretch()
        send_row.addWidget(self.send_btn)
        rl.addLayout(send_row)

        root.addWidget(right)
        root.setStretchFactor(1, 1)
        self.statusBar().showMessage("就绪")

    def load_settings(self):
        self.model.setCurrentText(self.settings.value("model", DEFAULT_MODEL))
        self.system_prompt.setPlainText(self.settings.value("system_prompt", ""))
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
        paths, _ = QFileDialog.getOpenFileNames(self, "添加上下文文件", "", "All files (*.*)")
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.file_list.addItem(Path(p).name)
        self.update_context()

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
        self.send_btn.setText("发送给 Gemini")
        self.statusBar().showMessage("生成完成", 4000)
        self.refresh_chat()

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
    window = MainWindow()
    window.show()
    return app.exec()

if __name__ == "__main__":
    raise SystemExit(main())
