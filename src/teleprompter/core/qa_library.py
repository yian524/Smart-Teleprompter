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


def normalize_query(text: str) -> str:
    """標準化：去標點、轉小寫、壓縮空白。查詢與問題共用同一套規則。"""
    norm = re.sub(r"[^\w\s一-鿿]+", " ", text.lower())
    return re.sub(r"\s+", " ", norm).strip()


@dataclass
class QAItem:
    question: str
    answer: str
    # 同一題的其他問法（Q 行以 / 分隔、或 K: 行的關鍵詞）；匹配時一併計分取最高
    aliases: list[str] = field(default_factory=list)
    # 命中此題時要跳到的投影片頁（1-based）；None = 不跳頁
    slide_page: int | None = None
    # 預計算形式 [(正規化, 拼音), ...]，第 0 筆為主問題
    _forms: list[tuple[str, str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._forms = []
        for text in [self.question, *self.aliases]:
            norm = normalize_query(text)
            if norm:
                self._forms.append((norm, to_pinyin_form(norm)))
        if not self._forms:
            self._forms.append(("", ""))

    @property
    def _question_normalized(self) -> str:
        """主問題正規化形式（向下相容既有呼叫端與測試）。"""
        return self._forms[0][0]

    @property
    def _question_pinyin(self) -> str:
        return self._forms[0][1]

    def score_against(self, query_norm: str, query_pinyin: str) -> float:
        """對已正規化的查詢計分：主問題與所有 alias 取最高分。"""
        best = 0.0
        for norm, pinyin in self._forms:
            if not norm:
                continue
            s = float(fuzz.partial_ratio(query_norm, norm))
            if query_pinyin and pinyin:
                s = max(s, float(fuzz.partial_ratio(query_pinyin, pinyin)))
            best = max(best, s)
        return best


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

    def _scored(self, query: str) -> list[tuple[QAItem, float]]:
        """共用計分：回傳 [(item, score), ...] 已按分數降冪。"""
        query_norm = normalize_query(query)
        query_pinyin = to_pinyin_form(query_norm)
        scored = [(item, item.score_against(query_norm, query_pinyin))
                  for item in self.items]
        scored.sort(key=lambda x: -x[1])
        return scored

    def match(self, query: str) -> QAMatch | None:
        """對一個觀眾提問找最相符的答案。"""
        if not self.items or not query.strip():
            return None
        scored = self._scored(query)
        best_item, best_score = scored[0]
        runner_up = scored[1][1] if len(scored) > 1 else 0.0
        return QAMatch(item=best_item, score=best_score, runner_up_score=runner_up)

    def top_k(self, query: str, k: int = 3) -> list[QAMatch]:
        """回傳 Top-K 候選（按分數降冪）。"""
        if not self.items or not query.strip():
            return []
        scored = self._scored(query)
        return [
            QAMatch(item=it, score=sc,
                    runner_up_score=(scored[i + 1][1] if i + 1 < len(scored) else 0.0))
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
        if not (q and a):
            continue
        raw_alias = entry.get("aliases") or entry.get("alias") or []
        if isinstance(raw_alias, str):
            raw_alias = [x.strip() for x in re.split(r"[,，、/／]", raw_alias)]
        aliases = [str(x).strip() for x in raw_alias if str(x).strip()]
        page = entry.get("slide_page", entry.get("page"))
        try:
            slide_page = int(page) if page not in (None, "") else None
        except (TypeError, ValueError):
            slide_page = None
        if slide_page is None:
            slide_page = parse_slide_page(a)
        items.append(QAItem(question=q, answer=a, aliases=aliases, slide_page=slide_page))
    return QALibrary(items)


# Q 行多問法分隔符（半形／全形斜線）
_ALIAS_SPLIT_RE = re.compile(r"[/／]")
# 答案內的翻頁指引，兩種寫法各自獨立匹配（避免 "K=4" 這類數字被誤抓）：
#   1) 備答編號：【翻到備答 B03 …】 → 需 backup_start_page 換算
#   2) 明確頁碼：【投影片 12 頁】【第 7 頁】【page 5】
_PAGE_MARK_B_RE = re.compile(r"[【\[][^】\]]*?[Bb]0*(\d{1,2})(?![0-9])[^】\]]*?[】\]]")
_PAGE_MARK_N_RE = re.compile(
    r"[【\[][^】\]]*?(?:第\s*(\d{1,3})\s*頁|(\d{1,3})\s*頁|page\s*(\d{1,3}))"
    r"[^】\]]*?[】\]]", re.IGNORECASE)


def parse_slide_page(text: str, backup_start_page: int = 0) -> int | None:
    """從答案文字解析目標投影片頁（1-based）。

    - `【翻到備答 B03 …】` → 需 backup_start_page（B01 對應的實際頁碼）換算；
      未設定時回 None（不亂跳頁）
    - `【投影片 12 頁】` / `【第 7 頁】` / `[page 5]` → 直接取數字
    """
    if not text:
        return None
    m = _PAGE_MARK_B_RE.search(text)
    if m:
        if backup_start_page <= 0:
            return None
        return backup_start_page + int(m.group(1)) - 1
    m = _PAGE_MARK_N_RE.search(text)
    if m:
        for g in m.groups():
            if g:
                n = int(g)
                return n if n > 0 else None
    return None


_QA_LINE_RE = re.compile(r"^\s*([QAK])[:：]\s*(.+)$", re.IGNORECASE)


def load_qa_markdown(path: Path, backup_start_page: int = 0) -> QALibrary:
    """讀 Markdown / 純文字：以 Q: / A: 標示問答對。"""
    for enc in ("utf-8", "utf-8-sig", "gbk", "big5"):
        try:
            content = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        content = path.read_text(encoding="utf-8", errors="ignore")

    return parse_qa_from_text(content, backup_start_page)


def parse_qa_from_text(text: str, backup_start_page: int = 0) -> QALibrary:
    """解析 Q:/A:/K: 文字格式。

    - `Q:` 一行可用 `/` 分隔多種問法，第一段為主問題、其餘存為 aliases
    - `K:` 可選，逗號分隔的額外關鍵詞，一併併入 aliases
    - `A:` 答案，後續非標記行會延續到同一個答案
    - 答案內若含 `【…頁】` 翻頁指引，解析為 slide_page
    """
    items: list[QAItem] = []
    current_q: str | None = None
    current_aliases: list[str] = []
    current_a_lines: list[str] = []

    def flush():
        nonlocal current_q, current_aliases, current_a_lines
        if current_q and current_a_lines:
            answer = chr(10).join(current_a_lines).strip()
            if answer:
                items.append(QAItem(
                    question=current_q.strip(),
                    answer=answer,
                    aliases=[a for a in current_aliases if a],
                    slide_page=parse_slide_page(answer, backup_start_page),
                ))
        current_q = None
        current_aliases = []
        current_a_lines = []

    for line in text.splitlines():
        m = _QA_LINE_RE.match(line)
        if m:
            marker = m.group(1).upper()
            body = m.group(2).strip()
            if marker == "Q":
                flush()
                parts = [x.strip() for x in _ALIAS_SPLIT_RE.split(body) if x.strip()]
                current_q = parts[0] if parts else body
                current_aliases = parts[1:]
            elif marker == "K":
                if current_q is not None:
                    current_aliases.extend(
                        x.strip() for x in re.split(r"[,，、]", body) if x.strip()
                    )
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


def load_qa(path: str | Path, backup_start_page: int = 0) -> QALibrary:
    """主入口：依副檔名選擇載入器。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"找不到 QA 檔：{path}")
    if path.suffix.lower() == ".json":
        return load_qa_json(path)
    return load_qa_markdown(path, backup_start_page)
