"""PyInstaller 打包腳本：將程式打包為可執行資料夾。

使用:
    python scripts/build.py

輸出:
    dist/Teleprompter/Teleprompter.exe   (含依賴的完整可執行資料夾)

說明：
- 使用 --onedir 而非 --onefile，啟動速度快 10 倍（避免每次解壓 3GB 依賴到 temp）
- 自動打包 NVIDIA cuBLAS / cuDNN DLL，無需額外安裝
- Whisper 模型在首次啟動會自動下載到 ~/.cache/huggingface，不打包到 exe
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def _find_nvidia_dll_dirs() -> list[Path]:
    """找出已安裝的 NVIDIA pip 套件 DLL 目錄，供 PyInstaller 打包。"""
    dirs: list[Path] = []
    for pkg_name in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            spec = importlib.util.find_spec(pkg_name)
        except (ImportError, ValueError):
            continue
        if spec is None or not spec.submodule_search_locations:
            continue
        for base in spec.submodule_search_locations:
            bin_dir = Path(base) / "bin"
            if bin_dir.is_dir():
                dirs.append(bin_dir)
    return dirs


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    # 用頂層 entry script（絕對 import，避免 PyInstaller 不認 relative import）
    entry = root / "scripts" / "run_teleprompter.py"
    resource_dir = root / "src" / "teleprompter" / "resources"

    sep = ";" if sys.platform == "win32" else ":"

    hooks_dir = root / "scripts" / "hooks"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",                          # 資料夾格式（啟動快）
        "--windowed",                        # 無 console 黑窗
        "--name",
        "Teleprompter",
        "--paths",
        str(root / "src"),
        "--additional-hooks-dir",
        str(hooks_dir),                      # 覆寫 buggy 的 webrtcvad hook
        "--add-data",
        f"{resource_dir}{sep}teleprompter/resources",
        # faster-whisper / ctranslate2 有隱式模組
        "--collect-submodules", "faster_whisper",
        "--collect-submodules", "ctranslate2",
        "--collect-data", "faster_whisper",
        # PySide6 Qt plugins
        "--collect-data", "PySide6",
        # webrtcvad：避開 pyinstaller_hooks_contrib 的 hook bug
        "--collect-all", "webrtcvad",
        # NVIDIA 運行時 library
        "--hidden-import", "nvidia.cublas",
        "--hidden-import", "nvidia.cudnn",
    ]

    # 把 NVIDIA DLL bin 目錄打包進去
    for dll_dir in _find_nvidia_dll_dirs():
        cmd.extend(["--add-binary", f"{dll_dir}{sep}."])

    cmd.append(str(entry))

    print("執行 PyInstaller...")
    print(" ".join(cmd))
    print()
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
