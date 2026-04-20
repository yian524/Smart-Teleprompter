"""Session：一個分頁 = 一份講稿 + 可選投影片 + 執行狀態。

Session 是**純資料容器**（無 Qt 依賴）。runtime 物件（AlignmentEngine、
Transcript、SlideDeck）由 `SessionManager` 懸掛在 session 上，並在
`serialize()` 時略過，只存路徑與可持久化的狀態。

SessionManager 負責：
- add / remove / set_active
- 序列化整個 session 清單到 JSON（給持久化用）
- 反序列化（不自動 open transcript/slides；留給 MainWindow 決定時機）
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QObject, Signal

from .alignment_engine import AlignmentEngine
from .pdf_renderer import SlideDeck
from .rich_text_format import FormatSpan
from .transcript_loader import Transcript

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """一個 Tab 的完整狀態。"""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = "未命名"
    transcript_path: str = ""
    slides_path: str = ""

    # 持久化狀態
    current_global_char: int = 0
    current_sentence_index: int = 0
    skipped_ranges: list[tuple[int, int]] = field(default_factory=list)
    format_spans: list[FormatSpan] = field(default_factory=list)
    # 編輯後的完整文本；非空代表使用者在編輯模式下改過，優先於 transcript_path
    modified_text: str = ""
    # 編輯結果尚未存回 .txt 檔
    dirty: bool = False

    # runtime（不持久化）
    transcript: Optional[Transcript] = field(default=None, compare=False, repr=False)
    slide_deck: Optional[SlideDeck] = field(default=None, compare=False, repr=False)
    engine: Optional[AlignmentEngine] = field(default=None, compare=False, repr=False)

    def to_json(self) -> dict[str, Any]:
        """只保存持久化欄位（不含 runtime 物件）。"""
        return {
            "session_id": self.session_id,
            "title": self.title,
            "transcript_path": self.transcript_path,
            "slides_path": self.slides_path,
            "current_global_char": self.current_global_char,
            "current_sentence_index": self.current_sentence_index,
            "skipped_ranges": list(self.skipped_ranges),
            "format_spans": [s.to_dict() for s in self.format_spans],
            "modified_text": self.modified_text,
            "dirty": self.dirty,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Session":
        spans = [FormatSpan.from_dict(d) for d in data.get("format_spans", [])]
        skipped = [tuple(r) for r in data.get("skipped_ranges", [])]
        return cls(
            session_id=data.get("session_id") or str(uuid.uuid4()),
            title=data.get("title") or "未命名",
            transcript_path=data.get("transcript_path", ""),
            slides_path=data.get("slides_path", ""),
            current_global_char=int(data.get("current_global_char", 0)),
            current_sentence_index=int(data.get("current_sentence_index", 0)),
            skipped_ranges=skipped,
            format_spans=spans,
            modified_text=data.get("modified_text", ""),
            dirty=bool(data.get("dirty", False)),
        )


class SessionManager(QObject):
    """多 Tab 管理器。"""

    sessions_changed = Signal()  # 增刪順序變更
    active_session_changed = Signal(str)  # 新 active session_id（"" 表示沒有）

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._sessions: list[Session] = []
        self._active_id: str = ""

    # ---------- 存取 ----------

    @property
    def sessions(self) -> list[Session]:
        return list(self._sessions)

    def __len__(self) -> int:
        return len(self._sessions)

    def get(self, session_id: str) -> Optional[Session]:
        for s in self._sessions:
            if s.session_id == session_id:
                return s
        return None

    @property
    def active(self) -> Optional[Session]:
        return self.get(self._active_id) if self._active_id else None

    @property
    def active_id(self) -> str:
        return self._active_id

    # ---------- 變更 ----------

    def add(self, session: Session, *, activate: bool = True) -> None:
        if self.get(session.session_id) is not None:
            logger.warning("session %s 已存在，忽略 add", session.session_id)
            return
        self._sessions.append(session)
        self.sessions_changed.emit()
        if activate:
            self.set_active(session.session_id)

    def remove(self, session_id: str) -> None:
        s = self.get(session_id)
        if s is None:
            return
        idx = self._sessions.index(s)
        # 關閉 runtime 資源
        if s.slide_deck is not None:
            try:
                s.slide_deck.close()
            except Exception:
                pass
        self._sessions.remove(s)
        # 若刪的是 active，切到鄰近的 session
        if self._active_id == session_id:
            if self._sessions:
                new_idx = min(idx, len(self._sessions) - 1)
                self._active_id = self._sessions[new_idx].session_id
            else:
                self._active_id = ""
            self.active_session_changed.emit(self._active_id)
        self.sessions_changed.emit()

    def set_active(self, session_id: str) -> None:
        if session_id == self._active_id:
            return
        if session_id and self.get(session_id) is None:
            logger.warning("set_active: 未知 session %s", session_id)
            return
        self._active_id = session_id
        self.active_session_changed.emit(session_id)

    def move(self, session_id: str, new_index: int) -> None:
        """重排 tab 順序。"""
        s = self.get(session_id)
        if s is None:
            return
        self._sessions.remove(s)
        new_index = max(0, min(new_index, len(self._sessions)))
        self._sessions.insert(new_index, s)
        self.sessions_changed.emit()

    # ---------- 持久化 ----------

    def save_to_disk(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "active_id": self._active_id,
            "sessions": [s.to_json() for s in self._sessions],
        }
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_from_disk(self, path: str | Path) -> None:
        p = Path(path)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("讀取 sessions.json 失敗: %s", e)
            return
        self._sessions = [Session.from_json(d) for d in data.get("sessions", [])]
        active = data.get("active_id", "")
        if active and self.get(active) is not None:
            self._active_id = active
        elif self._sessions:
            self._active_id = self._sessions[0].session_id
        else:
            self._active_id = ""
        self.sessions_changed.emit()
        self.active_session_changed.emit(self._active_id)


def default_sessions_path() -> Path:
    """預設 sessions.json 儲存位置（user data dir）。"""
    import os
    import sys

    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "SmartTeleprompter" / "sessions.json"
