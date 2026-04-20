"""ScreenVideoEncoder 單元測試。"""

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


def test_ffmpeg_binary_findable():
    """環境應有 ffmpeg (imageio-ffmpeg 或系統裝的)。"""
    from teleprompter.core.video_encoder import get_ffmpeg_binary
    p = get_ffmpeg_binary()
    if p is None:
        pytest.skip("無 ffmpeg")
    assert Path(p).exists()


def test_capture_pixmap_widget(app):
    from PySide6.QtWidgets import QLabel
    from teleprompter.core.video_encoder import (
        CaptureSource, CaptureTarget, capture_pixmap,
    )
    lbl = QLabel("hello"); lbl.resize(100, 50); lbl.show(); app.processEvents()
    target = CaptureTarget(source=CaptureSource.WIDGET, widget=lbl)
    pix = capture_pixmap(target)
    assert pix is not None and not pix.isNull()
    assert pix.width() == 100 and pix.height() == 50
    lbl.close()


@pytest.mark.skipif(not _ffmpeg_available(), reason="無 ffmpeg 跳過")
def test_encoder_start_stop_produces_mp4(tmp_path, app):
    from PySide6.QtWidgets import QLabel
    from teleprompter.core.video_encoder import (
        CaptureSource, CaptureTarget, ScreenVideoEncoder,
    )
    lbl = QLabel("enc test"); lbl.resize(200, 150); lbl.show(); app.processEvents()
    enc = ScreenVideoEncoder()
    mux_ok: list[str] = []
    errors: list[str] = []
    enc.mux_finished.connect(lambda p: mux_ok.append(p))
    enc.error.connect(lambda m: errors.append(m))

    target = CaptureTarget(source=CaptureSource.WIDGET, widget=lbl)
    output = tmp_path / "out.mp4"
    ok = enc.start(target, tmp_dir=tmp_path, output_mp4=output, fps=10)
    assert ok, f"start 失敗：{errors}"

    # 跑 1 秒
    for _ in range(10):
        app.processEvents(); time.sleep(0.1)
    enc.stop_and_mux()

    # 等 mux 完成
    for _ in range(40):
        app.processEvents(); time.sleep(0.25)
        if mux_ok:
            break
    assert mux_ok, f"mux 沒完成；errors={errors}"
    assert output.exists() and output.stat().st_size > 500
    # 中介檔應清掉
    assert not (tmp_path / "video.h264").exists()
    lbl.close()


def test_start_fails_without_ffmpeg(monkeypatch, tmp_path, app):
    from PySide6.QtWidgets import QLabel
    from teleprompter.core import video_encoder
    monkeypatch.setattr(video_encoder, "get_ffmpeg_binary", lambda: None)
    # 需要重建 ScreenVideoEncoder 讓 __init__ 拿到 None
    enc = video_encoder.ScreenVideoEncoder()
    lbl = QLabel("x"); lbl.resize(100, 100); lbl.show(); app.processEvents()
    target = video_encoder.CaptureTarget(
        source=video_encoder.CaptureSource.WIDGET, widget=lbl,
    )
    errors: list[str] = []
    enc.error.connect(lambda m: errors.append(m))
    ok = enc.start(target, tmp_dir=tmp_path, output_mp4=tmp_path / "x.mp4")
    assert ok is False
    assert errors
    lbl.close()
