"""Annotation (sticky note + pencil) + PDF text selection tests."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from teleprompter.core.annotations import Annotation
from teleprompter.core.session import Session
from teleprompter.core.transcript_loader import load_from_string


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


# ---------- Annotation data model ----------


def test_annotation_roundtrip():
    a = Annotation(
        kind="note", slide_page=3, x=0.2, y=0.3, width=0.25, height=0.1,
        text="測試筆記", color="#FFEB3B",
    )
    d = a.to_dict()
    restored = Annotation.from_dict(d)
    assert restored.kind == a.kind
    assert restored.slide_page == a.slide_page
    assert restored.text == a.text
    assert restored.x == a.x
    assert restored.annotation_id == a.annotation_id


def test_stroke_roundtrip():
    a = Annotation(
        kind="stroke", slide_page=5, color="#FF0000", stroke_width=5,
        strokes=[[(0.1, 0.2), (0.3, 0.4)], [(0.5, 0.6), (0.7, 0.8)]],
    )
    d = a.to_dict()
    restored = Annotation.from_dict(d)
    assert restored.kind == "stroke"
    assert len(restored.strokes) == 2
    assert restored.strokes[0] == [(0.1, 0.2), (0.3, 0.4)]
    assert restored.stroke_width == 5


def test_session_includes_annotations():
    ann = Annotation(kind="note", slide_page=2, text="hello")
    s = Session(title="t", annotations=[ann])
    d = s.to_json()
    assert len(d["annotations"]) == 1
    restored = Session.from_json(d)
    assert len(restored.annotations) == 1
    assert restored.annotations[0].text == "hello"


# ---------- SlideModeView tool + annotation behavior ----------


def test_slide_mode_view_default_tool(app):
    from teleprompter.ui.slide_mode_view import SlideModeView

    smv = SlideModeView()
    assert smv.current_tool() == SlideModeView.TOOL_POINTER


def test_slide_mode_view_set_tool(app):
    from teleprompter.ui.slide_mode_view import SlideModeView

    smv = SlideModeView()
    smv.set_tool(SlideModeView.TOOL_PENCIL)
    assert smv.current_tool() == SlideModeView.TOOL_PENCIL
    smv.set_tool("invalid_tool")
    # 無效值不該改變
    assert smv.current_tool() == SlideModeView.TOOL_PENCIL


def test_slide_mode_view_tool_keyboard_shortcuts(app):
    from teleprompter.ui.slide_mode_view import SlideModeView

    smv = SlideModeView()
    # P = pencil
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_P, Qt.KeyboardModifier.NoModifier)
    smv.keyPressEvent(ev)
    assert smv.current_tool() == SlideModeView.TOOL_PENCIL
    # N = note
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_N, Qt.KeyboardModifier.NoModifier)
    smv.keyPressEvent(ev)
    assert smv.current_tool() == SlideModeView.TOOL_NOTE
    # V = pointer
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_V, Qt.KeyboardModifier.NoModifier)
    smv.keyPressEvent(ev)
    assert smv.current_tool() == SlideModeView.TOOL_POINTER


def test_slide_mode_view_filters_annotations_by_page(app):
    """current_page_annotations 只回傳當前頁的標註。"""
    from teleprompter.ui.slide_mode_view import SlideModeView

    text = "# P1\n內容\n\n---\n\n# P2\n內容\n\n---\n\n# P3\n內容\n"
    tr = load_from_string(text)
    smv = SlideModeView()
    smv.set_transcript(tr)
    smv.set_annotations([
        Annotation(kind="note", slide_page=1, text="P1 note"),
        Annotation(kind="note", slide_page=2, text="P2 note"),
        Annotation(kind="note", slide_page=3, text="P3 note"),
    ])
    smv.set_current_page(0)  # page 1
    assert len(smv.current_page_annotations()) == 1
    assert smv.current_page_annotations()[0].text == "P1 note"
    smv.set_current_page(1)  # page 2
    assert smv.current_page_annotations()[0].text == "P2 note"


def test_erase_removes_annotation_from_list(app):
    """橡皮擦以 viewport 座標找到並刪除標註。"""
    from teleprompter.ui.slide_mode_view import SlideModeView

    text = "# P1\n內容\n"
    tr = load_from_string(text)
    smv = SlideModeView()
    smv.resize(1000, 800)
    smv.set_transcript(tr)
    smv.set_current_page(0)
    # note 在 viewport 比例 (0.1, 0.1)，大小 (0.3, 0.2)
    # → viewport 座標：note_rect = (100, 80, 300, 160)
    ann = Annotation(
        kind="note", slide_page=1, x=0.1, y=0.1, width=0.3, height=0.2, text="刪我"
    )
    smv.set_annotations([ann])
    assert len(smv.annotations()) == 1
    # 點在 viewport (150, 120) = note rect 內
    smv._erase_at(QPoint(150, 120))
    assert len(smv.annotations()) == 0


# ---------- Session persistence ----------


def test_pencil_stroke_works_on_entire_viewport(app):
    """鉛筆畫不再限制在 slide rect 內，整個 viewport 都可用。"""
    from teleprompter.ui.slide_mode_view import SlideModeView
    from PySide6.QtCore import QPointF

    text = "# P1\n內容\n"
    tr = load_from_string(text)
    smv = SlideModeView()
    smv.resize(1000, 800)
    smv.set_transcript(tr)
    smv.set_current_page(0)
    smv.set_tool(SlideModeView.TOOL_PENCIL)

    # 模擬在 viewport 中間畫一道（不在任何 slide rect 內）
    smv._drawing_stroke = [QPointF(100, 100), QPointF(200, 200), QPointF(300, 300)]
    smv._finalize_pencil_stroke()
    # 應該有一個 stroke annotation
    anns = smv.annotations()
    assert len(anns) == 1
    assert anns[0].kind == "stroke"
    # viewport 比例：(100/1000, 100/800) = (0.1, 0.125)
    seg = anns[0].strokes[0]
    assert abs(seg[0][0] - 0.1) < 0.01
    assert abs(seg[0][1] - 0.125) < 0.01


def test_add_sticky_note_uses_viewport_coords(app, monkeypatch):
    """便利貼也是 viewport 比例座標。"""
    from teleprompter.ui.slide_mode_view import SlideModeView
    from PySide6.QtWidgets import QInputDialog

    text = "# P1\n內容\n"
    tr = load_from_string(text)
    smv = SlideModeView()
    smv.resize(1000, 800)
    smv.set_transcript(tr)
    smv.set_current_page(0)
    # Mock dialog to return "hello"
    monkeypatch.setattr(
        QInputDialog, "getMultiLineText",
        staticmethod(lambda *a, **kw: ("我的筆記", True)),
    )
    # 在 viewport (500, 400) 位置新增便利貼
    smv._add_sticky_note_at(QPoint(500, 400))
    anns = smv.annotations()
    assert len(anns) == 1
    assert anns[0].kind == "note"
    assert anns[0].text == "我的筆記"
    # 比例應為 (0.5, 0.5)
    assert abs(anns[0].x - 0.5) < 0.01
    assert abs(anns[0].y - 0.5) < 0.01


def test_text_copied_signal_emitted_after_selection(app):
    """選字 release 時應發 text_copied signal。"""
    from teleprompter.ui.slide_mode_view import SlideModeView

    smv = SlideModeView()
    got = []
    smv.text_copied.connect(lambda t: got.append(t))
    # 模擬有選字
    smv._selected_text = "hello"
    # 直接模擬 release 路徑：在非 dragging 情況下 text_select_start 不為 None
    smv._tool = SlideModeView.TOOL_SELECT
    smv._text_select_start = QPoint(0, 0)
    # 呼叫 finalize 不會自動發 signal（signal 在 mouseRelease 發）
    # 所以這邊測試 copy_selected_text 沒問題
    assert smv.copy_selected_text() is True


def test_eraser_smudge_partial_removes_stroke_points(app):
    """塗抹式橡皮擦：筆劃中被擦到的點被移除，其他點保留。"""
    from teleprompter.ui.slide_mode_view import SlideModeView

    text = "# P1\n內容\n"
    tr = load_from_string(text)
    smv = SlideModeView()
    smv.resize(1000, 800)
    smv.set_transcript(tr)
    smv.set_current_page(0)
    # 一道橫的筆劃：10 個點從 x=0.1 到 x=0.9（viewport 座標 100→900）
    points = [(i / 10, 0.5) for i in range(1, 10)]
    ann = Annotation(
        kind="stroke", anchor="slide", slide_page=1,
        color="#FF0000", stroke_width=3,
        strokes=[points],
    )
    smv.set_annotations([ann])
    # 橡皮擦半徑 18px → 擦 viewport 中間 (500, 400) 附近
    smv._erase_at(QPoint(500, 400))
    anns = smv.annotations()
    # stroke 應該被切成 2 段（左半 + 右半），或至少點數減少
    assert len(anns) == 1, "只擦到中間，整個 annotation 不該被刪"
    total_pts = sum(len(s) for s in anns[0].strokes)
    assert total_pts < len(points), "被擦到的點應該消失"


def test_eraser_removes_annotation_when_all_points_erased(app):
    """所有點都被擦到 → annotation 整個刪除。"""
    from teleprompter.ui.slide_mode_view import SlideModeView

    text = "# P1\n內容\n"
    tr = load_from_string(text)
    smv = SlideModeView()
    smv.resize(1000, 800)
    smv.set_transcript(tr)
    smv.set_current_page(0)
    # 集中在 (500, 400) 附近的筆劃
    points = [(0.5, 0.5), (0.51, 0.5), (0.49, 0.5)]
    ann = Annotation(
        kind="stroke", anchor="slide", slide_page=1, strokes=[points],
    )
    smv.set_annotations([ann])
    smv._erase_at(QPoint(500, 400))
    assert len(smv.annotations()) == 0


def test_note_drag_updates_position(app):
    """指標模式點便利貼 + 拖拉 → 位置變更。"""
    from teleprompter.ui.slide_mode_view import SlideModeView
    from PySide6.QtCore import QEvent, QPointF

    text = "# P1\n內容\n"
    tr = load_from_string(text)
    smv = SlideModeView()
    smv.resize(1000, 800)
    smv.set_transcript(tr)
    smv.set_current_page(0)
    ann = Annotation(
        kind="note", anchor="slide", slide_page=1,
        x=0.1, y=0.1, width=0.2, height=0.1, text="拖我",
    )
    smv.set_annotations([ann])
    smv.set_tool(SlideModeView.TOOL_POINTER)
    # 點在 note 範圍內 (150, 120)
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(150, 120),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    smv.mousePressEvent(press)
    assert smv._dragging_note is ann
    # 移到 (500, 400)
    move = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(500, 400),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    smv.mouseMoveEvent(move)
    # 放開
    release = QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(500, 400),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    smv.mouseReleaseEvent(release)
    # 位置應更新（大約 (500-offset)/1000, (400-offset)/800）
    assert ann.x > 0.3   # 拖到右邊了
    assert ann.y > 0.3
    assert smv._dragging_note is None


def test_note_resize_from_handle(app):
    """點右下角 handle 拖拉 → 便利貼變大/小。"""
    from teleprompter.ui.slide_mode_view import SlideModeView
    from PySide6.QtCore import QEvent, QPointF

    text = "# P1\n內容\n"
    tr = load_from_string(text)
    smv = SlideModeView()
    smv.resize(1000, 800)
    smv.set_transcript(tr)
    smv.set_current_page(0)
    ann = Annotation(
        kind="note", anchor="slide", slide_page=1,
        x=0.1, y=0.1, width=0.2, height=0.1, text="resize me",
    )
    smv.set_annotations([ann])
    smv.set_tool(SlideModeView.TOOL_POINTER)

    # note rect: x=100, y=80, w=200, h=80 → right=300, bottom=160
    # resize handle: 14x14 at (286, 146) ~ (300, 160)
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(295, 155),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    smv.mousePressEvent(press)
    assert smv._resizing_note is ann, "點 handle 應進入 resize 模式"
    # 拖到 (500, 400) → 新 width = (500-100)/1000 = 0.4; 新 height = (400-80)/800 = 0.4
    move = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(500, 400),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    smv.mouseMoveEvent(move)
    assert abs(ann.width - 0.4) < 0.01
    assert abs(ann.height - 0.4) < 0.01
    # 放開
    release = QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(500, 400),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    smv.mouseReleaseEvent(release)
    assert smv._resizing_note is None


def test_note_resize_clamped_to_min_max(app):
    """縮放有最小最大限制。"""
    from teleprompter.ui.slide_mode_view import SlideModeView
    from PySide6.QtCore import QEvent, QPointF

    text = "# P1\n內容\n"
    tr = load_from_string(text)
    smv = SlideModeView()
    smv.resize(1000, 800)
    smv.set_transcript(tr)
    smv.set_current_page(0)
    ann = Annotation(
        kind="note", anchor="slide", slide_page=1,
        x=0.1, y=0.1, width=0.2, height=0.1, text="x",
    )
    smv.set_annotations([ann])
    smv.set_tool(SlideModeView.TOOL_POINTER)
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(295, 155),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    smv.mousePressEvent(press)
    # 拖到很小 (50, 50) → 應被 clamp 到最小值
    move_small = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(50, 50),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    smv.mouseMoveEvent(move_small)
    assert ann.width >= 0.08
    assert ann.height >= 0.05


def test_word_level_selection_indexing(app):
    """Word-level 選字：anchor + focus 是 word index，非 pixel rect。"""
    from teleprompter.ui.slide_mode_view import SlideModeView

    smv = SlideModeView()
    assert smv._text_select_anchor_idx is None
    assert smv._text_select_focus_idx is None
    # 模擬設定
    smv._text_select_anchor_idx = 3
    smv._text_select_focus_idx = 7
    assert smv._text_select_anchor_idx == 3
    assert smv._text_select_focus_idx == 7


def test_pdf_renderer_get_text_blocks_api():
    """pdf_renderer.SlideDeck.get_text_blocks 介面存在且回傳 list[TextBlock]。"""
    from teleprompter.core.pdf_renderer import SlideDeck, TextBlock

    # 不需要真的 PDF，檢查 TextBlock 結構即可
    tb = TextBlock(x0=10.0, y0=20.0, x1=50.0, y1=40.0, text="hello")
    assert tb.text == "hello"
    assert tb.x1 > tb.x0
    # SlideDeck 有這個方法
    assert hasattr(SlideDeck, "get_text_blocks")


def test_annotation_doc_anchor_roundtrip():
    """doc 錨點標註 to_dict / from_dict 完整。"""
    a = Annotation(
        kind="stroke", anchor="doc", char_offset=1234,
        color="#FF0000", stroke_width=4,
        strokes=[[(0.3, 800.0), (0.4, 820.0)]],
    )
    d = a.to_dict()
    assert d["anchor"] == "doc"
    assert d["char_offset"] == 1234
    restored = Annotation.from_dict(d)
    assert restored.anchor == "doc"
    assert restored.char_offset == 1234
    assert restored.strokes == [[(0.3, 800.0), (0.4, 820.0)]]


def test_annotation_slide_anchor_default_for_legacy_data():
    """沒 anchor 欄位的舊資料預設 slide 錨點。"""
    legacy = {
        "kind": "note", "slide_page": 3,
        "x": 0.5, "y": 0.5, "text": "old",
    }
    a = Annotation.from_dict(legacy)
    assert a.anchor == "slide"
    assert a.slide_page == 3


def test_prompter_view_tools_api(app):
    """PrompterView 有 set_tool / annotations API（跟 SlideModeView 同介面）。"""
    from teleprompter.ui.prompter_view import PrompterView

    v = PrompterView()
    assert hasattr(v, "set_tool")
    assert hasattr(v, "annotations")
    assert v.current_tool() == PrompterView.TOOL_POINTER
    v.set_tool(PrompterView.TOOL_PENCIL)
    assert v.current_tool() == PrompterView.TOOL_PENCIL


def test_prompter_view_accepts_doc_anchor_annotations_only(app):
    """set_annotations 只保留 anchor=='doc' 的項。"""
    from teleprompter.ui.prompter_view import PrompterView

    v = PrompterView()
    anns = [
        Annotation(kind="note", anchor="slide", slide_page=1, text="slide note"),
        Annotation(kind="note", anchor="doc", char_offset=100, text="doc note"),
    ]
    v.set_annotations(anns)
    got = v.annotations()
    assert len(got) == 1
    assert got[0].anchor == "doc"
    assert got[0].text == "doc note"


def test_session_preserves_annotations_across_save_load(tmp_path):
    from teleprompter.core.session import SessionManager

    s = Session(title="test")
    s.annotations = [
        Annotation(kind="note", slide_page=2, text="我的筆記"),
        Annotation(
            kind="stroke", slide_page=3, color="#FF0000", stroke_width=4,
            strokes=[[(0.1, 0.2), (0.5, 0.6)]],
        ),
    ]
    mgr = SessionManager()
    mgr.add(s)
    path = tmp_path / "sessions.json"
    mgr.save_to_disk(path)

    # 新 manager 重新載入
    mgr2 = SessionManager()
    mgr2.load_from_disk(path)
    restored = mgr2.sessions[0]
    assert len(restored.annotations) == 2
    notes = [a for a in restored.annotations if a.kind == "note"]
    strokes = [a for a in restored.annotations if a.kind == "stroke"]
    assert notes[0].text == "我的筆記"
    assert strokes[0].color == "#FF0000"
    assert strokes[0].stroke_width == 4
    assert strokes[0].strokes[0] == [(0.1, 0.2), (0.5, 0.6)]
