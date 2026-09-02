# Nautilus Quant System

[English](README.md) | **繁體中文**

> [!IMPORTANT]
> 這是建立於官方 [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) runtime 之上的獨立專案，並非 NautilusTrader 官方 fork，也不隸屬於、未受贊助或背書於 Nautech Systems。完整出處與第三方授權見 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

獨立、deterministic 的 Binance USD-M Futures 資料核心，使用 NautilusTrader `2.0.0rc2`。

## 從研究到驗證的工作流

本 repository 是更完整策略研究工作流中的下游驗證與執行層。外部 alpha 策略的探索先進入 [`alpha-strategy-research`](https://github.com/HCH725/alpha-strategy-research)：公開來源中的策略想法會被整理成可直接供 Hermes Wiki Brain 吸收、保留來源與研究邊界的標準化紀錄，再由 ChatGPT review 後直接寫入 Hermes Wiki Brain。

通過 review 的知識可再由 Hermes 組合、推演成可測試假說，並進入本 repository 做正式研究與驗證：

```text
外部公開來源
        ↓
Antigravity 研究
        ↓
alpha-strategy-research
（標準化、保留來源的研究紀錄）
        ↓
ChatGPT review
        ↓
Hermes Wiki Brain
        ↓
Hermes hypothesis / synthesis — Loop A (low-frequency; one thesis/family iteration → bounded meaningful branches → experiment spec)
        ↓
PyBroker Experiment & Attrition Loop — Loop B (deterministic N provisional candidates → batch screens/attrition; no LLM per candidate)
        ↓
Signal-Parity Gate (fail-closed)
        ↓
NautilusTrader high-fidelity historical verdict (survivors only)
        ↓
feedback / lineage / reuse — evidence-based outer feedback
        ↓
後續經 gate 進入 Paper → Binance Demo/Testnet → Live
```

Quant Research Pipeline — Three Layers, Two Loops, One Gate（正典模型）：

- **Data foundation**：Nautilus canonical market-data truth（bars / Funding / D-1）。
- **Loop A — Hermes Research Loop（low-frequency, theory/evidence-driven）**：Wiki Brain / reviewed strategy intake → falsifiable research thesis / strategy family → bounded meaningful hypothesis branches → experiment specification。外層迭代單位是**一個研究假說／策略族**，不是單一參數組合；Hermes 可產生有限個有意義分支（LLM tokens 決定 *what* to test）。
- **Loop B — PyBroker Experiment & Attrition Loop（high-throughput, deterministic）**：experiment specification → deterministic campaign expansion → N provisional candidates → batch backtests/screens → dedupe / invalid / reject / pass 記帳 → attrition funnel。**No LLM call per candidate**；machine compute 跑大量實驗。淘汰者不進入 Nautilus。
- **Gate — Formal signal parity / promotion gate（fail-closed）**：對 canonical data 獨立重算、要求 parity 才可晉升。
- **Nautilus High-Fidelity Validation（authoritative, scarce）**：僅 parity-passed survivors → historical accounting（含 fills/fees/funding/PnL）→ walk-forward / regime / cost robustness → strategy freeze → Shadow/Paper → Demo/Testnet → human/live boundary。
- **Outer feedback**：Hermes 檢視 survivor summaries、failure taxonomy 與 information gain，再 evidence-based 決定 stop / refine / open new batch；不編碼固定次數的 machine backtests。Kanban / reasoning iteration ≠ 單一 backtest run。

因此兩個 repository 的責任不同：`alpha-strategy-research` 是 public-safe 的上游策略研究與知識交接層；`nautilus-quant-system` 則是受控的策略驗證、accounting、execution research 與未來 deployment 層。策略出現在上游只代表**研究素材**，不代表已驗證，更不代表已具備交易資格。

## PyBroker 策略發源地

PyBroker 是隔離的上游策略研究前端：唯讀使用既有市場資料，實跑研究策略並輸出純資料 candidate；NautilusTrader 保持 canonical data、正式回測、fills、fees、Funding、positions、PnL 與 accounting 的唯一真值。

- PyBroker 只存在於獨立 `research/` environment，不加入正式 root runtime。
- Research 不改寫 canonical catalog／Funding、不持有 credentials 或訂單權限。
- Candidate 是 canonical JSON，不含 framework object、cache、pickle 或可執行 payload。
- PyBroker 結果一律是 provisional；Shadow、Paper、Binance Demo／Testnet 與 live 不在 v1 實作範圍。
- V1 已實跑 **Two Loops + One Gate** 研究閉環（**Loop A Hermes thesis/branches low-frequency → Loop B PyBroker N-candidate deterministic attrition high-throughput，無 LLM per candidate → Gate signal-parity fail-closed → 僅 survivors 進 Nautilus historical verdict**），並將 lineage、成功與失敗寫入 append-only SQLite ledger。單一 Hermes 推理迭代對應**一個研究假說／策略族**，由 Loop B deterministic 展開為 N 個 machine experiments；Kanban iteration ≠ 單一 backtest run。
- 長期操作模型允許 Hermes 自主新增 strategy family／公式，並要求 trading-eligible 候選通過 Paper 與 Binance Demo／Testnet；Live 仍是獨立授權。

計畫、責任邊界與 contracts：

- [`docs/plans/pybroker-nautilus-adoption.md`](docs/plans/pybroker-nautilus-adoption.md)
- [`docs/plans/2026-08-14-strategy-loop-v1.md`](docs/plans/2026-08-14-strategy-loop-v1.md)
- [`docs/architecture/hybrid-pybroker-nautilus.md`](docs/architecture/hybrid-pybroker-nautilus.md)
- [`docs/architecture/strategy-loop-operating-model.md`](docs/architecture/strategy-loop-operating-model.md)
- [`docs/contracts/pybroker-candidate-v1.md`](docs/contracts/pybroker-candidate-v1.md)
- [`docs/contracts/strategy-loop-v1.md`](docs/contracts/strategy-loop-v1.md)

隔離 runner 與 lock rebuild 指令見 [`research/README.md`](research/README.md)。Runtime candidates、verdicts 與 ledger state 保留在被忽略的 `var/` 下；root `nautilus-research` CLI 負責正式 Nautilus 歷史評估、feedback、reuse 與 funnel projection。

## 範圍

- Symbols：`BTCUSDT`、`ETHUSDT`、`BNBUSDT`、`SOLUSDT`
- 資料下載起點：`2022-01-01T00:00:00Z`
- 回測起點：`2022-07-01T00:00:00Z`；`2022-01-01 → 2022-06-30` 僅作策略指標 warm-up，不計入回測績效
- Intervals：`5m`、`15m`、`30m`、`1h`、`4h`、`1d`、`1w`
- Data：trade klines 與 funding rate；不抓 mark、index 或 premium index klines
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
| funding | versioned canonical `FundingObservation` v1 store |

`ParquetDataCatalog` 保存 instruments 與 bars。Funding 以版本化 generation、ready pointer 與 canonical JSONL 保存；每列保留自己的 funding time、rate、truth status，以及官方資料存在時的 settlement mark price。舊 `FundingRateUpdate` JSONL 只保留作短期 rollback evidence，不再是正式 reader／writer。

## 安裝與測試

```bash
uv sync --dev
.venv/bin/python -m unittest discover -s tests -v
```

以上命令應從 clone 後的 repository root 執行。`ops/RUNBOOK.md` 與 `ops/ai.nautilus.quant.data-sync.plist` 記錄目前維護者在 macOS 外接碟上的固定部署拓樸，不是通用安裝路徑；目前維護者機器已按該拓樸安裝並通過 RunAtLoad 驗活，其他環境必須先調整絕對路徑並重新完成驗收。

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

驗收面是 4 symbols × 7 intervals × 1 bar dataset = 28 bar streams，另加每個 symbol 一條 funding stream，共 32 streams、4 instruments。2026-08-11 實跑 readback 為 27,788 bars、180 funding events，沒有 Mark／Index／Premium stream；相同窗口第二輪的 instrument、bar、funding writes 全為 0。這只證明 bounded 全矩陣，不表示 `2022-01-01 → D-1` 已回填。

## OS-native 排程

維護者機器已安裝 `ai.nautilus.quant.data-sync` LaunchAgent；它在 `RunAtLoad` 與本機每日 `10:15` 執行正式 `config/market_data.json`，核心資料同步不需要 Hermes 存活。Launchd stdout／stderr 位於 `~/Library/Logs/NautilusQuant/`，domain run evidence 仍原子寫入 ignored `var/runs/`。

本次上線已驗證 RunAtLoad、自然 `10:15` calendar slot 與立即重跑皆可完成；最新健康狀態仍以 `nautilus-data status`、launchctl exit code 與 `var/runs/` readback 為準。PyBroker 研究前端不修改或重驗此 OS 排程。

安裝／重載、Removable Volumes 權限、failure recovery 與 evidence 檢查見 [`ops/RUNBOOK.md`](ops/RUNBOOK.md)。

## 安全邊界

目前已有隔離的 **Two Loops + One Gate** 歷史策略閉環（Loop B PyBroker attrition 為 high-throughput deterministic；僅 parity-passed survivors 進 Nautilus high-fidelity accounting），但尚無 shared live Strategy、Paper／Demo execution client、API key 或真實資金路徑。資料 smoke、歷史閉環或模擬環境通過都不代表 production trading ready；長期驗證與自主權邊界見 [`docs/architecture/strategy-loop-operating-model.md`](docs/architecture/strategy-loop-operating-model.md)。

API key、token、憑證與個人資料不得寫入 repository；應放在受保護的環境檔或作業系統 Keychain，程式只讀環境變數。Repository 以三層降低誤傳風險：敏感檔 `.gitignore`、版本化的 pre-commit／pre-push fail-closed 掃描，以及 GitHub secret scanning／push protection。Clone 後啟用本機 hooks：

```bash
git config core.hooksPath .githooks
```

Hooks 只回報安全檔案路徑、行號與類型；敏感或 credential-shaped 路徑改顯示不可逆 fingerprint，不輸出疑似 secret。Hooks 會檢查已知 provider key、敏感 label、未知高熵字串、常見 email／臺灣手機／身分證格式，並預設拒絕所有 binary blob，避免壓縮檔、Office 文件或資料庫夾帶未掃描資料。不得以 `--no-verify` 繞過。

任何掃描器仍無法理解所有姓名、地址、未知識別碼或語境，一旦懷疑 key 曾進入 Git 歷史，必須立即撤銷／輪替，不能只刪檔或補一次 commit。若未來確實需要公開 binary asset，必須先建立窄範圍 allowlist 與對應內容檢查，不可直接放寬全部 binary。
