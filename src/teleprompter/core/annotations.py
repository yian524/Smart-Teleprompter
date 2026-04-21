"""使用者在投影片上的筆記標註（便利貼 + 鉛筆畫）。

Data model：
- `Annotation`：單一標註，有 id、kind、slide_page、normalized position/strokes
- 位置用「相對比例」(0.0-1.0) 存，讓不同縮放下都在對的位置

Storage：
- Session 的 `annotations: list[Annotation]` 欄位
- `to_dict` / `from_dict` 序列化到 sessions.json

UI 介面（實作在 slide_mode_view.py）：
- kind="note"：便利貼，顯示文字方塊於 (x_ratio, y_ratio)
- kind="stroke"：鉛筆畫，多條 polyline（每條是 list[(x_ratio, y_ratio)]）
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Annotation:
    """單一標註物件（便利貼 or 鉛筆畫）。

    **錨點系統**：
    - anchor="slide"：綁在 PDF 某一頁上
        - slide_page: 1-based PDF 頁碼
        - x, y, strokes 座標都是 0..1 比例（相對 slide rect）
    - anchor="doc"：綁在講稿某位置（會跟著 scroll）
        - char_offset: 錨定到 transcript 的 char index
        - x: 0..1 比例（相對 viewport 寬）
        - y: 絕對 document Y 座標（像素）
        - strokes 每個點 = (x_ratio, doc_y_px)
    """

    kind: str                                  # "note" | "stroke"
    anchor: str = "slide"                       # "slide" | "doc"
    # --- slide 錨點 ---
    slide_page: int = 0                         # 1-based
    # --- doc 錨點 ---
    char_offset: int = 0                         # char index in transcript
    # --- 共用幾何 ---
    x: float = 0.0
    y: float = 0.0
    width: float = 0.2
    height: float = 0.1
    # 便利貼文字 / 鉛筆畫顏色
    text: str = ""
    color: str = "#FFEB3B"
    stroke_width: int = 3
    # 鉛筆畫的多段筆劃（依 anchor 類型，座標意義不同）
    strokes: list[list[tuple[float, float]]] = field(default_factory=list)
    annotation_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "anchor": self.anchor,
            "slide_page": self.slide_page,
            "char_offset": self.char_offset,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "text": self.text,
            "color": self.color,
            "stroke_width": self.stroke_width,
            "strokes": [[list(pt) for pt in s] for s in self.strokes],
            "annotation_id": self.annotation_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Annotation":
        strokes_raw = d.get("strokes", [])
        strokes: list[list[tuple[float, float]]] = [
            [(float(p[0]), float(p[1])) for p in s] for s in strokes_raw
        ]
        return cls(
            kind=str(d.get("kind", "note")),
            anchor=str(d.get("anchor", "slide")),
            slide_page=int(d.get("slide_page", 0)),
            char_offset=int(d.get("char_offset", 0)),
            x=float(d.get("x", 0.0)),
            y=float(d.get("y", 0.0)),
            width=float(d.get("width", 0.2)),
            height=float(d.get("height", 0.1)),
            text=str(d.get("text", "")),
            color=str(d.get("color", "#FFEB3B")),
            stroke_width=int(d.get("stroke_width", 3)),
            strokes=strokes,
            annotation_id=str(d.get("annotation_id") or uuid.uuid4()),
        )
