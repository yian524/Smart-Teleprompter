"""RecordingController 單元測試（MP4 輸出版本）。

若環境無 ffmpeg (imageio-ffmpeg 未裝) → 跳過需要 mux 的測試。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    import sys
    return QApplication.instance() or QApplication(sys.argv)


def _ffmpeg_available() -> bool:
    from teleprompter.core.video_encoder import get_ffmpeg_binary
    return get_ffmpeg_binary() is not None


def test_recorder_reports_availability(app):
    from teleprompter.core.recorder import RecordingController
    rec = RecordingController()
    assert rec.is_available() == _ffmpeg_available()


def test_start_without_ffmpeg_emits_error(tmp_path, app, monkeypatch):
    """若無 ffmpeg，start() 應回 False 並 emit error。"""
    from teleprompter.core.recorder import RecordingController
    from teleprompter.core.video_encoder import CaptureSource, CaptureTarget
    monkeypatch.setattr(
        "teleprompter.core.recorder.get_ffmpeg_binary", lambda: None
    )
    # 也要讓 encoder 內部視為無
    monkeypatch.setattr(
        "teleprompter.core.video_encoder.get_ffmpeg_binary", lambda: None
    )
    # 需要重建 recorder 讓內部的 encoder 重新取 binary
    rec = RecordingController()
    errors: list[str] = []
    rec.error.connect(lambda m: errors.append(m))
    from PySide6.QtWidgets import QLabel
    lbl = QLabel("x"); lbl.resize(100, 50); lbl.show(); app.processEvents()
    target = CaptureTarget(source=CaptureSource.WIDGET, widget=lbl)
    ok = rec.start(tmp_path, target=target)
    assert ok is False
    assert errors  # error was emitted
    lbl.close()


def test_cannot_start_twice(tmp_path, app):
    if not _ffmpeg_available():
        pytest.skip("無 ffmpeg")
    from teleprompter.core.recorder import RecordingController
    from teleprompter.core.video_encoder import CaptureSource, CaptureTarget
    from PySide6.QtWidgets import QLabel
    lbl = QLabel("test"); lbl.resize(320, 240); lbl.show(); app.processEvents()
    rec = RecordingController()
    target = CaptureTarget(source=CaptureSource.WIDGET, widget=lbl)
    assert rec.start(tmp_path, target=target) is True
    assert rec.start(tmp_path, target=target) is False
    rec.stop()
    # 等 mux 完成避免殘留
    time.sleep(2); app.processEvents()
    lbl.close()


def test_stop_without_start_no_crash(tmp_path, app):
    from teleprompter.core.recorder import RecordingController
    rec = RecordingController()
    rec.stop()  # 不該 raise


def test_audio_frames_dropped_when_not_running(tmp_path, app):
    from teleprompter.core.recorder import RecordingController
    rec = RecordingController()
    rec.on_audio_frame(b"\x00" * 960)
    assert not rec.is_running()


def test_default_recording_root_is_documents(app):
    from teleprompter.core.recorder import default_recording_root
    p = default_recording_root()
    assert "SmartTeleprompter" in str(p)
    assert "recordings" in str(p)


@pytest.mark.skipif(not _ffmpeg_available(), reason="無 ffmpeg 跳過實錄測試")
def test_full_recording_produces_mp4(tmp_path, app):
    """完整 start → wait → stop → 等 mux → 驗 MP4 存在。"""
    from PySide6.QtWidgets import QLabel
    from teleprompter.core.recorder import RecordingController
    from teleprompter.core.video_encoder import CaptureSource, CaptureTarget

    lbl = QLabel("test recording content"); lbl.resize(320, 240); lbl.show()
    app.processEvents()

    rec = RecordingController()
    stopped_paths: list[str] = []
    errors: list[str] = []
    rec.stopped.connect(lambda p: stopped_paths.append(p))
    rec.error.connect(lambda m: errors.append(m))

    target = CaptureTarget(source=CaptureSource.WIDGET, widget=lbl)
    ok = rec.start(tmp_path, target=target, fps=10)
    assert ok, f"start 失敗：{errors}"
    # 錄 1 秒
    for _ in range(20):
        rec.on_audio_frame(b"\x00" * 160)  # 10ms audio
        time.sleep(0.05); app.processEvents()
    rec.stop()
    # 等待 mux 完成（含 ffmpeg 兩階段）
    for _ in range(40):
        app.processEvents()
        if stopped_paths:
            break
        time.sleep(0.25)
    assert stopped_paths, f"mux 沒完成；errors={errors}"
    mp4 = Path(stopped_paths[0])
    assert mp4.exists() and mp4.stat().st_size > 1000, f"MP4 檔案異常：{mp4}"
    # 中介檔應該已清除
    assert not (mp4.parent / "video.h264").exists()
    assert not (mp4.parent / "_tmp_audio.wav").exists()
    lbl.close()


@pytest.mark.skipif(not _ffmpeg_available(), reason="無 ffmpeg")
def test_recording_does_not_disturb_raw_frame_pipeline(tmp_path, app):
    """訂閱 raw_frame 走錄音，不影響其他訂閱者。"""
    from PySide6.QtWidgets import QLabel
    from teleprompter.core.audio_capture import AudioCaptureController
    from teleprompter.core.recorder import RecordingController
    from teleprompter.core.video_encoder import CaptureSource, CaptureTarget

    audio = AudioCaptureController()
    rec = RecordingController()
    other_received: list[bytes] = []
    audio.raw_frame.connect(rec.on_audio_frame)
    audio.raw_frame.connect(lambda b: other_received.append(b))

    lbl = QLabel("x"); lbl.resize(100, 100); lbl.show(); app.processEvents()
    target = CaptureTarget(source=CaptureSource.WIDGET, widget=lbl)
    rec.start(tmp_path, target=target, fps=10)
    audio.raw_frame.emit(b"\x00" * 480)
    app.processEvents()
    assert len(other_received) == 1  # 另一訂閱者照常收到
    rec.stop()
    # 等 mux 完
    for _ in range(20):
        app.processEvents(); time.sleep(0.2)
    lbl.close()
