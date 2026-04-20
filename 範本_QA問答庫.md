# 範例 Q&A 預備庫

此檔案示範如何準備您的 Q&A 庫，供 Q&A 模式自動匹配使用。
格式：一行 `Q: ...` 為問題，下一行 `A: ...` 為答案。
答案可多行（後續連續非 Q/A 行會延續到上一個答案）。

Q: 為什麼選 Transformer 架構而不是 RNN
A: 主要有三個原因：
一是 Transformer 可以平行運算，訓練速度快很多；
二是 self-attention 能直接捕捉長距離依賴，不受序列長度限制；
三是有更好的 scaling 特性，模型變大時品質穩定提升。

Q: 訓練資料來源與規模
A: 我們使用公開的 Common Crawl 語料，約 100GB 文字。
經過去重、語言過濾、品質分類等預處理後，保留約 60GB 高品質資料。

Q: GPU 訓練時間多久
A: 使用 8 張 A100 訓練約 7 天，total compute 約 56 A100-days。
如用 H100 可縮短至 4 天。

Q: 模型參數量
A: 我們的 base 版本是 340M 參數，large 版本是 1.3B。

Q: 在 GLUE benchmark 的成績
A: Base 版本達到 88.5 分，超越 BERT-base 的 86.2，約 +2.3 百分點。
Large 版本達到 91.7，接近 human performance。

Q: 中文任務表現
A: 我們用 CMRC 2018 與 DRCD 兩個資料集測試，F1 score 分別達到 85.2 和 89.5。

Q: 跟商用模型如 GPT-4 的差距
A: 在特定 downstream 任務上我們的 specialized 模型反而有優勢；
通用能力仍不如 GPT-4，但我們模型只有其 1/100 的計算成本。

Q: 推論延遲
A: 在單張 V100 上，batch size 1 的推論延遲約 45ms；
batch size 32 時 throughput 約 800 queries/second。

Q: 是否會開源
A: 是的，我們會在會議結束後一個月內在 GitHub 開源模型權重與訓練代碼，
採用 Apache 2.0 授權。

Q: 有沒有考慮蒸餾出更小的模型
A: 是的，這是我們下一階段的工作。
目前已經有初步的 distil 版本，參數量 60M，保留 95% 的品質。

Q: 對算力不夠的研究者怎麼建議
A: 我們會釋出三個 checkpoint 大小供選用：60M、340M、1.3B。
另外建議用 LoRA 微調，4GB 消費級 GPU 就能 fine-tune 340M 版本。

Q: 這個方法有什麼限制
A: 主要有三點：
一是對極長序列（>4K tokens）效能會下降；
二是需要大量優質預訓練資料；
三是對低資源語言支援還不夠好。

Q: 對於未來研究方向的建議
A: 我認為有三個值得深耕的方向：
efficient attention（降低記憶體複雜度）、
multi-modal integration（跨模態融合）、
以及 test-time compute scaling（推論期計算擴展）。

Q: 謝謝你的報告
A: 謝謝您的關注，如果還有其他問題歡迎會後交流，
我的 email 與論文連結都在投影片最後一頁。
