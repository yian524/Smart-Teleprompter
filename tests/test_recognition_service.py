"""辨識資源仲裁：誰在用、什麼設定、何時該重啟。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from teleprompter.core.recognition_service import (  # noqa: E402
    SOURCE_LOOPBACK,
    SOURCE_MIC,
    RecognitionNeed,
    RecognitionService,
)


class FakeRecognizer:
    """記錄 start/stop 次數與最後參數，模擬「已在跑時 start 靜默略過」。"""

    def __init__(self) -> None:
        self.running = False
        self.starts: list[dict] = []
        self.stops = 0
        self.prompts: list[str] = []

    def is_running(self) -> bool:
        return self.running

    def start(self, **kwargs) -> None:
        if self.running:
            return
        self.running = True
        self.starts.append(kwargs)

    def stop(self) -> None:
        if self.running:
            self.running = False
            self.stops += 1

    def update_prompt(self, prompt: str) -> None:
        self.prompts.append(prompt)


class FakeAudio:
    def __init__(self) -> None:
        self.running = False
        self.starts: list[dict] = []
        self.stops = 0

    def is_running(self) -> bool:
        return self.running

    def start(self, device=None, *, loopback: bool = False) -> None:
        if self.running:
            return
        self.running = True
        self.starts.append({"device": device, "loopback": loopback})

    def stop(self) -> None:
        if self.running:
            self.running = False
            self.stops += 1


@pytest.fixture
def svc():
    rec, aud = FakeRecognizer(), FakeAudio()
    return RecognitionService(rec, aud), rec, aud


FOLLOW = RecognitionNeed(language="zh", initial_prompt="講稿開頭", source=SOURCE_MIC)
QA = RecognitionNeed(language="en", initial_prompt="", source=SOURCE_LOOPBACK)


def test_acquire_starts_with_requested_config(svc):
    service, rec, aud = svc
    service.acquire("follow", FOLLOW)

    assert rec.running and aud.running
    assert rec.starts[-1]["language"] == "zh"
    assert rec.starts[-1]["initial_prompt"] == "講稿開頭"
    assert aud.starts[-1]["loopback"] is False
    assert service.active_owner == "follow"


def test_release_stops_everything(svc):
    service, rec, aud = svc
    service.acquire("follow", FOLLOW)
    service.release("follow")

    assert not rec.running and not aud.running
    assert service.active_owner == ""
    assert service.applied_need is None


def test_same_need_does_not_restart(svc):
    """設定沒變就不該重載模型——這是先前無條件 stop+start 的主要浪費。"""
    service, rec, aud = svc
    service.acquire("follow", FOLLOW)
    starts_before, stops_before = len(rec.starts), rec.stops

    service.acquire("follow", FOLLOW)   # 重複登記同樣的需求
    service.ensure()

    assert len(rec.starts) == starts_before
    assert rec.stops == stops_before
    assert aud.stops == 0


def test_language_change_restarts_recognizer(svc):
    service, rec, _ = svc
    service.acquire("follow", FOLLOW)
    service.update("follow", language="en")

    assert rec.stops == 1
    assert rec.starts[-1]["language"] == "en"


def test_prompt_only_change_is_hot_updated(svc):
    """只有講稿偏向不同 → 熱更新，不需要重載模型。"""
    service, rec, _ = svc
    service.acquire("follow", FOLLOW)
    stops_before = rec.stops

    service.update("follow", initial_prompt="換了一份講稿")

    assert rec.stops == stops_before, "只改 prompt 不該重啟辨識器"
    assert rec.prompts[-1] == "換了一份講稿"


def test_qa_wins_over_follow(svc):
    """Q&A 進行時觀眾在等回答 → 由它取得資源，跟讀轉為等待。"""
    service, rec, aud = svc
    service.acquire("follow", FOLLOW)
    service.acquire("qa", QA)

    assert service.active_owner == "qa"
    assert service.is_waiting("follow")
    assert rec.starts[-1]["language"] == "en"
    assert aud.starts[-1]["loopback"] is True


def test_follow_resumes_after_qa_releases(svc):
    """Q&A 結束 → 跟讀的設定自動回復，不必使用者手動重按。"""
    service, rec, aud = svc
    service.acquire("follow", FOLLOW)
    service.acquire("qa", QA)
    service.release("qa")

    assert service.active_owner == "follow"
    assert rec.starts[-1]["language"] == "zh"
    assert rec.starts[-1]["initial_prompt"] == "講稿開頭"
    assert aud.starts[-1]["loopback"] is False


def test_audio_source_switch_restarts_audio(svc):
    service, _, aud = svc
    service.acquire("follow", FOLLOW)
    service.update("follow", source=SOURCE_LOOPBACK)

    assert aud.stops == 1
    assert aud.starts[-1]["loopback"] is True


def test_device_string_is_converted(svc):
    """設定裡的裝置可能是索引字串，要轉成 int 給 sounddevice。"""
    service, _, aud = svc
    service.acquire("follow", RecognitionNeed(device="3"))
    assert aud.starts[-1]["device"] == 3

    service.update("follow", device="麥克風 (USB)")
    assert aud.starts[-1]["device"] == "麥克風 (USB)"


def test_loopback_ignores_device(svc):
    service, _, aud = svc
    service.acquire("qa", RecognitionNeed(source=SOURCE_LOOPBACK, device="3"))
    assert aud.starts[-1]["device"] is None


def test_release_unknown_owner_is_safe(svc):
    service, rec, _ = svc
    service.acquire("follow", FOLLOW)
    service.release("nobody")
    assert rec.running, "釋放不存在的登記者不該把別人的資源關掉"


def test_state_changed_reports_switching(svc):
    service, _, _ = svc
    seen: list[tuple[str, bool]] = []
    service.state_changed.connect(lambda owner, busy: seen.append((owner, busy)))

    service.acquire("follow", FOLLOW)

    assert ("follow", True) in seen, "切換期間要通知 UI"
    assert seen[-1] == ("follow", False), "切換完成要回報最終狀態"
