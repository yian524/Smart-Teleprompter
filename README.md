# 智能語音提詞機 (Smart Teleprompter)

**會議報告用的桌面提詞工具** — 即時語音辨識自動對齊講稿位置，卡拉 OK 式逐字高亮，支援中英混合、多分頁 Session、PDF/PPTX 投影片同步、Q&A 模式、即時翻譯、Word 式文字格式化。

## ✨ 功能特色

### 提詞核心
- **本地離線 Whisper large-v3-turbo**（中英混合辨識、GPU 加速）
- **卡拉 OK 式逐字高亮**：句子定位 + 字元級對齊，目前念的字即時亮起
- **漏講偵測**：跳過的段落會用紅色刪除線標出
- **抗漂移演算法**：拼音比對、距離懲罰、歧義保護、卡住自救
- **穩定性三檔**：Conservative（會議）/ Balanced（預設）/ Aggressive（練習）

### 講稿支援
- **多格式載入**：.txt / .md / .docx
- **Markdown 註解**：`<!-- ... -->` 不會被辨識對齊，但會保留並以灰斜體顯示當備忘
- **分頁標示**：`---` 分隔投影片，自動顯示 📄 Slide X/Y
- **頁面標題**：Markdown 標題 `# xxx` 成為該頁標題

### 多分頁 Session（`Ctrl+T` 新分頁 / `Ctrl+W` 關閉）
- **同時準備多份會議**：一 Tab = 一份（講稿 + 可選投影片）組合
- **狀態獨立**：每 Tab 的位置、漏講標記、文字格式都各自保存
- **自動還原**：關閉 app 後再打開，所有 Tab（含目前位置）自動回到上次
- **雙擊分頁改名**、拖曳可重排
- **辨識只跟作用中 Tab**：切走時那支繼續跑，其他 Tab 純瀏覽

### 投影片視覺（右側面板，載入 PDF / PPTX）
- **同步翻頁**：講稿念到第 N 段（`---` 切的） → 右側自動翻到第 N 頁
- **點縮圖跳轉**：點右側縮圖 → 講稿跳到對應段落開頭
- **PPTX 自動轉 PDF**：有 PowerPoint 走 COM；無則找 LibreOffice fallback
- **低解析度縮圖 + 大圖 LRU 快取**，切頁流暢

### Word 式編輯（`Ctrl+E` 進入編輯模式）
- **剪下/複製/貼上/Undo/Redo**：QTextEdit 原生全支援
- **格式化**：粗體 `Ctrl+B`、斜體 `Ctrl+I`、底線 `Ctrl+U`、螢光筆 `Ctrl+H`、清除 `Ctrl+\`
- **格式自動保存**到 session；重開還原
- **格式不影響辨識**：對齊看的是純文字，格式只是視覺標註

### 時間管理
- 正向計時 + 倒數計時 + 語速健康度燈號（綠/黃/紅）
- 里程碑提醒（剩 5 分、剩 1 分）
- 講稿進度 vs 時間進度對比

### Q&A 模式（`Ctrl+Q`）
- **預備 QA 庫**：先準備好常見問答，系統即時匹配
- **模糊比對 + 拼音匹配**：提問與預備不完全相同也能找到答案
- **Top-3 候選**：信心不足時列出多個可能答案
- **辨識語言切換**：中/英/自動，適合國際會議

### 即時翻譯（Q&A 面板內）
- **Argos Translate**（本地離線、穩定、免網路）
- **OpenCC s2tw**：自動轉繁體中文
- **Google Translate fallback**：Argos 不可用時自動切換

## 🖥 系統需求

- Windows 10/11（macOS/Linux 可用但未完整測試）
- Python 3.10–3.13
- NVIDIA GPU 6GB+ VRAM（建議；CPU 模式亦可運作但較慢）
- 麥克風

## 📦 安裝

```powershell
# 1. clone 專案
git clone https://github.com/yian524/Smart-Teleprompter.git
cd Smart-Teleprompter

# 2. 建立虛擬環境並安裝
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"

# 3. 首次啟動會下載 Whisper 模型 (~1.5GB) 與 Argos 翻譯模型 (~100MB，僅在啟用翻譯時)
```

## 🚀 啟動

```powershell
python -m teleprompter.main
```

或打包成 .exe + 桌面捷徑：

```powershell
.\scripts\install.ps1
```

## 📝 講稿格式範例

### 最簡單（純文字）
```
大家好，我是今天的報告人。
今天分享 Transformer 架構在 NLP 的應用。
```

### 進階（含註解與分頁）
```markdown
<!-- 記得講到 PyTorch 時強調我們用的是 2.4 版 -->

# Slide 1 · 開場
大家好，我是今天的報告人。

---

# Slide 2 · 背景
<!-- 這張投影片要多停留 10 秒 -->
首先介紹研究背景...
在 2017 年之前，NLP 任務主要依賴 RNN。

---

# Slide 3 · 方法
我們使用 PyTorch 訓練 Transformer...
```

- `<!-- 註解 -->`：完全被忽略，不會被辨識也不會被顯示
- `---`：分頁符號，每頁獨立顯示 Slide X/Y
- `# 標題`：成為該頁標題，顯示在時間 bar 右側

詳見 `範本_中英混合.txt`。

## 🎤 Q&A 庫格式範例

```markdown
Q: 為什麼選 Transformer 而不是 RNN
A: 主要有三個原因：平行運算、長距離依賴、scaling 特性。

Q: 訓練資料多大
A: 約 100GB Common Crawl 語料。
經過去重、品質過濾後保留 60GB 高品質資料。
```

- 一個 `Q:` 接一個 `A:` 為一對
- 答案可多行（連續的非 Q/A 行會延續）
- 也支援 JSON 格式：`[{"q": "...", "a": "..."}]`

詳見 `範本_QA問答庫.md`。

## ⌨️ 快捷鍵

| 鍵 | 功能 |
|---|---|
| `Space` | 開始 / 暫停辨識 |
| `↑` / `↓` | 手動跳上/下一句 |
| `Ctrl + +` / `-` | 字體放大/縮小 |
| `Ctrl + 滾輪` | 字體縮放 |
| `Ctrl + K` | 手動標記漏講（從上次到現在） |
| `Ctrl + Shift + K` | 清除漏講標記 |
| `Ctrl + Q` | 切換 Q&A 模式 |
| `Ctrl + O` | 開啟講稿檔案 |
| `Ctrl + T` | 新分頁 |
| `Ctrl + W` | 關閉目前分頁 |
| `Ctrl + Shift + T` | 設定目標時長 |
| `Ctrl + E` | 進入/離開編輯模式 |
| `Ctrl + B` / `I` / `U` | 粗體 / 斜體 / 底線（編輯模式） |
| `Ctrl + H` | 螢光筆（編輯模式） |
| `Ctrl + \` | 清除選取格式（編輯模式） |
| `F11` | 全螢幕 |
| `T` | 顯示/隱藏時間面板 |
| `R` | 重置計時器 |

## ⚙️ 建議設定（按場景）

| 場景 | 穩定性模式 | 最大跳段 | 備註 |
|---|---|---|---|
| 🏛 大型正式會議 | Conservative | 3-5 句 | 最穩定，不會誤跳 |
| 📊 一般報告 | Balanced（預設） | 10 句 | 速度穩定平衡 |
| 🎓 練習排演 | Aggressive | 不限 | 最快反應，容錯低 |

## 🧪 測試

```powershell
pytest
```

目前 263/263 測試全綠。

## 📄 授權

MIT License
