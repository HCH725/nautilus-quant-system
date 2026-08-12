# PyBroker × NautilusTrader Hybrid 工程施工附錄

<!-- PLAN_ID:PYBROKER-NAUTILUS-ADOPTION-V1 -->
<!-- PLAN_SECTION:APPENDIX-META -->

> **閱讀定位：** 本文件只供實作者與程式審查工具使用，不是漢秦哥的審閱入口。產品目的、七階段導入、時程、停損條件、Kanban 治理與批准事項，以 `docs/plans/pybroker-nautilus-adoption.md` 為準。
>
> **For Hermes/Kanban:** 每張可執行卡先以 `scripts/verify_plan_ref.py` 驗證批准的 full commit、canonical plan 與本附錄 section IDs；只依卡片授權的 Stage／Task 施工。正式實作另載 `codex`、`test-driven-development`、`systematic-debugging`、`requesting-code-review` 與 `verified-repository-delivery`。

**Goal:** 在既有 `nautilus-quant-system` 中建立一套 Nautilus-first 的 Hybrid AI Quant System：PyBroker 只負責上游策略研究、ML、walk-forward 與候選排序；第一輪不得淘汰候選；NautilusTrader 保持 canonical data、權威回測、帳務、風控與 execution 的唯一真值。

**Architecture:** 邏輯上是一套產品、一條策略生命週期；物理上分成 Python 3.12 research process 與 Python 3.13 Nautilus runtime process。兩側只共享一個零框架依賴的 Strategy Core 與不可變 Candidate Capsule，不共享 PyBroker/Nautilus 內部物件、cache、帳本或憑證。

**Tech Stack:** Python 3.13.14、NautilusTrader 2.0.0rc2、Python 3.12、`lib-pybroker==1.2.14`（import 名稱 `pybroker`）、pandas 3.0.5、NumPy 2.5.2、scikit-learn 1.9.0、stdlib JSON/JSONL/SHA-256、`unittest`、uv 0.11.33。

**Plan status:** `PYBROKER-NAUTILUS-ADOPTION-V1` 已批准啟動 Stage 0。Stage 0 只授權基線／文件封存、Funding observation root-fix、versioned migration、deterministic Nautilus accounting 與資料流程驗活；Stage 1 前不得安裝 PyBroker 或建立 research runtime；本計畫永不授權 Testnet/live。

**Review provenance:** 主程序已完成repo/source/runtime直接驗證與機器一致性檢查；Codex完成過計畫審查，最終定向複審僅對Funding cutover與舊candidate重建回傳`BLOCKER: 無`。另行派出的三個async subagents均於600秒timeout且沒有final summary，狀態為`INDEPENDENT_REVIEW_TIMEOUT_NO_VERDICT`；其transcript只作待複驗線索，不能算PASS。本稿已由主程序直接複驗其中有效線索並據此修正，不宣稱三位子修批准本計畫。

---

<!-- PLAN_SECTION:APPENDIX-DECISION -->

## 0. 直白結論

採用方向，但採用的是 **可移除的 PyBroker research adapter**，不是把 PyBroker runtime 塞入 NautilusTrader。

完整 pilot 只完成一條可驗證的垂直切片；目前唯一批准的 Stage 0 先修好 Nautilus 裁判席，尚不建立 PyBroker research runtime：

```text
BTCUSDT 1h canonical D-1 snapshot
  → PyBroker + sklearn long/flat ML screening
  → immutable Candidate Capsule
  → framework-neutral parity/causality verification
  → NautilusTrader authoritative replay with fees/funding/accounting
  → structured rejection/acceptance feedback
```

第一輪終點是 `nautilus_reproduced`。Shadow、Testnet、live 只保留狀態語意，不在這輪接憑證或部署；待 pilot 證明研究週期確實縮短，再另開 runtime rollout 計畫。

如果 pilot 沒有帶來可量測的研究效率，直接移除 `research/` 與 PyBroker adapter；不留相容層、不 fork 上游，也不讓 Nautilus 主陣背負 PyBroker 技術債。

---

<!-- PLAN_SECTION:APPENDIX-EVIDENCE -->

## 1. 已驗證現況、推論與未知

### 1.1 已驗證（2026-08-12）

| 項目 | 實證 |
|---|---|
| 專案根目錄 | `/Volumes/ExpansionDrive/nautilus-system` |
| Git | `main`，HEAD 與 `origin/main` 同為 `2925fdce8736de1737698c1519e7c338fb790cf4` |
| 工作樹 | 計畫建立前乾淨；35 個 tracked files |
| 正式 remote | `https://github.com/HCH725/nautilus-quant-system` |
| 現有範圍 | deterministic Binance USD-M data foundation；目前沒有策略、回測或 ML 實作可直接「搬移」 |
| Runtime | Python 3.13.14、`nautilus_trader==2.0.0rc2` |
| Runtime 依賴面 | 沒有 NumPy、pandas、PyArrow、Polars、scikit-learn、PyBroker |
| 基線測試 | `python -m unittest discover -s tests -v`：87 tests，`OK` |
| Canonical config | BTC/ETH/BNB/SOL；5m/15m/30m/1h/4h/1d/1w；Trade Kline + Funding；資料起點 2022-01-01；backtest 起點 2022-07-01 |
| Daily sync | LaunchAgent 已註冊；最近 scoped status 為 `PASS`，last exit code 0 |
| BTCUSDT 1h catalog | 直接查詢`BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL`得40,416根；`2022-01-01T01:00:00Z`至`2026-08-12T00:00:00Z`；0 duplicate、0非1h鄰接、嚴格遞增 |
| PyBroker stable | PyPI distribution `lib-pybroker==1.2.14`；import `pybroker`；本機完整 upstream suite 4151 passed |
| PyBroker master/dev | master 宣告 1.2.15：4151 passed；dev 2.0.0 targeted：2950 passed、1 skipped |
| PyBroker 研究環境 | Python 3.12.13；已驗證 NumPy 2.5.2、pandas 3.0.5；scikit-learn 1.9.0 wheel 可解析 |
| PyBroker 真實 ML API | `pybroker.model(...) -> ModelSource`，再由 `Strategy.add_execution(models=...)` 接入；stable 沒有 `Strategy.add_model()` |
| PyBroker cache API | `disable_data_source_cache()`、`disable_indicator_cache()`、`disable_model_cache()` |
| PyBroker known defect | `exit_on_last_bar` 可造成期末 cash 帳本 1062、報表 portfolio 780 的不一致；PyBroker績效不得當真值 |
| PyBroker global state | `StaticScope` 為 process-global；每個 experiment 必須獨立 process |
| Cache/security | 1.2.x cache identity 不含資料源身分；diskcache 5.6.3 使用 pickle；不傳 cache、不載不明 pickle |
| License | Apache-2.0 + Commons Clause，並非標準 Apache-2.0 |
| Nautilus funding source evidence | 釘住的 v2 原始碼已有 `FundingRateUpdate → deferred settlement → funding boundary settlement` 路徑 |
| Binance funding payload | 2026-08-12 只讀 live probe 確認 `/fapi/v1/fundingRate` 每列含 `fundingRate`、`fundingTime`、`markPrice`；這仍是 Funding dataset，不需新增 Mark Kline stream |
| 現有 funding 資料缺口 | `funding_events()` 目前丟棄 API 的 `markPrice`，並把每列 `next_funding_ns` 設為下一列 timestamp；Nautilus 優先以 `next_funding_ns` 當 settlement boundary，存在整期位移風險，必須由 executable spike 定案與修復 |

### 1.2 目前只可推論，不得冒充已完成

- 最近 data sync `PASS` 證明該次 scoped sync 完成，**不等於**候選策略所需的每一列歷史資料已做 content-addressed snapshot 驗證。
- Nautilus v2 原始碼有 funding settlement 路徑，**不等於**本專案已用 Binance USD-M instrument、已知部位與已知 funding rate 做過帳戶增減斷言。
- PyBroker upstream suite 通過，**不等於**PyBroker 的績效、stop/fill/fee/帳務語意適合作為正式驗收。
- 研究環境能解析 scikit-learn 1.9.0，**不等於**正式 `research/uv.lock` 已建立及可重建。

### 1.3 各 Stage 必須消除的未知

1. **Stage 0：** Funding observations 在指定 `as_of` 的實際連續覆蓋、首末 timestamp、重複／缺口；BTCUSDT 1h catalog 本身已直接驗證，但 Task 4 仍須對每個實際 snapshot 窗口重跑內容檢查，不能沿用本次全域盤點當 candidate evidence。
2. **Stage 0：** 現有歷史 funding row 應在自身 `fundingTime` 還是 `next_funding_ns` 結算；必須以兩個不相等 rate 證明沒有整期位移。
3. **Stage 0：** Nautilus 2.0.0rc2 在本專案資料形態下的 funding debit／credit、fee、bar execution 與期末平倉語意；資料契約不抓 Mark Kline stream，但必須保留 Funding endpoint 自身提供的 settlement `markPrice`。
4. **Stage 3：** PyBroker stable 1.2.14 是否能在自定義 purge／embargo fold 邊界下，穩定產生完全可重現的 prediction／intent trace。
5. **商業化前另案：** Commons Clause 對未來付費 hosted service 的限制；第一輪只准內部研究 pilot。

任何一項 hard spike 不通，不往下一階段粉飾推進。

---

<!-- PLAN_SECTION:APPENDIX-ARCHITECTURE -->

## 2. 目標陣圖與責任邊界

```text
Hermes / AI Agent（控制平面）
  ├─ 建立 hypothesis / experiment spec
  ├─ 修改有測試的 Strategy Core（走 Git commit）
  ├─ 啟動隔離 research process
  ├─ 比較 provisional screening
  ├─ 建立候選並呼叫 deterministic gates
  └─ 自動推進至 nautilus_reproduced
                 │
                 ▼
PyBroker Research Adapter（Python 3.12；無交易憑證）
  ├─ 只讀 immutable snapshot
  ├─ feature/model training
  ├─ walk-forward screening
  ├─ provisional metrics
  └─ model export + golden vectors
                 │
                 ▼
Candidate Capsule（canonical JSON/JSONL；content-addressed）
  ├─ manifest.json
  ├─ strategy.json
  ├─ model.json
  ├─ training_ledger.json
  ├─ oof_trace.jsonl
  ├─ golden_vectors.jsonl
  └─ screening.json
                 │
                 ▼
Promotion Gate（Python 3.13；不 import PyBroker）
  ├─ hashes / provenance
  ├─ snapshot completeness
  ├─ feature/model/policy parity
  ├─ time-causality / leakage
  └─ deterministic replay prerequisites
                 │
                 ▼
NautilusTrader Trading Core（唯一 truth）
  ├─ authoritative backtest
  ├─ fee / funding / margin / execution
  ├─ Portfolio / account / fill truth
  ├─ rejection reason feedback
  └─ 未來才接 shadow / Testnet / live
```

### 2.1 Domain ownership

| Domain | Owner | PyBroker 可否改寫 |
|---|---|---:|
| Binance public data ingestion、D-1 completeness | `nautilus_quant` data foundation | 否 |
| Canonical bars/funding | Nautilus data foundation（Parquet bars + FundingObservation store） | 否，只讀 snapshot |
| Feature/model/policy 定義 | framework-neutral Strategy Core | 透過 Git 修改，不在 artifact 夾帶 code |
| ML training / walk-forward / provisional screening | PyBroker research adapter | 是 |
| Candidate provenance與 payload | Candidate Capsule contract | 只能新建，不可覆寫 |
| Feature/prediction/intent correctness | deterministic verifier | 否，PyBroker 自評不算 |
| Fills、fees、funding、Portfolio、accounting | NautilusTrader | 否 |
| Risk、sizing、leverage、reduce-only、TIF | NautilusTrader adapter/runtime | 否 |
| Promotion state | official CLI hash-chained event state machine | 正式CLI只接受機器證據；同使用者任意改檔不在v1安全承諾內 |
| Testnet/live credentials | Nautilus runtime only | 絕對不可 |

<!-- PLAN_SECTION:APPENDIX-INVARIANTS -->

### 2.2 不可違反的 invariants

1. `research` 環境不安裝進 root `.venv`；runtime 不新增 pandas/NumPy/sklearn/PyBroker。
2. 正式 research runner 拒絕 `data/catalog` 路徑，只讀由 runtime 匯出的 immutable snapshot；同一 macOS 使用者下這是受測的程式邊界，不是假裝能抵禦惡意任意 Python process 的 OS sandbox。
3. Candidate 不含 `.py`、`.pkl`、`.pickle`、`.joblib`、shared cache、任意 import path 或可執行 payload。
4. Strategy Core 不 import PyBroker、NautilusTrader、pandas、NumPy、網路/檔案 I/O。
5. PyBroker `TestResult` 及 metrics 一律標記 `provisional`。
6. Candidate 通過 verification 仍不代表可 shadow/Testnet/live；第一輪只到 `nautilus_reproduced`。
7. LLM 可提出與執行研究，但不能改 canonical data、帳本、RiskEngine、憑證或 gate evidence。
8. Hermes/research process 掛掉時，Nautilus data/runtime 仍可獨立安全運行。
9. 所有失敗 experiment 都保留；不得只留贏家。
10. Pilot 到 `nautilus_reproduced` 不使用 sealed holdout；未來要越過這一狀態時，同一 sealed holdout 對同一 canonical candidate family 只准正式 claim 一次，且 claim 必須先於 holdout path 揭露。
11. 第一輪 PyBroker 只排序與建議，不得淘汰任何候選；Stage 6 每一個預先封存 hypothesis 都必須跑 PyBroker→Nautilus 與直接 Nautilus 兩條路徑。

---

<!-- PLAN_SECTION:APPENDIX-PILOT -->

## 3. v1 Pilot：刻意狹窄的垂直切片

### 3.1 固定範圍

- Symbol：`BTCUSDT-PERP.BINANCE`
- Bar type：`BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL`
- Direction：`LONG` / `FLAT`；不做 short
- Decision timing：bar `t` 收妥後計算，最早在下一個可執行事件作用；禁止用 bar `t+1` 資訊決定 bar `t`
- Model：`StandardScaler` + `LogisticRegression(solver="liblinear", random_state=42, max_iter=1000)`；固定binary class order
- Seed：42
- Feature set v1：
  - 1-bar log return：`ln(close_t / close_{t-1})`
  - 6-bar log return：`ln(close_t / close_{t-6})`
  - close / 24-bar arithmetic mean − 1
  - 最近24個1-bar log returns的population standard deviation
  - `(volume_t - mean(last_24_volumes)) / pstdev(last_24_volumes)`；zero variance時為0
- Warmup：至少25根bars；不足不產生feature
- Label horizon：6 bars；`label_t = 1 if ln(close_{t+6}/close_t) > 0 else 0`，availability timestamp固定為`ts_event_{t+6}`
- Policy：雙閾值 hysteresis，輸出 target intent；v1 default `entry_score=0.55`、`exit_score=0.45`
- PyBroker bootstrap：`calc_bootstrap=False`；IID bootstrap 不作正式統計保證
- Parallel：`disable_parallel=True`，先換取重現性
- 正式績效：只看 Nautilus replay；pilot 的第一目標是 correctness/evidence pipeline，不是找到可上線 alpha

### 3.2 不做的事

- 不做 Qlib、AutoML、Bayesian search、genetic programming。
- 不做多商品、多 timeframe、portfolio optimizer。
- 不做微服務、HTTP API、MCP、queue、DB、dashboard、cron。
- 不 fork PyBroker，也不修改 upstream checkout。
- 不把 PyBroker cache、pickle/joblib model 傳進 runtime。
- 不建立 live strategy、不接 Binance API key。
- 不把「PyBroker 與 Nautilus PnL 一樣」當目標；兩者 execution semantics 本來就不同。要對齊的是 **feature / prediction / intent**，正式 PnL 只由 Nautilus產生。

---

<!-- PLAN_SECTION:APPENDIX-LAYOUT -->

## 4. Repository 目標布局

```text
nautilus-system/
├── pyproject.toml                         # runtime；仍為 Python 3.13 + Nautilus
├── uv.lock
├── packages/
│   └── strategy-core/
│       ├── pyproject.toml                 # Python >=3.12，零第三方依賴
│       ├── src/nautilus_strategy_core/
│       │   ├── __init__.py
│       │   ├── types.py                   # BarSnapshot/FeatureVector/Intent
│       │   ├── features.py                # 純函式 feature set
│       │   ├── linear_model.py            # JSON model + stdlib inference
│       │   ├── policy.py                  # score → LONG/FLAT intent
│       │   ├── canonical.py               # canonical JSON/hash helpers
│       │   └── capsule.py                 # snapshot/candidate schema validation
│       └── tests/
│           ├── test_features.py
│           ├── test_linear_model.py
│           ├── test_policy.py
│           ├── test_canonical.py
│           └── test_capsule.py
├── research/
│   ├── .python-version                    # 3.12
│   ├── pyproject.toml                     # lib-pybroker 1.2.14 + sklearn 1.9.0
│   ├── uv.lock                            # 與 root lock 完全分離
│   ├── config/
│   │   └── btcusdt_1h_logistic_v1.json
│   ├── src/pybroker_lab/
│   │   ├── __init__.py
│   │   ├── cli.py
│   │   ├── guards.py                      # cache/network/env guards
│   │   ├── data.py                        # snapshot JSONL → DataFrame
│   │   ├── folds.py                       # purge/embargo folds
│   │   ├── model.py                       # PyBroker ModelSource + JSON export
│   │   ├── runner.py                      # one process = one experiment
│   │   └── artifacts.py                   # experiment/candidate writer
│   └── tests/
│       ├── test_environment.py
│       ├── test_guards.py
│       ├── test_data.py
│       ├── test_folds.py
│       ├── test_model.py
│       ├── test_runner.py
│       └── test_artifacts.py
├── src/nautilus_quant/
│   ├── ...existing data foundation...
│   ├── funding_observation.py             # Funding row + official mark price
│   ├── snapshot.py                        # canonical snapshot create/verify
│   ├── candidate.py                       # capsule verification
│   ├── strategy_adapter.py                # Strategy Core → Nautilus Strategy
│   ├── backtest.py                        # authoritative replay + funding
│   ├── promotion.py                       # hash-chained official state transitions
│   └── hybrid_cli.py                      # snapshot/verify/replay/promote/status
├── config/
│   ├── market_data.json
│   └── backtest_pilot.json
├── tests/
│   ├── ...existing tests...
│   ├── test_snapshot.py
│   ├── test_candidate.py
│   ├── test_backtest_funding.py
│   ├── test_strategy_adapter.py
│   ├── test_backtest.py
│   ├── test_promotion.py
│   └── test_hybrid_cli.py
├── scripts/
│   └── verify_hybrid.py                   # 一個可執行全陣驗證入口
├── docs/
│   ├── architecture/hybrid-pybroker-nautilus.md
│   ├── contracts/candidate-capsule-v1.md
│   └── reports/pybroker-pilot-YYYY-MM-DD.md
└── var/                                   # 已 gitignored；只放執行 artifacts
    ├── snapshots/<snapshot-id>/
    ├── research/experiments/<attempt-id>/
    ├── candidates/<candidate-id>/
    ├── candidate-state/<candidate-id>/events.jsonl
    ├── holdout-claims/<family-id>/<holdout-id>.json
    ├── holdout-evaluations/<evaluation-id>/
    ├── replays/<replay-id>/
    └── locks/
```

`strategy-core` 是唯一新增抽象，理由是它有兩個真實 consumer（research 與 runtime）。其餘不再建立 domain framework、plugin registry 或 service layer。

---

<!-- PLAN_SECTION:APPENDIX-STRATEGY-CONTRACT -->

## 5. Strategy Contract v1

### 5.1 純介面

```python
features(history: Sequence[BarSnapshot], spec: FeatureSpec) -> FeatureVector | None
predict(model: LinearBinaryModel, features: FeatureVector) -> float
policy(score: float, previous: Intent, spec: PolicySpec) -> Intent
```

### 5.2 型別與語意

- `BarSnapshot`：`ts_event_ns`、OHLCV；輸入已按 timestamp 嚴格遞增。
- `FeatureVector`：固定且有序的 `names` 與 finite float values；順序是 model contract 的一部分。
- `LinearBinaryModel`：feature names、scaler mean/scale、coefficients、intercept、class order；從 `model.json` 讀入。
- `Intent`：v1 只有 `LONG`、`FLAT`。
- `policy` 不讀 Portfolio，不建 order，不決定 quantity/leverage/TIF。
- runtime adapter 將 intent 映射成 fixed pilot notional；正式 sizing/risk 永遠留在 Nautilus。

### 5.3 決定性規則

- Feature window 只可使用 `ts_event <= decision_ts` 的 bars。
- Zero variance volume window 明確輸出 0，不產生 NaN/Inf。
- 不足 warmup 時回傳 `None`，不得以補零偽造特徵。
- Model inference 用固定順序與 `math.fsum`，不依賴 BLAS 排序。
- `model.json` 數值用 decimal string；載入後拒絕 NaN/Inf、重複 feature、未知 model type。
- 比對容差：`abs <= 1e-12` 或 `rel <= 1e-10`；intent 必須逐列完全一致。
- Strategy Core import 測試必須證明不載入 `pybroker`、`nautilus_trader`、pandas、NumPy。

---

<!-- PLAN_SECTION:APPENDIX-SNAPSHOT -->

## 6. Immutable Data Snapshot v1

### 6.1 Snapshot payload

```text
var/snapshots/<snapshot-id>/
├── manifest.json
├── bars.jsonl
└── funding.jsonl
```

`bars.jsonl` 每列只含 canonical fields，固定 key order、UTF-8、LF、無空白；OHLCV 以 decimal string 儲存。`funding.jsonl` 每列必含 instrument ID、`funding_time_ns`、rate、官方 funding-history row 內的 `mark_price` 與 rate type（若 API 提供）。目前既有 `FundingJsonStore` 已丟失 mark price，不能拿舊檔偽造；Task 3 必須從官方 Funding endpoint 重抓 pilot 範圍、寫入 `data/funding/<instrument-id>.observations.v1.jsonl` 並完整 readback 後，才允許建立 snapshot。

### 6.2 Manifest 必填

- schema version
- symbol、instrument ID、bar type
- `start_boundary`、`close_boundary`
- `as_of_utc_day`（只允許完整 D-1）
- row counts、first/last timestamp
- config SHA-256
- `exporter_commit`（只代表匯出程式版本，不冒充整個增量 catalog 的來源 commit）
- 參與該覆蓋範圍的 sync evidence hashes；在尚無逐段 ingestion lineage 前明示 `catalog_lineage_status: incomplete`
- bars/funding SHA-256
- gap/duplicate count（必須為 0）
- snapshot ID

Snapshot ID = `SHA-256(canonical(manifest_without_snapshot_id))`；該manifest body已含bars/funding payload hashes，算出後才加入`snapshot_id`，避免circular hash。

### 6.3 Snapshot hard gates

- 僅 runtime env 可從 catalog 建立。
- 嚴格遞增、無 duplicate、1h 連續、OHLC 合法、volume 非負。
- 現有 bars 以 close timestamp 標記，故窗口明定為 `start_boundary < bar.ts_event <= close_boundary`；`close_boundary` 必須是 UTC day boundary，且不晚於執行日 00:00 UTC。Funding 另按其已驗證 settlement boundary 規則納入，不能把 bars 的 `<=` 規則盲套過去；每個 historical rate 與官方 mark price 綁定同一 `funding_time_ns`。
- 1h bars 必須斷言 `expected_count == (close_boundary - start_boundary) / 1h`、首列=`start_boundary+1h`、末列=`close_boundary`。
- 同一輸入重跑得到相同 bytes 與 snapshot ID。
- 寫入採 temp directory + fsync + atomic rename；完成後 payload chmod read-only。
- Research 僅接收 snapshot path；不得接收 catalog path。
- 任一 byte 竄改後 `snapshot verify` 必須非零退出。

---

<!-- PLAN_SECTION:APPENDIX-EXPERIMENT -->

## 7. Experiment、holdout 與防過擬合

### 7.1 Experiment identity

- `experiment_key`：snapshot ID、strategy spec hash、code commit、research lock hash、seed 的 content hash。
- `attempt_id`：UTC timestamp + `experiment_key` short hash；相同實驗重跑也保留不同 attempt。
- 每個 attempt 目錄至少有 `request.json`、`result.json`、`stdout.log`、`stderr.log`。
- 即使 import/training/backtest 失敗，也原子寫入 `result.json(status="failed")`。

### 7.2 Split discipline

- `shuffle=False`。
- `label_horizon_bars=6`。
- `purge_bars >= label_horizon_bars`。
- `embargo_bars >= label_horizon_bars`。
- folds 先由我們的 `folds.py` 產生並測試，再映射進 PyBroker；不可把 PyBroker default split 當 promotion truth。
- 第一個 API compatibility spike 必須比較我們指定的 train/test timestamps 與 PyBroker 實際輸出；若無法一致，停止使用 PyBroker 內建 walk-forward training，降級為「自家 split/training + PyBroker execution screening」，不得偷偷放寬 purge/embargo。
- 每個 fold 必須輸出 `training_ledger.json`：精確 train feature row IDs、label row IDs、label availability timestamps、test row IDs、purge/embargo boundaries、model coefficient hash。Verifier 會從 snapshot 重建 labels/splits並逐列驗證，不能只信 fold metadata。
- 每個 fold 必須輸出由 sklearn/PyBroker 路徑獨立產生的完整 OOF prediction/intent trace；shared stdlib core 另行重算並比較。這才叫 cross-implementation parity，不是同一份 core 自我比較。

### 7.3 Sealed holdout

- Pilot 到 `nautilus_reproduced` 只使用 development snapshot/window；sealed holdout 不屬於這輪自動 gate，避免在還沒證明工程鏈時提早消耗它。
- 未來越過 `nautilus_reproduced` 前，runtime 另建 development 與 holdout 兩個 snapshot IDs；research runner平常只看 development path。
- `canonical_family_id` 只由 executable feature/label/model/policy specs、搜尋空間與祖先 family ID 計算，不含可任意改寫的 hypothesis prose。
- 可信 promotion owner 必須先以 `flock + O_EXCL + fsync` 對 `(canonical_family_id, holdout_id)` 建立一次性 claim，claim 成功後才向 evaluator揭露holdout path；process在揭露後crash也視為已使用。
- 同一使用者仍可繞過普通檔案權限，故 v1 claim 只能作操作防誤用與可追蹤證據，不能宣稱抵禦惡意 Agent。要自動進 shadow/Testnet/live 前，必須升級到不同 macOS帳號/container或外部不可改寫store，並另案驗證。

### 7.4 Screening output

`screening.json` 必須寫：

- `truth_status: "provisional"`
- 所有 fold 邊界、seed、feature/model/policy spec
- trial count、`canonical_family_id`、parent experiment
- 每 fold predictions/intents/trades/metrics
- 失敗 fold 與理由
- PyBroker version、Python version、lock hash
- 明文警告：不可作 Portfolio/accounting/live promotion 真值

---

<!-- PLAN_SECTION:APPENDIX-CAPSULE -->

## 8. Candidate Capsule v1

### 8.1 Payload

```text
var/candidates/<candidate-id>/
├── manifest.json
├── strategy.json
├── model.json
├── training_ledger.json
├── oof_trace.jsonl
├── golden_vectors.jsonl
└── screening.json
```

v1 改用 JSONL 而不是先前草圖中的 Parquet：目前 runtime 沒有 PyArrow，research env 也未安裝 PyArrow；為一個數百列 parity fixture 引入大型依賴不划算。只有 golden vectors 實測成為瓶頸時才評估 Parquet，並以 `ponytail:` 註解標示升級點。

### 8.2 `manifest.json` 必填

- schema version、candidate ID、experiment/attempt/family/parent ID
- repository URL、clean Git commit；`dirty=true` 是 promotion hard veto
- snapshot ID + snapshot manifest SHA-256
- runtime lock hash、research lock hash、`strategy_core_tree_hash`與版本
- symbol/timeframe
- train/validation windows；pilot 的 holdout 必須明示 `status: not_used`，不得塞入假 window
- label horizon、purge、embargo、seed
- feature/model/policy schema versions
- 六個非 manifest payload（`strategy.json`、`model.json`、`training_ledger.json`、`oof_trace.jsonl`、`golden_vectors.jsonl`、`screening.json`）的 size + SHA-256；manifest 絕不列自己的 byte hash
- source attempt timestamp（取自既有attempt ledger，不另取candidate建立時wall clock）

Candidate ID = `SHA-256(canonical(manifest_without_candidate_id))`。該manifest body已包含source attempt ID/timestamp、所有identity/provenance欄位與上述六個非-manifest payload hashes；算出後才加入`candidate_id`並落盤。Manifest不列自己的byte hash，因此沒有self-hash循環；任何manifest欄位或payload改動都會使重算ID失配。

候選建立前，source commit必須已push到正式`origin`的candidate branch或`main`，並以遠端讀回確認object ID；manifest記錄ref與commit，但commit hash才是identity。日後verify若當前checkout不同，controller必須在recorded commit建立clean detached worktree，以該commit內的root/research locks與`git rev-parse <commit>:packages/strategy-core` tree hash重建環境。若commit、locked artifacts或dependency downloads無法重建，結果是`UNREPRODUCIBLE_ENV`並阻擋晉級，不能沿用舊green report。v1不自建wheel registry；若遠端package retention成為實測風險，再新增受信artifact store。

### 8.3 `strategy.json`

只放資料，不放 Python：

- feature set ID/version/parameters
- model type/version
- ordered feature names
- policy thresholds
- allowed intents
- decision timing
- runtime sizing profile reference

### 8.4 `model.json`

v1 唯一允許 `standardized_logistic_binary_v1`：

- ordered feature names
- scaler mean / scale
- coefficients / intercept
- classes
- training library provenance

研究端可以用 sklearn training object，但匯出後立刻丟棄其 pickle 表示；golden vectors 與 runtime inference都必須改走 shared stdlib model。

### 8.5 Training evidence

- `training_ledger.json` 記錄每 fold 的精確 row IDs、label availability、purge/embargo、trainer config與係數hash。
- `oof_trace.jsonl` 是由 sklearn/PyBroker training path直接輸出的完整 out-of-fold predictions/intents，不可由 shared runtime core 回填。
- Verifier從snapshot重建feature/label/split，確認每個training label在該fold決策時間已可得，再以shared core比較OOF trace。
- 最終model必須可在recorded training rows上 deterministic retrain；retrain後 coefficients/intercept、OOF predictions與`model.json`在容差內一致，否則不晉級。

### 8.6 `golden_vectors.jsonl`

至少 256 列，涵蓋：

- warmup 後第一列
- 每個 fold 邊界前後
- score 接近 entry/exit threshold 的列
- development window 的 deterministic evenly spaced samples
- 最末可決策列

每列含 timestamp、ordered features、score、previous intent、target intent。Runtime 從 snapshot 重算，不接受 candidate 自帶 raw bars 當真值。

### 8.7 Capsule hard gates

- 只允許上述七個普通檔案；拒絕 symlink、device、額外檔、absolute path/traversal。
- 拒絕 executable bit與可執行副檔名。
- hash、candidate ID、Git clean commit、lock hash 全部吻合。
- capsule 完成後 read-only；promotion state 寫在外部hash-chained events，不修改 capsule。
- 任一 payload byte 被改動，`candidate verify` 必須 fail closed。

---

<!-- PLAN_SECTION:APPENDIX-PROMOTION -->

## 9. Promotion Gate 與狀態機

### 9.1 狀態

```text
Research ledger（candidate尚不存在）:
idea → experiment → screened

Candidate lifecycle（candidate ID建立後）:
candidate
 → verified
 → nautilus_reproduced
 → shadow
 → testnet
 → live_candidate
 → live
 ↘ retired（任何階段均可）
```

本輪正式 CLI 只允許依機器證據推進到 `nautilus_reproduced`。`shadow` 之後的 transition schema 可先保留，但 command 必須回傳 `NOT_IMPLEMENTED_FOR_PILOT`，不能假裝成功。

### 9.2 `candidate verify` 必過

1. Capsule/path/hash/provenance 完整。
2. Snapshot content完整且未變。
3. Source commit clean且可由origin讀回；verify在recorded commit的clean detached worktree與相符locks執行，不要求當前checkout恰好相同。
4. Feature parity：每個 golden row 容差內一致。
5. Prediction parity：容差內一致。
6. Intent parity：逐列完全一致。
7. Prefix causality：以完整資料與 timestamp prefix 計算，該 timestamp 結果一致。
8. Future mutation：修改 timestamp 之後的資料，不得改變之前輸出。
9. 從 snapshot 重建每fold的features、labels與availability，逐列核對`training_ledger.json`；任何training label在當時不可得即fail。
10. Deterministic retrain後係數/intercept與完整OOF trace吻合，證明candidate確實來自宣告的training rows，而非只讓shared core自我比較。
11. Fold boundaries、label/purge/embargo 合法。
12. Model/strategy schema allowlist。
13. 兩次 verify 的 canonical report hash相同。

LLM 的文字判斷不能取代以上任何一項。

### 9.3 `nautilus replay` 必過

- 使用同一 snapshot 與 Strategy Core。
- 先輸出 feature/prediction/intent trace，再由 Nautilus adapter 做 sizing/order。
- 使用明示的 starting balance、account type、OMS、bar execution、fee/fill/funding assumptions。
- funding events 真正進 backtest settlement，不只出現在報告旁欄。
- 系統仍不新增 Mark Kline stream；但 Binance Funding endpoint 本身已帶每個結算點的 `markPrice`。Task 3 必須完整保留，replay時在同一boundary先送對應`MarkPriceUpdate`、再送`FundingRateUpdate`；同timestamp排序必有integration test。只有缺欄/不一致fixture才測top-of-book fallback，真實pilot不得默默fallback。Report必寫`funding_price_source=binance_funding_history_mark_price`，否則狀態只能是`modeled_funding`且不得宣稱正式績效。
- 現有historical funding row的`next_funding_ns`會被Nautilus優先當結算boundary；Phase 0未證明/修正一期間位移前，真實replay不得通過。
- 期末 open order/position處理規則明示，不能重演 PyBroker `exit_on_last_bar` 帳本/報表分裂。
- 同 candidate + config 重跑兩次，去除 wall-clock metadata 後 report hash相同。
- report 同時保留 account events、orders/fills、funding、fees、positions、PnL 與 rejection reasons。
- `screening.json` 與 Nautilus report 不要求 PnL 相等；任何差異標成可解釋的 execution feedback。

### 9.4 State event（tamper-evident，不冒充不可改寫store）

每個 transition 追加一列 canonical JSONL：

- candidate ID
- from / to
- monotonic sequence
- previous event hash / event hash
- command/tool version
- evidence report hash
- actor type（agent/operator）
- UTC timestamp
- result + machine reason codes

正式 CLI 禁止 `--force`、不接受呼叫者自報`passed=true`、不寫可手改的 current-state真值；current state由完整hash chain replay得出，另以atomic head file偵測一般截尾/回滾。

但這些gitignored檔案與Agent同屬一個macOS使用者，hash chain不能阻止惡意process連events與head一起重寫。因此v1只宣稱：**官方transition工具無法靠LLM文字跳關，且一般損壞/截尾可偵測**；不宣稱對同使用者任意檔案寫入具安全性。要自動進shadow/Testnet/live前，最新head必須錨定到不同權限owner或外部不可改寫store，否則保留operator promotion。

---

<!-- PLAN_SECTION:APPENDIX-AGENT-BOUNDARY -->

## 10. AI Agent 自治邊界

### 10.1 Agent 可自動做

- 建立 hypothesis、strategy spec、實驗 family。
- 在 branch/worktree 中新增或修改 Strategy Core 與測試。
- 產生 research config、啟動獨立 experiment process。
- 保留並分析成功/失敗結果。
- 對 provisional screening 排序。
- 建立 capsule、執行 verify、執行 Nautilus replay。
- 透過正式 CLI 根據 deterministic evidence 自動推進到 `nautilus_reproduced`（cooperative automation boundary）。
- 將 Nautilus rejection reason轉成下一輪研究約束。

### 10.2 Agent 絕對不可做

- 改寫 canonical catalog/funding 或 snapshot payload。
- 刪除失敗 experiment、holdout usage、state events。
- 把 dirty/uncommitted code候選推過 gate。
- 修改 RiskEngine/OMS/accounting evidence以迎合策略。
- 載入外來 pickle/joblib/cache。
- 取得 Binance Testnet/live key。
- 直接把 LLM回答當 signal/order。
- 透過正式 CLI 使用`--force` promotion或跳 state（CLI根本不提供此旗標）；同使用者任意檔案寫入不在v1安全承諾內。
- 自動推進至 shadow/Testnet/live。

### 10.3 Runtime failure independence

- `nautilus-data sync/status` 不 import任何 hybrid/research module。
- research package不是 root runtime dependency。
- Candidate verifier/replay只依賴 Strategy Core + Nautilus。
- Hermes、research venv或 PyBroker損壞時，daily data sync仍須通過原有 87+ tests 與 live status。

---

<!-- PLAN_SECTION:APPENDIX-ROLLOUT -->

## 11. 分階段 rollout、entry/exit gate 與 rollback

| Stage | 內容 | 對應 Tasks | Entry | Exit evidence | Hard veto / rollback |
|---|---|---|---|---|---|
| 0 | 基線／legal 文件、Funding observation root-fix、versioned migration、Nautilus accounting | Task 0、Task 3 | 漢秦哥已批准；clean main + baseline green | Funding boundary／mark／fee／accounting、atomic cutover／rollback、full suite 與自然排程全綠 | 任一語意不明、半切換或資料回歸即停止；不得建立 research env |
| 1 | 隔離 research environment | Task 1 | Stage 0 audit PASS 且漢秦哥另行批准 | Python 3.12 research lock 可重建；root dependency 不變 | lock 不可重建或污染 root 即停止 |
| 2 | Strategy Core + immutable snapshot | Task 2、Task 4 | Stage 1 green 且另行批准 | core 在兩 env 同測；snapshot deterministic／tamper-proof | data regression、時間邊界或 hash 不穩即停止 |
| 3 | Guards、PyBroker split spike、experiment ledger 與排序 | Task 5、Task 6、Task 7 | Stage 2 green 且另行批准 | cache／network guard、fold discipline、失敗留存、同實驗重現；第一輪不淘汰候選 | 碰 catalog、產生 cache、leakage 或結果漂移即停 |
| 4 | Candidate Capsule、framework-neutral verify、Nautilus authoritative replay | Task 8、Task 9、Task 10 | Stage 3 green 且另行批准 | hash／provenance／parity／causality／tamper、fees／funding／accounting 與雙重 replay hash 全綠 | 任一 mismatch 即 retire candidate；不得放寬容差或後補 PnL |
| 5 | Evidence promotion、reason codes 與離線總驗活 | Task 11、Task 12 | Stage 4 green 且另行批准 | 只憑機器 evidence 推進至 `nautilus_reproduced`；offline verifier fail closed | AI 可跳 gate 或驗活不能阻擋 push 即停止 |
| 6 | End-to-end reference + 至少 12 個雙路徑攻擊型試點／採用判決 | Task 13 | Stage 5 green 且另行批准 | routing recall 100%、operational replay -50%，且時間 -30% 或吞吐 2x | 未達即限制使用或完整移除 |

Shadow／Testnet／live 是本計畫之外的另案，不是 Stage 7，也不會由任何 Stage 自動釋放。

---

<!-- PLAN_SECTION:APPENDIX-TASKS -->

## 12. 逐 task 實作計畫（TDD + frequent commits）

<!-- PLAN_SECTION:TASK-0 -->

### Task 0：封存架構決策與 legal scope

**Objective:** 先把 Hybrid 邊界、license、非目標寫進 repo，防止後續 Agent 把 PyBroker 擴進 runtime。

**Files:**
- Create: `docs/architecture/hybrid-pybroker-nautilus.md`
- Create: `docs/contracts/candidate-capsule-v1.md`
- Modify: `THIRD_PARTY_NOTICES.md`
- Modify: `README.md`

**Steps:**
1. 將本計畫第 2、5、8、9、10 節轉成穩定文件，不把研究報告全文複製進 repo。
2. 在 notice 寫 `lib-pybroker 1.2.14 — Apache-2.0 with Commons Clause` 及 internal research pilot 限制。
3. README 明示 runtime/research 雙 env、Nautilus唯一 truth、第一輪不接 live。
4. 精確 stage 本 Task 擁有的檔案後執行：`.venv/bin/python scripts/check_secrets.py --staged`
5. Run baseline: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v`
6. Expected: secrets exit 0；至少原有 87 tests全綠。
7. Commit: `docs: define PyBroker hybrid pilot boundaries`

<!-- PLAN_SECTION:TASK-1 -->

### Task 1：建立完全隔離的 research environment

**Objective:** 讓 PyBroker只存在 Python 3.12 research艙，root `.venv` dependency graph不變。

**Files:**
- Create: `research/.python-version`
- Create: `research/pyproject.toml`
- Create: `research/uv.lock`
- Create: `research/src/pybroker_lab/__init__.py`
- Create: `research/tests/test_environment.py`

**Dependency pins:**

```toml
requires-python = ">=3.12,<3.13"
dependencies = [
  "lib-pybroker==1.2.14",
  "numpy==2.5.2",
  "pandas==3.0.5",
  "scikit-learn==1.9.0",
]
```

Task 1先以這四個第三方依賴建立可重建 research env；Task 2建立Strategy Core後才把`nautilus-strategy-core` local source加進兩環境並重建兩份lock。不得用不存在的placeholder dependency，也不得在root pyproject加入PyBroker。

**TDD steps:**
1. Write failing test：assert Python 3.12、`pybroker.__version__ == "1.2.14"`、sklearn 1.9.0；assert root env `find_spec("pybroker") is None`；以`inspect.signature(Strategy.walkforward)`鎖住stable 1.2.14確實接受`seed`。
2. Run research test，Expected: FAIL（env/package尚不存在）。
3. 建立不含local core的research pyproject與lock；不可用不可攜absolute file URL。
4. Run: `/Volumes/ExpansionDrive/.nautilus-tools/uv-0.11.33/bin/uv lock --project research --python 3.12`
5. Run: `UV_CACHE_DIR=/tmp/nautilus-research-uv-cache /Volumes/ExpansionDrive/.nautilus-tools/uv-0.11.33/bin/uv sync --project research --locked`
6. Run tests，Expected: PASS。
7. Re-run root baseline，Expected: root仍無 PyBroker且 87+ tests全綠。
8. Commit: `build: isolate PyBroker research environment`

<!-- PLAN_SECTION:TASK-2 -->

### Task 2：建立零依賴 Strategy Core

**Objective:** 同一份 feature/model/policy語意供 research與Nautilus引用，消除 LLM逐候選翻譯漂移。

**Files:** 依第 4 節 `packages/strategy-core/` 全部檔案。

**Also modify:**
- `pyproject.toml`、`uv.lock`：加入`nautilus-strategy-core` local source，仍不加入PyBroker/pandas/NumPy/sklearn。
- `research/pyproject.toml`、`research/uv.lock`：加入同一`nautilus-strategy-core` local source。

**TDD steps:**
1. Write failing tests for：warmup不足、feature formulas、zero-volume variance、NaN拒絕、model feature order、stable sigmoid、LONG/FLAT hysteresis、canonical hash。
2. Write import-boundary test：import core後 `sys.modules` 不含 PyBroker/Nautilus/pandas/NumPy。
3. Run with root env，Expected: FAIL。
4. 寫最少 dataclass/enum/pure functions；不建 registry/plugin abstraction。
5. 將local source同時接入root/research pyproject，重建兩份lock並以`--locked` sync；Strategy Core沒有第三份虛構lock，以`strategy_core_tree_hash`辨識。
6. Run root tests，Expected: PASS且root仍找不到PyBroker/pandas/NumPy/sklearn。
7. Run同一 suite with research env，Expected: PASS且 vectors/hash完全相同。
8. 加一個 cross-process fixture：兩 env各輸出 JSON結果，byte compare。
9. Commit: `feat: add framework-neutral strategy core`

<!-- PLAN_SECTION:TASK-3 -->

### Task 3：修正 Funding observation contract 並完成 Nautilus accounting spike

**Objective:** 在封存任何 snapshot 前，完整保留 Binance Funding dataset 的結算 mark price、釘住 historical rate 的正確 settlement boundary，並證明 Nautilus 真的按該語意入帳。

**Files:**
- Create: `src/nautilus_quant/funding_observation.py`
- Create: `tests/test_funding_observation.py`
- Create: `src/nautilus_quant/backtest.py`（先只放最小 fixture runner）
- Create: `tests/test_backtest_funding.py`
- Create: `config/backtest_pilot.json`
- Modify: `src/nautilus_quant/binance_public.py`
- Modify: `src/nautilus_quant/nautilus_io.py`
- Modify: `src/nautilus_quant/sync.py`
- Modify: `src/nautilus_quant/service.py`
- Modify: `src/nautilus_quant/cli.py`
- Modify: `tests/test_binance_public.py`
- Modify: `tests/test_nautilus_io.py`
- Modify: `tests/test_sync.py`
- Modify: `tests/test_verify_smoke.py`
- Modify: `tests/test_cli.py`
- Modify: `scripts/verify_smoke.py`

**TDD steps:**
1. 先寫 API payload boundary tests：每個 Funding row 必須有 `fundingTime`、finite `fundingRate`、positive finite `markPrice`；optional `rateType` 原樣保存。缺欄、重複、衝突都 fail closed。
2. 建立versioned `FundingObservation` / store，以canonical JSONL原子保存`instrument_id + funding_time_ns + rate + mark_price + rate_type`；新canonical path為`data/funding/<instrument-id>.observations.v1.jsonl`。不得由既有native event檔反推/偽造mark price。
3. 以兩個不相等連續 rates 寫 failing test，決定 Nautilus historical `FundingRateUpdate` 應如何綁定自身 `fundingTime`；現有「row n 的 `next_funding_ns = row n+1 timestamp`」若造成整期位移必須在共用 `funding_events()` 根修。
4. 建 synthetic USD-M perpetual、known starting balance、known long/short notional、兩個明確官方 mark prices；在第一/第二 boundary 前後分別開平倉。
5. Assertions：每個 rate只在自身已驗證 boundary 套用一次；long正rate精確debit、short相反、flat不變；mark price優先，缺mark fixture才測 top-of-book fallback；fee與funding分欄。
6. Run targeted tests，Expected: RED，且failure必須來自尚未保存mark/boundary尚未釘住，不是import typo。
7. 用 Nautilus 2.0.0rc2 最小公開 API 實作；不得自己在報表後補一筆「假 funding」。Assert account events、ending balance與每個 settlement timestamp。
8. 新增一次性`nautilus-data migrate-funding-observations --config ...`：與daily sync共用`var/locks/data-sync.lock`，由官方Funding endpoint從configured start重抓全部symbols到temp paths，逐列/coverage/readback驗證後atomic rename並寫ready manifest。任一symbol失敗則不建立ready manifest、不切換reader/writer。
9. Cutover後`run_sync`、status與`verify_smoke.py`只把observation store當canonical funding truth；同步修改smoke：驗timestamp/rate/mark完整性，並依spike定案驗每個rate綁自身boundary，刪除「row n必須指向row n+1 timestamp」舊假設。既有`data/funding/*.jsonl`保持原封不動作短期rollback evidence，但不再讀寫；pilot採用判決後刪除legacy reader/code，不留長期雙寫相容層。
10. 在現有LaunchAgent下一個排程前完成migration + immediate `nautilus-data sync/status`驗活；若migration/cutover失敗，revert code並沿用未改動legacy files，不能留下半切換狀態。
11. 真實replay只在report寫出`funding_price_source=binance_funding_history_mark_price`時可作正式帳務；缺mark時狀態只能是`modeled_funding`且阻擋正式績效宣稱。
12. Run twice，canonical result hash相同；再跑原有full suite與bounded smoke fixture。
13. 若boundary/mark/accounting無法通過：停止Task 4及後續replay，記錄blocker，不以手工PnL adjustment繞過。
14. Commit（僅green時）: `fix: preserve and settle Binance funding observations`

<!-- PLAN_SECTION:TASK-4 -->

### Task 4：建立 deterministic snapshot create/verify

**Objective:** 將 Nautilus catalog + 已驗證 Funding observations 轉成 PyBroker可讀、不可變、帶hash的D-1輸入。

**Files:**
- Create: `src/nautilus_quant/snapshot.py`
- Create: `src/nautilus_quant/hybrid_cli.py`
- Create: `tests/test_snapshot.py`
- Create: `tests/test_hybrid_cli.py`
- Modify: `pyproject.toml`（只新增 `nautilus-hybrid` entry point；local strategy-core已在Task 2加入）
- Modify: `uv.lock`

**TDD steps:**
1. 以temp catalog + FundingObservation fixture寫failing tests：bar/funding gap、duplicate、OHLC/mark invalid、future/incomplete day、bar close-boundary inclusion、funding boundary inclusion、tamper、symlink、idempotent bytes。
2. Run targeted tests，Expected: FAIL。
3. 實作canonical JSONL、payload/manifest hash、atomic directory write、single-writer lock、read-only payload；snapshot ID按第6節重建，無自我雜湊。
4. CLI：
   - `.venv/bin/nautilus-hybrid snapshot create --config config/market_data.json --symbol BTCUSDT --interval 1h --as-of YYYY-MM-DD`
   - `.venv/bin/nautilus-hybrid snapshot verify --snapshot var/snapshots/<id>`
5. `nautilus-data`既有CLI/排程不得import hybrid module；snapshot只讀其outputs與sync evidence。
6. Run targeted + full root tests，Expected: PASS。
7. 用bounded fixture重跑兩次，assert相同snapshot ID/hash、末根1h bar正好在`close_boundary`、funding rate/mark綁同一boundary。
8. Commit: `feat: export immutable research snapshots`

<!-- PLAN_SECTION:TASK-5 -->

### Task 5：Research guards 與 snapshot DataFrame adapter

**Objective:** 確保 PyBroker只用 snapshot、不建 cache、不意外連網或繼承交易憑證。

**Files:**
- Create: `research/src/pybroker_lab/guards.py`
- Create: `research/src/pybroker_lab/data.py`
- Create: `research/tests/test_guards.py`
- Create: `research/tests/test_data.py`

**TDD steps:**
1. Tests：拒絕 catalog path、snapshot hash錯誤、額外/缺列、非遞增 timestamp、NaN；轉成PyBroker要求的 `date/symbol/open/high/low/close/volume`。
2. Tests：runner啟動立刻呼叫三個 `disable_*_cache()`；temp HOME下不得產生 PyBroker/diskcache檔。
3. Tests：清除 known `BINANCE_*`/API credential env；socket connect在research run期間raise。
4. 實作 allowlisted env與accidental-network guard；明示這不是惡意程式sandbox。
5. Run research tests，Expected: PASS。
6. Re-run root tests，Expected:不受影響。
7. Commit: `feat: guard isolated PyBroker research inputs`

<!-- PLAN_SECTION:TASK-6 -->

### Task 6：自家 folds + PyBroker API compatibility spike

**Objective:** 先鎖住時間切分，再決定能否安全使用 PyBroker built-in walk-forward training。

**Files:**
- Create: `research/src/pybroker_lab/folds.py`
- Create: `research/tests/test_folds.py`
- Create: `research/tests/test_pybroker_compat.py`

**TDD steps:**
1. Tests：train/purge/test/embargo單調且不重疊；purge/embargo皆至少6 bars；每個training label availability不晚於train cutoff；無future leakage。
2. 先以`inspect.signature`實證stable 1.2.14的`walkforward`包含`seed`，再用tiny deterministic DataFrame實跑 `Strategy.walkforward(windows=..., lookahead=6, shuffle=False, calc_bootstrap=False, disable_parallel=True, seed=42)`；若未來釘版signature改變，fail-fast，不猜top-level seed API。
3. 比較 `walkforward_split` 實際 indices與我們的 fold manifest。
4. 若能精確映射：封裝 adapter並釘測試。
5. 若不能：將 decision記錄為 fallback，改用自家 fold逐段training，再用 PyBroker `Strategy(...).add_execution(models=...)` 做 test-slice execution；不得把邊界放寬到「差不多」。
6. 相同 subprocess重跑兩次，predictions/indices byte-identical。
7. 輸出fixture training ledger與獨立OOF trace，證明後續verifier有真實cross-implementation evidence。
8. Commit: `test: pin PyBroker walk-forward semantics`

<!-- PLAN_SECTION:TASK-7 -->

### Task 7：實作 reference ML experiment 與保留式 ledger

**Objective:** 完成 BTCUSDT 1h logistic reference experiment，成功與失敗都可追溯。

**Files:**
- Create: `research/config/btcusdt_1h_logistic_v1.json`
- Create: `research/src/pybroker_lab/model.py`
- Create: `research/src/pybroker_lab/runner.py`
- Create: `research/src/pybroker_lab/artifacts.py`
- Create: `research/src/pybroker_lab/cli.py`
- Create: `research/tests/test_model.py`
- Create: `research/tests/test_runner.py`
- Create: `research/tests/test_artifacts.py`
- Modify: `research/pyproject.toml`（console script）

**TDD steps:**
1. Failing model test：sklearn訓練結果匯出 canonical JSON後，shared stdlib inference與 sklearn `predict_proba`在容差內一致。
2. Failing ledger tests：success/failure/duplicate attempts全保留；atomic write；中斷留下failed evidence而非消失；每fold精確training/label/test row IDs與label availability可讀回。
3. 實作 `pybroker.model(...) -> ModelSource`，由 `Strategy.add_execution(models=...)` 接入；禁止呼叫不存在的 `Strategy.add_model()`。
4. `calc_bootstrap=False`；所有 metrics加 `truth_status=provisional`。
5. CLI：`research/.venv/bin/pybroker-research run --snapshot <path> --spec research/config/btcusdt_1h_logistic_v1.json`。
6. CLI必須是one-shot process；不做daemon/reuse worker。
7. 用fixture實跑相同spec兩次：experiment key相同、attempt IDs不同、扣除attempt provenance後的training ledger/OOF/model canonical outputs相同。
8. 故意讓trainer失敗一次，assert failure被保留且下一次run不受 `StaticScope`污染。
9. Commit: `feat: run reproducible PyBroker ML experiments`

<!-- PLAN_SECTION:TASK-8 -->

### Task 8：建立 Candidate Capsule builder 與 schema validator

**Objective:** 將 screened experiment轉為無可執行payload、可獨立驗證的candidate。

**Files:**
- Create/Modify: `packages/strategy-core/src/nautilus_strategy_core/capsule.py`
- Create/Modify: `packages/strategy-core/tests/test_capsule.py`
- Modify: `research/src/pybroker_lab/artifacts.py`
- Modify: `research/src/pybroker_lab/cli.py`
- Modify: `research/tests/test_artifacts.py`

**TDD steps:**
1. Tests for exact seven files、`SHA-256(canonical(manifest_without_candidate_id))`、extra file、symlink、traversal、dirty commit、unknown model、NaN、tamper；另測manifest與payload任一byte改動都使candidate ID失配。
2. 用local bare Git remote fixture測source-ref gate：只有當`refs/tags/candidate-source/<full-commit>`存在、peeled/readback commit等於manifest commit，builder才可建立candidate；missing/moved ref皆fail closed。
3. 真實candidate建立前，controller先將clean source commit推成不可重用命名的lightweight tag：`git push origin <commit>:refs/tags/candidate-source/<full-commit>`，再以`git ls-remote origin refs/tags/candidate-source/<full-commit>`讀回完全相同commit。這個tag是候選可重建根，不得刪除或force-update；manifest記錄source ref與commit。
4. Builder只從completed experiment + 已遠端讀回的clean Git commit建立新candidate；已存在ID只可byte-identical no-op。
5. 帶入training ledger與完整OOF trace；golden vectors至少256列並按第8.6節選樣。
6. sklearn object不落盤；掃描candidate目錄不得出現pickle/joblib/pyc。
7. CLI：`pybroker-research candidate --attempt <id>`。
8. 用fixture建立兩次，assert candidate ID與payload hashes相同。
9. Commit: `feat: package immutable strategy candidates`

<!-- PLAN_SECTION:TASK-9 -->

### Task 9：實作 framework-neutral `candidate verify`

**Objective:** 在完全沒有 PyBroker/pandas/sklearn的 runtime env重算候選語意。

**Files:**
- Create: `src/nautilus_quant/candidate.py`
- Create: `tests/test_candidate.py`
- Modify: `src/nautilus_quant/hybrid_cli.py`
- Modify: `tests/test_hybrid_cli.py`

**TDD steps:**
1. Failing tests涵蓋manifest/hash/model/feature/prediction/intent parity。
2. 從snapshot重建labels/folds，逐列驗training ledger與label availability；故意把future row放進train必fail。
3. 在隔離research subprocess用recorded rows/config做deterministic retrain；比較coefficients/intercept與完整OOF trace，再由runtime shared core比較同一OOF。
4. Prefix-causality test：完整snapshot與每個prefix結果一致。
5. Future-mutation test：改變 decision timestamp後資料，不影響之前輸出。
6. Tamper table逐檔修改一byte，全部fail closed。
7. Runtime verification phase assert `find_spec("pybroker")/pandas/numpy/sklearn is None`；只有明確retrain subcommand進research env。
8. CLI：`.venv/bin/nautilus-hybrid candidate verify --candidate <path>`；stdout只輸出structured JSON summary，非零exit表示fail。
9. 同candidate verify兩次，canonical report hash一致。
10. Commit: `feat: verify training provenance parity and causality`

<!-- PLAN_SECTION:TASK-10 -->

### Task 10：Nautilus Strategy adapter 與 authoritative replay

**Objective:** 讓同一 Strategy Core 在 Nautilus 事件迴路中產生intent，再由Nautilus決定部位、orders、fees、funding與帳務。

**Files:**
- Create: `src/nautilus_quant/strategy_adapter.py`
- Expand: `src/nautilus_quant/backtest.py`
- Create: `tests/test_strategy_adapter.py`
- Create: `tests/test_backtest.py`
- Modify: `config/backtest_pilot.json`
- Modify: `src/nautilus_quant/hybrid_cli.py`

**TDD steps:**
1. Adapter tests：warmup、bar close timing、LONG/FLAT transition、duplicate bar、out-of-order bar、single-price bar、stop cleanup。
2. Assert adapter不讀PyBroker artifact除candidate JSON；不接受candidate quantity/order instructions。
3. Backtest fixture明示 starting balance、MARGIN account、NETTING OMS、fixed pilot notional、fee/fill/bar-execution/funding assumptions。
4. Replay先完成signal trace parity，再允許下單；parity fail時不得啟動engine execution。
5. Assertions：orders/fills/positions/account balance、fee/funding明細、期末open state、rejection reasons。
6. CLI：`.venv/bin/nautilus-hybrid replay --candidate <id> --config config/backtest_pilot.json`。
7. 同輸入重跑兩次，去除run timestamp後report hash一致。
8. PyBroker與Nautilus PnL差異只作diagnostic，不作parity gate；intent mismatch則hard fail。
9. Commit: `feat: replay candidates authoritatively in Nautilus`

<!-- PLAN_SECTION:TASK-11 -->

### Task 11：Hash-chained cooperative promotion state 與回饋碼

**Objective:** 讓合作式 Agent 只能透過正式CLI與機器證據推進至 `nautilus_reproduced`；不把同使用者檔案宣稱為對抗式安全邊界，且正式CLI不實作任何live transition。

**Files:**
- Create: `src/nautilus_quant/promotion.py`
- Create: `tests/test_promotion.py`
- Modify: `src/nautilus_quant/hybrid_cli.py`
- Modify: `tests/test_hybrid_cli.py`

**TDD steps:**
1. Tests：合法/非法transition、corrupt/truncated events、previous-hash/head mismatch、missing evidence、evidence hash mismatch、concurrent writer、retired不可透過正式CLI復活。
2. State由events hash-chain replay；atomic head只作tamper evidence，不冒充different-owner anchor，也不寫獨立current-state真值。
3. `candidate promote`每次重新讀verify/replay evidence，不信呼叫者傳入`passed=true`。
4. 只實作 through `nautilus_reproduced`；shadow之後回 `NOT_IMPLEMENTED_FOR_PILOT`。
5. Reason codes至少：`DATA_INCOMPLETE`、`HASH_MISMATCH`、`FEATURE_MISMATCH`、`PREDICTION_MISMATCH`、`INTENT_MISMATCH`、`LEAKAGE_DETECTED`、`FUNDING_ACCOUNTING_FAILED`、`NONDETERMINISTIC_REPLAY`、`EXECUTION_COST_DOMINATED`、`EXCESSIVE_TURNOVER`、`INSUFFICIENT_OOS`。
6. CLI：`.venv/bin/nautilus-hybrid candidate promote --candidate <id>` 與 `candidate status`。
7. Commit: `feat: gate candidate lifecycle with evidence`

<!-- PLAN_SECTION:TASK-12 -->

### Task 12：建立單一驗活入口與操作文件

**Objective:** 讓AI Agent不必猜測要跑哪些命令，且任何非輕量變更都有一個可執行檢查。

**Files:**
- Create: `scripts/verify_hybrid.py`
- Create: `tests/test_verify_hybrid.py`
- Create: `tests/test_git_hooks.py`
- Modify: `.githooks/pre-push`
- Modify: `ops/RUNBOOK.md`
- Modify: `README.md`
- Modify: `docs/architecture/hybrid-pybroker-nautilus.md`

**Verification script順序:**

1. secret scan
2. root unit tests
3. Strategy Core在root env測試
4. research env lock/version tests
5. Strategy Core在research env測試
6. research unit tests
7. bounded snapshot → experiment → capsule → verify → replay → promote fixture
8. assert root env沒有PyBroker/pandas/NumPy/sklearn
9. assert正式research runner拒絕catalog path、正常run沒有catalog file-open evidence，且已清除credential env；不宣稱能阻止同使用者惡意任意Python process
10. print one canonical JSON summary；任何一步fail即non-zero

**Steps:**
1. 先寫test/mock runner證明任一步失敗會傳遞non-zero。
2. 實作最少subprocess orchestration，不引入workflow engine。
3. Run: `.venv/bin/python scripts/verify_hybrid.py`
4. Expected: all stages `PASS`，summary含每階段duration/hash；不宣稱未跑的live data。
5. Run既有 `nautilus-data status`，Expected:仍可獨立工作。
6. 將既有`.githooks/pre-push`從只做secret scan改為依序執行：`check_secrets.py --pre-push`，再以root `.venv/bin/python`執行`verify_hybrid.py`；第二步非零必阻擋push。不得把Task 13真實D-1 pilot或任何外部API call塞進hook。
7. `tests/test_git_hooks.py`至少驗：hook保有executable bit、`sh -n`通過、secret scan先於offline verifier、任一command失敗時不繼續。`tests/test_verify_hybrid.py`以fake stages驗fail-fast與canonical summary。
8. 本pilot刻意不新增`.github/workflows`：repo目前沒有hosted CI，單一operator採既有local hook＋正式delivery遠端讀回即可；這不等於remote branch protection。若未來開放多人merge或unattended remote PR，必須另加GitHub Actions required check，不能拿本地hook冒充remote enforcement。
9. Commit: `test: add end-to-end hybrid verification`

<!-- PLAN_SECTION:TASK-13 -->

### Task 13：實跑 BTCUSDT 1h pilot 並作採用判決

**Objective:** 用真實D-1 snapshot證明整條生命週期，而不是只讓fixtures通過。

**Files:**
- Create: `docs/reports/pybroker-pilot-YYYY-MM-DD.md`
- Runtime evidence：只寫入gitignored `var/`；報告記錄其hash，不提交raw market data/model output。

**Steps:**
1. 確認Task 12 source commit乾淨、root/research locks與full verify全綠；將該commit先推到`refs/tags/candidate-source/<full-commit>`並以`git ls-remote`讀回，確認ref可持續取得。這一步在任何真實experiment/candidate前完成，不等pilot報告才push。
2. 建立BTCUSDT 1h D-1 snapshot並verify。
3. 執行固定reference experiment兩次，確認deterministic canonical output。
4. 建capsule，執行candidate verify。
5. 執行Nautilus replay兩次，確認accounting/funding與report hash。
6. Promote到`nautilus_reproduced`。
7. 在複本candidate中逐檔tamper，證明gate全部擋下；不得破壞正式artifact。
8. 記錄PyBroker screening與Nautilus authority差異及reason codes。
9. 做paired efficiency benchmark：預先封存至少12個受控hypotheses與Nautilus engineering-viability規則；**每一個**hypothesis都跑PyBroker前篩與直接Nautilus兩條路。PyBroker accept/reject decision先凍結，之後才揭露全量Nautilus結果作ground truth。依固定seed counterbalance執行順序；每次fresh process、PyBroker caches全關、相同snapshot/commit/hardware，分列cold-start與steady-state wall time、operational full-replay數、失敗定位時間及false negatives。
10. 寫採用報告，分列verified/inferred/unknown；不把reference策略包裝成alpha。
11. Commit: `docs: record verified PyBroker pilot decision`
12. Pilot報告commit完成後依正式delivery流程push `main`並遠端讀回；candidate source tag已在步驟1先行push。本計畫審閱階段本身不commit/push。

---

<!-- PLAN_SECTION:APPENDIX-TEST-MATRIX -->

## 13. 測試矩陣與可量測驗收

### 13.1 Correctness gates（全為 hard gate）

| Gate | Pass condition |
|---|---|
| Existing data foundation | 原有87 tests全綠；`nautilus-data status`獨立可用 |
| Dependency isolation | root env找不到PyBroker/pandas/NumPy/sklearn；research env無Nautilus runtime dependency |
| Snapshot | 0 gaps、0 duplicates、D-1完整、相同輸入相同hash、tamper必fail |
| Feature parity | 所有golden rows在容差內；0 missing/nonfinite |
| Prediction parity | 所有golden rows在容差內 |
| Intent parity | 100%逐列一致 |
| Causality | prefix與future-mutation tests 100%一致 |
| Artifact trust | 無symlink/extra/executable/pickle/joblib/cache；所有hash吻合 |
| Funding | 兩個不同rates/marks精確套在自身boundary；真實replay保存official funding-history mark price並有account events；缺mark只能`modeled_funding` |
| Replay | 同candidate/config兩次canonical report hash一致 |
| Promotion | 正式CLI無`--force`、驗完整hash chain且LLM文字不能替代evidence；文件明示同使用者任意改檔不是v1安全邊界 |
| Failure retention | 故意失敗attempt可讀回，且不污染下一process |
| Local delivery gate | 既有pre-push先secret scan再跑offline `verify_hybrid.py`；任一步非零即阻擋push；不宣稱為remote CI/branch protection |

### 13.2 Pilot adoption metrics

PyBroker是否留下，不看「策略看起來賺錢」，看是否真正讓研究工廠更有效：

- correctness hard gates：0 bypass、0 unexplained mismatch。
- 至少12個paired hypotheses的benchmark中，PyBroker前篩至少達成其一：
  - median end-to-end iteration wall time降低≥30%；或
  - 同等時間可篩hypotheses數≥2倍。
- 依事先封存規則被Nautilus判為engineering-viable的cases，PyBroker routing recall必須100%；任何false negative都是NO-GO，不用小樣本百分比粉飾。
- 在上述100% recall成立下，operational進入full Nautilus replay的候選數至少降低50%；benchmark為計算ground truth而做的補跑不計入operational replay數，但必須另列。
- 新增runtime依賴數：除local Strategy Core外為0。
- daily data sync不因research故障而失效。

這些閾值是pilot go/no-go，並非live alpha門檻。若未達成，刪除PyBroker adapter；不以「以後也許有用」保留。

### 13.3 Alpha/statistical gates（pilot後才校準）

- 不在本計畫硬寫Sharpe/return數字，避免先射箭後畫靶。
- 未來需依策略類型明示：minimum OOS samples/trades、turnover、fees/funding後績效、fold穩定度、concentration、drawdown、parameter sensitivity。
- 搜尋總次數與candidate family必進報告，避免multiple-testing winner被誤認為edge。
- sealed holdout與shadow forward evidence才可支援後續promotion；PyBroker bootstrap不算。

---

<!-- PLAN_SECTION:APPENDIX-RISK -->

## 14. 風險、對策與 rollback

| 風險 | 預防/偵測 | Rollback |
|---|---|---|
| PyBroker global `StaticScope`污染 | one experiment = one process；故意失敗後重跑測試 | 殺掉process；artifact標failed；不重用worker |
| Disk cache identity/pickle | 三類cache預設關；候選allowlist；禁止pickle/joblib | 刪research cache/venv；候選retire |
| Python/依賴衝突 | root 3.13與research 3.12分lock/venv | 整個刪`research/.venv`，runtime無變 |
| PyBroker帳本/報表不一致 | metrics provisional；Nautilus唯一accounting truth | 不晉級；不修補PyBroker Portfolio |
| Feature語意漂移 | shared pure core + golden parity | mismatch即retire；修core後產生新candidate ID |
| Time leakage/過擬合 | purge/embargo、prefix/future mutation、sealed holdout、trial ledger | family封存；只能等新forward data |
| Commons Clause | internal pilot；notice；付費hosted前legal review | 移除`research/` dependency；core/capsule不依賴PyBroker |
| Artifact竄改 | content hashes、read-only、symlink/path checks | fail closed；從experiment重建新capsule |
| Nautilus RC API變動 | pin 2.0.0rc2；funding/replay executable contracts | 升級另開PR；舊candidate不默默重跑新語意 |
| Funding資料或結算語意錯 | 保存Funding endpoint markPrice + 兩rate/兩boundary exact account test + true account events | 阻擋所有正式績效宣稱 |
| Same-user holdout並非強sandbox | workflow隔離、事件記錄；明示限制 | 若風險變高再上獨立帳號/container |
| JSONL後續過慢 | 先量測；pilot規模數百golden rows | 只有實測瓶頸才升Parquet/Arrow |
| PyBroker沒有提升效率 | matched pilot metrics | 完整移除adapter，不留compat layer |
| Research破壞daily data | import/dependency isolation + baseline tests | revert research commits；data/catalog不遷移、不刪除 |

### 14.1 完整移除路徑

若採用判決為NO-GO：

1. 停止所有research experiment process（v1沒有daemon/cron）。
2. 移除`research/`與PyBroker notices/文件段落。
3. 從任何verify script移除research stages。
4. 若Strategy Core/snapshot/candidate/replay對Nautilus自身仍有獨立價值，可保留；否則連同local dependency一併revert。
5. 不修改、不重寫、不遷移`data/`與既有run reports。
6. `var/research`可封存後刪除；先保留pilot report hashes。
7. 跑root full tests與live status，確認回到data foundation正常狀態。

---

<!-- PLAN_SECTION:APPENDIX-EXECUTION-DISCIPLINE -->

## 15. 實作順序與 review discipline

依賴順序：

```text
Stage 0（目前唯一批准）
  Task 0 docs/legal boundary
    → Task 3 funding observation/accounting/migration/cutover
    → independent Stage 0 audit + durable closeout

Stage 1（另行批准後）
  Task 1 isolated research env

Stage 2（另行批准後）
  Task 2 shared Strategy Core
    → Task 4 immutable snapshot

Stage 3（另行批准後）
  Task 5 guards/data
    → Task 6 folds/API spike
    → Task 7 experiment runner + candidate ranking（不得淘汰）

Stage 4（另行批准後）
  Task 8 capsule
    → Task 9 verifier
    → Task 10 Nautilus replay

Stage 5（另行批准後）
  Task 11 evidence state machine
    → Task 12 full offline verifier

Stage 6（另行批准後）
  Task 13 reference + ≥12 paired hypotheses + adoption decision
```

每個task：

1. fresh implementation worker或Codex worktree；不得多人同改同檔。
2. 先RED test，實跑確認是預期failure。
3. 最少production code轉GREEN。
4. spec-compliance review。
5. code-quality/security review。
6. targeted tests + relevant full suite。
7. clean diff review，確認未碰canonical data與無關dirty changes。
8. 小commit；下一task才開始。

Task 3、6 是硬 spike：紅燈時停下回報，不把 fallback 偷渡成既定實作。Stage 0 只執行 Task 0、Task 3 與獨立 audit／closeout；Task 1～13 的其餘工作不得因 dependency graph 自動開工，必須逐 Stage 取得漢秦哥批准。Task 13 前不得建立 cron、service、Testnet 或 live wiring。

---

<!-- PLAN_SECTION:APPENDIX-DESIGN-DEFAULTS -->

## 16. 審閱時只需拍板的四個設計決定

小蒨建議直接採下列defaults；若漢秦哥沒有異議，實作不再反覆詢問：

1. **研究版本：** `lib-pybroker==1.2.14` stable，不用master/dev。
2. **Pilot模型：** sklearn logistic regression；candidate只帶canonical JSON係數，runtime stdlib inference，不用pickle/ONNX。
3. **Golden vectors：** JSONL，不為數百列fixture引入PyArrow。
4. **第一輪終點：** 自動到`nautilus_reproduced`；shadow/Testnet/live另案，不能因pilot green自動上線。

這四項是已採用的技術 defaults，但不等於批准所有 Stage。現在只執行 Stage 0 的 Task 0→Task 3；Stage 0 audit PASS 後才向漢秦哥申請 Stage 1。PyBroker split hard spike 位於 Stage 3，必須等前兩個 Stage 另行批准並完成。

---

<!-- PLAN_SECTION:APPENDIX-DEFINITION-OF-DONE -->

## 17. 最終 Definition of Done

PyBroker導入只有在以下全部有真實tool output時才算完成：

- [ ] Root data foundation tests全綠，daily status可獨立工作。
- [ ] Research lock可從零重建，root env無PyBroker重依賴。
- [ ] 同一Strategy Core在兩env產生相同feature/model/policy vectors。
- [ ] D-1 snapshot有hash、完整性、idempotence與tamper證據。
- [ ] 成功與失敗experiments均保留，官方runner不覆寫或只留贏家。
- [ ] Candidate Capsule不含code/pickle/cache，且任一byte竄改必fail。
- [ ] Training ledger、deterministic retrain、OOF/feature/prediction/intent parity與causality gates全綠。
- [ ] Binance Funding observation保留mark price、boundary無一期間位移，Nautilus funding/fees/accounting有可執行斷言。
- [ ] 同candidate replay兩次canonical report hash相同。
- [ ] 正式state CLI只憑evidence推進、hash-chain可偵測一般損壞；同使用者任意改檔限制已明示，shadow前另升級信任邊界。
- [ ] Recorded source commit已push並遠端讀回；在該commit clean worktree與相符locks可重驗舊candidate。
- [ ] Local pre-push在offline verifier失敗時確實阻擋push；真實D-1 pilot與外部API未被塞入hook。
- [ ] 真實BTCUSDT 1h candidate到`nautilus_reproduced`。
- [ ] Pilot效率對照達到go/no-go門檻；否則已執行乾淨移除。
- [ ] 每個已批准 Stage 的完整 diff、tests、commit、push 與遠端讀回均已驗證；未批准 Stage 保持未執行。

沒有上述實證，就只能叫「正在導入」，不能叫完成。
