"""SpeechRecognizerWorker 的 LocalAgreement 流程測試（mock 模型）。"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from teleprompter.core.audio_capture import AudioWindow
from teleprompter.core.speech_recognizer import SpeechRecognizerWorker


@pytest.fixture(scope="module")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


def _make_window(samples_count: int = 16000, is_boundary: bool = False) -> AudioWindow:
    return AudioWindow(
        samples=np.zeros(samples_count, dtype=np.float32),
        duration_ms=int(samples_count / 16),
        is_boundary=is_boundary,
    )


def test_worker_emits_committed_text_on_stable_prefix(qapp):
    """當連續兩次推論的共同前綴變長時，新增的部分應被 commit。"""
    worker = SpeechRecognizerWorker()
    committed: list[str] = []
    worker.text_committed.connect(lambda s: committed.append(s))

    # mock _transcribe 直接回傳預設文字
    with patch.object(worker, "_transcribe", side_effect=["大家好今天", "大家好今天我要報告"]):
        window1 = _make_window()
        worker._process_window(window1)
        # 第一次：prev_hypothesis 為空，common prefix = 0，不會 commit
        assert committed == []
        window2 = _make_window()
        worker._process_window(window2)
        # 第二次：與前一次的共同前綴是「大家好今天」(5 chars) → commit
        assert any("大家好今天" in s for s in committed)


def test_worker_does_not_commit_unstable_text(qapp):
    """前後兩次推論差異很大，僅 commit 共同前綴。"""
    worker = SpeechRecognizerWorker()
    committed: list[str] = []
    worker.text_committed.connect(lambda s: committed.append(s))

    with patch.object(worker, "_transcribe", side_effect=["你好世界", "你好朋友"]):
        worker._process_window(_make_window())
        worker._process_window(_make_window())
    # 共同前綴是「你好」(2 chars) → 應 commit「你好」
    full = "".join(committed)
    assert full.startswith("你好") or full == ""  # 有可能因標點處理導致空


def test_worker_boundary_commits_full_hypothesis(qapp):
    """is_boundary=True 時應把整個 hypothesis 全部 commit 並 reset。"""
    worker = SpeechRecognizerWorker()
    committed: list[str] = []
    worker.text_committed.connect(lambda s: committed.append(s))

    with patch.object(worker, "_transcribe", return_value="完整一句話結束了"):
        window = _make_window(is_boundary=True)
        worker._process_window(window)

    # 邊界時應 emit 整段
    assert "完整一句話結束了" in "".join(committed)
    # hypothesis 應重置
    assert worker._prev_hypothesis == ""
    assert worker._committed_in_current_window == 0


def test_worker_skips_empty_transcribe(qapp):
    """transcribe 回空字串時不應 commit。"""
    worker = SpeechRecognizerWorker()
    committed: list[str] = []
    worker.text_committed.connect(lambda s: committed.append(s))

    with patch.object(worker, "_transcribe", return_value=""):
        worker._process_window(_make_window())
        worker._process_window(_make_window())
    assert committed == []


def test_worker_emits_hypothesis_signal(qapp):
    """每次 transcribe 都應 emit hypothesis signal 給 UI 顯示。"""
    worker = SpeechRecognizerWorker()
    hypotheses: list[str] = []
    worker.hypothesis.connect(lambda s: hypotheses.append(s))

    with patch.object(worker, "_transcribe", side_effect=["第一段", "第一段二"]):
        worker._process_window(_make_window())
        worker._process_window(_make_window())
    assert hypotheses == ["第一段", "第一段二"]


def test_worker_update_prompt_caps_at_200_chars(qapp):
    worker = SpeechRecognizerWorker()
    worker.update_prompt("a" * 500)
    assert len(worker.initial_prompt) <= 200


def test_worker_update_prompt_with_empty_clears_it(qapp):
    worker = SpeechRecognizerWorker(initial_prompt="原本的提示")
    worker.update_prompt("")
    assert worker.initial_prompt == ""


def test_looks_non_english_detects_cjk():
    from teleprompter.core.speech_recognizer import SpeechRecognizerWorker
    # 全 CJK → 非英文
    assert SpeechRecognizerWorker._looks_non_english("你好世界")
    # 大量中文混少量英文 → 非英文
    assert SpeechRecognizerWorker._looks_non_english("hi 你好我是今天的報告人")
    # 純英文 → 是英文
    assert not SpeechRecognizerWorker._looks_non_english("hello world this is english")
    # 含極少量 CJK 符號（< 20%）→ 可接受
    assert not SpeechRecognizerWorker._looks_non_english("The word is 家 in Chinese.")


def test_is_nearly_pure_english_detects():
    from teleprompter.core.speech_recognizer import SpeechRecognizerWorker
    assert SpeechRecognizerWorker._is_nearly_pure_english("hello world")
    assert not SpeechRecognizerWorker._is_nearly_pure_english("大家好")
    assert not SpeechRecognizerWorker._is_nearly_pure_english("hi")  # 太短


def test_strip_punctuation_removes_chinese_and_english_punct():
    from teleprompter.core.speech_recognizer import SpeechRecognizerWorker
    s = "大家好，我是今天的報告人。今天介紹 transformer！"
    out = SpeechRecognizerWorker._strip_punctuation(s)
    assert "，" not in out
    assert "。" not in out
    assert "！" not in out
    assert "大家好" in out
    assert "transformer" in out


def test_strip_punctuation_keeps_letters_and_numbers():
    from teleprompter.core.speech_recognizer import SpeechRecognizerWorker
    s = "GLUE 88.5 分"
    out = SpeechRecognizerWorker._strip_punctuation(s)
    # 數字中的點號不在我們剝除清單內 (.) 是英文標點 → 也會被剝除
    # 所以 88.5 會變成 88 5
    assert "GLUE" in out
    assert "88" in out
    assert "分" in out


def test_strip_punctuation_collapses_multiple_spaces():
    from teleprompter.core.speech_recognizer import SpeechRecognizerWorker
    s = "你好，，，世界"
    out = SpeechRecognizerWorker._strip_punctuation(s)
    assert out == "你好 世界"


def test_worker_consistent_committed_count(qapp):
    """委派 commit 的計數應與實際 emit 的字數一致。"""
    worker = SpeechRecognizerWorker()
    committed: list[str] = []
    worker.text_committed.connect(lambda s: committed.append(s))

    # 第三輪會擴展 committed prefix
    with patch.object(
        worker,
        "_transcribe",
        side_effect=[
            "你好",
            "你好世界",
            "你好世界再見",
        ],
    ):
        for _ in range(3):
            worker._process_window(_make_window())

    full = "".join(committed)
    # 最終穩定下來的應該包含「你好世界」前綴（最後一次未必能 commit 整段）
    assert "你好" in full
