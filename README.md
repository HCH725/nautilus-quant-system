# Nautilus Quant System

> [!IMPORTANT]
> 這是建立於官方 [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) runtime 之上的獨立專案，並非 NautilusTrader 官方 fork，也不隸屬於、未受贊助或背書於 Nautech Systems。完整出處與第三方授權見 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

獨立、deterministic 的 Binance USD-M Futures 資料核心，使用 NautilusTrader `2.0.0rc2`。

## 範圍

- Symbols：`BTCUSDT`、`ETHUSDT`、`BNBUSDT`、`SOLUSDT`
- 歷史起點：`2022-08-01T00:00:00Z`
- Intervals：`5m`、`15m`、`30m`、`1h`、`4h`、`1d`、`1w`
- Data：trade、mark、index klines 與 funding rate；不包含 premium index klines
- 邊界：只同步前一個完整 UTC 日；週線只到最後一個完整 Monday boundary
- 不包含 open interest

## 出處與授權

- 本 repository 的原創程式碼採 [MIT License](LICENSE)。
- [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) 由 [Nautech Systems](https://www.nautechsystems.io/) 維護，並以 `LGPL-3.0-or-later` 授權。
- NautilusTrader 只作為獨立、未修改的官方 PyPI runtime dependency，由 `uv.lock` 固定版本、來源與雜湊；本 repository 不包含或重新散布其原始碼與 binary wheel。
- 市場資料來自 Binance USD-M Futures 公開 HTTP API；repository 不包含下載後的市場資料、API key 或交易憑證。

授權邊界、官方連結與非隸屬聲明詳見 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

## 資料模型

| 來源 | Nautilus 表示 |
|---|---|
| trade klines | perpetual instrument 的 `Bar/LAST` |
| mark klines | perpetual instrument 的 `Bar/MARK` |
| index klines | 原生 `IndexInstrument` 的 `Bar/LAST` |
| funding | 原生 `FundingRateUpdate` JSONL event store |

`ParquetDataCatalog` 保存 instruments 與 bars。`2.0.0rc2` 尚無公開 funding Parquet writer，因此 funding 暫以 `FundingRateUpdate.to_json()` 原子檔保存；程式中的 `ponytail:` 註解標出未來可直接換回 Catalog 的位置。

## 安裝與測試

```bash
uv sync --dev
.venv/bin/python -m unittest discover -s tests -v
```

以上命令應從 clone 後的 repository root 執行。`ops/RUNBOOK.md` 與 `ops/ai.nautilus.quant.data-sync.plist` 記錄目前維護者在 macOS 外接碟上的固定部署拓樸，不是通用安裝路徑；其他環境必須先調整絕對路徑，且不得直接載入尚未驗活的 launchd 草稿。

## 同步與狀態

```bash
.venv/bin/nautilus-data sync --config config/market_data.json
.venv/bin/nautilus-data status --config config/market_data.json
```

同步具備：

- 公開 REST pagination 與 `418/429/5xx` bounded retry
- resume 前逐 `BarType` 驗證 configured start 到目前 tail 的完整序列
- UTC D-1／完整週線邊界
- duplicate、gap、tail completeness 與 precision fail-closed
- Binance 高週期內部缺口只在完整、連續的官方 `1m` REST 資料可用時按 OHLC(V) 聚合；每條成功 stream 回報 `reconstructed` 數量與 `reconstructed_open_ms`，後續失敗的 run report 仍保留已 readback 的 `reconstructed_chunks`
- `1m` 本身不完整、缺口位於 head/tail，或單一 chunk 需重建超過一天時仍 fail-closed，不填假資料
- Catalog 寫後 readback
- cross-process `flock` single writer
- 每次完成或失敗的 JSON run evidence（`var/runs/`）

## Bounded 全矩陣驗收

`config/bounded_matrix.json` 固定驗證 `2026-07-27 → 2026-08-10 UTC`，資料只寫入 ignored `.local/`：

```bash
.venv/bin/nautilus-data sync \
  --config config/bounded_matrix.json \
  --now 2026-08-11T00:00:00Z
.venv/bin/python scripts/verify_smoke.py \
  --config config/bounded_matrix.json \
  --now 2026-08-11T00:00:00Z
```

驗收面是 4 symbols × 7 intervals × 3 bar datasets = 84 bar streams，另加每個 symbol 一條 funding stream，共 88 streams、8 instruments。2026-08-11 實跑 readback 為 83,364 bars、180 funding events，且未出現 Premium instrument/stream；相同窗口第二輪的 instrument/bar/funding writes 全為 0。這只證明 bounded 全矩陣，不表示 `2022-08-01 → D-1` 已回填。

## OS-native 排程

`ops/ai.nautilus.quant.data-sync.plist` 目前只是**未安裝、未啟用**的 launchd 草稿；若未來通過完整回填、容量、恢復、監控與 review 驗收，設計上會在 `RunAtLoad` 與本機每日 10:15 執行。資料服務不需要 Hermes 存活。

Preflight、failure recovery、evidence 與未來 scheduler 操作見 [`ops/RUNBOOK.md`](ops/RUNBOOK.md)。

## 安全邊界

目前只有 data foundation，沒有策略、execution client、API key 或真實資金路徑。安裝與資料 smoke 通過不代表 production trading ready。

API key、token、憑證與個人資料不得寫入 repository；應放在受保護的環境檔或作業系統 Keychain，程式只讀環境變數。Repository 以三層降低誤傳風險：敏感檔 `.gitignore`、版本化的 pre-commit／pre-push fail-closed 掃描，以及 GitHub secret scanning／push protection。Clone 後啟用本機 hooks：

```bash
git config core.hooksPath .githooks
```

Hooks 只回報安全檔案路徑、行號與類型；敏感或 credential-shaped 路徑改顯示不可逆 fingerprint，不輸出疑似 secret。Hooks 會檢查已知 provider key、敏感 label、未知高熵字串、常見 email／臺灣手機／身分證格式，並預設拒絕所有 binary blob，避免壓縮檔、Office 文件或資料庫夾帶未掃描資料。不得以 `--no-verify` 繞過。

任何掃描器仍無法理解所有姓名、地址、未知識別碼或語境，一旦懷疑 key 曾進入 Git 歷史，必須立即撤銷／輪替，不能只刪檔或補一次 commit。若未來確實需要公開 binary asset，必須先建立窄範圍 allowlist 與對應內容檢查，不可直接放寬全部 binary。
