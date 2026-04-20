"""Q&A 預備庫：使用者事先準備問答對，即時匹配觀眾提問並顯示答案。

支援格式：
1. **JSON**：`[{"q": "...", "a": "..."}, ...]`
2. **Markdown**：以 `Q:` 開頭為問題、`A:` 開頭為答案，成對匹配
3. **純文字**：以 `Q:` / `A:` 分段（同 Markdown）

匹配演算法：字元 + 拼音雙重 partial_ratio，取最高分者。
若超過 Top-2 分差 < 5，視為不確定（顯示多個候選讓使用者選）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz

from .text_utils import to_pinyin_form


@dataclass
class QAItem:
    question: str
    answer: str
    # 預計算的標準化形式（提升匹配速度）
    _question_normalized: str = field(default="", repr=False)
    _question_pinyin: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        # 簡單標準化：去標點、轉小寫
        norm = re.sub(r"[^\w\s\u4e00-\u9fff]+", " ", self.question.lower())
        norm = re.sub(r"\s+", " ", norm).strip()
        self._question_normalized = norm
        self._question_pinyin = to_pinyin_form(norm)


@dataclass
class QAMatch:
    item: QAItem
    score: float  # 0-100
    runner_up_score: float = 0.0  # Top-2 分數（判斷是否確定）

    @property
    def is_confident(self) -> bool:
        """是否有足夠信心：分差 ≥ 5 且分數 ≥ 60。"""
        return self.score >= 60 and (self.score - self.runner_up_score) >= 5


class QALibrary:
    """Q&A 庫：載入、儲存、匹配。"""

    def __init__(self, items: list[QAItem] | None = None) -> None:
        self.items: list[QAItem] = items or []

    def __len__(self) -> int:
        return len(self.items)

    def add(self, question: str, answer: str) -> None:
        self.items.append(QAItem(question=question, answer=answer))

    def clear(self) -> None:
        self.items.clear()

    def match(self, query: str) -> QAMatch | None:
        """對一個觀眾提問找最相符的答案。"""
        if not self.items or not query.strip():
            return None
        query_norm = re.sub(r"[^\w\s\u4e00-\u9fff]+", " ", query.lower())
        query_norm = re.sub(r"\s+", " ", query_norm).strip()
        query_pinyin = to_pinyin_form(query_norm)

        scored: list[tuple[QAItem, float]] = []
        for item in self.items:
            char_s = fuzz.partial_ratio(query_norm, item._question_normalized)
            pinyin_s = 0.0
            if query_pinyin and item._question_pinyin:
                pinyin_s = fuzz.partial_ratio(query_pinyin, item._question_pinyin)
            score = max(char_s, pinyin_s)
            scored.append((item, score))

        scored.sort(key=lambda x: -x[1])
        best_item, best_score = scored[0]
        runner_up = scored[1][1] if len(scored) > 1 else 0.0
        return QAMatch(item=best_item, score=best_score, runner_up_score=runner_up)

    def top_k(self, query: str, k: int = 3) -> list[QAMatch]:
        """回傳 Top-K 候選（按分數降冪）。"""
        if not self.items or not query.strip():
            return []
        query_norm = re.sub(r"[^\w\s\u4e00-\u9fff]+", " ", query.lower())
        query_norm = re.sub(r"\s+", " ", query_norm).strip()
        query_pinyin = to_pinyin_form(query_norm)

        scored: list[tuple[QAItem, float]] = []
        for item in self.items:
            char_s = fuzz.partial_ratio(query_norm, item._question_normalized)
            pinyin_s = 0.0
            if query_pinyin and item._question_pinyin:
                pinyin_s = fuzz.partial_ratio(query_pinyin, item._question_pinyin)
            scored.append((item, max(char_s, pinyin_s)))
        scored.sort(key=lambda x: -x[1])
        return [
            QAMatch(item=it, score=sc, runner_up_score=(scored[i + 1][1] if i + 1 < len(scored) else 0.0))
            for i, (it, sc) in enumerate(scored[:k])
        ]


# ============================================================
# 檔案載入
# ============================================================

def load_qa_json(path: Path) -> QALibrary:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("QA JSON 必須是列表格式 [{q: ..., a: ...}]")
    items = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        q = str(entry.get("q") or entry.get("question") or "").strip()
        a = str(entry.get("a") or entry.get("answer") or "").strip()
        if q and a:
            items.append(QAItem(question=q, answer=a))
    return QALibrary(items)


_QA_LINE_RE = re.compile(r"^\s*([QA])[:：]\s*(.+)$", re.IGNORECASE)


def load_qa_markdown(path: Path) -> QALibrary:
    """讀 Markdown / 純文字：以 Q: / A: 標示問答對。"""
    for enc in ("utf-8", "utf-8-sig", "gbk", "big5"):
        try:
            content = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        content = path.read_text(encoding="utf-8", errors="ignore")

    return parse_qa_from_text(content)


def parse_qa_from_text(text: str) -> QALibrary:
    items: list[QAItem] = []
    current_q: str | None = None
    current_a_lines: list[str] = []

    def flush():
        nonlocal current_q, current_a_lines
        if current_q and current_a_lines:
            answer = "\n".join(current_a_lines).strip()
            if answer:
                items.append(QAItem(question=current_q.strip(), answer=answer))
        current_q = None
        current_a_lines = []

    for line in text.splitlines():
        m = _QA_LINE_RE.match(line)
        if m:
            marker = m.group(1).upper()
            body = m.group(2).strip()
            if marker == "Q":
                flush()
                current_q = body
            elif marker == "A":
                if current_q is not None:
                    current_a_lines.append(body)
        else:
            # 延續當前答案
            if current_q is not None and current_a_lines:
                line = line.strip()
                if line:
                    current_a_lines.append(line)
    flush()
    return QALibrary(items)


def load_qa(path: str | Path) -> QALibrary:
    """主入口：依副檔名選擇載入器。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"找不到 QA 檔：{path}")
    if path.suffix.lower() == ".json":
        return load_qa_json(path)
    return load_qa_markdown(path)
