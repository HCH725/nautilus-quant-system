# Nautilus 資料服務 Runbook

## 狀態與範圍

目前服務只負責 Binance USD-M 公開歷史資料同步，尚非 production-ready，未安裝 launchd，也不包含交易、下單或真實資金連線。

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
reports    /Volumes/ExpansionDrive/nautilus-system/var/runs
logs       /Volumes/ExpansionDrive/nautilus-system/var/log
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

## Launchd 草稿前置條件

`ops/ai.nautilus.quant.data-sync.plist` 目前只是一份未安裝草稿。正式 bootstrap 前至少必須完成：

1. 全範圍歷史回填與 D-1 resume/restart 驗證。
2. 磁碟容量預算與低空間失敗行為。
3. code review blocking findings 歸零。
4. 故障恢復、reconciliation、監控與告警驗活。
5. 建立 launchd 需要的輸出目錄：

   ```bash
   mkdir -p /Volumes/ExpansionDrive/nautilus-system/var/{locks,log,runs}
   plutil -lint /Volumes/ExpansionDrive/nautilus-system/ops/ai.nautilus.quant.data-sync.plist
   ```

在上述條件完成前，不執行 `launchctl bootstrap` 或 `kickstart`。

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
  --now 2026-08-10T12:00:00Z
.venv/bin/python scripts/verify_smoke.py \
  --config config/bounded_matrix.json \
  --now 2026-08-10T12:00:00Z
```

必須同時滿足：112 bar streams、4 funding streams、12 instruments、所有 stream 完整 readback；第二次相同同步的 instrument/bar/funding writes 全為 0。Funding 沒有 kline interval，不得為湊 `4 × 7 × 5` 而重複抓七次。
