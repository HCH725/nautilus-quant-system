# PyBroker × NautilusTrader 導入總計畫

<!-- PLAN_ID:PYBROKER-NAUTILUS-ADOPTION-V1 -->
<!-- PLAN_SECTION:META -->

## 文件身分與優先順序

- `plan_id`: `PYBROKER-NAUTILUS-ADOPTION-V1`
- Canonical path: `docs/plans/pybroker-nautilus-adoption.md`
- Engineering appendix: `docs/plans/pybroker-nautilus-implementation-appendix.md`
- 專案 Board: `pybroker-nautilus-adoption`
- 初始批准範圍：Stage 0；Stage 1～6 需逐階段另行放行

本文件是漢秦哥唯一需要審閱的導入計畫，也是專案範圍、責任邊界、階段順序、停損條件與採用判決的最高真值。工程附錄只補充如何施工與驗證，不得改寫本文件的目的與邊界。

若兩份文件、Kanban 卡片或實作互相矛盾，立即停止該卡；不得由 worker 自行猜測或選擇較方便的版本。

Kanban 卡片必須以完整 Git commit SHA、檔案路徑與本文件的穩定 `PLAN_SECTION` ID 指向批准版本。計畫不嵌入自己的 commit SHA，避免自我引用；SHA 由每張卡的 `plan_ref` 釘住。

---

<!-- PLAN_SECTION:DECISION -->

## 先說結論

採用 **Conditional Go**：

- PyBroker 是隔離的研究前端，負責特徵、ML、walk-forward、實驗留存與候選排序。
- NautilusTrader 是唯一正式資料、成交、手續費、Funding、部位、帳務與回測真值。
- 第一輪 PyBroker **只能排序與建議，不能淘汰任何候選**。
- 第一輪終點是候選在 NautilusTrader 完成可重現的正式回測，即 `nautilus_reproduced`。
- Shadow、Testnet、實盤、交易金鑰與訂單提交不在本計畫授權範圍。

目前施工從 Stage 0 開始。Stage 0 未通過前，不安裝 PyBroker、不建立 research runtime，也不往上蓋研究前端。

```text
AI Agent 提出策略想法
        ↓
隔離 PyBroker 研究艙：特徵／ML／walk-forward／排序
        ↓
純資料 Candidate Capsule
        ↓
NautilusTrader 重算與正式回測
        ↓
固定失敗原因回饋 AI Agent
```

這是一套系統、一條生命週期，不是兩套互相競爭的帳務或交易系統。

---

<!-- PLAN_SECTION:WHY -->

## 為什麼導入 PyBroker

NautilusTrader 適合精確回測、帳務、風控與未來執行，但大量嘗試特徵、模型與時間序列切分的研究成本較高。PyBroker 的價值在於降低研究摩擦，不是提供第二份績效真相。

本輪要驗證的不是「PyBroker 能不能跑」，而是它能否在不犧牲正確性的前提下：

1. 讓 AI Agent 更快建立可重現的研究實驗；
2. 保存成功與失敗嘗試，避免重複踩坑；
3. 正確排序值得送入 NautilusTrader 的候選；
4. 減少 operational Nautilus 完整回測數；
5. 若沒有實質價值，可完整移除而不傷及 Nautilus 系統。

---

<!-- PLAN_SECTION:ARCHITECTURE -->

## 目標架構與資料流

```text
Binance USD-M public API
        ↓
Nautilus canonical bars + Funding observations
        ↓
固定 D-1、帶雜湊、唯讀 Immutable Snapshot
        ├──────────────→ PyBroker 研究與候選排序
        │                         ↓
        │                 Candidate Capsule
        │                         ↓
        └──────────────→ NautilusTrader 正式驗證
```

物理上分成兩個環境：

- 正式 Nautilus runtime：Python 3.13；不新增 PyBroker、pandas、NumPy 或 scikit-learn。
- PyBroker research：Python 3.12；獨立 `research/pyproject.toml`、`research/uv.lock` 與 `.venv`。

兩側只共享：

- 零框架依賴的 Strategy Core；
- immutable snapshot；
- 純 JSON／JSONL Candidate Capsule；
- 可重算、可拒絕的機器證據。

不共享 PyBroker／Nautilus 內部物件、cache、pickle、帳本或憑證。

---

<!-- PLAN_SECTION:INVARIANTS -->

## 永久不可違反的邊界

1. NautilusTrader 是唯一正式帳務與回測真值。
2. PyBroker 不得成為 canonical data source，也不得改寫 catalog 或 Funding store。
3. PyBroker 不得持有 API key、交易權限、訂單指令或 runtime credentials。
4. 第一輪 PyBroker 只能排序與建議，沒有候選淘汰權。
5. 每一個 Stage 都是獨立 gate；紅燈即停，不以人工調整數字或文字說服繞過。
6. Candidate 不得含 `.py`、`.pkl`、`.pickle`、`.joblib`、cache、任意 import path 或可執行 payload。
7. PyBroker metrics 一律標記 provisional；正式損益、費用、Funding 與帳務只看 Nautilus。
8. Hermes、LLM 或 research process 故障時，既有 Nautilus 資料同步仍須獨立運作。
9. 成功與失敗 experiment 都要留存，不得只保存贏家。
10. 不新增 Dashboard、微服務、MCP、queue、DB 或第二套排程來完成第一輪 pilot。
11. 不做 Qlib、AutoML、深度學習、多商品、多 timeframe 或 portfolio optimizer。
12. Shadow、Testnet、live 另案；任何 Stage green 都不構成自動上線授權。
13. PyBroker 授權為 Apache 2.0 with Commons Clause；本輪只准內部研究 pilot。未來付費 hosting、顧問或商品化須另做正式法律審查。
14. 所有非平凡變更必須有可執行驗證；卡片 `done` 不是完成證據。
15. 正式 remote 已設定時，完成預設包含驗證、commit、push 與遠端讀回；紅燈或敏感／實跑資料不得推送。

---

<!-- PLAN_SECTION:PILOT -->

## 第一個試點範圍

- Instrument：`BTCUSDT-PERP.BINANCE`
- Bar：`BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL`
- Direction：做多／空手，不做放空
- Model：`StandardScaler` + Logistic Regression
- Seed：42
- 資料：既有 Binance Trade Kline 與 Funding observation
- Decision timing：bar `t` 收妥後計算，最早在下一個可執行事件作用
- 終點：候選到 `nautilus_reproduced`

本輪不以找到可上線 alpha 為成功標準。第一目標是正確性、證據鏈、可移除性與研究效率。

---

<!-- PLAN_SECTION:STAGE-0 -->

## Stage 0：先把 Nautilus 裁判席修好

**工作量：3～5 個工程日。**

### 要做的事

1. 封存 Git、套件、既有測試與每日資料排程基線。
2. 修正 Funding 資料契約：每筆保存自己的 `fundingTime`、`fundingRate` 與 Binance 同筆 `markPrice`，不得把本期 rate 延後一期結算。
3. 從 Binance 官方 Funding History 重建 versioned observation store。
4. 先寫暫存路徑、逐列與 coverage 驗證、ready manifest，再原子切換；舊資料短期保留作 rollback evidence，不做永久雙寫。
5. 建立最小 deterministic Nautilus backtest：多／空、正／負 Funding、flat、mark price、手續費、帳戶增減與重跑一致性。
6. 完成 migration、immediate sync、status、smoke 與下一次自然排程驗活。

### 通過條件

- Funding 時間、價格、方向與金額符合已知數值。
- 每個 rate 只在自己的 boundary 套用一次。
- 正式 replay 使用 Funding History 同筆 `markPrice`；缺 mark 時只能標記 `modeled_funding`，不得宣稱正式帳務真值。
- 新舊資料可安全回退，不留半切換狀態。
- 每日資料流程不依賴 Hermes／LLM／Dashboard。
- targeted tests、完整 suite、bounded smoke 與資料 readback 全綠。

### 停損條件

任一時間／價格／方向／帳務語意無法以 executable test 定案，或 migration 無法 fail closed，立即停止 Stage 0；Stage 1 不得開始。

---

<!-- PLAN_SECTION:STAGE-1 -->

## Stage 1：建立完全隔離的 PyBroker 研究艙

**工作量：1～2 個工程日。**

### 要做的事

- 建立獨立 `research/`、Python 3.12、`lib-pybroker==1.2.14`、獨立 lock 與 venv。
- 正式 root `.venv` 保持 Python 3.13 + NautilusTrader，不加入 PyBroker 重依賴。
- 研究 runner 不碰金鑰、Testnet、live、canonical catalog 或變動中資料。
- 不共用 cache；不輸出 pickle／joblib；每個 experiment 使用全新 process。

### 通過條件

- research lock 可從零重建。
- root dependency graph 與既有資料流程不受影響。
- 刪除 `research/` 不影響 Nautilus runtime。

---

<!-- PLAN_SECTION:STAGE-2 -->

## Stage 2：建立共用策略語言與資料橋

**工作量：3～5 個工程日。**

### 要做的事

- 建立零框架依賴 Strategy Core：特徵、模型參數、進出場門檻與 LONG／FLAT 決策語意。
- 建立固定 D-1、帶 hash、同輸入同 snapshot ID 的 Immutable Snapshot。
- 鎖定時間規則：bar 收完才能算；最早下一事件成交；label 未來收益不得提前出現在訓練資料。
- walk-forward 使用 purge／embargo，並保存精確 fold 邊界與 label availability。

### 通過條件

- Strategy Core 在 root 與 research 兩個環境產生 byte-identical vectors／hash。
- snapshot 對 gap、duplicate、future row、tamper 與 symlink fail closed。
- 同輸入重跑得到相同 snapshot ID 與 payload hash。

---

<!-- PLAN_SECTION:STAGE-3 -->

## Stage 3：建立 PyBroker 研究前端

**工作量：2～4 個工程日。第一版不做 UI。**

### 要做的事

- 只讀 immutable snapshot。
- 計算固定特徵、訓練 Logistic Regression、執行 walk-forward。
- 逐列輸出 prediction 與 LONG／FLAT intent。
- 保存每次成功與失敗實驗。
- 對候選進行排序並提出建議。

### 通過條件

- 相同 snapshot／spec／seed 在 fresh process 重跑，canonical output 一致。
- 未來資料不可影響先前 prediction／intent。
- 第一輪不淘汰任何候選；所有候選都可進兩條路徑作 benchmark。

---

<!-- PLAN_SECTION:STAGE-4 -->

## Stage 4：安全交棒與 Nautilus 正式驗證

**工作量：4～7 個工程日。**

Candidate Capsule 固定七件套：

1. `manifest.json`
2. `strategy.json`
3. `model.json`
4. `training_ledger.json`
5. `oof_trace.jsonl`
6. `golden_vectors.jsonl`
7. `screening.json`

Nautilus 驗證順序：

1. 驗 hash、provenance 與 snapshot；
2. 重算特徵、prediction 與 intent；
3. 逐列比對；
4. 只有 parity 通過才進 engine execution；
5. 由 Nautilus 唯一計算成交、手續費、Funding、部位與資金；
6. 任一不一致即 fail closed，AI 文字不能取代 evidence。

### 通過條件

- feature／prediction parity 在明示容差內；intent 100% 一致。
- candidate 任一 byte 竄改都被攔截。
- 正式 replay 兩次 canonical report hash 一致。
- 報告分列 raw return、fees、Funding、帳戶與期末部位。

---

<!-- PLAN_SECTION:STAGE-5 -->

## Stage 5：建立 AI Agent 研究閉環

**工作量：3～5 個工程日。**

### 要做的事

- AI Agent 可從 hypothesis 推進至 `nautilus_reproduced`。
- 失敗原因轉成固定 reason codes 與下一輪限制。
- 建立單一離線總驗活指令；任一步失敗即 non-zero 並阻擋 push。

### 永久禁止

AI Agent 不得跳 gate、改帳務 evidence、存取交易金鑰、接 Shadow／Testnet／live，或把 PyBroker provisional metrics 當正式判決。

---

<!-- PLAN_SECTION:STAGE-6 -->

## Stage 6：攻擊型試點與採用判決

**工作量：3～5 個工程日。**

至少事先封存 12 個策略／對照：

- always-long；
- no-trade；
- trend；
- mean reversion；
- breakout；
- funding-sensitive；
- 高換手費用壓力；
- future leakage detector；
- timestamp boundary detector；
- random negative control；
- 其餘在揭露 Nautilus 結果前封存。

每個 hypothesis 都跑：

1. PyBroker → Nautilus；
2. 直接 Nautilus。

先凍結 PyBroker 排序，再揭露 Nautilus ground truth。

### 保留標準

- correctness gates：零繞過、零無法解釋的不一致；
- routing recall：100%；任何 false negative 都是 NO-GO；
- 在 100% recall 下，operational Nautilus 完整回測數減少至少 50%；
- 並至少達成一項：
  - median end-to-end 研究時間降低至少 30%；或
  - 同等時間研究吞吐量達到 2 倍。

### 三種結果

1. **正式保留：** 正確性與效率門檻全達成。
2. **限制使用：** 保留分析與排序，但沒有淘汰權。
3. **完整移除：** 無實質效率提升，刪除 PyBroker research adapter，不留相容層。

---

<!-- PLAN_SECTION:MILESTONES -->

## 四個里程碑

1. **可信裁判完成：** Stage 0 Funding 與 deterministic backtest 驗活。
2. **研究前端完成：** Stage 1～3 隔離 ML／walk-forward／排序，仍無淘汰權。
3. **安全交棒完成：** Stage 4～5 Nautilus 重算、正式回測與固定失敗原因閉環。
4. **採用判決完成：** Stage 6 完成 12 個以上雙路徑試點並作保留／限制／移除決定。

---

<!-- PLAN_SECTION:TIMELINE -->

## 工作量估算

| Stage | 工程日 |
|---|---:|
| 0 | 3～5 |
| 1 | 1～2 |
| 2 | 3～5 |
| 3 | 2～4 |
| 4 | 4～7 |
| 5 | 3～5 |
| 6 | 3～5 |
| **合計** | **19～33** |

這是工程工作量，不是日曆承諾。任何 hard gate 紅燈都會延後或終止後續施工。

---

<!-- PLAN_SECTION:KANBAN-GOVERNANCE -->

## Kanban 專案治理

Kanban 是執行與證據控制面，不取代本計畫。

### 三層真值

1. 本文件決定為什麼做、做到哪裡、何時停止。
2. Kanban 決定現在做哪張卡、由誰執行、依賴與狀態。
3. 程式、測試、artifact、commit、遠端讀回與 runtime evidence 決定是否真的完成。

### 每張可執行卡必填

```yaml
plan_ref:
  plan_id: PYBROKER-NAUTILUS-ADOPTION-V1
  repository: HCH725/nautilus-quant-system
  commit: <approved full 40-character commit SHA>
  path: docs/plans/pybroker-nautilus-adoption.md
  section_ids: [<PLAN_SECTION IDs>]
implementation_ref:
  commit: <same approved commit>
  path: docs/plans/pybroker-nautilus-implementation-appendix.md
  section_ids: [<APPENDIX PLAN_SECTION IDs>]
authorized_scope:
  stage: <STAGE-N>
  card: <card code>
  next_stage_authorized: false
```

另須包含：`objective`、`in_scope`、`out_of_scope`、`inputs`、`files`、`deliverables`、`definition_of_done`、`verification`、`stop_conditions`、`rollback`、`evidence_paths` 與 `user_gate`。

### Plan preflight

worker 開工前必須執行：

```bash
.venv/bin/python scripts/verify_plan_ref.py \
  --commit <approved full SHA> \
  --plan-id PYBROKER-NAUTILUS-ADOPTION-V1 \
  --path docs/plans/pybroker-nautilus-adoption.md \
  --section STAGE-0
```

驗不到 commit、檔案或 section 時，必須 `BLOCKED: PLAN_REFERENCE_INVALID`；不得改讀最新 `main` 或憑卡片摘要施工。

### 階段放行

- 專案章程卡是不可派工的導航與批准紀錄，不指派 worker，也不作任何可執行卡的 dependency parent。
- 一次只建立或釋放已批准 Stage 的 executable cards。
- Stage 完成必須有獨立 audit 與 durable closeout evidence。
- Stage PASS 不會自動授權下一 Stage；後續批准記錄在章程卡 comment／Kanban event。
- 計畫內容未變時，不為更新執行狀態而重寫計畫或更換所有卡片 commit。

### 狀態措辭

- 卡片完成：只代表該卡驗收通過。
- Stage 完成：該階段所有卡與 audit PASS。
- 專案完成：Stage 0～6 與最終採用判決全部完成。

回報必須明示，例如：`P0-02 done；Stage 0 尚未完成；整體專案尚未完成。`

---

<!-- PLAN_SECTION:PLAN-CHANGE -->

## 計畫變更流程

若 worker 發現本計畫有缺口或矛盾：

1. 停止當前卡並留下精確證據；
2. 提出 Plan Change Request：原文、發現、最小修正、受影響卡片與驗證；
3. 漢秦哥批准後才修改計畫；
4. commit、push、遠端讀回，形成新 plan commit；
5. 已完成卡保留舊 reference，不改寫歷史；
6. 尚未開始的受影響卡更新 `plan_ref`；
7. 執行中的卡若受實質影響，停止並建立 replacement card；不得無聲切版。

單純修辭、排版或狀態更新不足以升版。真正改變範圍、順序、硬邊界、驗收或停損條件才走此流程。

---

<!-- PLAN_SECTION:ROLLBACK -->

## 整體 rollback 原則

- Stage 0 migration：temp write → validate → ready manifest → atomic cutover；舊資料短期保留，失敗立即沿用 legacy path。
- Research：刪除 `research/` 與其 venv，不影響 root runtime。
- Candidate／snapshot：immutable；錯誤只 retire，不覆寫。
- PyBroker NO-GO：移除 research adapter、notice 與 research verification stages；不修改或搬移 canonical market data。
- 不以永久雙寫、相容層、第二套帳本或新工作流引擎換取表面 rollback。

---

<!-- PLAN_SECTION:AUTHORIZATION -->

## 已批准與仍需批准

漢秦哥已批准：

- Hybrid 架構方向；
- 本計畫與 Kanban 治理方式；
- 啟動 Stage 0；
- Stage 0 內的 Funding 契約修正、versioned migration、deterministic backtest 與既有資料流程驗活。

目前仍未批准：

- Stage 1～6 的實際派工；
- Shadow、Testnet、live；
- 交易金鑰或訂單提交；
- 任何付費／商業化 PyBroker 使用；
- 超出本計畫的額外平台、UI 或基礎設施。

Stage 0 audit 完成後，只回報：完成內容、實際證據、偏離／風險、是否建議進 Stage 1。是否放行 Stage 1 由漢秦哥決定。

---

<!-- PLAN_SECTION:DEFINITION-OF-DONE -->

## 最終 Definition of Done

只有以下全部有真實工具輸出時，才可稱為「PyBroker 導入完成」：

- [ ] Stage 0 Funding boundary／mark price／fee／accounting 全有 executable assertion。
- [ ] Root data foundation full suite 與 daily status 獨立全綠。
- [ ] Research lock 可從零重建，root env 無 PyBroker 重依賴。
- [ ] 同一 Strategy Core 在兩環境產生相同 vectors／hash。
- [ ] Immutable Snapshot 完整、可重現、tamper fail closed。
- [ ] 成功與失敗 experiments 全留存，fresh-process replay 一致。
- [ ] Candidate Capsule 無 code／pickle／cache，任一 byte 竄改皆被擋。
- [ ] Feature／prediction／intent parity 與 causality gates 全綠。
- [ ] 同 candidate 的 Nautilus replay 兩次 canonical report hash 一致。
- [ ] AI Agent 只能憑機器 evidence 推進到 `nautilus_reproduced`。
- [ ] 至少 12 個預先封存 hypotheses 完成雙路徑 benchmark。
- [ ] routing recall 100%，並達到 operational replay 與效率門檻；否則已限制使用或完整移除。
- [ ] 所有 accepted source 變更均完成 tests、commit、push 與遠端讀回。
- [ ] Board 無未解釋的 running／blocked residue，最終 artifact index 可讀回。

少一項都只能稱為「正在導入」，不能稱為完成。
