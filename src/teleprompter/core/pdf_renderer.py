"""投影片（PDF）渲染：為 SlidePreview 提供大圖 + 縮圖的 QPixmap。

設計要點：
- 延遲開檔（第一次 render 時才 open）
- LRU 快取渲染結果（避免切頁反覆重算）
- 縮圖低解析度常駐；大圖依實際視窗寬度動態算
- 供上層 UI：`SlideDeck.render(page, width)` / `thumbnail(page)` 都回傳 QPixmap
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from PySide6.QtGui import QImage, QPixmap

logger = logging.getLogger(__name__)

# 縮圖解析度（固定）
THUMBNAIL_WIDTH = 160


@dataclass
class SlidePage:
    """單張投影片（1-based 頁碼 + 原始尺寸）。"""

    number: int
    width_pt: float
    height_pt: float


@dataclass
class TextBlock:
    """PDF 頁面上的文字區塊 — bbox 以 PDF points 表示（原始座標系）。
    x0,y0 = 左上、x1,y1 = 右下。text = 該區塊的文字內容。
    縮放到畫面時：pixel_x = pdf_x * (pix_w / page.width_pt)
    """

    x0: float
    y0: float
    x1: float
    y1: float
    text: str


class SlideDeck:
    """一份投影片 = PDF 檔案的抽象；延遲開檔、LRU 快取渲染結果。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(Path(path))
        self._doc = None  # type: ignore[assignment]
        self._pages: list[SlidePage] = []
        self._render_cache: dict[tuple[int, int], QPixmap] = {}
        self._render_order: list[tuple[int, int]] = []
        self._thumb_cache: dict[int, QPixmap] = {}
        self._text_block_cache: dict[int, list[TextBlock]] = {}

    @property
    def pages(self) -> list[SlidePage]:
        if not self._pages:
            self._ensure_open()
        return self._pages

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def _ensure_open(self) -> None:
        if self._doc is not None:
            return
        import fitz  # PyMuPDF

        self._doc = fitz.open(self.path)
        pages: list[SlidePage] = []
        for i, page in enumerate(self._doc):
            rect = page.rect
            pages.append(
                SlidePage(number=i + 1, width_pt=rect.width, height_pt=rect.height)
            )
        self._pages = pages

    def render(self, page_no: int, width_px: int) -> Optional[QPixmap]:
        """取得指定頁面的 QPixmap，寬度為 width_px，高度等比。

        page_no: 1-based。越界回傳 None。
        """
        self._ensure_open()
        if page_no < 1 or page_no > len(self._pages):
            return None
        width_px = max(64, min(4096, int(width_px)))
        key = (page_no, width_px)
        cached = self._render_cache.get(key)
        if cached is not None:
            return cached

        pix = self._render_pixmap(page_no, width_px)
        self._render_cache[key] = pix
        self._render_order.append(key)
        # LRU：超過上限 pop 最舊
        while len(self._render_order) > 12:
            old = self._render_order.pop(0)
            self._render_cache.pop(old, None)
        return pix

    def get_text_blocks(self, page_no: int) -> list[TextBlock]:
        """回傳該頁的文字 block 列表（bbox + text）。座標為 PDF points。

        用於實作「投影片可選取文字」功能：呼叫端把 bbox 乘以 scale
        得到螢幕座標，做 hit-testing。
        """
        self._ensure_open()
        if page_no < 1 or page_no > len(self._pages):
            return []
        cached = self._text_block_cache.get(page_no)
        if cached is not None:
            return cached
        page = self._doc[page_no - 1]
        blocks: list[TextBlock] = []
        # fitz "words": list of (x0, y0, x1, y1, word, block_no, line_no, word_no)
        try:
            words = page.get_text("words")
        except Exception:
            words = []
        for w in words:
            if len(w) >= 5 and w[4].strip():
                blocks.append(
                    TextBlock(
                        x0=float(w[0]), y0=float(w[1]),
                        x1=float(w[2]), y1=float(w[3]),
                        text=str(w[4]),
                    )
                )
        self._text_block_cache[page_no] = blocks
        return blocks

    def thumbnail(self, page_no: int) -> Optional[QPixmap]:
        """取縮圖；低解析度常駐快取。"""
        self._ensure_open()
        if page_no < 1 or page_no > len(self._pages):
            return None
        cached = self._thumb_cache.get(page_no)
        if cached is not None:
            return cached
        pix = self._render_pixmap(page_no, THUMBNAIL_WIDTH)
        self._thumb_cache[page_no] = pix
        return pix

    def _render_pixmap(self, page_no: int, width_px: int) -> QPixmap:
        import fitz  # noqa

        page = self._doc[page_no - 1]
        # 用頁面原始寬算 scale，保持比例
        rect = page.rect
        scale = width_px / rect.width if rect.width > 0 else 1.0
        matrix = fitz.Matrix(scale, scale)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        # 轉 QImage/QPixmap（samples 是 RGB bytes）
        img = QImage(
            pixmap.samples,
            pixmap.width,
            pixmap.height,
            pixmap.stride,
            QImage.Format.Format_RGB888,
        )
        # QImage 對 samples buffer 持有 reference，必須 copy() 避免 PyMuPDF pixmap 釋放後出問題
        return QPixmap.fromImage(img.copy())

    def close(self) -> None:
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:  # pragma: no cover
                pass
            self._doc = None
        self._render_cache.clear()
        self._render_order.clear()
        self._thumb_cache.clear()
        self._text_block_cache.clear()


def load_slide_deck(path: str | Path) -> SlideDeck:
    """便利函式：建立 SlideDeck 並嘗試 open 以驗證有效；失敗丟 ValueError。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"投影片檔案不存在: {p}")
    deck = SlideDeck(p)
    try:
        deck._ensure_open()
    except Exception as e:
        raise ValueError(f"無法開啟 PDF: {e}") from e
    return deck
