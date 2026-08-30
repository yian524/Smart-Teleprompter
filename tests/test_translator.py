"""Q&A 即時翻譯：方向、去重、語言過濾。

真實模型 1.2GB，測試不載它——用假的 CTranslate2 translator 驗證邏輯。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from teleprompter.core.translator import TranslatorWorker  # noqa: E402


# ============================================================
# 翻譯方向
# ============================================================

def test_default_direction_is_en_to_zh():
    w = TranslatorWorker()
    assert w._src_code == "eng_Latn"
    assert w._tgt_code == "zho_Hant"


def test_set_direction_maps_language_codes():
    w = TranslatorWorker()
    w.set_direction("zh", "en")
    assert w._src_code == "zho_Hant"
    assert w._tgt_code == "eng_Latn"


def test_unknown_language_falls_back_to_default():
    """認不得的代碼不該讓翻譯整個壞掉。"""
    w = TranslatorWorker()
    w.set_direction("klingon", "elvish")
    assert w._src_code == "eng_Latn"
    assert w._tgt_code == "zho_Hant"


def test_locale_suffix_is_tolerated():
    """UI 可能傳 zh-TW / en-US 這種帶地區的代碼。"""
    w = TranslatorWorker()
    w.set_direction("en-US", "zh-TW")
    assert w._src_code == "eng_Latn"
    assert w._tgt_code == "zho_Hant"


# ============================================================
# 語言過濾（舊版寫死「沒英文就不翻」，中→英永遠不會動）
# ============================================================

def test_skips_text_already_in_target_language():
    w = TranslatorWorker()               # en → zh
    assert w._is_already_target("這是中文問題") is True
    assert w._is_already_target("Is this English?") is False


def test_reverse_direction_skips_english_input():
    w = TranslatorWorker()
    w.set_direction("zh", "en")          # zh → en
    assert w._is_already_target("Is this English?") is True
    assert w._is_already_target("這是中文問題") is False


# ============================================================
# 輸出清理（MT 模型常把同一句重譯數次）
# ============================================================

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("統計結果是否顯著？ 統計結果是否顯著？ ？ ？", "統計結果是否顯著？"),
        ("為什麼選這五個維度? ? ?", "為什麼選這五個維度?"),
        ("這是一句沒有標點的翻譯", "這是一句沒有標點的翻譯"),
        # 有句末標點就截到那裡（保留一個問號是正確的句子結尾）
        ("尾巴一堆符號 ？？？", "尾巴一堆符號 ？"),
        ("完全沒有標點的一句話", "完全沒有標點的一句話"),
        ("", ""),
    ],
)
def test_clean_output_trims_repetition(raw, expected):
    assert TranslatorWorker._clean_output(raw) == expected


def test_clean_output_keeps_short_leading_punctuation():
    """開頭就是標點時不該把整句截成一個字。"""
    out = TranslatorWorker._clean_output("？這句其實有內容。")
    assert len(out) > 2


# ============================================================
# 引擎未就緒時的行為
# ============================================================

def test_translate_without_engine_raises_clearly():
    w = TranslatorWorker()
    with pytest.raises(RuntimeError, match="尚未就緒"):
        w._translate("anything")


def test_translate_uses_target_prefix(monkeypatch):
    """驗證送進 CTranslate2 的參數：來源語言標記 + target_prefix。"""
    w = TranslatorWorker()

    class FakeResult:
        hypotheses = [["zho_Hant", "翻", "譯", "結", "果", "。"]]

    calls = {}

    class FakeTranslator:
        def translate_batch(self, batch, **kwargs):
            calls["batch"] = batch
            calls["kwargs"] = kwargs
            return [FakeResult()]

    class FakeSP:
        def encode(self, text, out_type=str):
            return list(text)

        def decode(self, tokens):
            return "".join(tokens)

    w._ready = True
    w._translator = FakeTranslator()
    w._sp_src = w._sp_tgt = FakeSP()
    w._s2tw = None

    out = w._translate("hi")

    assert calls["batch"][0][-2:] == ["</s>", "eng_Latn"], "要標記來源語言"
    assert calls["kwargs"]["target_prefix"] == [["zho_Hant"]], "要指定目標語言"
    assert out == "翻譯結果。", "目標語言標記要從輸出移除"


def test_source_gets_sentence_ending(monkeypatch):
    """辨識片段常缺句末標點，補上可避免模型重複整句。"""
    w = TranslatorWorker()

    seen = {}

    class FakeSP:
        def encode(self, text, out_type=str):
            seen["text"] = text
            return list(text)

        def decode(self, tokens):
            return "".join(tokens)

    class FakeTranslator:
        def translate_batch(self, batch, **kwargs):
            class R:
                hypotheses = [["ok"]]
            return [R()]

    w._ready = True
    w._translator = FakeTranslator()
    w._sp_src = w._sp_tgt = FakeSP()
    w._s2tw = None

    w._translate("no ending punctuation")
    assert seen["text"].endswith("."), "應補上句末標點"
