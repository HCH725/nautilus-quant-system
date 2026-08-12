# PyBroker × NautilusTrader Hybrid 邊界

## 決策

本專案採用 Nautilus-first 的雙環境架構。PyBroker 若在後續 Stage 獲准，只能作為可移除的上游 research adapter；NautilusTrader 保持 canonical data、權威回測、帳務、風控與 execution 的唯一真值。

目前只批准 Stage 0。此階段不安裝 PyBroker、不建立 research runtime，也不接 Shadow、Testnet 或 live。

## 執行環境

| 環境 | 責任 | 禁止事項 |
|---|---|---|
| Root runtime（Python 3.13） | Binance 公開資料、Nautilus replay、帳務與 deterministic gates | 不安裝 PyBroker、pandas、NumPy 或 scikit-learn；不接受 provisional metrics 當正式績效 |
| Research（未建立；規劃 Python 3.12） | 未來的 ML、walk-forward、provisional screening 與候選排序 | 不讀或改 canonical catalog；不持有憑證、交易權限或訂單指令；不得淘汰第一輪候選 |

兩側未來只共享零框架依賴的 Strategy Core 與不可變 Candidate Capsule。不得共享 framework 內部物件、cache、pickle、帳本或 credentials。Hermes、LLM 或 research process 故障時，既有 Nautilus 資料同步仍須獨立運作。

## Domain ownership

| Domain | 唯一 owner | Research 權限 |
|---|---|---|
| Binance public ingestion、D-1 completeness | Nautilus data foundation | 只讀已匯出的 immutable snapshot |
| Canonical bars 與 funding | Nautilus data foundation | 不得改寫 |
| Feature、model、policy 定義 | Framework-neutral Strategy Core | 只可透過有測試的 Git 變更 |
| Training、walk-forward、screening | PyBroker research adapter | 輸出一律 provisional |
| Candidate payload 與 provenance | Candidate Capsule contract | 只能新建，不可覆寫 |
| Feature、prediction、intent correctness | Deterministic verifier | 自評不算 gate evidence |
| Fills、fees、funding、positions、PnL、accounting | NautilusTrader | 無改寫權 |
| Risk、sizing、leverage、reduce-only、TIF | Nautilus runtime | 無改寫權 |
| Testnet/live credentials | Nautilus runtime（本輪不建立） | 絕對不可存取 |

## Strategy Contract v1

未來的 Strategy Core 只提供三個純函式語意：

```text
features(history, spec) -> feature vector or none
predict(model, features) -> score
policy(score, previous_intent, spec) -> LONG or FLAT
```

它不得 import PyBroker、NautilusTrader、pandas、NumPy，亦不得執行網路或檔案 I/O。Feature 只能使用 decision timestamp 當下可得的資料；不足 warmup 時不產生結果；NaN、Inf、重複 feature 或未知 model type 一律拒絕。Policy 只輸出 target intent，不建立 order，也不決定 quantity、leverage 或 TIF。

## Candidate 與 promotion

Candidate 必須符合 [`candidate-capsule-v1.md`](../contracts/candidate-capsule-v1.md)：只含 canonical JSON/JSONL 資料與 provenance，不含程式碼、import path、cache 或可執行序列化。

第一輪 lifecycle 最多自動到：

```text
candidate -> verified -> nautilus_reproduced
```

`shadow`、`testnet`、`live_candidate` 與 `live` 不在本輪授權範圍。任何 candidate 可轉為 `retired`。Promotion 只能依 deterministic evidence；LLM 文字、PyBroker metrics 或呼叫者自報 `passed=true` 均不能跳 gate。

## AI Agent 邊界

Agent 可提出 hypothesis、修改有測試的 Strategy Core、執行隔離研究、建立 capsule、驗證並要求 Nautilus replay。Agent 不得：

- 改寫 canonical catalog、funding、snapshot、帳務或 gate evidence；
- 刪除失敗 experiment、holdout usage 或 state events；
- 推進 dirty／uncommitted candidate；
- 載入外來 pickle、joblib 或 cache；
- 取得交易憑證或將 LLM 輸出直接作為 signal／order；
- 自動推進至 Shadow、Testnet 或 live。

## 停損與移除

任一 Stage 的 correctness、causality、accounting 或 reproducibility gate 失敗即停，不放寬容差或人工補 PnL。若 pilot 未量測到研究效率改善，移除 research adapter，不保留相容層，也不影響 Nautilus data/runtime。
