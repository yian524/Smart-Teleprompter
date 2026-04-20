"""講稿載入：支援 .txt / .md / .docx，輸出統一的 Sentence 列表。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 中英混合句子終止標點
_SENT_TERMINATORS = "。！？!?；;"
# 在這些字元後若接空白或換行，視為句子邊界
_SENT_TERMINATORS_SOFT = ".!?"

_SENT_REGEX = re.compile(
    r"[^"
    + re.escape(_SENT_TERMINATORS)
    + r"\n]+["
    + re.escape(_SENT_TERMINATORS)
    + r"]+|"
    r"[^\n]+(?=\n|$)"
)


@dataclass
class Sentence:
    """講稿中的一個句子。

    text: 原始文本（含標點與大小寫，用於顯示）
    normalized: 標準化後的純文字（用於比對）
    start: 在全文中的起始字元位置
    end: 在全文中的結束字元位置（不含）
    char_map: 標準化字元索引 → 全文中的字元索引
    """

    text: str
    normalized: str
    start: int
    end: int
    char_map: list[int] = field(default_factory=list)

    def normalized_to_global(self, norm_idx: int) -> int:
        """把標準化字元位置對映回全文字元位置。"""
        if not self.char_map:
            return self.start
        if norm_idx <= 0:
            return self.char_map[0]
        if norm_idx >= len(self.char_map):
            return self.end
        return self.char_map[norm_idx]


@dataclass
class Page:
    """投影片分頁（對應 --- 分隔符劃分的區段）。"""

    number: int                     # 1-based 頁碼
    sentence_start: int             # 在 Transcript.sentences 中的起始索引
    sentence_end: int               # 結束索引（不含）
    title: str = ""                 # 選用的頁面標題（註解提取或使用者標示）

    def contains_sentence(self, sent_idx: int) -> bool:
        return self.sentence_start <= sent_idx < self.sentence_end


@dataclass
class Transcript:
    """完整講稿（句子列表 + 全文 + 分頁）。"""

    sentences: list[Sentence] = field(default_factory=list)
    full_text: str = ""
    pages: list[Page] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return len(self.full_text)

    def page_of_sentence(self, sent_idx: int) -> Optional[Page]:
        """查某句屬於哪一頁。若無分頁，回傳 None。"""
        for p in self.pages:
            if p.contains_sentence(sent_idx):
                return p
        return None


def _is_kept(ch: str) -> bool:
    """判斷標準化後是否保留這個字元（中英文字、數字、空白）。"""
    if ch.isspace():
        return True
    if ch.isalnum():
        return True
    # CJK 統一漢字
    code = ord(ch)
    if 0x4E00 <= code <= 0x9FFF:
        return True
    return False


def normalize_with_map(text: str, base_offset: int = 0) -> tuple[str, list[int]]:
    """文字標準化並回傳每個標準化字元對應的原始位置（含 base_offset）。

    處理：
    - 全形 → 半形
    - 英文小寫
    - 移除標點，多重空白合併為單一空白
    """
    out_chars: list[str] = []
    out_map: list[int] = []
    last_was_space = True  # 開頭視為已是空白以避免前導空白
    for i, ch in enumerate(text):
        code = ord(ch)
        # 全形 → 半形
        if 0xFF01 <= code <= 0xFF5E:
            ch = chr(code - 0xFEE0)
            code = ord(ch)
        elif code == 0x3000:
            ch = " "
        # 英文小寫
        if "A" <= ch <= "Z":
            ch = ch.lower()

        if not _is_kept(ch):
            # 視為空白以分隔
            if not last_was_space:
                out_chars.append(" ")
                out_map.append(base_offset + i)
                last_was_space = True
            continue

        if ch.isspace():
            if last_was_space:
                continue
            out_chars.append(" ")
            out_map.append(base_offset + i)
            last_was_space = True
        else:
            out_chars.append(ch)
            out_map.append(base_offset + i)
            last_was_space = False

    # 移除尾端空白
    while out_chars and out_chars[-1] == " ":
        out_chars.pop()
        out_map.pop()
    return "".join(out_chars), out_map


def normalize_text(text: str) -> str:
    """文字標準化（無 char_map，供辨識文字使用）。"""
    return normalize_with_map(text)[0]


def _make_sentence(raw: str, global_start: int, global_end: int) -> Sentence:
    normalized, char_map = normalize_with_map(raw, base_offset=global_start)
    return Sentence(
        text=raw,
        normalized=normalized,
        start=global_start,
        end=global_end,
        char_map=char_map,
    )


def split_sentences(text: str) -> list[Sentence]:
    """將文字切成句子，保留每個句子在全文中的索引。

    跳過：
    - 空白行
    - Markdown 標題行（以 #, ##, ### 開頭）—— 標題作為頁面元資訊，不做為講稿內容
    - 分頁符號行（---, ===, ***）
    """
    sentences: list[Sentence] = []
    if not text:
        return sentences

    pos = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            pos += len(line) + 1
            continue
        # 跳過 Markdown 標題（當作頁面 metadata，不當講稿）
        if stripped.startswith("#"):
            pos += len(line) + 1
            continue
        # 跳過分頁符號
        if _PAGE_SEPARATOR_RE.match(line):
            pos += len(line) + 1
            continue
        line_pos = 0
        for match in re.finditer(
            r"[^" + re.escape(_SENT_TERMINATORS) + r"]*?["
            + re.escape(_SENT_TERMINATORS) + r"]+\s*",
            line,
        ):
            chunk = match.group(0)
            start_in_line = match.start()
            end_in_line = match.end()
            if not chunk.strip():
                continue
            sentences.append(
                _make_sentence(chunk, pos + start_in_line, pos + end_in_line)
            )
            line_pos = end_in_line
        # 行尾殘餘
        if line_pos < len(line):
            tail = line[line_pos:]
            if tail.strip():
                sentences.append(
                    _make_sentence(tail, pos + line_pos, pos + len(line))
                )
        pos += len(line) + 1

    sentences = [s for s in sentences if s.normalized]
    return sentences


def load_txt(path: Path) -> str:
    """嘗試多種編碼讀取純文字檔。"""
    for enc in ("utf-8", "utf-8-sig", "gbk", "big5", "cp950"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    # 最後手段：忽略錯誤
    return path.read_text(encoding="utf-8", errors="ignore")


def load_md(path: Path) -> str:
    """讀取 Markdown，去除 Markdown 標記只保留文字。"""
    raw = load_txt(path)
    # 簡單去除常見 Markdown 標記
    text = raw
    # 程式碼區塊整個移除
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    # 圖片與連結保留文字
    text = re.sub(r"!\[([^\]]*)\]\([^\)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]*\)", r"\1", text)
    # 標題符號
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    # 粗體斜體
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)
    # 引用
    text = re.sub(r"^>\s*", "", text, flags=re.MULTILINE)
    # 列表符號
    text = re.sub(r"^[\s]*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*\d+\.\s+", "", text, flags=re.MULTILINE)
    # 水平線 --- / === / *** 作為「分頁符」保留（不刪除）
    # 多重空行壓縮
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def load_docx(path: Path) -> str:
    """讀取 Word 文件，按段落串接成文字。"""
    try:
        from docx import Document
    except ImportError as e:
        raise RuntimeError("python-docx 未安裝，無法載入 .docx 檔案") from e

    doc = Document(str(path))
    paragraphs: list[str] = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


# ============================================================
# 講稿預處理：註解剝除 + 分頁識別
# ============================================================

# HTML 風格註解：<!-- ... --> （可跨行，不影響提詞）
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# 分頁分隔符：一行裡只有 --- 或 === 或 ***（前後可有空白）
_PAGE_SEPARATOR_RE = re.compile(r"^\s*(?:---+|===+|\*\*\*+)\s*$", re.MULTILINE)


def strip_comments(text: str) -> str:
    """剝除 <!-- 註解 --> 但保留其他文字。"""
    return _COMMENT_RE.sub("", text)


def _split_by_pages(text: str) -> list[str]:
    """依 --- 分頁符號切分文字。若無分頁符號則回傳單元素 list。"""
    # 把連續的 \n---\n 變成唯一分隔符
    parts = _PAGE_SEPARATOR_RE.split(text)
    # 過濾全空白段
    return [p for p in parts if p.strip()]


def _extract_page_title(page_text: str) -> str:
    """取頁面標題：優先取第一個 Markdown 標題 `# xxx`，否則第一行前 20 字。

    跳過分頁符號本身（---、===、***）與空行。
    """
    for line in page_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 跳過分頁符號
        if _PAGE_SEPARATOR_RE.match(line):
            continue
        # Markdown 標題
        if line.startswith("#"):
            return line.lstrip("# ").strip()[:40]
        # 一般第一行
        return line[:20]
    return ""


def parse_transcript(text: str) -> Transcript:
    """統一解析：剝除註解 → 切分頁 → 切句 → 建立 Page 對映。"""
    # 1. 剝除註解
    text = strip_comments(text)
    # 2. 切分頁（保留原文以供 full_text）
    # 先把 page separator 轉為一行空白（保留位置不變），這樣句子索引仍與原文一致
    # 策略：找出每個 separator 的位置，用其為分界
    matches = list(_PAGE_SEPARATOR_RE.finditer(text))
    # 用 match 位置切分 sentence ranges
    sentences = split_sentences(text)

    pages: list[Page] = []
    if not matches:
        # 無分頁符 → 全部歸為單頁
        if sentences:
            pages.append(Page(number=1, sentence_start=0, sentence_end=len(sentences), title=""))
    else:
        # 有分頁符 → 按字元位置分頁
        page_boundaries_chars = [0] + [m.start() for m in matches] + [len(text)]
        for i in range(len(page_boundaries_chars) - 1):
            page_start_char = page_boundaries_chars[i]
            page_end_char = page_boundaries_chars[i + 1]
            # 找該字元範圍內的句子
            sent_start_idx = None
            sent_end_idx = len(sentences)
            for si, s in enumerate(sentences):
                if s.start >= page_start_char and sent_start_idx is None:
                    sent_start_idx = si
                if s.start >= page_end_char:
                    sent_end_idx = si
                    break
            if sent_start_idx is None:
                continue  # 此頁沒有任何句子（分頁符之間只有空白）
            if sent_end_idx <= sent_start_idx:
                continue
            page_text = text[page_start_char:page_end_char]
            title = _extract_page_title(page_text)
            pages.append(Page(
                number=len(pages) + 1,
                sentence_start=sent_start_idx,
                sentence_end=sent_end_idx,
                title=title,
            ))

    return Transcript(sentences=sentences, full_text=text, pages=pages)


def load_transcript(path: str | Path) -> Transcript:
    """主要入口：依副檔名選擇載入器，回傳 Transcript。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"檔案不存在: {path}")

    suffix = path.suffix.lower()
    if suffix == ".docx":
        text = load_docx(path)
    elif suffix in (".md", ".markdown"):
        text = load_md(path)
    elif suffix in (".txt", ""):
        text = load_txt(path)
    else:
        text = load_txt(path)

    return parse_transcript(text)


def load_from_string(text: str) -> Transcript:
    """直接從字串載入（貼上文字使用）。"""
    return parse_transcript(text)
