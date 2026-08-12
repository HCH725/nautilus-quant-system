# Candidate Capsule v1 Contract

Status：Stage 0 文件契約；尚未建立 runtime implementation，亦未授權 Stage 1。

Candidate Capsule 是 PyBroker research 與 Nautilus authoritative replay 之間的不可變資料邊界。它只攜帶普通 JSON/JSONL 與 provenance，不攜帶可執行內容或 framework 物件。

本階段 in scope 只有契約封存；capsule writer／verifier、research runtime、依賴與排程均 out of scope。

## Layout

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

只允許以上七個 regular files。拒絕額外檔案、symlink、device、executable bit、absolute path、path traversal，以及 `.py`、`.pkl`、`.pickle`、`.joblib`、shared cache 或任意 import path。

## `manifest.json`

必填：

- schema version、candidate ID、experiment／attempt／family／parent ID；
- repository URL、remote ref 與 clean source commit；`dirty=true` 是 hard veto；
- snapshot ID 與 snapshot manifest SHA-256；
- runtime lock hash、research lock hash、Strategy Core tree hash 與版本；
- symbol、timeframe、train／validation windows；pilot holdout 固定明示 `status: not_used`；
- label horizon、purge、embargo、seed；
- feature、model、policy schema versions；
- 下列六個非 manifest payload 的 byte size 與 SHA-256；
- source attempt ID 與其既有 timestamp。

Candidate ID 為 canonical `manifest_without_candidate_id` 的 SHA-256。該 body 已含所有 identity、provenance 與六個 payload hash；算出後才加入 `candidate_id`。Manifest 不列自己的 byte hash，避免 self-hash cycle。

Source commit 必須已推送至正式 `origin` 並以遠端讀回確認 object ID。日後 verifier 必須能以 recorded commit、locks 與 Strategy Core tree 重建；無法重建時回報 `UNREPRODUCIBLE_ENV`，不得沿用舊 green report。

## Payload contracts

### `strategy.json`

只放 feature set ID／version／parameters、model type／version、ordered feature names、policy thresholds、allowed intents、decision timing 與 runtime sizing profile reference；不得放 Python。

### `model.json`

v1 只允許 `standardized_logistic_binary_v1`，包含 ordered feature names、scaler mean／scale、coefficients、intercept、binary classes 與 training library provenance。所有數值必須 finite，feature names 不得重複。Research training object 不得以 pickle 形式進 capsule。

### `training_ledger.json`

記錄每個 fold 的精確 train feature row IDs、label row IDs、label availability timestamps、test row IDs、purge／embargo boundaries、trainer config 與 coefficient hash。

### `oof_trace.jsonl`

由 sklearn／PyBroker training path 直接輸出的完整 out-of-fold prediction／intent trace；不得由 shared Strategy Core 回填。

### `golden_vectors.jsonl`

至少 256 列，涵蓋 warmup 後第一列、每個 fold boundary 前後、接近 entry／exit threshold 的列、development window 的 deterministic evenly spaced samples，以及最末可決策列。每列含 timestamp、ordered features、score、previous intent 與 target intent；runtime 必須從 snapshot 重算。

### `screening.json`

必須明示 `truth_status: "provisional"`，並保存 fold boundaries、seed、feature／model／policy spec、trial count、family／parent identity、每 fold predictions／intents／trades／metrics、所有失敗與理由、PyBroker／Python version 與 research lock hash。它不得被當作 Portfolio、accounting 或 live promotion 真值。

## Verification gates

Verifier 必須 fail closed，至少檢查：

1. layout、regular-file、permission、schema、size、hash、candidate ID 與 provenance；
2. snapshot 完整且未變；source commit 可由 `origin` 讀回並重建；
3. golden feature／prediction tolerance 與 intent exact parity；
4. prefix causality 與 future-mutation invariance；
5. 從 snapshot 重建每個 fold 的 features、labels、availability、purge／embargo；
6. deterministic retrain 後 coefficients、intercept 與完整 OOF trace 吻合；
7. 同一輸入兩次 verification 的 canonical report hash 相同；
8. 任一 payload byte 被改動即非零退出。

Capsule 完成後設為 read-only。Promotion state 必須寫在 capsule 外部，不得改寫 capsule。

## 永久紅線與 Promotion boundary

本輪正式狀態只允許：

```text
candidate -> verified -> nautilus_reproduced
```

第一輪 PyBroker 只有排序／建議權，沒有淘汰權。NautilusTrader 是 fills、fees、funding、positions、PnL 與 account 的唯一真值。Shadow、Testnet 與 live 不在本 contract 的 operational authorization 內。
