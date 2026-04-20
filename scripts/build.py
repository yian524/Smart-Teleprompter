"""PyInstaller 打包腳本：將程式打包為單一 .exe。

使用:
    python scripts/build.py

輸出:
    dist/Teleprompter.exe
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    entry = root / "src" / "teleprompter" / "main.py"
    resource_dir = root / "src" / "teleprompter" / "resources"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        "Teleprompter",
        "--paths",
        str(root / "src"),
        "--add-data",
        f"{resource_dir}{';' if sys.platform == 'win32' else ':'}teleprompter/resources",
        # faster-whisper 依賴隱式模組
        "--collect-submodules",
        "faster_whisper",
        "--collect-submodules",
        "ctranslate2",
        str(entry),
    ]
    print("執行:", " ".join(cmd))
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
