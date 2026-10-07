from __future__ import annotations

import base64
import html
import http.server
import difflib
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import tempfile
import urllib.error
import uuid
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from PySide6.QtCore import QProcess, QSettings, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QFont, QIcon, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QSlider, QSpinBox, QSplitter, QTabWidget, QTextBrowser,
    QTextEdit, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from PySide6.QtWebEngineCore import QWebEnginePage
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


class GitHubRepoDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent_window = parent
        self.settings = parent.settings
        self.repo_owner = ""
        self.repo_name = ""
        self.setWindowTitle("GitHub 仓库")
        self.resize(760, 620)
        self.build_ui()
        self.load_saved()

    def build_ui(self):
        layout = QVBoxLayout(self)

        title = QLabel("绑定 GitHub 仓库")
        title.setStyleSheet("font-size:18px;font-weight:700;")
        layout.addWidget(title)

        help_text = QLabel(
            "公开仓库可不填 Token；私有仓库请填写 GitHub Personal Access Token。"
            "Token 仅保存到本机设置中，不会写入项目或上传到仓库。"
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helperText")
        layout.addWidget(help_text)

        form = QFormLayout()
        self.repo_url = QLineEdit()
        self.repo_url.setPlaceholderText("https://github.com/owner/repository")
        form.addRow("仓库地址", self.repo_url)

        self.branch = QLineEdit()
        self.branch.setPlaceholderText("main")
        form.addRow("分支", self.branch)

        token_row = QHBoxLayout()
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("GitHub PAT（私有仓库需要）")
        token_row.addWidget(self.token, 1)
        self.show_token = QPushButton("显示")
        self.show_token.setCheckable(True)
        self.show_token.setFixedWidth(58)
        self.show_token.toggled.connect(self.toggle_token)
        token_row.addWidget(self.show_token)
        form.addRow("GitHub Token", token_row)

        self.remember_token = QCheckBox("在本机记住 Token")
        form.addRow("", self.remember_token)

        layout.addLayout(form)

        actions = QHBoxLayout()
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self.test_connection)
        self.load_btn = QPushButton("加载文件树")
        self.load_btn.clicked.connect(self.load_tree)
        self.add_btn = QPushButton("加入 AI 上下文")
        self.add_btn.clicked.connect(self.add_selected_to_context)
        self.edit_btn = QPushButton("编辑 / Diff / Push")
        self.edit_btn.clicked.connect(self.edit_selected_file)
        actions.addWidget(self.test_btn)
        actions.addWidget(self.load_btn)
        actions.addStretch()
        actions.addWidget(self.add_btn)
        actions.addWidget(self.edit_btn)
        layout.addLayout(actions)

        self.status = QLabel("未连接")
        self.status.setObjectName("helperText")
        layout.addWidget(self.status)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["仓库文件", "类型"])
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        layout.addWidget(self.tree, 1)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

    def toggle_token(self, checked):
        self.token.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        self.show_token.setText("隐藏" if checked else "显示")

    def load_saved(self):
        self.repo_url.setText(self.settings.value("github_repo_url", ""))
        self.branch.setText(self.settings.value("github_branch", "main"))
        remember = self.settings.value("github_remember_token", False, type=bool)
        self.remember_token.setChecked(remember)
        if remember:
            self.token.setText(self.settings.value("github_token", ""))

    def save_binding(self):
        self.settings.setValue("github_repo_url", self.repo_url.text().strip())
        self.settings.setValue("github_branch", self.branch.text().strip() or "main")
        self.settings.setValue("github_remember_token", self.remember_token.isChecked())
        if self.remember_token.isChecked():
            self.settings.setValue("github_token", self.token.text().strip())
        else:
            self.settings.remove("github_token")

    def parse_repo(self):
        url = self.repo_url.text().strip().rstrip("/")
        if url.endswith(".git"):
            url = url[:-4]
        m = re.search(r"github\.com[/:]([^/]+)/([^/]+)$", url, re.I)
        if not m:
            raise ValueError("仓库地址格式不正确。示例：https://github.com/owner/repo")
        self.repo_owner, self.repo_name = m.group(1), m.group(2)
        return self.repo_owner, self.repo_name

    def github_request(self, path, method="GET", payload=None):
        owner, name = self.parse_repo()
        url = f"https://api.github.com/repos/{owner}/{name}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "GeminiDevStudio",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self.token.text().strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            if e.code == 401:
                raise RuntimeError("GitHub Token 无效或已过期。")
            if e.code == 403:
                raise RuntimeError("GitHub 拒绝访问。请检查 Token 的 Contents 写入权限或 API 限额。")
            if e.code == 404:
                raise RuntimeError("找不到仓库/文件，或当前 Token 无权访问该私有仓库。")
            if e.code == 409:
                raise RuntimeError("GitHub 提交冲突。请重新加载文件后再修改提交。")
            if e.code == 422:
                raise RuntimeError(f"GitHub 拒绝提交：{body_text[:600]}")
            raise RuntimeError(f"GitHub API HTTP {e.code}: {body_text[:600]}")

    def test_connection(self):
        try:
            data = self.github_request("")
            self.save_binding()
            default_branch = data.get("default_branch") or "main"
            if not self.branch.text().strip():
                self.branch.setText(default_branch)
            self.status.setText(
                f"连接成功：{data.get('full_name', '')} · 默认分支 {default_branch}"
            )
            QMessageBox.information(self, "连接成功", "GitHub 仓库连接正常。")
        except Exception as e:
            self.status.setText("连接失败")
            QMessageBox.warning(self, "GitHub 连接失败", str(e))

    def load_tree(self):
        try:
            owner, name = self.parse_repo()
            branch = self.branch.text().strip() or "main"
            data = self.github_request(f"/git/trees/{urllib.parse.quote(branch, safe='')}?recursive=1")
            self.tree.clear()
            count = 0
            for entry in data.get("tree", []):
                path = entry.get("path", "")
                typ = entry.get("type", "")
                if not path:
                    continue
                item = QTreeWidgetItem([path, "文件" if typ == "blob" else "目录"])
                item.setData(0, Qt.UserRole, entry)
                if typ != "blob":
                    item.setDisabled(True)
                self.tree.addTopLevelItem(item)
                count += 1
                if count >= 5000:
                    break
            self.save_binding()
            self.status.setText(f"已加载 {count} 个条目")
        except Exception as e:
            QMessageBox.warning(self, "加载失败", str(e))

    def edit_selected_file(self):
        items = [i for i in self.tree.selectedItems() if not i.isDisabled()]
        if len(items) != 1:
            QMessageBox.information(self, "请选择一个文件", "编辑与提交时请只选择一个仓库文件。")
            return
        if not self.token.text().strip():
            QMessageBox.warning(
                self,
                "需要 GitHub Token",
                "修改并 Push 仓库需要具有 Contents 写权限的 GitHub PAT。"
            )
            return

        try:
            entry = items[0].data(0, Qt.UserRole) or {}
            path = entry.get("path")
            if not path:
                return
            branch = self.branch.text().strip() or "main"
            quoted = urllib.parse.quote(path, safe="/")
            data = self.github_request(
                f"/contents/{quoted}?ref={urllib.parse.quote(branch, safe='')}"
            )
            if data.get("encoding") != "base64" or not data.get("content"):
                raise RuntimeError("该文件无法通过 GitHub Contents API 读取。")
            raw = base64.b64decode(data["content"])
            if b"\x00" in raw[:8192]:
                raise RuntimeError("当前文件看起来是二进制文件，暂不支持直接代码编辑。")
            text = raw.decode("utf-8", errors="replace")
            sha = data.get("sha")
            if not sha:
                raise RuntimeError("未获取到文件 SHA，无法安全提交。")

            editor = GitHubFileEditorDialog(self, path, text, sha)
            editor.exec()
        except Exception as e:
            QMessageBox.warning(self, "打开编辑器失败", str(e))

    def add_selected_to_context(self):
        items = [i for i in self.tree.selectedItems() if not i.isDisabled()]
        if not items:
            QMessageBox.information(self, "未选择文件", "请先在文件树中选择一个或多个文件。")
            return
        try:
            branch = self.branch.text().strip() or "main"
            temp_root = Path(tempfile.gettempdir()) / "GeminiDevStudio" / "github"
            temp_root.mkdir(parents=True, exist_ok=True)
            added = []
            for item in items[:100]:
                entry = item.data(0, Qt.UserRole) or {}
                path = entry.get("path")
                if not path:
                    continue
                quoted = urllib.parse.quote(path, safe="/")
                data = self.github_request(f"/contents/{quoted}?ref={urllib.parse.quote(branch, safe='')}")
                if data.get("encoding") == "base64" and data.get("content"):
                    raw = base64.b64decode(data["content"])
                    target = temp_root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                    added.append(str(target))
            self.parent_window.attach_paths(added)
            self.status.setText(f"已加入 AI 上下文：{len(added)} 个文件")
            QMessageBox.information(self, "已加入上下文", f"已加入 {len(added)} 个仓库文件。")
        except Exception as e:
            QMessageBox.warning(self, "读取失败", str(e))



class GitHubFileEditorDialog(QDialog):
    def __init__(self, repo_dialog, path, original_text, sha):
        super().__init__(repo_dialog)
        self.repo_dialog = repo_dialog
        self.path = path
        self.original_text = original_text
        self.sha = sha
        self.setWindowTitle(f"编辑仓库文件 · {path}")
        self.resize(980, 760)
        self.build_ui()
        self.refresh_diff()

    def build_ui(self):
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        path_label = QLabel(self.path)
        path_label.setStyleSheet("font-weight:700;")
        top.addWidget(path_label)
        top.addStretch()
        self.diff_btn = QPushButton("刷新 Diff")
        self.diff_btn.clicked.connect(self.refresh_diff)
        top.addWidget(self.diff_btn)
        layout.addLayout(top)

        tabs = QTabWidget()

        edit_page = QWidget()
        edit_layout = QVBoxLayout(edit_page)
        edit_hint = QLabel("修改代码后先查看 Diff，再确认提交。")
        edit_hint.setObjectName("helperText")
        edit_layout.addWidget(edit_hint)
        self.editor = QTextEdit()
        self.editor.setFont(QFont("Consolas", 10))
        self.editor.setPlainText(self.original_text)
        edit_layout.addWidget(self.editor)
        tabs.addTab(edit_page, "编辑")

        diff_page = QWidget()
        diff_layout = QVBoxLayout(diff_page)
        self.diff_view = QTextEdit()
        self.diff_view.setReadOnly(True)
        self.diff_view.setFont(QFont("Consolas", 10))
        diff_layout.addWidget(self.diff_view)
        tabs.addTab(diff_page, "Diff")

        layout.addWidget(tabs, 1)

        commit_form = QFormLayout()
        self.commit_message = QLineEdit()
        self.commit_message.setPlaceholderText(f"update {self.path}")
        commit_form.addRow("Commit Message", self.commit_message)
        layout.addLayout(commit_form)

        warning = QLabel("提交会直接更新当前绑定仓库/分支。提交前会再次要求确认。")
        warning.setWordWrap(True)
        warning.setObjectName("helperText")
        layout.addWidget(warning)

        row = QHBoxLayout()
        row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        commit_btn = QPushButton("确认并 Push")
        commit_btn.setStyleSheet(
            "QPushButton { background:#2563eb; color:white; border:none; font-weight:600; padding:7px 18px; }"
            "QPushButton:hover { background:#1d4ed8; }"
        )
        commit_btn.clicked.connect(self.commit_changes)
        row.addWidget(cancel_btn)
        row.addWidget(commit_btn)
        layout.addLayout(row)

    def refresh_diff(self):
        current = self.editor.toPlainText()
        diff = difflib.unified_diff(
            self.original_text.splitlines(),
            current.splitlines(),
            fromfile=f"a/{self.path}",
            tofile=f"b/{self.path}",
            lineterm=""
        )
        text = "\n".join(diff)
        self.diff_view.setPlainText(text or "没有改动。")

    def commit_changes(self):
        current = self.editor.toPlainText()
        if current == self.original_text:
            QMessageBox.information(self, "没有改动", "当前文件内容没有变化，无需提交。")
            return

        message = self.commit_message.text().strip() or f"update {self.path}"
        branch = self.repo_dialog.branch.text().strip() or "main"

        self.refresh_diff()
        answer = QMessageBox.question(
            self,
            "确认提交",
            f"确定将修改提交到：\n{self.repo_dialog.repo_url.text().strip()}\n"
            f"分支：{branch}\n文件：{self.path}\n\nCommit：{message}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        try:
            payload = {
                "message": message,
                "content": base64.b64encode(current.encode("utf-8")).decode("ascii"),
                "sha": self.sha,
                "branch": branch,
            }
            quoted = urllib.parse.quote(self.path, safe="/")
            data = self.repo_dialog.github_request(
                f"/contents/{quoted}",
                method="PUT",
                payload=payload,
            )
            commit_sha = ((data.get("commit") or {}).get("sha") or "")[:12]
            self.repo_dialog.status.setText(f"Push 成功：{self.path} · {commit_sha}")
            QMessageBox.information(
                self,
                "提交成功",
                f"文件已提交并推送到 GitHub。\nCommit: {commit_sha or '已创建'}",
            )
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "提交失败", str(e))


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
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    def expand_input_files(self):
        expanded = []
        temp_dirs = []
        skip_parts = {
            ".git", ".idea", ".vscode", "node_modules", "build", "dist",
            ".gradle", "__pycache__", ".next", "target", "vendor"
        }
        text_exts = {
            ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".kts", ".cs",
            ".cpp", ".c", ".h", ".hpp", ".html", ".htm", ".css", ".scss", ".sass",
            ".json", ".xml", ".yaml", ".yml", ".md", ".txt", ".sql", ".sh", ".ps1",
            ".bat", ".cmd", ".toml", ".ini", ".cfg", ".conf", ".properties",
            ".gradle", ".dart", ".go", ".rs", ".php", ".rb", ".swift", ".vue",
            ".svelte", ".env", ".gitignore", ".dockerignore"
        }

        for original in self.files:
            p = Path(original)
            if p.suffix.lower() != ".zip":
                expanded.append(str(p))
                continue

            try:
                out = Path(tempfile.mkdtemp(prefix="gemini-zip-"))
                temp_dirs.append(out)
                with zipfile.ZipFile(p, "r") as zf:
                    safe_members = []
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        member = Path(info.filename)
                        if member.is_absolute() or ".." in member.parts:
                            continue
                        if any(part.lower() in skip_parts for part in member.parts):
                            continue
                        if info.file_size > 20 * 1024 * 1024:
                            continue
                        safe_members.append(info)
                        if len(safe_members) >= 600:
                            break

                    for info in safe_members:
                        target = out / info.filename
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info, "r") as src, open(target, "wb") as dst:
                            dst.write(src.read())

                candidates = []
                for file in out.rglob("*"):
                    if not file.is_file():
                        continue
                    suffix = file.suffix.lower()
                    mime, _ = mimetypes.guess_type(str(file))
                    mime = mime or ""
                    if (
                        suffix in text_exts
                        or mime.startswith("image/")
                        or mime.startswith("video/")
                        or mime.startswith("audio/")
                        or mime == "application/pdf"
                    ):
                        candidates.append(str(file))

                candidates.sort(key=lambda x: (Path(x).suffix.lower() not in text_exts, len(x), x))
                expanded.extend(candidates[:300])
            except Exception as e:
                raise RuntimeError(f"无法解压项目 {p.name}：{e}")

        return expanded, temp_dirs

    def run(self):
        if self.api_base:
            self.run_openai_compatible()
            return

        uploaded = []
        temp_dirs = []
        try:
            client = genai.Client(api_key=self.api_key)
            input_files, temp_dirs = self.expand_input_files()
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
            for f in input_files:
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
                if self._cancel_requested:
                    self.completed.emit("".join(result))
                    return
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
            for d in temp_dirs:
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass

    def run_openai_compatible(self):
        temp_dirs = []
        try:
            input_files, temp_dirs = self.expand_input_files()
            messages = []
            if self.system_instruction:
                messages.append({"role": "system", "content": self.system_instruction})

            for m in self.history:
                role = "assistant" if m.role == "assistant" else "user"
                messages.append({"role": role, "content": m.text})

            prompt_text = self.prompt or "请分析附件并给出结果。"
            content = [{"type": "text", "text": prompt_text}]

            for path in input_files:
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
                elif mime == "application/pdf":
                    content.append({
                        "type": "file",
                        "file": {
                            "filename": file_path.name,
                            "file_data": data_url
                        }
                    })
                else:
                    content.append({
                        "type": "text",
                        "text": f"\n[已跳过接口不支持的附件类型：{file_path.name} ({mime})]"
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

            if self._cancel_requested:
                self.completed.emit("")
                return

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

            if self._cancel_requested:
                self.completed.emit("")
                return

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
            if "mime type is not supported" in body.lower():
                self.failed.emit(
                    "附件中包含当前模型接口不支持的文件类型。\n"
                    "新版会自动解压 ZIP 并只发送模型支持的源码、图片、视频、音频和 PDF。\n\n"
                    f"接口返回：HTTP {e.code}"
                )
            else:
                self.failed.emit(f"第三方 API 请求失败 HTTP {e.code}:\n{body[:1500]}")
        except Exception as e:
            self.failed.emit(f"第三方 API 请求失败：\n{e}")
        finally:
            for d in temp_dirs:
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass


class PreviewWebPage(QWebEnginePage):
    console_message = Signal(str)

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        source = source_id or "app"
        self.console_message.emit(f"[JS] {source}:{line_number}  {message}")
        super().javaScriptConsoleMessage(level, message, line_number, source_id)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.history = []
        self.files = []
        self.worker = None
        self.current_response = ""
        self.project_root = None
        self.build_process = None
        self.preview_server = None
        self.preview_server_thread = None
        self.preview_url = ""
        self.auto_fix_attempts = 0
        self.max_auto_fix_attempts = 2
        self._build_log_buffer = ""
        self._runtime_error_buffer = []
        self._auto_fix_timer = QTimer(self)
        self._auto_fix_timer.setSingleShot(True)
        self._auto_fix_timer.timeout.connect(self.trigger_runtime_auto_fix)

        default_workspace = Path.home() / "Documents" / "GeminiDevStudioData" / "workspaces"
        self.workspace_root = Path(self.settings.value("workspace_root", str(default_workspace)))
        self.workspace_root.mkdir(parents=True, exist_ok=True)

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
            ("GitHub 仓库", self.open_github_repo),
            ("导出回答", self.export_answer),
        ]:
            action = QAction(name, self)
            action.triggered.connect(fn)
            bar.addAction(action)

        bar.addSeparator()
        theme_label = QLabel("主题")
        bar.addWidget(theme_label)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["浅色", "深色", "护眼", "灰蓝"])
        self.theme_combo.setFixedWidth(88)
        self.theme_combo.currentTextChanged.connect(self.apply_theme)
        bar.addWidget(self.theme_combo)

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
        self.file_list.setMaximumHeight(220)
        self.file_list.setIconSize(QSize(72, 54))
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
        self.repo_label = QLabel("GitHub：未绑定")
        self.repo_label.setObjectName("helperText")
        head.addWidget(self.repo_label)
        self.context_label = QLabel("0 条消息 · 0 个附件")
        head.addWidget(self.context_label)
        rl.addLayout(head)

        self.tabs = QTabWidget()
        self.chat = QWebEngineView()

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
        visual_layout.setContentsMargins(0, 0, 0, 0)

        visual_split = QSplitter(Qt.Horizontal)

        # 左侧：AI 对话与操作区
        visual_left = QWidget()
        visual_left.setMinimumWidth(310)
        visual_left.setMaximumWidth(460)
        visual_left_layout = QVBoxLayout(visual_left)
        visual_left_layout.setContentsMargins(10, 10, 8, 10)

        left_title_row = QHBoxLayout()
        left_title = QLabel("✦ Gemini")
        left_title.setStyleSheet("font-size:15px;font-weight:700;")
        left_title_row.addWidget(left_title)
        left_title_row.addStretch()
        self.visual_model_label = QLabel(self.model.currentText())
        self.visual_model_label.setObjectName("helperText")
        left_title_row.addWidget(self.visual_model_label)
        visual_left_layout.addLayout(left_title_row)

        self.visual_chat = QWebEngineView()
        self.visual_chat.setMinimumHeight(360)
        visual_left_layout.addWidget(self.visual_chat, 1)

        quick_row = QHBoxLayout()
        for title, prompt_text in [
            ("修复错误", "检查当前项目并修复应用程序中的错误"),
            ("新增功能", "基于当前项目新增一个实用功能"),
            ("优化界面", "优化当前应用界面和交互体验"),
        ]:
            btn = QPushButton(title)
            btn.clicked.connect(lambda _=False, p=prompt_text: self.visual_quick_prompt(p))
            quick_row.addWidget(btn)
        visual_left_layout.addLayout(quick_row)

        self.visual_prompt = SendTextEdit()
        self.visual_prompt.setPlaceholderText("进行修改、添加新功能，提出任何要求…")
        self.visual_prompt.setMaximumHeight(105)
        self.visual_prompt.send_requested.connect(self.send_visual_prompt)
        self.visual_prompt.files_attached.connect(self.attach_paths)
        visual_left_layout.addWidget(self.visual_prompt)

        visual_send_row = QHBoxLayout()
        self.visual_attach_btn = QPushButton("＋")
        self.visual_attach_btn.setFixedWidth(38)
        self.visual_attach_btn.clicked.connect(self.add_files)
        visual_send_row.addWidget(self.visual_attach_btn)
        visual_send_row.addStretch()
        self.visual_build_btn = QPushButton("构建")
        self.visual_build_btn.clicked.connect(self.send_visual_prompt)
        visual_send_row.addWidget(self.visual_build_btn)
        visual_left_layout.addLayout(visual_send_row)

        # 右侧：真实可视化画布
        visual_right = QWidget()
        visual_right_layout = QVBoxLayout(visual_right)
        visual_right_layout.setContentsMargins(0, 0, 0, 0)

        visual_bar = QHBoxLayout()
        visual_bar.setContentsMargins(10, 8, 10, 8)

        self.visual_status = QLabel("预览")
        self.visual_status.setStyleSheet("font-weight:700;")
        visual_bar.addWidget(self.visual_status)

        self.project_status = QLabel("未载入项目")
        self.project_status.setObjectName("helperText")
        visual_bar.addWidget(self.project_status)

        code_switch_btn = QPushButton("</>")
        code_switch_btn.setToolTip("切换到代码")
        code_switch_btn.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        visual_bar.addWidget(code_switch_btn)

        visual_bar.addStretch()

        self.preview_device = QComboBox()
        self.preview_device.addItems(["iPhone 16 Pro", "Android", "平板", "网页"])
        self.preview_device.setFixedWidth(125)
        self.preview_device.currentTextChanged.connect(self.on_preview_device_changed)
        visual_bar.addWidget(self.preview_device)

        self.preview_path = QLineEdit("/")
        self.preview_path.setFixedWidth(170)
        self.preview_path.setAlignment(Qt.AlignCenter)
        visual_bar.addWidget(self.preview_path)

        visual_refresh = QPushButton("↻")
        visual_refresh.setToolTip("刷新预览")
        visual_refresh.clicked.connect(self.refresh_visual_preview)
        visual_bar.addWidget(visual_refresh)

        self.preview_zoom = QComboBox()
        self.preview_zoom.addItems(["75%", "90%", "100%", "110%", "125%"])
        self.preview_zoom.setCurrentText("100%")
        self.preview_zoom.setFixedWidth(78)
        self.preview_zoom.currentTextChanged.connect(self.apply_preview_zoom)
        visual_bar.addWidget(self.preview_zoom)

        fullscreen_btn = QPushButton("⛶")
        fullscreen_btn.setToolTip("全屏预览")
        fullscreen_btn.clicked.connect(self.toggle_preview_fullscreen)
        visual_bar.addWidget(fullscreen_btn)

        visual_right_layout.addLayout(visual_bar)

        self.preview_stage = QWidget()
        self.preview_stage.setObjectName("previewStage")
        self.preview_stage.setStyleSheet("#previewStage { background:#05080f; }")
        stage_layout = QHBoxLayout(self.preview_stage)
        stage_layout.setContentsMargins(18, 18, 18, 18)
        stage_layout.addStretch()

        self.device_frame = QWidget()
        self.device_frame.setObjectName("deviceFrame")
        self.device_frame.setStyleSheet(
            "#deviceFrame { background:#111827; border:10px solid #111827; border-radius:34px; }"
        )
        frame_layout = QVBoxLayout(self.device_frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)

        self.web_preview = QWebEngineView()
        self.preview_page = PreviewWebPage(self.web_preview)
        self.preview_page.console_message.connect(self.on_preview_console_message)
        self.web_preview.setPage(self.preview_page)
        self.web_preview.loadFinished.connect(self.on_preview_load_finished)
        frame_layout.addWidget(self.web_preview)

        stage_layout.addWidget(self.device_frame, 0, Qt.AlignCenter)
        stage_layout.addStretch()

        visual_right_layout.addWidget(self.preview_stage, 1)
        self.apply_device_frame()

        visual_split.addWidget(visual_left)
        visual_split.addWidget(visual_right)
        visual_split.setStretchFactor(0, 0)
        visual_split.setStretchFactor(1, 1)
        visual_split.setSizes([360, 980])

        visual_layout.addWidget(visual_split, 1)

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
        self.stop_btn = QPushButton("停止")
        self.stop_btn.clicked.connect(self.stop_generation)
        self.stop_btn.setMinimumHeight(40)
        self.stop_btn.setEnabled(False)

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
        send_row.addWidget(self.stop_btn)
        send_row.addWidget(self.send_btn)
        rl.addLayout(send_row)

        root.addWidget(right)
        root.setStretchFactor(1, 1)
        self.statusBar().showMessage("就绪")

    def apply_theme(self, theme_name):
        self.settings.setValue("theme", theme_name)

        themes = {
            "浅色": {
                "window": "#edf1f7",
                "panel": "#f7f9fc",
                "card": "#ffffff",
                "input": "#ffffff",
                "text": "#182230",
                "muted": "#667085",
                "border": "#cfd6e2",
                "tab": "#e6ebf3",
                "tab_selected": "#ffffff",
                "hover": "#edf3ff",
                "pressed": "#e1e8f2",
                "accent": "#2563eb",
                "accent_hover": "#1d4ed8",
                "assistant": "#ffffff",
                "user": "#e9f0ff",
            },
            "深色": {
                "window": "#17191d",
                "panel": "#1e2127",
                "card": "#23272f",
                "input": "#1f232a",
                "text": "#e8ebf0",
                "muted": "#a7afbd",
                "border": "#3a414c",
                "tab": "#2a2f37",
                "tab_selected": "#343a44",
                "hover": "#303641",
                "pressed": "#3a414c",
                "accent": "#4f8cff",
                "accent_hover": "#6b9cff",
                "assistant": "#23272f",
                "user": "#263449",
            },
            "护眼": {
                "window": "#e9e6dc",
                "panel": "#f0eee6",
                "card": "#f7f5ee",
                "input": "#f3f1e9",
                "text": "#2d312c",
                "muted": "#70756c",
                "border": "#c9c6bb",
                "tab": "#dedbd1",
                "tab_selected": "#f7f5ee",
                "hover": "#e8e4d8",
                "pressed": "#ddd8ca",
                "accent": "#4f7d5d",
                "accent_hover": "#41694d",
                "assistant": "#f7f5ee",
                "user": "#e3ecdf",
            },
            "灰蓝": {
                "window": "#1f2833",
                "panel": "#26313d",
                "card": "#2c3946",
                "input": "#25313d",
                "text": "#eef3f8",
                "muted": "#aebdca",
                "border": "#415161",
                "tab": "#334252",
                "tab_selected": "#3b4b5d",
                "hover": "#38495a",
                "pressed": "#435568",
                "accent": "#5b9cf5",
                "accent_hover": "#74adfa",
                "assistant": "#2c3946",
                "user": "#2e435a",
            },
        }

        t = themes.get(theme_name, themes["护眼"])
        QApplication.instance().setStyleSheet(f"""
            QMainWindow, QWidget {{
                font-family: "Microsoft YaHei UI", "Segoe UI";
                font-size: 13px;
                color: {t['text']};
                background: {t['window']};
            }}
            QToolBar {{
                spacing: 8px;
                padding: 7px 10px;
                border: none;
                border-bottom: 1px solid {t['border']};
                background: {t['panel']};
            }}
            QToolButton {{
                padding: 7px 10px;
                border-radius: 6px;
                color: {t['text']};
            }}
            QToolButton:hover {{
                background: {t['hover']};
            }}
            QLabel {{
                background: transparent;
                color: {t['text']};
            }}
            QLabel#helperText {{
                color: {t['muted']};
                font-size: 12px;
            }}
            QLineEdit, QTextEdit, QTextBrowser, QListWidget, QTreeWidget,
            QComboBox, QSpinBox {{
                background: {t['input']};
                color: {t['text']};
                border: 1px solid {t['border']};
                border-radius: 7px;
                padding: 6px;
                selection-background-color: {t['accent']};
                selection-color: #ffffff;
            }}
            QLineEdit:focus, QTextEdit:focus, QListWidget:focus,
            QTreeWidget:focus, QComboBox:focus {{
                border: 1px solid {t['accent']};
            }}
            QComboBox QAbstractItemView {{
                background: {t['card']};
                color: {t['text']};
                border: 1px solid {t['border']};
                selection-background-color: {t['hover']};
            }}
            QPushButton {{
                min-height: 30px;
                padding: 4px 12px;
                border-radius: 7px;
                border: 1px solid {t['border']};
                color: {t['text']};
                background: {t['card']};
            }}
            QPushButton:hover {{
                background: {t['hover']};
            }}
            QPushButton:pressed {{
                background: {t['pressed']};
            }}
            QTabWidget::pane {{
                border: 1px solid {t['border']};
                background: {t['card']};
                border-radius: 8px;
            }}
            QTabBar::tab {{
                padding: 8px 16px;
                margin-right: 3px;
                border-top-left-radius: 7px;
                border-top-right-radius: 7px;
                color: {t['text']};
                background: {t['tab']};
            }}
            QTabBar::tab:selected {{
                color: {t['accent']};
                background: {t['tab_selected']};
                font-weight: 600;
            }}
            QStatusBar {{
                background: {t['panel']};
                color: {t['text']};
                border-top: 1px solid {t['border']};
            }}
            QSplitter::handle {{
                background: {t['border']};
            }}
            QScrollBar:vertical {{
                background: {t['panel']};
                width: 12px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {t['border']};
                min-height: 28px;
                border-radius: 5px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
        """)

        self.send_btn.setStyleSheet(
            f"QPushButton {{ background:{t['accent']}; color:white; border:none; "
            "font-weight:600; padding:7px 20px; border-radius:7px; }}"
            f"QPushButton:hover {{ background:{t['accent_hover']}; }}"
            "QPushButton:disabled { background:#7a7f87; color:#d7d7d7; }"
        )
        self.stop_btn.setStyleSheet(
            "QPushButton { background:#dc2626; color:white; border:none; "
            "font-weight:600; padding:7px 18px; border-radius:7px; }"
            "QPushButton:hover { background:#b91c1c; }"
            "QPushButton:disabled { background:#7a7f87; color:#d7d7d7; }"
        )

        self._theme_colors = t
        self.refresh_chat()

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

    def open_github_repo(self):
        dialog = GitHubRepoDialog(self)
        dialog.exec()
        repo_url = self.settings.value("github_repo_url", "")
        if repo_url:
            self.repo_label.setText(f"GitHub：{repo_url.rstrip('/').split('/')[-1]}")
        else:
            self.repo_label.setText("GitHub：未绑定")

        theme = self.settings.value("theme", "护眼")
        idx = self.theme_combo.findText(theme)
        if idx >= 0:
            self.theme_combo.setCurrentIndex(idx)
        else:
            self.theme_combo.setCurrentText("护眼")
        self.apply_theme(self.theme_combo.currentText())

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

        repo_url = self.settings.value("github_repo_url", "")
        if repo_url:
            self.repo_label.setText(f"GitHub：{repo_url.rstrip('/').split('/')[-1]}")
        else:
            self.repo_label.setText("GitHub：未绑定")

    def save_settings(self):
        self.settings.setValue("workspace_root", str(self.workspace_root))
        self.settings.setValue("model", self.model.currentText().strip())
        self.settings.setValue("api_base", self.api_base.text().strip())
        self.settings.setValue("system_prompt", self.system_prompt.toPlainText())
        self.settings.setValue("temperature", self.temperature.value())
        self.settings.setValue("top_p", self.top_p.value())
        self.settings.setValue("top_k", self.top_k.value())
        self.settings.setValue("max_tokens", self.max_tokens.value())
        self.settings.setValue("theme", self.theme_combo.currentText())
        self.settings.setValue("remember_key", self.remember.isChecked())
        if self.remember.isChecked():
            self.settings.setValue("api_key", self.api_key.text().strip())
        else:
            self.settings.remove("api_key")

    def closeEvent(self, event):
        self.stop_preview_server()
        if self.build_process and self.build_process.state() != QProcess.NotRunning:
            self.build_process.kill()
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
                mime = mime or "application/octet-stream"
                label = Path(p).name

                if Path(p).suffix.lower() == ".zip":
                    label += "   [项目压缩包 · 自动解压]"
                    try:
                        self.import_project_zip(Path(p))
                    except Exception as e:
                        QMessageBox.warning(self, "项目导入失败", str(e))
                elif mime.startswith("image/"):
                    label += "   [图片]"
                elif mime.startswith("video/"):
                    label += "   [视频]"
                elif mime.startswith("audio/"):
                    label += "   [音频]"
                elif mime == "application/pdf":
                    label += "   [PDF]"
                else:
                    label += f"   [{mime}]"

                item = QListWidgetItem(label)
                item.setToolTip(str(Path(p).resolve()))
                if mime.startswith("image/"):
                    pix = QPixmap(p)
                    if not pix.isNull():
                        thumb = pix.scaled(72, 54, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        item.setIcon(QIcon(thumb))
                self.file_list.addItem(item)
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

    def collect_project_context_files(self):
        if not self.project_root or not Path(self.project_root).exists():
            return []

        root = Path(self.project_root)
        skip_dirs = {
            ".git", ".idea", ".vscode", "node_modules", "dist", "build",
            "out", "www", ".next", ".gradle", "__pycache__", "coverage",
            "Pods", "DerivedData"
        }
        allowed_exts = {
            ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
            ".json", ".html", ".css", ".scss", ".sass", ".less",
            ".vue", ".svelte", ".md", ".txt", ".xml", ".yaml", ".yml",
            ".java", ".kt", ".kts", ".gradle", ".properties",
            ".py", ".go", ".rs", ".php", ".rb", ".swift",
            ".c", ".cpp", ".h", ".hpp", ".cs", ".dart",
            ".sql", ".sh", ".ps1", ".bat", ".cmd", ".toml", ".ini", ".env"
        }
        priority_names = {
            "package.json", "vite.config.ts", "vite.config.js",
            "tsconfig.json", "index.html", "README.md",
            "capacitor.config.ts", "capacitor.config.json"
        }

        files = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(part in skip_dirs for part in rel.parts):
                continue
            if path.name in priority_names or path.suffix.lower() in allowed_exts:
                try:
                    if path.stat().st_size <= 800 * 1024:
                        files.append(path)
                except OSError:
                    continue

        def score(p):
            rel = p.relative_to(root).as_posix()
            if p.name in priority_names:
                return (0, len(rel), rel)
            if rel.startswith("src/"):
                return (1, len(rel), rel)
            if rel.startswith("app/"):
                return (2, len(rel), rel)
            return (3, len(rel), rel)

        files.sort(key=score)
        return [str(p) for p in files[:180]]

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
        self.stop_btn.setEnabled(True)

        auto_instruction = """
你正在一个可自动写回项目并实时构建的桌面开发环境中。
如果用户要求修改、修复、增加功能或调整界面，请在解释之后输出每一个实际修改文件的【完整内容】，
格式必须严格使用：

```file:相对项目路径
完整文件内容
```

例如：
```file:src/App.tsx
...完整文件...
```

不要只输出 diff，不要省略未修改部分，不要用“其余不变”。
只有真正需要修改或新增的文件才输出 file: 文件块。
如果是 Web/React/Vite/Capacitor 项目，确保修改后项目能通过现有 build 脚本构建。
你会收到当前项目的最新源码上下文。必须基于现有文件修改，不要凭空重建无关架构。
所有需要变更的文件都必须以 file: 文件块输出，桌面程序会自动写入并立即构建、运行和刷新可视化。
"""
        system_instruction = self.system_prompt.toPlainText().strip()
        if self.project_root:
            system_instruction = (system_instruction + "\n\n" + auto_instruction).strip()

        context_files = []
        seen = set()
        for p in list(self.files) + self.collect_project_context_files():
            if p not in seen and Path(p).exists():
                seen.add(p)
                context_files.append(p)

        self.worker = GeminiWorker(
            key,
            self.model.currentText().strip() or DEFAULT_MODEL,
            self.api_base.text().strip(),
            previous,
            prompt,
            context_files,
            system_instruction,
            self.temperature.value() / 100,
            self.top_p.value() / 100,
            self.top_k.value(),
            self.max_tokens.value(),
        )
        self.worker.chunk.connect(self.on_chunk)
        self.worker.completed.connect(self.on_done)
        self.worker.failed.connect(self.on_error)
        self.worker.start()

    def stop_generation(self):
        if not self.worker or not self.worker.isRunning():
            self.stop_btn.setEnabled(False)
            return

        self.worker.cancel()
        self.stop_btn.setEnabled(False)
        self.statusBar().showMessage("正在停止生成…")

        if self.history and self.history[-1].role == "user":
            pass

    def on_chunk(self, text):
        self.current_response += text
        self.raw.moveCursor(QTextCursor.End)
        self.raw.insertPlainText(text)
        self.refresh_chat(streaming=self.current_response)

    def on_done(self, text):
        stopped = bool(self.worker and getattr(self.worker, "_cancel_requested", False))
        final_text = text or self.current_response

        if final_text:
            self.history.append(ChatMessage("assistant", final_text))

        self.current_response = ""
        self.send_btn.setEnabled(True)
        self.send_btn.setText("发送")
        self.stop_btn.setEnabled(False)

        if stopped:
            self.statusBar().showMessage("已停止生成，指令与附件已保留", 5000)
        else:
            self.files.clear()
            self.file_list.clear()
            self.statusBar().showMessage("生成完成", 4000)

        self.refresh_chat()

        if (not stopped) and final_text and self.project_root:
            changed = self.apply_ai_file_blocks(final_text)
            if changed:
                self.statusBar().showMessage(f"已自动修改 {len(changed)} 个项目文件，正在构建…", 5000)
                self.start_project_build(changed)
            else:
                self.refresh_visual_preview()
        else:
            self.refresh_visual_preview()

    def import_project_zip(self, zip_path):
        if not zipfile.is_zipfile(zip_path):
            raise RuntimeError("不是有效的 ZIP 项目压缩包。")

        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", zip_path.stem).strip("-") or "project"
        target = self.workspace_root / safe_name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    continue
                if info.file_size > 100 * 1024 * 1024:
                    continue
                out = target / member
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)

        children = [p for p in target.iterdir()]
        if len(children) == 1 and children[0].is_dir():
            candidate = children[0]
            if (candidate / "package.json").exists() or (candidate / "index.html").exists():
                target = candidate

        self.project_root = target
        self.settings.setValue("last_project_root", str(target))
        if hasattr(self, "project_status"):
            self.project_status.setText(f"项目：{target.name}")
            self.project_status.setToolTip(str(target))
        self.statusBar().showMessage(f"项目已导入工作区：{target}", 6000)

    def parse_ai_file_blocks(self, text):
        changes = []
        pattern = re.compile(
            r"\`\`\`file:([^\r\n]+)\r?\n(.*?)\`\`\`",
            re.S | re.I,
        )
        for match in pattern.finditer(text or ""):
            rel = match.group(1).strip().replace("\\\\", "/")
            body = match.group(2)
            if not rel:
                continue
            rel_path = Path(rel)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                continue
            changes.append((rel_path, body))
        return changes

    def apply_ai_file_blocks(self, text):
        if not self.project_root:
            return []

        root = self.project_root.resolve()
        applied = []
        for rel_path, body in self.parse_ai_file_blocks(text):
            target = (root / rel_path).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            applied.append(str(rel_path))

        if applied:
            self.raw.setPlainText(
                "已自动写入当前项目，并将立即重新构建：\n\n"
                + "\n".join(f"✓ {p}" for p in applied)
            )
            if hasattr(self, "project_status"):
                self.project_status.setText(f"项目：{Path(self.project_root).name} · 已修改 {len(applied)} 个文件")
        return applied

    def detect_preview_directory(self):
        if not self.project_root:
            return None
        root = self.project_root
        for name in ("dist", "build", "www", "out"):
            candidate = root / name
            if (candidate / "index.html").exists():
                return candidate
        if (root / "index.html").exists():
            return root
        return None

    def start_project_build(self, changed_files=None):
        if not self.project_root:
            self.refresh_visual_preview()
            return

        root = self.project_root
        package_file = root / "package.json"

        if not package_file.exists():
            preview_dir = self.detect_preview_directory()
            if preview_dir:
                self.auto_fix_attempts = 0
        self.start_preview_server(preview_dir)
            else:
                self.visual_status.setText("项目没有可预览入口")
            return

        try:
            package = json.loads(package_file.read_text(encoding="utf-8"))
        except Exception as e:
            self.visual_status.setText("package.json 无法读取")
            self.statusBar().showMessage(str(e), 7000)
            return

        scripts = package.get("scripts") or {}
        if "build" not in scripts:
            preview_dir = self.detect_preview_directory()
            if preview_dir:
                self.start_preview_server(preview_dir)
            else:
                self.visual_status.setText("package.json 没有 build 脚本")
            return

        npm = shutil.which("npm.cmd") or shutil.which("npm")
        if not npm:
            self.visual_status.setText("需要安装 Node.js")
            QMessageBox.warning(
                self,
                "无法自动构建",
                "当前电脑没有检测到 npm。\n\n"
                "React / Vite / Capacitor 项目需要安装 Node.js 后，软件才能自动执行 npm install 和 npm run build。"
            )
            return

        if self.build_process and self.build_process.state() != QProcess.NotRunning:
            self.build_process.kill()

        self.build_process = QProcess(self)
        self.build_process.setWorkingDirectory(str(root))
        self.build_process.setProcessChannelMode(QProcess.MergedChannels)
        self.build_process.readyReadStandardOutput.connect(self.on_build_output)
        self.build_process.finished.connect(self.on_build_finished)

        self.visual_status.setText("正在构建…")
        self.visual_build_btn.setEnabled(False)
        self._build_log_buffer = ""

        if not (root / "node_modules").exists():
            self._build_stage = "install"
            self.build_process.start(npm, ["install", "--no-audit", "--no-fund"])
        else:
            self._build_stage = "build"
            self.build_process.start(npm, ["run", "build"])

    def on_build_output(self):
        if not self.build_process:
            return
        text = bytes(self.build_process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if text:
            self._build_log_buffer = (self._build_log_buffer + text)[-60000:]
            current = self.raw.toPlainText()
            self.raw.setPlainText((current + "\n" + text)[-120000:])
            cursor = self.raw.textCursor()
            cursor.movePosition(QTextCursor.End)
            self.raw.setTextCursor(cursor)

    def on_build_finished(self, exit_code, exit_status):
        if not self.build_process:
            return

        npm = shutil.which("npm.cmd") or shutil.which("npm")
        if getattr(self, "_build_stage", "") == "install" and exit_code == 0 and npm:
            self._build_stage = "build"
            self.visual_status.setText("依赖完成，正在构建…")
            self.build_process.start(npm, ["run", "build"])
            return

        self.visual_build_btn.setEnabled(True)

        if exit_code != 0:
            self.visual_status.setText("构建失败")
            self.statusBar().showMessage("项目构建失败，正在尝试让 AI 自动修复…", 8000)
            self.tabs.setCurrentIndex(1)
            if self.auto_fix_attempts < self.max_auto_fix_attempts:
                self.auto_fix_attempts += 1
                self.request_auto_fix(
                    "构建失败",
                    self._build_log_buffer or self.raw.toPlainText()[-12000:]
                )
            return

        preview_dir = self.detect_preview_directory()
        if not preview_dir:
            self.visual_status.setText("构建成功，但未找到预览目录")
            self.statusBar().showMessage("未找到 dist/build/www/out/index.html", 8000)
            return

        self.start_preview_server(preview_dir)

    def start_preview_server(self, directory):
        self.stop_preview_server()

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

        handler = lambda *args, **kwargs: QuietHandler(
            *args, directory=str(directory), **kwargs
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.preview_server = server
        port = server.server_address[1]
        self.preview_url = f"http://127.0.0.1:{port}/"

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.preview_server_thread = thread
        thread.start()

        self.visual_status.setText("运行中")
        self.preview_path.setText("/")
        self.statusBar().showMessage(f"构建成功，实时预览：{self.preview_url}", 7000)
        self.refresh_visual_preview()

    def stop_preview_server(self):
        if self.preview_server:
            try:
                self.preview_server.shutdown()
                self.preview_server.server_close()
            except Exception:
                pass
        self.preview_server = None
        self.preview_server_thread = None
        self.preview_url = ""

    def visual_quick_prompt(self, text):
        self.visual_prompt.setPlainText(text)
        self.visual_prompt.setFocus()

    def send_visual_prompt(self):
        text = self.visual_prompt.toPlainText().strip()
        if not text:
            return
        self.prompt.setPlainText(text)
        self.visual_prompt.clear()
        self.tabs.setCurrentIndex(2)
        self.send_prompt()

    def apply_preview_zoom(self, value):
        try:
            zoom = int(value.replace("%", "")) / 100.0
            self.web_preview.setZoomFactor(zoom)
        except Exception:
            self.web_preview.setZoomFactor(1.0)

    def toggle_preview_fullscreen(self):
        self.web_preview.setWindowFlag(Qt.Window, True)
        if self.web_preview.isFullScreen():
            self.web_preview.showNormal()
        else:
            self.web_preview.showFullScreen()

    def on_preview_device_changed(self, _text):
        self.apply_device_frame()
        self.refresh_visual_preview()

    def apply_device_frame(self):
        if not hasattr(self, "device_frame"):
            return
        device = self.preview_device.currentText() if hasattr(self, "preview_device") else "iPhone 16 Pro"

        if device == "网页":
            self.device_frame.setMinimumSize(700, 500)
            self.device_frame.setMaximumSize(16777215, 16777215)
            self.device_frame.setStyleSheet(
                "#deviceFrame { background:#111827; border:2px solid #334155; border-radius:14px; }"
            )
        elif device == "平板":
            self.device_frame.setFixedSize(820, 1080)
            self.device_frame.setStyleSheet(
                "#deviceFrame { background:#111827; border:12px solid #111827; border-radius:28px; }"
            )
        elif device == "Android":
            self.device_frame.setFixedSize(410, 864)
            self.device_frame.setStyleSheet(
                "#deviceFrame { background:#111827; border:10px solid #111827; border-radius:34px; }"
            )
        else:
            self.device_frame.setFixedSize(417, 876)
            self.device_frame.setStyleSheet(
                "#deviceFrame { background:#111827; border:12px solid #111827; border-radius:46px; }"
            )

    def on_preview_console_message(self, message):
        if not message:
            return
        current = self.raw.toPlainText()
        if message not in current[-12000:]:
            self.raw.setPlainText((current + "\n" + message)[-120000:])
            cursor = self.raw.textCursor()
            cursor.movePosition(QTextCursor.End)
            self.raw.setTextCursor(cursor)
        lower = message.lower()
        if any(k in lower for k in ("uncaught", "typeerror", "referenceerror", "syntaxerror", "failed to load", "chunkloaderror")):
            self.visual_status.setText("运行错误")
            self.statusBar().showMessage("预览检测到运行错误，准备自动修复…", 8000)
            self._runtime_error_buffer.append(message)
            self._runtime_error_buffer = self._runtime_error_buffer[-20:]
            if self.auto_fix_attempts < self.max_auto_fix_attempts and not self._auto_fix_timer.isActive():
                self._auto_fix_timer.start(1400)

    def trigger_runtime_auto_fix(self):
        if not self._runtime_error_buffer:
            return
        if self.auto_fix_attempts >= self.max_auto_fix_attempts:
            return
        errors = "\n".join(self._runtime_error_buffer[-12:])
        self._runtime_error_buffer.clear()
        self.auto_fix_attempts += 1
        self.request_auto_fix("运行时错误", errors)

    def request_auto_fix(self, reason, error_text):
        if not self.project_root:
            return
        if self.worker and self.worker.isRunning():
            return

        fix_prompt = (
            f"当前项目自动修改后出现{reason}。请根据当前项目最新源码和下面错误日志直接修复。"
            "必须输出所有需要修改文件的完整 file: 文件块，不要只解释。\n\n"
            f"错误日志：\n{error_text[-14000:]}"
        )
        self.prompt.setPlainText(fix_prompt)
        self.tabs.setCurrentIndex(2)
        self.statusBar().showMessage(f"AI 自动修复中（{self.auto_fix_attempts}/{self.max_auto_fix_attempts}）…", 8000)
        self.send_prompt()

    def on_preview_load_finished(self, ok):
        if ok:
            if self.preview_url:
                self.visual_status.setText("运行中")
                self._runtime_error_buffer.clear()
                QTimer.singleShot(2500, self.mark_preview_stable)
        else:
            self.visual_status.setText("预览加载失败")
            self.statusBar().showMessage("应用页面加载失败，请查看“代码”页日志", 8000)

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
        self.apply_device_frame()

        if self.preview_url:
            path = self.preview_path.text().strip() if hasattr(self, "preview_path") else "/"
            if not path.startswith("/"):
                path = "/" + path
            app_url = self.preview_url.rstrip("/") + path
            self.web_preview.load(QUrl(app_url))
            self.visual_status.setText("正在加载…")
        else:
            code = self.raw.toPlainText().strip()
            page_html = self.extract_html(code)
            if not page_html:
                page_html = """
                <!doctype html>
                <html>
                <body style="margin:0;background:#07111f;color:#e5edf7;font-family:Segoe UI,sans-serif;">
                  <div style="padding:24px">
                    <h2>等待项目构建</h2>
                    <p style="line-height:1.7;color:#aebfd1">
                    上传项目 ZIP 后，Gemini 返回的 file: 文件块会自动写入项目，
                    软件随后自动构建，并在这里直接运行真实应用。
                    </p>
                  </div>
                </body>
                </html>
                """
                self.visual_status.setText("等待项目构建")
            self.web_preview.setHtml(page_html)

        self.apply_preview_zoom(self.preview_zoom.currentText())

        if hasattr(self, "visual_chat"):
            self.visual_chat.setHtml(self.chat_document())

    def mark_preview_stable(self):
        if self.visual_status.text() == "运行中":
            self.auto_fix_attempts = 0

    def on_error(self, text):
        self.send_btn.setEnabled(True)
        self.send_btn.setText("发送")
        self.stop_btn.setEnabled(False)

        error_record = f"【请求失败】\n{text}"
        self.history.append(ChatMessage("assistant", error_record))

        self.raw.setPlainText(error_record)
        self.statusBar().showMessage("请求失败，指令与错误记录已保留", 6000)

        QMessageBox.critical(
            self,
            "Gemini API 请求失败",
            f"{text}\n\n本次指令和错误信息已保留在对话中，附件也不会被清空。"
        )
        self.refresh_chat()

    @staticmethod
    def esc(text):
        return html.escape(text or "")

    def render_markdownish(self, text):
        text = text or ""
        parts = []
        pos = 0
        code_re = re.compile(r"\`\`\`([A-Za-z0-9_+.-]*)\n?(.*?)\`\`\`", re.S)

        for match in code_re.finditer(text):
            before = text[pos:match.start()]
            if before:
                safe = html.escape(before)
                safe = re.sub(r"(?m)^###\s+(.+)$", r"<h3>\1</h3>", safe)
                safe = re.sub(r"(?m)^##\s+(.+)$", r"<h2>\1</h2>", safe)
                safe = re.sub(r"(?m)^#\s+(.+)$", r"<h1>\1</h1>", safe)
                safe = re.sub(r"(?m)^\s*[-*]\s+(.+)$", r"• \1", safe)
                safe = safe.replace("\n", "<br>")
                parts.append(f"<div class='prose'>{safe}</div>")

            lang = html.escape(match.group(1) or "text")
            code = match.group(2)
            encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
            safe_code = html.escape(code)
            parts.append(
                "<div class='code-card'>"
                f"<div class='code-head'><span>{lang}</span>"
                f'<button onclick="copyB64(\'{encoded}\', this)">复制代码</button></div>'
                f"<pre><code>{safe_code}</code></pre></div>"
            )
            pos = match.end()

        tail = text[pos:]
        if tail:
            safe = html.escape(tail)
            safe = re.sub(r"(?m)^###\s+(.+)$", r"<h3>\1</h3>", safe)
            safe = re.sub(r"(?m)^##\s+(.+)$", r"<h2>\1</h2>", safe)
            safe = re.sub(r"(?m)^#\s+(.+)$", r"<h1>\1</h1>", safe)
            safe = re.sub(r"(?m)^\s*[-*]\s+(.+)$", r"• \1", safe)
            safe = safe.replace("\n", "<br>")
            parts.append(f"<div class='prose'>{safe}</div>")

        return "".join(parts)

    def chat_document(self, streaming=""):
        colors = getattr(self, "_theme_colors", {
            "user": "#e9f0ff",
            "assistant": "#ffffff",
            "text": "#182230",
            "border": "#cfd6e2",
            "window": "#edf1f7",
            "panel": "#f7f9fc",
            "muted": "#667085",
            "accent": "#2563eb",
        })

        cards = []
        messages = list(self.history)
        if streaming:
            messages.append(ChatMessage("assistant", streaming))

        for m in messages:
            is_user = m.role == "user"
            title = "你" if is_user else "Gemini"
            bg = colors["user"] if is_user else colors["assistant"]
            body = self.render_markdownish(m.text)
            raw_b64 = base64.b64encode((m.text or "").encode("utf-8")).decode("ascii")

            actions = ""
            if not is_user:
                actions = (
                    f'<button class="copy-answer" onclick="copyB64(\'{raw_b64}\', this)">复制回答</button>'
                )

            if (not is_user) and len(m.text or "") > 1800:
                body = (
                    "<details class='fold'><summary>回答较长，点击展开全部</summary>"
                    f"<div class='fold-body'>{body}</div></details>"
                )

            role_class = "user-card" if is_user else "assistant-card"
            avatar = "你" if is_user else "G"
            cards.append(
                f"<section class='msg {role_class}' style='background:{bg};'>"
                f"<div class='msg-head'><span class='avatar'>{avatar}</span>"
                f"<strong>{title}</strong><span class='spacer'></span>{actions}</div>"
                f"<div class='msg-body'>{body}</div></section>"
            )

        return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing:border-box; }}
html,body {{ margin:0; padding:0; background:{colors['window']}; color:{colors['text']};
font-family:'Microsoft YaHei UI','Segoe UI',sans-serif; }}
body {{ padding:18px 22px 28px; }}
.msg {{ max-width:960px; margin:0 auto 16px; border:1px solid {colors['border']};
border-radius:16px; padding:14px 16px; box-shadow:0 1px 2px rgba(0,0,0,.04); }}
.user-card {{ margin-left:auto; max-width:82%; }}
.assistant-card {{ margin-right:auto; width:100%; }}
.msg-head {{ display:flex; align-items:center; gap:9px; margin-bottom:10px; }}
.avatar {{ width:28px; height:28px; border-radius:50%; display:inline-flex;
align-items:center; justify-content:center; font-weight:700; color:white; background:{colors['accent']}; }}
.user-card .avatar {{ background:#64748b; }}
.spacer {{ flex:1; }}
.msg-body {{ line-height:1.72; font-size:14px; }}
.user-card .msg-body {{ font-weight:600; }}
.assistant-card .msg-body {{ font-weight:400; }}
.prose h1,.prose h2,.prose h3 {{ margin:14px 0 8px; line-height:1.35; }}
.prose h1 {{ font-size:20px; }} .prose h2 {{ font-size:18px; }} .prose h3 {{ font-size:16px; }}
button {{ border:1px solid {colors['border']}; background:{colors['panel']}; color:{colors['text']};
border-radius:7px; padding:5px 9px; cursor:pointer; }}
.code-card {{ margin:12px 0; border:1px solid {colors['border']}; border-radius:10px;
overflow:hidden; background:#0f172a; }}
.code-head {{ display:flex; justify-content:space-between; align-items:center; padding:7px 10px;
background:#111827; color:#cbd5e1; font-size:12px; }}
.code-head button {{ background:#243244; color:#e5e7eb; border-color:#3c4b5f; }}
pre {{ margin:0; padding:13px 14px; overflow:auto; color:#e5e7eb; background:#0f172a;
font-family:Consolas,'Courier New',monospace; font-size:13px; line-height:1.55; white-space:pre; }}
.fold summary {{ cursor:pointer; color:{colors['accent']}; font-weight:600; padding:8px 0; }}
.fold-body {{ margin-top:8px; }}
.copy-answer {{ font-size:12px; }}
</style>
<script>
function copyText(text, btn) {{
  const ta=document.createElement('textarea');
  ta.value=text; ta.style.position='fixed'; ta.style.opacity='0';
  document.body.appendChild(ta); ta.focus(); ta.select();
  try {{
    document.execCommand('copy');
    const old=btn.innerText; btn.innerText='已复制';
    setTimeout(()=>btn.innerText=old,1200);
  }} catch(e) {{ btn.innerText='复制失败'; }}
  document.body.removeChild(ta);
}}
function copyB64(b64, btn) {{
  try {{
    const bytes=Uint8Array.from(atob(b64), c=>c.charCodeAt(0));
    const text=new TextDecoder('utf-8').decode(bytes);
    copyText(text, btn);
  }} catch(e) {{ btn.innerText='复制失败'; }}
}}
</script>
</head>
<body>
{''.join(cards)}
</body>
</html>"""

    def refresh_chat(self, streaming=""):
        doc = self.chat_document(streaming)
        self.chat.setHtml(doc)
        self.chat.page().runJavaScript("window.scrollTo(0, document.body.scrollHeight);")
        if hasattr(self, "visual_chat"):
            self.visual_chat.setHtml(doc)
            self.visual_chat.page().runJavaScript("window.scrollTo(0, document.body.scrollHeight);")
        if hasattr(self, "visual_model_label"):
            self.visual_model_label.setText(self.model.currentText())
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
