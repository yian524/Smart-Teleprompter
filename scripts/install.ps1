#
# 一鍵安裝腳本：打包 .exe + 建桌面捷徑
#
# 使用方式（在 PowerShell 中執行）：
#   cd C:\path\to\Smart-Teleprompter
#   .\scripts\install.ps1
#
# 需求：
#   - 已執行 `pip install -e .[dev]` 安裝所有相依
#   - PyInstaller 已安裝（不然腳本會自動裝）
#

$ErrorActionPreference = "Stop"

# 找到專案根目錄
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host ""
Write-Host "=== 智能語音提詞機 安裝程序 ===" -ForegroundColor Cyan
Write-Host "專案位置: $ProjectRoot" -ForegroundColor Gray
Write-Host ""

# 1. 檢查 Python 可用
Write-Host "[1/4] 檢查 Python 環境..." -ForegroundColor Yellow
try {
    $pyVer = python --version 2>&1
    Write-Host "  $pyVer" -ForegroundColor Gray
} catch {
    Write-Host "錯誤: 找不到 python，請先安裝 Python 3.10-3.12" -ForegroundColor Red
    exit 1
}

# 2. 確認 PyInstaller 已安裝
Write-Host "[2/4] 確認 PyInstaller 已安裝..." -ForegroundColor Yellow
python -m pip show pyinstaller 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  安裝 PyInstaller..." -ForegroundColor Gray
    python -m pip install pyinstaller
}

# 3. 執行打包（呼叫 build.py）
Write-Host "[3/4] 執行 PyInstaller 打包（首次約需 3-5 分鐘）..." -ForegroundColor Yellow
python scripts\build.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "錯誤: 打包失敗" -ForegroundColor Red
    exit 1
}

$ExePath = Join-Path $ProjectRoot "dist\Teleprompter\Teleprompter.exe"
if (-Not (Test-Path $ExePath)) {
    Write-Host "錯誤: 找不到打包後的 exe: $ExePath" -ForegroundColor Red
    exit 1
}
Write-Host "  打包完成: $ExePath" -ForegroundColor Green

# 4. 建立桌面捷徑
Write-Host "[4/4] 建立桌面捷徑..." -ForegroundColor Yellow
$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "智能語音提詞機.lnk"

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $ExePath
$Shortcut.WorkingDirectory = Split-Path $ExePath
$Shortcut.Description = "智能語音提詞機 — Smart Voice-Aligned Teleprompter"
$Shortcut.IconLocation = "$ExePath,0"
$Shortcut.Save()

Write-Host "  桌面捷徑已建立: $ShortcutPath" -ForegroundColor Green

Write-Host ""
Write-Host "=== 安裝完成！===" -ForegroundColor Cyan
Write-Host ""
Write-Host "請在桌面雙擊「智能語音提詞機」圖示啟動" -ForegroundColor White
Write-Host ""
Write-Host "首次啟動會自動下載 Whisper 模型（約 1.5GB，存於 %USERPROFILE%\.cache\huggingface）" -ForegroundColor Gray
Write-Host "之後啟動速度很快（10-15 秒）" -ForegroundColor Gray
Write-Host ""
