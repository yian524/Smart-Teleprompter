"""即時螢幕錄影編碼器。

流程：
1. QTimer 每 1/fps 秒抓一張 QPixmap（from widget 或 QScreen）
2. 轉 BGRA/RGB24 bytes → 寫到 ffmpeg subprocess stdin
3. ffmpeg 即時 H.264 編碼 → 輸出 video.h264（raw elementary stream，無容器）
4. stop_encoding() 時關 stdin → 等 ffmpeg 完成
5. mux_to_mp4() 把 video.h264 + audio.wav → recording.mp4（第二個 ffmpeg 快速 mux）
6. mux 完成自動刪中介檔

支援 **來源**：
- QWidget（錄某個 widget / 視窗內容）
- QScreen（整個螢幕或指定螢幕）

所有 ffmpeg 呼叫經過 QThread / subprocess，不阻塞 UI。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage, QPixmap, QScreen
from PySide6.QtWidgets import QWidget

logger = logging.getLogger(__name__)


class CaptureSource(Enum):
    WIDGET = "widget"      # 錄指定 widget（通常是 MainWindow）
    SCREEN = "screen"      # 錄單一螢幕
    ALL_SCREENS = "all"    # 錄所有螢幕組成的虛擬桌面（跨螢幕拼接）


@dataclass
class CaptureTarget:
    source: CaptureSource
    widget: Optional[QWidget] = None
    screen: Optional[QScreen] = None
    # 子區域裁切（viewport 座標；None = 整個 source）
    rect: Optional[tuple[int, int, int, int]] = None


def get_ffmpeg_binary() -> Optional[str]:
    """優先用系統 PATH 的 ffmpeg（通常較新），否則回 imageio-ffmpeg 自帶。"""
    sys_path = shutil.which("ffmpeg")
    if sys_path:
        return sys_path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def capture_pixmap(target: CaptureTarget) -> Optional[QPixmap]:
    """抓一張目前 target 的 QPixmap。失敗回 None。"""
    try:
        if target.source == CaptureSource.WIDGET and target.widget is not None:
            return target.widget.grab()
        if target.source == CaptureSource.SCREEN and target.screen is not None:
            geom = target.screen.geometry()
            return target.screen.grabWindow(
                0, geom.x(), geom.y(), geom.width(), geom.height()
            )
        if target.source == CaptureSource.ALL_SCREENS:
            # 跨所有螢幕的虛擬桌面：用 primaryScreen 抓整個 virtualGeometry
            primary = QGuiApplication.primaryScreen()
            if primary is None:
                return None
            vgeo = primary.virtualGeometry()
            return primary.grabWindow(
                0, vgeo.x(), vgeo.y(), vgeo.width(), vgeo.height()
            )
    except Exception as e:
        logger.warning("capture failed: %s", e)
    return None


class ScreenVideoEncoder(QObject):
    """即時視訊編碼器。

    - start()：開 ffmpeg subprocess + QTimer 開始抓 frame pipe 進去
    - add_audio_wav_path()：告訴編碼器最終要 mux 的 wav 檔
    - stop_and_mux()：關 stdin → 等 ffmpeg → mux → 清中介 → emit finished
    """

    mux_started = Signal()
    mux_finished = Signal(str)   # mp4 path
    error = Signal(str)
    frame_dropped = Signal()     # UI 可選擇顯示

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._ffmpeg_bin: Optional[str] = get_ffmpeg_binary()
        self._proc: Optional[subprocess.Popen] = None
        self._timer: Optional[QTimer] = None
        self._target: Optional[CaptureTarget] = None
        self._size: tuple[int, int] = (0, 0)   # (w, h)
        self._fps: int = 30
        self._tmp_dir: Optional[Path] = None
        self._video_path: Optional[Path] = None
        self._audio_wav_path: Optional[Path] = None
        self._output_mp4: Optional[Path] = None
        self._last_valid_frame: Optional[bytes] = None
        self._frame_count: int = 0
        self._running: bool = False
        self._mux_thread: Optional[threading.Thread] = None

    # ---------- 狀態 ----------

    def is_available(self) -> bool:
        return self._ffmpeg_bin is not None

    def is_running(self) -> bool:
        return self._running

    def frame_count(self) -> int:
        return self._frame_count

    # ---------- 控制 ----------

    def start(
        self,
        target: CaptureTarget,
        *,
        tmp_dir: Path,
        output_mp4: Path,
        fps: int = 30,
    ) -> bool:
        if self._running:
            return False
        if self._ffmpeg_bin is None:
            self.error.emit("找不到 ffmpeg（請執行 `pip install imageio-ffmpeg`）")
            return False

        # 第一張 pixmap 決定尺寸（固定尺寸後不再變）
        pix = capture_pixmap(target)
        if pix is None or pix.isNull():
            self.error.emit("無法抓取初始畫面（視窗可能被最小化）")
            return False
        w, h = pix.width(), pix.height()
        # 確保偶數（libx264 要求）
        w = w - (w % 2)
        h = h - (h % 2)
        if w <= 0 or h <= 0:
            self.error.emit(f"視窗尺寸異常：{pix.width()}x{pix.height()}")
            return False

        tmp_dir = Path(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        video_path = tmp_dir / "video.h264"

        # 啟 ffmpeg：吃 stdin rawvideo RGB24 → 輸出 H.264 raw bitstream
        cmd = [
            self._ffmpeg_bin, "-y",
            "-f", "rawvideo",
            "-pixel_format", "rgb24",
            "-video_size", f"{w}x{h}",
            "-framerate", str(fps),
            "-i", "-",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-f", "h264",
            str(video_path),
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_creation_flags(),
            )
        except Exception as e:
            self.error.emit(f"啟動 ffmpeg 失敗：{e}")
            return False

        self._target = target
        self._size = (w, h)
        self._fps = fps
        self._tmp_dir = tmp_dir
        self._video_path = video_path
        self._output_mp4 = output_mp4
        self._frame_count = 0
        self._last_valid_frame = None
        self._running = True

        # 即刻 pipe 第一張
        self._pipe_pixmap(pix)

        self._timer = QTimer(self)
        self._timer.setInterval(max(1, int(1000 / fps)))
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()
        return True

    def set_audio_wav_path(self, path: Path) -> None:
        self._audio_wav_path = Path(path)

    def stop_and_mux(self) -> None:
        """停止錄影 → 等 ffmpeg → mux → emit mux_finished。非阻塞。"""
        if not self._running:
            return
        self._running = False
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

        # 關 ffmpeg stdin → wait（在執行緒內）
        self._mux_thread = threading.Thread(
            target=self._finalize_video_then_mux, daemon=True
        )
        self._mux_thread.start()

    # ---------- 內部：frame pipeline ----------

    def _on_tick(self) -> None:
        if not self._running or self._target is None:
            return
        pix = capture_pixmap(self._target)
        if pix is None or pix.isNull():
            # 補上一張或 black frame
            if self._last_valid_frame is not None:
                self._write_bytes(self._last_valid_frame)
                self.frame_dropped.emit()
            return
        self._pipe_pixmap(pix)

    def _pipe_pixmap(self, pix: QPixmap) -> None:
        # scale 到固定尺寸（視窗 resize 時）
        w, h = self._size
        if pix.width() != w or pix.height() != h:
            from PySide6.QtCore import Qt
            pix = pix.scaled(
                w, h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        img = pix.toImage().convertToFormat(QImage.Format.Format_RGB888)
        # QImage 記憶體每 scanline 可能有 padding，需取 bits()
        bits = img.constBits()
        if bits is None:
            return
        # bytesPerLine 可能 != width*3；逐 scanline 複製
        bpl = img.bytesPerLine()
        expected = w * 3
        if bpl == expected:
            raw = bytes(bits)[: h * expected]
        else:
            raw_lines = []
            buf = bytes(bits)
            for y in range(h):
                off = y * bpl
                raw_lines.append(buf[off: off + expected])
            raw = b"".join(raw_lines)
        self._last_valid_frame = raw
        self._write_bytes(raw)

    def _write_bytes(self, raw: bytes) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(raw)
            self._frame_count += 1
        except BrokenPipeError:
            logger.warning("ffmpeg stdin 已關閉")
        except Exception as e:
            logger.warning("pipe frame failed: %s", e)

    # ---------- 內部：收尾 + mux ----------

    def _finalize_video_then_mux(self) -> None:
        # 關 stdin
        try:
            if self._proc is not None and self._proc.stdin is not None:
                self._proc.stdin.close()
        except Exception:
            pass
        # 等 ffmpeg 完成
        try:
            if self._proc is not None:
                self._proc.wait(timeout=30)
        except Exception as e:
            logger.warning("ffmpeg wait timeout: %s", e)
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

        # mux video + audio → mp4
        self.mux_started.emit()
        try:
            self._run_mux()
        except Exception as e:
            self.error.emit(f"合成 MP4 失敗：{e}")
            return

        # 清中介檔
        try:
            if self._video_path is not None and self._video_path.exists():
                self._video_path.unlink()
        except Exception:
            pass
        try:
            if self._audio_wav_path is not None and self._audio_wav_path.exists():
                self._audio_wav_path.unlink()
        except Exception:
            pass

        self.mux_finished.emit(str(self._output_mp4))

    def _run_mux(self) -> None:
        assert self._ffmpeg_bin is not None
        assert self._video_path is not None
        assert self._output_mp4 is not None

        audio_ok = (
            self._audio_wav_path is not None
            and Path(self._audio_wav_path).exists()
            and Path(self._audio_wav_path).stat().st_size > 44  # 大於 WAV header
        )

        cmd = [
            self._ffmpeg_bin, "-y",
            "-r", str(self._fps),
            "-i", str(self._video_path),
        ]
        if audio_ok:
            cmd += ["-i", str(self._audio_wav_path)]
            # 明確 mapping：0 號輸入取視訊、1 號輸入取音訊
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
            # 視訊直接複製；音訊升頻 48kHz 立體聲 AAC（相容性最好）
            cmd += [
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "48000",
                "-ac", "2",
            ]
        else:
            cmd += ["-c:v", "copy"]
            logger.warning(
                "mux：找不到或 WAV 過小，MP4 將無音軌 (%s)",
                self._audio_wav_path,
            )
        cmd += [
            "-movflags", "+faststart",
            str(self._output_mp4),
        ]
        logger.info("ffmpeg mux cmd: %s", " ".join(str(x) for x in cmd))
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=_creation_flags(),
        )
        if result.returncode != 0:
            msg = result.stderr.decode(errors="ignore") if result.stderr else ""
            raise RuntimeError(
                f"ffmpeg mux failed (exit {result.returncode}):\n{msg[-500:]}"
            )
        # 驗證 MP4 有音軌（若原本應該有）
        if audio_ok:
            self._verify_mp4_has_audio()

    def _verify_mp4_has_audio(self) -> None:
        """用 ffprobe 式的 ffmpeg 查詢確認 MP4 有音軌。"""
        if self._output_mp4 is None or self._ffmpeg_bin is None:
            return
        try:
            r = subprocess.run(
                [self._ffmpeg_bin, "-i", str(self._output_mp4), "-hide_banner"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=_creation_flags(),
                timeout=5,
            )
            info = r.stderr.decode(errors="ignore")
            if "Audio:" not in info:
                logger.warning("產出的 MP4 無 Audio stream：\n%s", info[-500:])
            else:
                logger.info("MP4 audio stream OK")
        except Exception as e:
            logger.warning("verify mp4 failed: %s", e)


def _creation_flags() -> int:
    """Windows 上隱藏 subprocess 黑色 console 視窗。"""
    import sys
    if sys.platform == "win32":
        return 0x08000000  # CREATE_NO_WINDOW
    return 0


def list_available_screens() -> list[QScreen]:
    return list(QGuiApplication.screens())
