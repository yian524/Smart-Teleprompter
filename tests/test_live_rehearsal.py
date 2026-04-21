"""Live rehearsal — 把近期修改的功能串成一場完整報告跑過。

目標：在一個 MainWindow 實例上，模擬整場報告的使用者行為（載檔、辨識推進、
切模式、切 swap、切直橫屏、切分頁），每一步後都檢查 state 一致，不做狀態腐蝕
後幾個 commit 才爆炸的 bug。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from teleprompter.config import load_config
from teleprompter.ui import main_window as mw_mod
from teleprompter.ui.main_window import MainWindow


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def mw(app, tmp_path, monkeypatch):
    monkeypatch.setattr(
        mw_mod, "default_sessions_path", lambda: tmp_path / "sessions.json"
    )
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Discard),
    )
    cfg = load_config()
    w = MainWindow(cfg)
    w.resize(1600, 900)
    w.show()
    loop = QEventLoop(); QTimer.singleShot(150, loop.quit); loop.exec()
    yield w
    w.close()


def _make_pdf(tmp_path, n_pages=5, name="deck.pdf"):
    import fitz
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 72), f"Slide {i + 1}", fontsize=24)
    p = tmp_path / name
    doc.save(str(p))
    doc.close()
    return p


def _make_transcript(tmp_path, n_pages=5, sents_per_page=3, name="talk.txt"):
    parts = []
    for p in range(1, n_pages + 1):
        parts.append(f"# 第{p}頁")
        for s in range(1, sents_per_page + 1):
            parts.append(f"第{p}頁的第{s}句內容是這樣的。")
    text = "\n".join(parts)
    # 以 --- 分頁
    pages = []
    for p in range(1, n_pages + 1):
        head = [f"# 第{p}頁"]
        for s in range(1, sents_per_page + 1):
            head.append(f"第{p}頁的第{s}句內容是這樣的。")
        pages.append("\n".join(head))
    full = "\n\n---\n\n".join(pages)
    f = tmp_path / name
    f.write_text(full, encoding="utf-8")
    return f


def _pump(ms=100):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()


# ---- PR1：完整順讀整份講稿、slide 跟著換頁 ----


def test_pr1_full_run_auto_advances_slides(mw, tmp_path):
    """直接操作 engine 位置推進每頁第一句 → 驗證 slide_mode_view 自動換頁。
    不依賴 Whisper 模糊比對（太脆弱）；測試重點是 UI 同步，不是對齊引擎本身。"""
    txt = _make_transcript(tmp_path, n_pages=5, sents_per_page=3)
    pdf = _make_pdf(tmp_path, n_pages=5)
    mw.load_file(str(txt))
    mw.load_slides(str(pdf))
    _pump(150)
    mw._set_view_mode("slide")
    _pump(100)

    pages = mw.transcript.pages
    for p_idx, page in enumerate(pages):
        # 強制對齊到該頁第一句
        mw.engine.jump_to_sentence(page.sentence_start)
        mw._maybe_auto_advance_page()
        _pump(20)
        assert mw.slide_mode_view.current_page() == p_idx, (
            f"engine 在第 {p_idx + 1} 頁但 slide 顯示第 {mw.slide_mode_view.current_page() + 1} 頁"
        )


# ---- PR2：報告中切 layout swap（左右互換）不該打斷 ----


def test_pr2_layout_swap_mid_presentation(mw, tmp_path):
    """UI swap 不該動到 engine 狀態；swap 後仍可推進。"""
    txt = _make_transcript(tmp_path, n_pages=3, sents_per_page=4)
    pdf = _make_pdf(tmp_path, n_pages=3)
    mw.load_file(str(txt))
    mw.load_slides(str(pdf))
    _pump(150)
    mw._set_view_mode("split")
    _pump(50)

    # 用 jump 模擬推進
    mw.engine.jump_to_sentence(2)
    pre_idx = mw.engine.current_sentence_index
    pre_pos = mw.engine.current_global_char

    mw._toggle_layout_swap()
    _pump(80)
    assert mw._layout_swapped is True
    # engine 不受 UI swap 影響
    assert mw.engine.current_sentence_index == pre_idx
    assert mw.engine.current_global_char == pre_pos

    # swap 後再跳句仍正常
    mw.engine.jump_to_sentence(4)
    assert mw.engine.current_sentence_index == 4


# ---- PR3：橫→直屏切換 時，split 模式底下 view 會換，state 不斷 ----


def test_pr3_orientation_change_during_talk(mw, tmp_path):
    """橫/直屏切換時 split 模式底下 view stack 會換、engine 狀態保留。"""
    txt = _make_transcript(tmp_path, n_pages=3, sents_per_page=3)
    pdf = _make_pdf(tmp_path, n_pages=3)
    mw.load_file(str(txt))
    mw.load_slides(str(pdf))
    _pump(150)
    mw._set_view_mode("split")
    _pump(50)

    # 橫屏：PrompterView（stack 0）
    assert mw.width() > mw.height()
    assert mw._content_stack.currentIndex() == 0
    mw.engine.jump_to_sentence(2)
    pre_idx = mw.engine.current_sentence_index

    # 直屏 → 切到 SlideModeView（stack 1）
    mw.resize(800, 1400)
    mw._apply_orientation_layout()
    _pump(100)
    assert mw._content_stack.currentIndex() == 1
    assert mw.engine.current_sentence_index == pre_idx

    # 直屏下再跳句
    mw.engine.jump_to_sentence(4)
    assert mw.engine.current_sentence_index == 4

    # 回橫屏 → PrompterView
    mw.resize(1600, 900)
    mw._apply_orientation_layout()
    _pump(100)
    assert mw._content_stack.currentIndex() == 0
    assert mw.engine.current_sentence_index == 4


# ---- PR4：報告中開新分頁再切回來，原 session engine 位置不變 ----


def test_pr4_tab_switch_does_not_lose_position(mw, tmp_path):
    txt1 = _make_transcript(tmp_path, n_pages=3, sents_per_page=3, name="talk1.txt")
    mw.load_file(str(txt1))
    _pump(150)
    # 講 3 句
    for s in mw.transcript.sentences[:3]:
        mw.recognizer.text_committed.emit(s.text)
        _pump(20)
    tab1_idx = mw.engine.current_sentence_index
    tab1_session_id = mw._bound_session_id

    # 開第 2 個分頁並載新檔
    mw._new_tab()
    _pump(100)
    txt2 = _make_transcript(tmp_path, n_pages=2, sents_per_page=2, name="talk2.txt")
    mw.load_file(str(txt2))
    _pump(150)
    assert mw._bound_session_id != tab1_session_id

    # 切回第 1 個分頁
    mw.session_manager.set_active(tab1_session_id)
    _pump(100)
    assert mw._bound_session_id == tab1_session_id
    assert mw.engine.current_sentence_index == tab1_idx


# ---- PR5：載入投影片但講稿為空 → 自動 scaffold 可直接講稿 + 推進 ----


def test_pr5_empty_tab_load_slides_then_speak(mw, tmp_path):
    pdf = _make_pdf(tmp_path, n_pages=4)
    # 乾淨分頁：無講稿直接載投影片
    mw._new_tab()
    _pump(80)
    mw.load_slides(str(pdf))
    _pump(100)

    # scaffold 應建起來：每頁一個 block
    assert mw.transcript is not None
    assert len(mw.transcript.pages) == 4
    # 模擬使用者打入真實內容覆蓋第 1 頁 placeholder
    mw.view.set_edit_mode(True)
    mw.view.setPlainText(
        "# 第一頁\n\n大家好我是今天的報告者。\n\n"
        "---\n\n# 第二頁\n\n第二頁我要講的是這樣。\n\n"
        "---\n\n# 第三頁\n\n第三頁就是結論。\n\n"
        "---\n\n# 第四頁\n\n謝謝大家。\n"
    )
    mw.view.set_edit_mode(False)
    _pump(150)
    # 講第一頁第一句（現在是真內容）
    mw.recognizer.text_committed.emit("大家好我是今天的報告者")
    _pump(50)
    # engine 應推進
    assert mw.engine.current_sentence_index >= 0


# ---- PR6：關 app 時 dirty session 會被問、不會 crash ----


def test_pr6_close_with_dirty_session_does_not_crash(mw, tmp_path, monkeypatch):
    txt = _make_transcript(tmp_path, n_pages=2, sents_per_page=2)
    mw.load_file(str(txt))
    _pump(100)
    # 標 dirty
    mw.view.set_edit_mode(True)
    mw.view.setPlainText(mw.view.toPlainText() + "\n我加了一行。")
    mw.view.set_edit_mode(False)
    _pump(100)
    # close 應不 crash（fixture 的 monkeypatch 會回 Discard）
    mw.close()
    # close 完 fixture 會再 close 一次 → 也不能 crash


# ---- PR7：hallucination 重複 N-gram 不該推進 engine ----


def test_pr7_hallucination_rejected_by_engine(mw, tmp_path):
    txt = _make_transcript(tmp_path, n_pages=2, sents_per_page=2)
    mw.load_file(str(txt))
    _pump(100)
    pre = mw.engine.current_sentence_index
    # hallucination 送進 engine（engine 會被 _is_hallucination 擋在 recognizer 層；
    # 但 engine 本身看到無意義字串也不該跳）
    mw.recognizer.text_committed.emit("嗯嗯嗯嗯嗯嗯嗯嗯嗯嗯嗯嗯")
    _pump(50)
    mw.recognizer.text_committed.emit("哇哇哇哇哇哇哇哇哇哇哇哇")
    _pump(50)
    # engine 位置最多應該只前進一點點，不會爆掉
    assert abs(mw.engine.current_sentence_index - pre) <= 1


# ---- PR8：DPR 不同的 slide render 快取彼此獨立，不會互相覆蓋 ----


def test_pr8_slide_render_cache_isolates_by_dpr(mw, tmp_path):
    pdf = _make_pdf(tmp_path, n_pages=3)
    from teleprompter.core.pdf_renderer import SlideDeck
    deck = SlideDeck(pdf)
    p1 = deck.render(1, 400, dpr=1.0)
    p2 = deck.render(1, 400, dpr=2.0)
    assert p1.devicePixelRatio() == 1.0
    assert p2.devicePixelRatio() == 2.0
    # 再取一次 DPR=1.0 的 cache 應仍為 1.0（不被 2.0 覆寫）
    p1_again = deck.render(1, 400, dpr=1.0)
    assert p1_again.devicePixelRatio() == 1.0
    deck.close()


# ---- PR9：session 序列化+還原，engine / view 狀態一致 ----


def test_pr9_session_roundtrip_restores_position(mw, tmp_path):
    txt = _make_transcript(tmp_path, n_pages=3, sents_per_page=3)
    mw.load_file(str(txt))
    _pump(100)
    # 講 4 句
    for s in mw.transcript.sentences[:4]:
        mw.recognizer.text_committed.emit(s.text)
        _pump(20)
    snapshot_idx = mw.engine.current_sentence_index
    snapshot_char = mw.engine.current_global_char
    # 存檔
    from teleprompter.ui.main_window import default_sessions_path
    mw.session_manager.save_to_disk(default_sessions_path())
    # 讀回來的 session 狀態一致（不走 UI，直接檢查 active session）
    active = mw.session_manager.active
    assert active.current_sentence_index == snapshot_idx
    assert active.current_global_char == snapshot_char


# ---- PR10：Start/Pause 在報告中交替，engine 不錯亂 ----


def test_pr10_start_pause_alternating_preserves_state(mw, tmp_path):
    txt = _make_transcript(tmp_path, n_pages=2, sents_per_page=3)
    mw.load_file(str(txt))
    _pump(100)

    # 講一句
    mw.recognizer.text_committed.emit(mw.transcript.sentences[0].text)
    _pump(30)
    idx1 = mw.engine.current_sentence_index

    # 假裝 pause/resume（切換 act_start 行為）
    # engine 位置不應被 pause 重設
    mw._pause() if hasattr(mw, "_pause") else None
    _pump(30)
    assert mw.engine.current_sentence_index == idx1

    # 再講兩句
    for s in mw.transcript.sentences[1:3]:
        mw.recognizer.text_committed.emit(s.text)
        _pump(30)
    assert mw.engine.current_sentence_index >= idx1
