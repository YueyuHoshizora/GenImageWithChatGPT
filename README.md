# GenImage

純文字輸入的繪圖 CLI：OpenAI `gpt-image-2.5-flare`（官方 ID；第三方平台有時稱 "flash"），以 Sign in with ChatGPT（OAuth 2.0 + PKCE）登入。

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python genimage.py                 # 互動模式：輸入描述 → 出圖
.venv/bin/python genimage.py "灰色貓娘，水彩風"  # 單次出圖
.venv/bin/python genimage.py --login         # 強制重新登入
.venv/bin/python genimage.py --logout        # 撤銷並清除 token
```

- 第一次執行會開瀏覽器登入並同意「使用 ChatGPT 方案用量」；之後自動 refresh。
- 圖片存到 `./output/<時間>.png`，`-o` 可改資料夾，`-m` 可改模型 ID。
- 憑證存在 `~/.config/genimage/credentials.json`（0600，可用 `GENIMAGE_HOME` 改位置），不在專案內。

## 已知限制

OpenAI 官方文件目前只開放 ChatGPT 方案 OAuth token 呼叫 `POST /v1/responses`，且不支援 image generation 工具；`gpt-image-2.5-flare` 只能走 `/v1/images/generations`。因此 Images API 可能拒絕此 token（`subscription_sharing_route_not_supported`、403）。遇到時工具會印出完整錯誤與 request id。
