# Nautilus Quant System

獨立、deterministic 的 Binance USD-M Futures 資料核心，使用 NautilusTrader `2.0.0rc2`。

## 範圍

- Symbols：`BTCUSDT`、`ETHUSDT`、`BNBUSDT`、`SOLUSDT`
- 歷史起點：`2021-01-01T00:00:00Z`
- Intervals：`5m`、`15m`、`30m`、`1h`、`4h`、`1d`、`1w`
- Data：trade、mark、index、premium klines 與 funding rate
- 邊界：只同步前一個完整 UTC 日；週線只到最後一個完整 Monday boundary
- 不包含 open interest

## 資料模型

| 來源 | Nautilus 表示 |
|---|---|
| trade klines | perpetual instrument 的 `Bar/LAST` |
| mark klines | perpetual instrument 的 `Bar/MARK` |
| index klines | 原生 `IndexInstrument` 的 `Bar/LAST` |
| premium index klines | 原生 `IndexInstrument` 的 `Bar/LAST` |
| funding | 原生 `FundingRateUpdate` JSONL event store |

`ParquetDataCatalog` 保存 instruments 與 bars。`2.0.0rc2` 尚無公開 funding Parquet writer，因此 funding 暫以 `FundingRateUpdate.to_json()` 原子檔保存；程式中的 `ponytail:` 註解標出未來可直接換回 Catalog 的位置。

## 安裝與測試

```bash
/Volumes/ExpansionDrive/.nautilus-tools/uv-0.11.33/bin/uv sync --dev
.venv/bin/python -m unittest discover -s tests -v
```

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
- Catalog 寫後 readback
- cross-process `flock` single writer
- 每次完成或失敗的 JSON run evidence（`var/runs/`）

## Bounded 全矩陣驗收

`config/bounded_matrix.json` 固定驗證 `2026-07-27 → 2026-08-10 UTC`，資料只寫入 ignored `.local/`：

```bash
.venv/bin/nautilus-data sync \
  --config config/bounded_matrix.json \
  --now 2026-08-10T12:00:00Z
.venv/bin/python scripts/verify_smoke.py \
  --config config/bounded_matrix.json \
  --now 2026-08-10T12:00:00Z
```

驗收面是 4 symbols × 7 intervals × 4 bar datasets = 112 bar streams，另加每個 symbol 一條 funding stream，共 116 streams。2026-08-11 實跑 readback 為 12 instruments、103,744 bars、168 funding events；相同窗口第二輪所有 writes 為 0。這只證明 bounded 全矩陣，不表示 `2021-01-01 → D-1` 已回填。

## OS-native 排程

`ops/ai.nautilus.quant.data-sync.plist` 目前只是**未安裝、未啟用**的 launchd 草稿；若未來通過完整回填、容量、恢復、監控與 review 驗收，設計上會在 `RunAtLoad` 與本機每日 10:15 執行。資料服務不需要 Hermes 存活。

Preflight、failure recovery、evidence 與未來 scheduler 操作見 [`ops/RUNBOOK.md`](ops/RUNBOOK.md)。

## 安全邊界

目前只有 data foundation，沒有策略、execution client、API key 或真實資金路徑。安裝與資料 smoke 通過不代表 production trading ready。
