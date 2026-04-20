# 智能語音提詞機 (Smart Teleprompter)

遠距會議報告用的桌面提詞工具：透過即時語音辨識，自動對齊您正在念的講稿位置，提供卡拉 OK 式逐字高亮效果。

## 功能特色

- **本地離線語音辨識**（faster-whisper, large-v3 模型，原生支援中英混合）
- **卡拉 OK 式逐字高亮**：句子定位 + 字元級對齊，目前正在念的字即時亮起
- **多格式講稿載入**：純文字 (.txt) / Markdown (.md) / Word (.docx)
- **時間管理**：正向計時 + 倒數計時 + 語速健康度燈號
- **靈活字體調整**：Ctrl + 滾輪 / 快捷鍵即時調整 (16-96pt)
- **雙螢幕優化**：自動偵測並顯示在副螢幕
- **抗干擾對齊演算法**：容忍口誤、停頓、跳句

## 系統需求

- Windows 10/11
- Python 3.10–3.12
- NVIDIA GPU 6GB+ VRAM（建議；CPU 模式亦可運作）
- 麥克風

## 安裝

```bash
# 使用 uv（推薦）
uv venv
uv pip install -e ".[dev]"

# 或使用 pip
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

GPU 使用者額外安裝 CUDA 版 PyTorch（faster-whisper 自動偵測）：
```bash
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

## 啟動

```bash
teleprompter
# 或
python -m teleprompter.main
```

## 快捷鍵

| 鍵 | 功能 |
|---|---|
| `Space` | 開始 / 暫停辨識 |
| `↑` / `↓` | 手動跳上/下一句 |
| `Ctrl` + `+` / `-` | 字體放大/縮小 |
| `Ctrl` + 滾輪 | 字體縮放 |
| `F11` | 全螢幕 |
| `T` | 顯示/隱藏時間面板 |
| `R` | 重置計時器 |
| `Ctrl + T` | 設定目標時長 |
| `Ctrl + O` | 開啟講稿檔案 |

## 測試

```bash
pytest
```

## 打包

```bash
python scripts/build.py
```
