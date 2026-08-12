# Nautilus 資料服務 Runbook

## 狀態與範圍

目前服務只負責 Binance USD-M 公開歷史資料同步，已在維護者 Mac 安裝 OS-native LaunchAgent 並通過 RunAtLoad 驗活；仍不包含策略、交易、下單或真實資金連線。

核心執行不依賴 Hermes、LLM、Dashboard 或聊天平台。

## 維護者固定部署位置

以下為目前維護者在 macOS 外接碟上的固定拓樸，不是通用安裝預設；其他環境必須同步調整本文件與 `ops/ai.nautilus.quant.data-sync.plist` 內的絕對路徑，並重新完成所有驗活關卡。

```text
repo       /Volumes/ExpansionDrive/nautilus-system
runtime    /Volumes/ExpansionDrive/nautilus-system/.venv
config     /Volumes/ExpansionDrive/nautilus-system/config/market_data.json
catalog    /Volumes/ExpansionDrive/nautilus-system/data/catalog
funding    /Volumes/ExpansionDrive/nautilus-system/data/funding
lock       /Volumes/ExpansionDrive/nautilus-system/var/locks/data-sync.lock
reports         /Volumes/ExpansionDrive/nautilus-system/var/runs
local audits    /Volumes/ExpansionDrive/nautilus-system/var/log
launchd stdio   /Users/hong/Library/Logs/NautilusQuant
```

## 手動執行

```bash
cd /Volumes/ExpansionDrive/nautilus-system
.venv/bin/nautilus-data sync --config config/market_data.json
.venv/bin/nautilus-data status --config config/market_data.json
```

成功不只看 exit code：同步輸出與最新 `var/runs/sync-*.json` 必須是 `PASS`。`PARTIAL` 只表示 bounded verification 尚未走完整範圍。

## 不變量與恢復

- OS `flock` 是唯一 writer fence；看到 `BUSY` 代表另一個 writer 持鎖，不可繞過 lock。
- HTTP `418`、`429`、`5xx`、timeout 與暫時性 URL error 會依程式內 bounded retry/backoff 處理。
- Bar resume 前會從 configured start 驗證既有 Catalog 序列；每個新 chunk 寫後再 readback 該 range。
- Funding resume 前與寫後都會驗證 head、instrument identity、順序、整分鐘 interval、next-event link 與 D-1 tail。
- API gap、Catalog gap、funding gap、precision loss 或 readback mismatch 都是 correctness failure，不得手工補假資料或直接改 Parquet/JSONL。
- 失敗後先讀最新 FAIL report 與 stderr。修正根因後直接重跑；連續 prefix 會冪等 resume。
- 若 persistence 本身損壞而無法 readback，停止 writer，永久刪除受影響的本地資料集後由公開 API 重建；不可保留一份來源不明的副本混回 Catalog。

## 外接碟不可用

若 `/Volumes/ExpansionDrive` 未掛載，程式、config、catalog 與 lock 都不可用；此時不得改寫到內接碟替代路徑。確認磁碟重新掛載且可寫後，先手動跑 `status` 與 bounded smoke，再考慮重啟排程。

## Launchd 安裝、重載與驗活

固定 label 為 `ai.nautilus.quant.data-sync`。它使用 `RunAtLoad`，並依 Mac 本機時區每日 `10:15` 執行；`ThrottleInterval` 為 300 秒。

```bash
cd /Volumes/ExpansionDrive/nautilus-system
install -d -m 0755 /Users/hong/Library/Logs/NautilusQuant
if launchctl print gui/$(id -u)/ai.nautilus.quant.data-sync >/dev/null 2>&1; then
  launchctl bootout gui/$(id -u)/ai.nautilus.quant.data-sync
fi
install -m 0644 ops/ai.nautilus.quant.data-sync.plist \
  /Users/hong/Library/LaunchAgents/ai.nautilus.quant.data-sync.plist
plutil -lint /Users/hong/Library/LaunchAgents/ai.nautilus.quant.data-sync.plist
launchctl bootstrap gui/$(id -u) \
  /Users/hong/Library/LaunchAgents/ai.nautilus.quant.data-sync.plist
```

若 macOS 顯示 `python3.13` 要求存取 Removable Volumes，必須由操作者明確按「允許」；不得修改 TCC database 繞過同意。若 `launchctl` 顯示 `running`，但 application log、run report 與 writer lock 都沒有前進，應先 sample PID 並檢查 unified TCC log，不可把 PID 存在誤當成同步已執行。

每次安裝或重載後都必須讀回：

```bash
launchctl print gui/$(id -u)/ai.nautilus.quant.data-sync
.venv/bin/nautilus-data status --config config/market_data.json
```

完成條件是 `runs` 已增加、同步結束後 `state = not running`、`last exit code = 0`、最新 `var/runs/sync-*.json` 為 `PASS`、stderr 為空，且相同 D-1 再跑一次時 instrument、全部 bar stream 與 funding stream 的 writes 都為 0。短時間內 `kickstart` 可能因 300 秒 throttle 顯示 `spawn scheduled`；等待實際 run，不可藉由重裝或新增另一個 owner 繞過。首個自然 `10:15` calendar slot 也必須產生新的成功 report，registration 本身不算排程驗活。

暫停排程只需：

```bash
launchctl bootout gui/$(id -u)/ai.nautilus.quant.data-sync
```

## 驗證命令

```bash
cd /Volumes/ExpansionDrive/nautilus-system
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
/Volumes/ExpansionDrive/.nautilus-tools/uv-0.11.33/bin/uv pip check --python .venv/bin/python
/Volumes/ExpansionDrive/.nautilus-tools/uv-0.11.33/bin/uv lock --check
plutil -lint ops/ai.nautilus.quant.data-sync.plist
```

Bounded public-API smoke 使用 `.local/config/smoke-strict.json`，完成後必須再跑：

```bash
.venv/bin/python scripts/verify_smoke.py --config .local/config/smoke-strict.json
```

這個 smoke 只證明限定資料切片可下載、冪等並能 readback，不代表完整歷史已回填或服務已可實盤。

Full-scope bounded matrix 使用固定兩週窗口，週線因此至少有兩根可驗連續性：

```bash
.venv/bin/nautilus-data sync \
  --config config/bounded_matrix.json \
  --now 2026-08-11T00:00:00Z
.venv/bin/python scripts/verify_smoke.py \
  --config config/bounded_matrix.json \
  --now 2026-08-11T00:00:00Z
```

必須同時滿足：28 bar streams、4 funding streams、4 instruments、所有 stream 完整 readback，且不得出現 Mark、Index 或 Premium stream；第二次相同同步的 instrument/bar/funding writes 全為 0。Funding 沒有 kline interval，不得為湊矩陣而重複抓七次。
