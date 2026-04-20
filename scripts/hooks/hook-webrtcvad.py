# Override PyInstaller 的 buggy hook-webrtcvad (來自 pyinstaller_hooks_contrib)
# 原 hook 在 webrtcvad-wheels 上會失敗，這裡用空 hook 讓 PyInstaller 預設行為處理
from PyInstaller.utils.hooks import collect_dynamic_libs

binaries = collect_dynamic_libs("webrtcvad")
