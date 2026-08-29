"""線性向量圖示（取代介面上的 emoji）。

設計規格（與 design/toolbar_mockup_v2.html 一致）：
- 16×16 檢視框、stroke-width 1.6、圓端圓角
- 只描邊不填色，顏色由呼叫端指定 → 同一顆圖示可用於亮/暗底、啟用/停用

用法：
    from .icons import icon
    action.setIcon(icon("play"))
    action.setIcon(icon("play", color="#ffffff"))
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# 介面預設圖示色（深色 chrome 上的一般文字色）
DEFAULT_COLOR = "#d6d9de"

# 每個值是 <svg> 內的 path 內容；統一 16×16、stroke 由樣板注入
_PATHS: dict[str, str] = {
    "play": '<path d="M4.5 3.2v9.6L12.8 8z"/>',
    "pause": '<path d="M6 3.5v9M10 3.5v9"/>',
    "folder": '<path d="M1.8 4.2c0-.6.4-1 1-1h3.4l1.5 1.6h5.5c.6 0 1 .4 1 1v6c0 .6-.4 1-1 1'
              'H2.8c-.6 0-1-.4-1-1z"/>',
    "slides": '<rect x="1.8" y="3" width="12.4" height="9" rx="1"/>'
              '<path d="M5.5 12v1.8M10.5 12v1.8M4.5 9.5l2.4-2.6 1.8 1.7 2.8-3"/>',
    "locate": '<circle cx="8" cy="8" r="2.2"/>'
              '<path d="M8 1.6v2.2M8 12.2v2.2M1.6 8h2.2M12.2 8h2.2"/>',
    "top": '<path d="M8 13V4M4.4 7.6L8 4l3.6 3.6M3.5 2h9"/>',
    "clock": '<circle cx="8" cy="8" r="6"/><path d="M8 4.6V8l2.3 1.5"/>',
    "reset": '<path d="M2.6 6.5A6 6 0 1 1 2 9.5M2.6 3v3.5H6"/>',
    "mic": '<rect x="6" y="1.8" width="4" height="7" rx="2"/>'
           '<path d="M3.4 7.4a4.6 4.6 0 0 0 9.2 0M8 12v2.2"/>',
    "record": '<circle cx="8" cy="8" r="3.6"/>',
    "fullscreen": '<path d="M2.5 5.5v-3h3M13.5 5.5v-3h-3M2.5 10.5v3h3M13.5 10.5v3h-3"/>',
    "gear": '<circle cx="8" cy="8" r="2.1"/>'
            '<path d="M8 1.8v1.8M8 12.4v1.8M13.4 4.9l-1.6.9M4.2 10.2l-1.6.9'
            'M13.4 11.1l-1.6-.9M4.2 5.8l-1.6-.9"/>',
    "pen": '<path d="M2.5 13.5l.8-3.1 7.5-7.5 2.3 2.3-7.5 7.5zM9.9 3.8l2.3 2.3"/>',
    "draw": '<path d="M2.2 12.8c2.4.8 3.4-.6 3-2-.5-1.5 1-2.6 2.4-1.8 1.7 1 4.5-1.5 5.6-5.4"/>'
            '<circle cx="13.4" cy="3.4" r="1.1"/>',
    "cursor": '<path d="M4 2.5l8.5 5.7-3.8.9-1.6 3.6z"/>',
    "highlight": '<path d="M3 12.5h10M5 10l5.6-5.7 1.8 1.8L6.8 11.8H5z"/>',
    "note": '<path d="M2.5 3.5c0-.6.4-1 1-1h9c.6 0 1 .4 1 1v6l-3 3h-7c-.6 0-1-.4-1-1z'
            'M10.5 12.5v-3h3"/>',
    "eraser": '<path d="M5.5 13h8M3 10.4l6-6a1 1 0 0 1 1.4 0l2.2 2.2a1 1 0 0 1 0 1.4'
              'L8.4 12.2H5z"/>',
    "trash": '<path d="M3 4.5h10M6.3 2.5h3.4M4.2 4.5l.7 8.3c0 .5.5.9 1 .9h4.2c.5 0 1-.4 1-.9'
             'l.7-8.3"/>',
    "save": '<path d="M2.5 3.5c0-.6.4-1 1-1h7.6l2.4 2.4v7.6c0 .6-.4 1-1 1h-9c-.6 0-1-.4-1-1z'
            'M5 2.8V6h5.4V2.8M5 13.2V9.6h6v3.6"/>',
    "comment": '<path d="M2.5 3.8c0-.6.4-1 1-1h9c.6 0 1 .4 1 1v6c0 .6-.4 1-1 1H7l-3 2.6v-2.6'
               'H3.5c-.6 0-1-.4-1-1z"/>',
    "broom": '<path d="M9.6 2.2L7.5 8.5M4.2 13.6c.3-2.6 1.5-4.4 3.3-5.1 1.8.7 3 2.5 3.3 5.1z"/>',
    "clearfmt": '<path d="M4 3h8M8.9 3l-2.2 8M4.6 11h4M11 10l3 3M14 10l-3 3"/>',
    "wave": '<path d="M2 8h1.5M5 5v6M8 2.8v10.4M11 5v6M12.8 8h1.4"/>',
    "align": '<path d="M2 4h12M2 8h8M2 12h10"/>',
    "book": '<path d="M8 3.4C6.8 2.5 4.8 2.2 2.5 2.5v10c2.3-.3 4.3 0 5.5.9 1.2-.9 3.2-1.2 5.5-.9'
            'v-10C11.2 2.2 9.2 2.5 8 3.4zM8 3.4v10"/>',
    "globe": '<circle cx="8" cy="8" r="6"/>'
             '<path d="M2 8h12M8 2c-3.3 3.6-3.3 8.4 0 12 3.3-3.6 3.3-8.4 0-12z"/>',
    "check": '<path d="M3 8.5l3.2 3.2L13 5"/>',
    "close": '<path d="M4 4l8 8M12 4l-8 8"/>',
    "skip": '<path d="M3 3l10 10M13 3L3 13"/>',
    "palette": '<path d="M8 2a6 6 0 0 0 0 12c.9 0 1.3-.6 1.3-1.2 0-.7-.5-1-.5-1.6 0-.5.4-.9 1-.9'
               'h1.3A2.9 2.9 0 0 0 14 7.4C14 4.4 11.3 2 8 2z"/>'
               '<circle cx="5.4" cy="6.4" r=".9"/><circle cx="8" cy="4.9" r=".9"/>',
    "text": '<path d="M4 3h8M8 3v10M6 13h4"/>',
    "podium": '<path d="M4.5 6.5h7l1 7h-9zM6.5 6.5v-2a1.5 1.5 0 0 1 3 0v2M3 6.5h10"/>',
    "doc": '<path d="M3.5 2.5h6l3 3v8c0 .3-.2.5-.5.5h-8.5c-.3 0-.5-.2-.5-.5v-11z"/>'
           '<path d="M9.3 2.6V5.6h3"/>',
    "split": '<rect x="1.8" y="3" width="12.4" height="10" rx="1"/><path d="M8 3v10"/>',
    "swap": '<path d="M3 5.5h9M9.5 3l2.5 2.5L9.5 8M13 10.5H4M6.5 8L4 10.5 6.5 13"/>',
    "more": '<circle cx="3.4" cy="8" r=".9" fill="currentColor" stroke="none"/>'
            '<circle cx="8" cy="8" r=".9" fill="currentColor" stroke="none"/>'
            '<circle cx="12.6" cy="8" r=".9" fill="currentColor" stroke="none"/>',
}

_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" '
    'fill="none" stroke="{color}" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round" color="{color}">{paths}</svg>'
)


def available_icons() -> list[str]:
    """回傳所有可用的圖示名稱（供測試與除錯）。"""
    return sorted(_PATHS)


@lru_cache(maxsize=256)
def _render(name: str, color: str, size: int) -> QPixmap:
    paths = _PATHS.get(name)
    if paths is None:
        return QPixmap()
    svg = _TEMPLATE.format(color=color, paths=paths)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pm = QPixmap(QSize(size, size))
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    renderer.render(painter)
    painter.end()
    return pm


def icon(name: str, color: str = DEFAULT_COLOR, size: int = 16) -> QIcon:
    """取得圖示。名稱不存在時回傳空 QIcon（不拋例外，避免介面建構中斷）。"""
    pm = _render(name, color, size)
    if pm.isNull():
        return QIcon()
    # 同時提供 2× 供高 DPI 螢幕
    ic = QIcon(pm)
    hi = _render(name, color, size * 2)
    if not hi.isNull():
        ic.addPixmap(hi)
    return ic
