# GenImage

純文字輸入的繪圖 CLI。包裝官方 [Codex CLI](https://learn.chatgpt.com/docs/codex/cli) 的內建 image generation，以 ChatGPT 訂閱（Sign in with ChatGPT / OAuth）出圖，不需要 API Key。

## 為什麼是 Codex

OpenAI 目前不允許第三方 app 以 Sign in with ChatGPT 的 OAuth token 呼叫圖像 API：

- `POST /v1/images/generations` → 401 `hardened_oauth_rule_missing`
- `POST /v1/responses` 的 `image_generation` 工具 → 400 `subscription_sharing_unsupported_capability`

訂閱出圖只有官方 Codex 的內建功能可用，所以本工具直接呼叫 `codex exec`，登入與 token 完全由 Codex 管理，本工具不碰任何憑證。

> **模型**：Codex 內建出圖使用的是 `gpt-image-2`（見[官方文件](https://learn.chatgpt.com/docs/image-generation)），無法指定 `gpt-image-2.5-flare`。

## 需求

- Python 3.9+（只用標準函式庫，免安裝依賴）
- 已安裝 [Codex CLI](https://learn.chatgpt.com/docs/codex/cli)（`brew install --cask codex` 或 `npm install -g @openai/codex`）
- 付費的 ChatGPT 方案（Free 方案不支援內建出圖）

## 使用

```bash
python3 genimage.py                        # 互動模式：輸入描述後按 Enter 出圖
python3 genimage.py "灰色貓娘，水彩風"        # 單次出圖
python3 genimage.py -o ~/Pictures "夕陽海邊"  # 指定輸出資料夾
python3 genimage.py --login                # 執行 codex login
python3 genimage.py --logout               # 執行 codex logout
```

- 互動模式中，空行、`exit`、`quit` 或 Ctrl-D 離開。
- 尚未登入時，第一次出圖會自動執行 `codex login`（開瀏覽器登入 ChatGPT）。
- 圖片存成 `output/<YYYYMMDD-HHMMSS>.png`（`-o` 可改），同一秒重複出圖會加 `-1`、`-2` 後綴，不會覆蓋。
- 每張圖約需數十秒。

## 運作方式

執行 `codex exec --json --enable image_generation --sandbox read-only "$imagegen <提示詞>"`（在暫存資料夾、不寫入 session 檔），從 `thread.started` 事件取得 thread id，再把 `$CODEX_HOME/generated_images/<thread-id>/` 底下的圖片複製到輸出資料夾。`CODEX_HOME` 預設為 `~/.codex`。Codex 自己存的那份不會被刪除。

提示詞會附上「照原文使用、不要改寫」的指示，否則 Codex 會自行潤飾提示詞。

## 用量

出圖計入你的 Codex 用量額度，官方說明平均比同類的純文字回合快 3–5 倍消耗額度。大量生成建議改用 API Key 走 API 計費。見 [Codex pricing](https://learn.chatgpt.com/docs/pricing#image-generation-usage-limits)。

## 疑難排解

| 現象 | 原因／處理 |
| --- | --- |
| `找不到 codex 指令` | 尚未安裝 Codex CLI，或不在 PATH |
| `Codex 沒有產生圖片` | Free 方案不支援、提示詞被安全機制拒絕，或額度用盡；訊息中會附上 Codex 的回覆 |
| `Codex 回報失敗` | 顯示 Codex 的錯誤訊息；多半是登入失效，執行 `--login` |
| `codex 超過 600 秒沒有完成` | 網路或服務異常，稍後重試 |
