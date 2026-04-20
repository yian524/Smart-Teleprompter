"""講稿載入：支援 .txt / .md / .docx，輸出統一的 Sentence 列表。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

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
class Transcript:
    """完整講稿（句子列表 + 全文）。"""

    sentences: list[Sentence] = field(default_factory=list)
    full_text: str = ""

    @property
    def total_chars(self) -> int:
        return len(self.full_text)


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
    """將文字切成句子，保留每個句子在全文中的索引。"""
    sentences: list[Sentence] = []
    if not text:
        return sentences

    pos = 0
    for line in text.split("\n"):
        if not line.strip():
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
    # 水平線
    text = re.sub(r"^[\s]*[-*_]{3,}[\s]*$", "", text, flags=re.MULTILINE)
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
        # 未知格式嘗試當純文字讀
        text = load_txt(path)

    sentences = split_sentences(text)
    return Transcript(sentences=sentences, full_text=text)


def load_from_string(text: str) -> Transcript:
    """直接從字串載入（貼上文字使用）。"""
    sentences = split_sentences(text)
    return Transcript(sentences=sentences, full_text=text)
