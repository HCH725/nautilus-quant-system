# Strategy Loop：Family → Robustness → Paper → Binance Demo 建置計畫

> **狀態：2026-08-20 已吸收 V2 control/evidence 審查；Card 1 `FAMILY-KERNEL-V2` 已獲授權並在本次變更實作，Card 2～6 尚未開工且不因 Card 1 完成自動取得授權。**
>
> 本文件把 [`../architecture/strategy-loop-operating-model.md`](../architecture/strategy-loop-operating-model.md) 的已接受方向拆成可驗收的建置卡。Card 1 施工基線為 `f2c88b2e6fd4244f7bc2ce39ff75b962d49defb7`；既有 historical strategy-loop V1 已在 `2e424a38fcf9993d142cb31a53960066534f84a1` 完成並驗活。

## 1. 目標與完成邊界

把目前「單一 momentum family 的歷史兩代回饋證明」擴成下列可長期經營的閉環：

```text
Hermes Research Controller 自主建立／修改 family 與公式
        ↓
同一份 deterministic family kernel
        ↓
PyBroker campaign + provisional screen
        ↓
Candidate v2（provisional）
        ↓
SIGNAL PARITY GATE
        ↓
Nautilus historical accounting + robustness
        ↓
Strategy Freeze
        ↓
production-data Shadow / Sandbox Paper
        ↓
Binance Demo（必要時既有 Testnet）execution validation
        ↓
read-only Promotion Projection
        ↓
════════ HUMAN / LIVE POLICY BOUNDARY ════════
        ↓
Runtime Qualification（另立、未授權）
        ↓
Bounded Live（另立、未授權）

每一 tier 的 ledger verdict → Hermes bounded next action
        ├─ MUTATE
        ├─ NEW_FAMILY
        ├─ KILL
        ├─ ADVANCE
        └─ FIX_TECHNICAL
```

本計畫完成時，系統可以把新 family 從公式一路送到 Paper 與 Binance 模擬環境，並保留可稽核證據；**不包含真金 Live 啟用**。Live 必須另立資金、風險、停機與授權契約。

## 2. 已有能力與真實缺口

| 面向 | 現況 | 缺口／決定 |
|---|---|---|
| Historical loop | H0 → Nautilus verdict → H1 已真實跑通；失敗、lineage、hash、funnel 可查 | 保留，不重寫 |
| Hypothesis | V1 保持原 bytes／ID；Card 1 已新增 registry 驗證的 `strategy-hypothesis-v2` 與 family version | 新 family 仍須 tracked code + validator + golden vectors |
| Ledger | Card 1 已把 legacy `strategies` transactionally 遷成 `parameters_json`／family version／identity schema，保留舊 row、ID、外鍵與三個 V1 stage semantics | 正式 ledger 不 reset；後續 tier 只新增專用 append-only evidence |
| Formula | Card 1 已把既有 momentum 公式原樣移入純 stdlib shared kernel；PyBroker 不再持有第二份公式 | live runtime adapter 仍在 Card 4 |
| Candidate | V1 持續可讀；Card 1 已新增 Candidate v2、完整 signal/family/kernel/source/context identities | Candidate 永遠 provisional |
| Signal parity | Card 1 已新增正式 fail-closed gate，獨立 incremental 重算，PASS 後只把重算序列交給 Nautilus | live/historical parity 延伸留在 Card 4 |
| Control plane | Card 1 已新增 evaluation context、V2 experiment invalidation、evidence-envelope contract 與 append-only parity evidence | robustness/cost/risk/promotion policy IDs 依後續 tier 版本化 |
| Campaign | 一次 CLI 只跑一個 hypothesis | 需 deterministic campaign expander、budget、dedupe、cohort summary |
| PyBroker screen | 現在只驗 candidate 結構與 hash，合法就 `PASSED` | 需預先釘住的 provisional metrics 與 rejection policy |
| Historical evaluator | Nautilus 真正負責 fills、fees、Funding、positions、accounting | 沿用；補 bounded window／stress 輸入 |
| Robustness | Funnel 的 `Robustness passed` 與 `Promotion eligible` 目前硬寫 `0` | 需真實 walk-forward／regime／cost evidence |
| Multiple testing | 既有計畫有 generation budget 與 dedupe，但沒有完整 trial census | 保存 search space、parameter-search policy、generated/deduped/executed/rejected/surviving counts；DSR/PBO 延後到有足夠 trial 再版本化導入 |
| Paper | 無 live bar strategy、Shadow、sandbox account、prospective cohort | 需 LiveNode + shared kernel + state/restart evidence |
| Market-data paths | D-1 historical store 已有唯一 writer；Paper live feed 尚未實作 | 保持相同 canonical semantics、不同 delivery path；live feed 只寫 runtime evidence，不直接寫 canonical store |
| Risk & Execution | Alpha kernel 不得持有 quantity/risk/order/accounting 的 domain boundary 已存在 | 需獨立 tracked Risk & Execution Policy 及 identity，供 Paper/Demo/promotion 綁定 |
| Demo/Testnet | 無 adapter runtime、credentials、order lifecycle、reconciliation | 需明確非 LIVE 的 bounded validation |
| Execution truth | Demo 計畫已有 instrument filters readback；Mark Price、metadata/fee snapshot identity 尚未明列 | 在 Paper/Demo qualification 補齊，不擴 historical alpha store，Open Interest 非 P0 |
| Loop action | 現在只有 `REVISE`／`RETAIN_FOR_RESEARCH` | 需 canonical next-action artifact 與跨 tier routing |
| Operator view | Funnel 可讀，但後兩級為未實作零值 | 最後改為 ledger-derived 真值；仍先不做 Web Dashboard |

### 2.1 2026-08-20 handoff 分類結論

下表分類的是本輪修改前的 `e745dfd0e24280359d8cec68441ce64e84311952` 基線；「納入 Card」表示缺口已被本文件更新吸收，不代表功能已實作。

| 要求 | 分類 | 判斷 |
|---|---|---|
| Shared kernel、historical/live parity、Candidate v2 | **plan 已存在但圖／gate 順序不完整** | 方向正確；尚未實作 |
| Formal Signal Parity Gate | **尚未完整規劃** | 既有 golden parity tests 不等於 formal trust-boundary gate；納入 Card 1 |
| Versioned policies / shared IDs | **V1 已部分存在；V2 plan 部分存在** | 補 evidence envelope 與「缺 required identity 不得 PASS/reuse」規則 |
| Historical store / prospective feed 分流 | **plan 已存在** | batch/incremental parity 正確；但交接中的 `live feed → canonical store` 直接寫入箭頭與唯一 D-1 writer／禁止第二同步衝突，拒絕採用 |
| Independent Risk & Execution Policy | **domain boundary 已存在，正式 policy contract 尚未規劃** | 納入 Card 4，Card 5 只加 venue-specific execution envelope |
| Multiple-testing evidence | **plan 部分存在** | budget/dedupe 已有；完整 trial census 與 search-policy identity 尚缺；DSR/PBO 暫緩而非假裝完成 |
| Mark Price / instrument metadata / actual fees | **filters 部分已規劃，其餘尚未明列** | 放在 Paper/Demo execution/risk qualification，不阻塞 Card 1、不改 historical alpha data scope |
| Runtime Qualification | **尚未列入六卡計畫** | 新增 post-plan、pre-Live gate；目前不升級 rc2，也不授權 Live |

沒有發現需要推翻既有 V1 或六卡主順序的重大衝突。唯一明確拒絕的是讓 live feed 直接成為第二個 canonical historical writer。

### 已完成的 runtime 可行性探測

本機 root venv 為 NautilusTrader `2.0.0rc2`。實際安裝 API 與部分舊文件範例名稱不同，實作時以本機 `.pyi` 與窄 smoke 為準：

- `nautilus_trader.live.LiveNode` 可用；不是舊路徑的 `TradingNode`。
- `LiveNode.builder(...)` 已用「`BinanceDataClientConfig(environment=LIVE)` + `SandboxExecutionClientConfig`」完成離線 composition build / dispose；這只證明接線 API 可組合，不冒充真實網路／成交驗活。
- `nautilus_trader.adapters.binance` 提供 `BinanceDataClientConfig`、`BinanceExecClientConfig`、`BinanceDataClientFactory`、`BinanceExecutionClientFactory`。
- `BinanceEnvironment` 明確有 `LIVE`、`DEMO`、`TESTNET`；新 Binance Futures 模擬建置預設選 `DEMO`。[1]

因此目前**不需要為 Paper 先升級 Nautilus 或新增套件**；但每一個 adapter 接線仍須由安裝版本的實跑證據驗活，不能複製舊版範例就宣稱完成。

## 3. 不可跨越的邊界

1. Canonical data sync、catalog 與 Funding store 繼續獨立於 Hermes、PyBroker、Paper、Demo、Dashboard。
2. Family kernel 只能持有 formula／parameters／warm-up／signal semantics；不得持有 credentials、quantity、leverage、fees、Funding policy、order type或 accounting truth。
3. Hermes 只在 experiment、Paper window、Demo lifecycle 或 campaign 邊界被喚起；不進入每根 bar 或每張 order 的事件路徑。
4. PyBroker 指標與績效一律 provisional；正式 historical accounting 只看 Nautilus。
5. 全歷史資料已被既有 runner 檢視，不能重新包裝成 sealed holdout。Historical robustness 是「已檢視歷史的壓力證據」，真正的新資料證據來自策略版本凍結後的 prospective Paper。
6. Paper 使用 production market data，但成交與帳務仍是模擬；Paper PnL 不代表 venue execution quality。
7. Demo/Testnet 主要驗 execution engineering，不可拿模擬 order book 反覆調 alpha。
8. `LIVE` environment、production endpoint、真金 credentials 與真金 order 一律 fail closed；本計畫不授權它們。
9. 不新增 Qlib、AutoML、ORM、queue、MCP、Web Dashboard、第二套資料同步或 Hermes cron。
10. Runtime secrets 不進 repo、memory、logs、artifact 或 command line；只接受執行時注入。
11. 一次只有一個 writer。沒有實際命令輸出、測試、commit、push、remote readback 與 read-only audit，不得把卡片標成完成。
12. Candidate v2 仍是不可信 provisional artifact；Signal Parity Gate 未 PASS 前不得啟動 Nautilus accounting。
13. 每個 tier 的 required policy/data/runtime/environment identity 缺一即 `N/A/BLOCKED`，不可 PASS 或 reuse；campaign membership 不改 execution identity。
14. Prospective live feed 只寫 append-only runtime evidence；canonical historical store 仍由既有 D-1 writer 單寫，日後以同 bars 的 normalized bytes + signal IDs 做 reconciliation。
15. Projection 永遠 read-only；不能建立 verdict、補數字或跨越 Live policy boundary。

## 4. Kanban 管制方式

下列 **6 張順序卡**是六個可獨立驗活的 coherent diff，不是六個同時開工的 phase umbrella。Card 1 依 operator 指示由單一 writer 直接執行；後續可直接執行或映射到既有 board，但不以 Kanban 儀式代替驗證：

1. `FAMILY-KERNEL-V2`
2. `CAMPAIGN-SCREEN-V1`
3. `ROBUSTNESS-LOOP-V1`
4. `PAPER-PROSPECTIVE-V1`
5. `BINANCE-DEMO-EXEC-V1`
6. `PROMOTION-PROJECTION-V1`

Runtime Qualification 不偷塞成第七張已授權卡；它是 promotion 之後、Live 之前另立且需 operator 授權的未來計畫。

每張卡固定走：

```text
RED tests
  → minimum implementation
  → focused GREEN
  → root + research full suites
  → data status + secrets + diff checks
  → commit + push + remote readback
  → independent read-only audit
  → 才能啟動下一張卡
```

控制規則：

- 同時最多一張卡 `in_progress`，不得 fan-out 多個 writer 修改同一 repo。
- Auditor 只能回 PASS／FAIL + evidence；FAIL 不得自行開 remediation swarm。
- Strategy hypothesis、mutation、funnel stage 不建立 Kanban 卡；它們進 immutable ledger。
- 技術故障標 `FIX_TECHNICAL`，不能算成 strategy reject。
- 任何 scope 擴張先更新本計畫並由 operator 讀回，不在施工途中偷偷加功能。

## 5. Card 1 — `FAMILY-KERNEL-V2`

> **Implementation status：本次變更已完成實作；只有 focused/full tests、copied-ledger migration、固定快照審計、秘密掃描、push 與 remote readback 全綠後才可視為交付完成。**

### 目的

先把單一內嵌公式抽成 historical／Paper／Demo 都能呼叫的唯一 signal truth，並讓 hypothesis／ledger 能安全表示多 family。

### 預期檔案

- Create: `src/nautilus_quant/strategy_families.py`
- Create: `tests/test_strategy_families.py`
- Create: `docs/contracts/strategy-hypothesis-v2.md`
- Create: `docs/contracts/pybroker-candidate-v2.md`
- Create: `docs/contracts/signal-parity-gate-v1.md`
- Create: `docs/contracts/strategy-evidence-envelope-v2.md`
- Modify: `src/nautilus_quant/strategy_lab.py`
- Modify: `tests/test_strategy_lab.py`
- Modify: `src/nautilus_quant/pybroker_candidate.py`
- Modify: `tests/test_pybroker_candidate.py`
- Modify: `src/nautilus_quant/candidate_backtest.py`
- Modify: `tests/test_candidate_backtest.py`
- Modify: `research/pybroker_research.py`
- Modify: `research/test_pybroker_research.py`

### 實作順序

1. **先寫 RED golden-vector tests**：同一組 canonical closed OHLCV bars + family version + kernel version + parameters，batch、incremental、root 與 research 必須得到相同 timestamp、canonical finite score bytes、target intent、reason 與 content-addressed signal ID。
2. 建立純 stdlib 的最小 kernel：
   - `ClosedBar`；
   - `FamilyDecision`；
   - 小型 tracked registry；
   - 每個 family 明確的 `version`、warm-up、parameter validator 與 evaluator。
3. Kernel 只能由 completed bars 產生 position/order-independent 的 target decision；warm-up 後每根 eligible completed bar 都形成一筆 canonical decision evidence，即使 target intent 未改變也不靠持倉狀態省略。現有 callback 的 `ctx.long_pos()`、持倉轉移、重複 order 抑制、quantity 與 order mapping 留在外層 runtime。把 `lookback-momentum-long-flat` 公式原樣搬入 kernel；此卡先證明 parity，不趁機改 alpha 公式。
4. Research runtime 直接從 repo `src/` 載入這個純模組，不把 Nautilus dependency 加進 Python 3.12 research lock；repo-local path shortcut 要加 `ponytail:` ceiling 註解與 import-boundary test。
5. 新增 canonical `strategy-hypothesis-v2`：含 `family_version`、任意 plain-JSON parameters、approved instrument／bar type、thesis、falsification 與 lineage；新 v2 strategy ID 必須包含 family version。
6. 加 schema-dispatch loader：既有 `pybroker-candidate-v1` bytes、ID 與 artifact 保持可讀；新 `pybroker-candidate-v2` 每筆 signal 必含 `signal_id`、`reason`、`score`、`target_intent`、`ts_event_ns`、family ID/version 與 kernel version/hash。Candidate 明文標示 `truth_status=provisional`，不得被當成 fills、PnL 或 accounting truth。
7. 定義共用 `strategy-evidence-envelope-v2`：為每個 tier 列出 required／not-applicable identity matrix，至少涵蓋 strategy、family/version、kernel version/hash、data snapshot/as-of、code commit、screen/robustness/cost/risk policy、evaluation context、runtime/environment 與 artifact hash；`risk_policy_id` 明定為 `strategy-risk-execution-policy-v1` 的 content hash。缺 required identity 不得 PASS/reuse；不同 kernel、data、policy、context、runtime 或 environment 不得 reuse。Campaign membership 不屬於 execution identity。
8. 在 Candidate v2 與 Nautilus accounting 之間實作正式 `signal-parity-gate-v1`：formal consumer 依 candidate 綁定的 canonical data snapshot 重新載入 bars，透過獨立 incremental adapter 呼叫同一 kernel，逐筆精確比對 sequence length/order、`signal_id`、`ts_event_ns`、score bytes、`target_intent`、`reason`、family/version 與 kernel version/hash。PASS 後只把 gate 重算的 canonical sequence交給 Nautilus；不得直接信任 Candidate signals。
9. Gate PASS／ERROR 寫入專用 append-only `signal_parity_results`，綁定 experiment/candidate/evidence envelope/artifact hash；不要擴寫、重標或冒充既有三個 V1 `stage_results`。Parity mismatch、missing identity、duplicate、recompute exception 另寫 technical error evidence，reason code 至少有 `SIGNAL_PARITY_MISMATCH`，required action 為 `FIX_TECHNICAL`；不得建立 economic reject，且測試必須證明 Nautilus `BacktestEngine`／accounting path 完全未啟動。完整跨-tier `strategy-action-v1` 仍留在 Card 3，不為此提前擴張。
10. 把 kernel version/hash 納入 `_engine_identity`；補 duplicate bar、save/load、restart 後零 duplicate signal 的 RED tests，證明 batch 與 incremental 路徑一致。
11. 先用 copied legacy ledger 寫 migration RED test，再在單一 transaction 內把固定 strategy parameter 欄位遷成 canonical `parameters_json` + `family_version` + `identity_schema`，並新增有 UPDATE/DELETE protection 的 `signal_parity_results`；完整保留 v1 strategy／hypothesis／experiment／verdict／stage／error row、原始 `stage_results` CHECK semantics 與外鍵。既有 row 標為 `strategy-id-v1` 並保留原 ID；只有新 row 使用包含 family version 的 `strategy-id-v2`，不得為了統一格式重算歷史 lineage。
12. Migration 前後做 row count、content ID、foreign-key、artifact hash 與 append-only trigger readback；任何不一致必須 rollback，禁止清空或原地試改 `var/strategy-loop`。Migration 測試只操作 copied fixture，正式 ledger 只在功能卡通過且有單一 writer 時遷移。

### 驗收

- 舊 v1 hypothesis 仍可讀、舊 ledger row 全數保留。
- v2 可註冊不同 parameter shape 的第二個測試 family。
- 同 bars 下 batch／incremental／research／root kernel bytes 完全一致；duplicate bar、save/load 與 restart 不產生重複 signal。
- Registry test 固定 family version + golden vectors；formula 行為變更必須同步 bump version，read-only audit 以 diff 拒絕「只改 expected output、不改 version」。
- Candidate v1 validator 與既有 H0/H1 artifact 仍可讀；所有新 Candidate v2 signal 與 source identity 欄位完整。
- Positive parity fixture PASS 後，Nautilus 只收到 gate 重算序列；任意竄改一個 score/reason/timestamp/signal ID/version 的 negative fixture 都回 `FIX_TECHNICAL`，且 accounting call count 為零。
- 相同 canonical decision bytes 重跑得到相同 `signal_id`；改變 timestamp、score、target intent、reason、family/version 或 kernel version/hash 任一 identity-bearing field 都不能沿用舊 signal ID。
- `signal_parity_results` 的 PASS／ERROR 與 artifact hash 可 append-only readback，UPDATE/DELETE 均被 trigger拒絕；舊 V1 `stage_results` bytes、rows 與 CHECK semantics 不變。
- Kernel version/hash、data snapshot、screen policy、evaluation context 或 runtime/environment 變更都會改新 experiment identity／禁止 verdict reuse；campaign 分組變更本身不會改 execution identity。
- 每個 Card 1 tier 的 required identity 缺一即 `N/A/BLOCKED`；尚未進入 robustness/Paper 的 policy 欄位明確 not-applicable，不偽造 placeholder ID。
- 無新 runtime dependency。

### 停止線

若 Signal Parity Gate 無法保證 fail 前 accounting 零執行，或 SQLite migration 無法在 copied legacy ledger 上保留 ID、foreign key 與 immutable trigger，就停止此卡；不得用「直接 replay candidate」或「刪 ledger 重跑」繞過。

## 6. Card 2 — `CAMPAIGN-SCREEN-V1`

### 目的

讓 Hermes 一次提出 bounded campaign，deterministic runner 展開、去重、執行與 provisional screen；禁止一個 strategy 一次 LLM call。

### 預期檔案

- Create: `src/nautilus_quant/strategy_campaign.py`
- Create: `tests/test_strategy_campaign.py`
- Create: `config/strategy_research_policy.json`
- Create: `docs/contracts/strategy-campaign-v1.md`
- Modify: `src/nautilus_quant/strategy_lab.py`
- Modify: `tests/test_strategy_lab.py`
- Modify: `research/pybroker_research.py`
- Modify: `research/test_pybroker_research.py`

### 實作順序

1. 先新增 `research-result-v2`，只保存有限且 finite 的 provisional metrics：至少 trade count、signal count、total return、max drawdown；必要時從 positions/orders 直接導出 turnover／exposure，但不複製完整 dataframe。
2. `strategy_research_policy.json` 在看結果前釘住最低活動量、最大 provisional drawdown、turnover 上限與 no-signal rejection；policy hash 與 `screen_policy_id` 進 evaluation context／experiment identity。
3. 先用**單一 hypothesis**證明 `Research screened` 能真實 `PASSED` 或 `REJECTED`；被拒絕者不可進入 Nautilus historical。這個 screen vertical slice 未通過前，不得擴成 campaign。
4. 再定義 canonical `strategy-campaign-v1`：family/version、完整 search space、approved instruments/bar types、deterministic parameter values、parameter-search policy ID、seed、data-as-of、generation budget／maximum candidates、screen policy ID。
5. 用 stdlib `itertools.product` 依 canonical 順序展開；在讀資料或啟 subprocess 前先 budget check、content-ID dedupe 與 ledger reuse。
6. Campaign ID 與 cohort membership 寫進獨立 membership/trial table；不能混進相同 execution semantics 的 experiment identity。每個 generated attempt 都保存 immutable 狀態與 reason：`DUPLICATE_SUPPRESSED`、`TECHNICAL_INVALID`、`SCREEN_REJECTED` 或 `SURVIVED`。即使 content ID reuse 而不重跑，也要留下本 campaign 的 trial membership，不准只保存成功者。
7. 產生 bounded cohort summary：trial count、generation budget、family count、candidate count、generated/deduped/executed/technical-invalid/rejected/surviving counts、top reason codes、search/policy/data IDs 與每個 family 的進出數；不把 PyBroker PnL 寫成正式績效。
8. 此卡最後由小蒨自主建立一個**新的 tracked family/formula 作為機制證明**：先寫 thesis、falsification、golden vectors 和 family version，再放進 campaign；不能先看 campaign 結果才補公式理由。
9. 此卡只記錄 screen reason codes 與 campaign membership；canonical `strategy-action-v1` 延後到 Card 3，等 robustness evidence 完整後才允許 `ADVANCE`。

### 驗收

- 同 campaign spec 重跑得到相同 expansion IDs，沒有重複 experiment。
- 超出 budget 在任何研究 process 啟動前 fail closed。
- Screen threshold 改動會改 policy/experiment identity。
- 同一 execution semantics 放入不同 campaign 只新增 membership，不重算 experiment identity。
- 至少一個合法但無活動或違反 screen policy 的 candidate 真實被 `REJECTED`。
- 被 screen 拒絕的 candidate 沒有 Nautilus historical experiment。
- 每個 generated trial 只有一個 terminal status；各 terminal-status count 加總等於 generated，`execution_started` 另作正交欄位，避免把 preflight technical invalid 假算成已執行。Direct ledger query 與 cohort summary 完全一致。
- 相同 candidate 被不同 campaign reuse 時不重算 execution，但每個 campaign membership 與 duplicate/reuse reason 都不可變可查。
- 新 family 與既有 momentum 都使用同一 registry/kernel。
- 一個 campaign 只交給 Hermes 一份 bounded summary。

### 停止線

若 provisional metric 不 finite、data cohort 不一致，或 campaign 在 threshold 未凍結前已執行，整個 cohort 標 technical invalid；不得事後調門檻再沿用同一結果。

## 7. Card 3 — `ROBUSTNESS-LOOP-V1`

### 目的

讓 historical survivor 通過可重現的 walk-forward／regime／cost stress，並把經濟失敗與技術失敗送回不同 loop。

### 預期檔案

- Create: `src/nautilus_quant/strategy_robustness.py`
- Create: `tests/test_strategy_robustness.py`
- Create: `config/strategy_robustness_policy.json`
- Create: `docs/contracts/strategy-robustness-v1.md`
- Create: `docs/contracts/nautilus-verdict-v2.md`
- Create: `docs/contracts/strategy-feedback-v2.md`
- Create: `docs/contracts/strategy-action-v1.md`
- Modify: `src/nautilus_quant/candidate_backtest.py`
- Modify: `tests/test_candidate_backtest.py`
- Modify: `src/nautilus_quant/strategy_lab.py`
- Modify: `tests/test_strategy_lab.py`

### 實作順序

1. 先寫 window generator RED tests：UTC boundaries、無倒序、無未完成 bar、每個 window 有 immutable `evaluation_context_id`；data-as-of 之後的資料永遠不可讀。
2. 讓 `CandidateBacktestRequest` 接受明確 evaluation start/end 與 tracked cost policy，不改 candidate payload。
3. 建立固定 robustness matrix：expanding／rolling windows、trend／range／high-volatility labels、fee/funding stress、latency/execution-delay stress、parameter-neighborhood stability，以及經測試的 deterministic slippage-bps stress。Regime label 必須由預先定義的 deterministic rule 產生；若 rc2 無法在不繞過 Nautilus accounting 的情況下注入某項 stress，該項保持 `unmodeled` 並阻止 robustness PASS，不得用文字假裝已測。
4. 每一格仍呼叫 Nautilus formal evaluator；PyBroker 不接管 fees、Funding、fills 或 accounting。
5. 每個 historical／robustness verdict 使用 `nautilus-verdict-v2`／`strategy-feedback-v2`，綁定 strategy、candidate、evaluation context、policy、data、engine、reason codes 與 artifact hash；window、tier 或 policy 改變不得 reuse 舊 verdict。
6. 聚合 verdict 只保留 bounded metrics、worst window、reason codes、Funding truth、claimability 與 artifact hash；`Robustness passed` 從 ledger evidence 查詢，不再硬寫零。
7. Robustness 完整後才產生 canonical `strategy-action-v1`：至少包含 source verdict/tier、action、consumed reason codes、changed dimension、campaign/generation、child strategy ID。Technical fail 只能是 `FIX_TECHNICAL`；`ADVANCE` 必須綁定完整 robustness evidence。
8. Economic fail → `MUTATE`／`NEW_FAMILY`／`KILL`；任何 strategy/family 變更都建立 child version，重新跑 historical + robustness；不可沿用舊 robustness verdict。A verdict → action → B hypothesis → B verdict 兩輪重跑必須得到相同 IDs 與順序。
9. Robustness aggregate 必須引用 Card 2 的 immutable trial census、search space、generation budget、family/candidate/survivor/reject counts 與 parameter-search policy。Deflated Sharpe Ratio／PBO 在第一版明確記為 `NOT_MODELED`；只有 trial volume、方法與 threshold 在看結果前被新 policy version 凍結後才可加入，不能事後替舊 cohort補一個漂亮分數。

### 驗收

- 相同 policy/data/strategy 重跑完全 reuse，不膨脹 funnel。
- Window 或 cost policy 變更會產生不同 experiment ID。
- 至少一個 robustness rejection 與一個 technical failure 都被正確分類。
- Technical fail 不會產生 mutation child；`ADVANCE` 缺任一 robustness evidence 時 fail closed。
- Action artifact 可由 source verdict 完整追到 child hypothesis；相同輸入兩輪重跑不新增重複 lineage。
- Funnel 第六級由真實 ledger stage 計算。
- Verdict 明確寫出 historical data 已 inspected，不能自稱 sealed holdout 或 production evidence。
- Robustness verdict 可回溯完整 trial/search context，不會只看 survivor；DSR/PBO 未實作時顯示 `NOT_MODELED`，不是 `0`。

### 停止線

只要任何 window 越過 data-as-of、Funding truth 不明、trial census 不完整、stress label 被弱化，或 technical failure 被算成 economic rejection，就停止 promotion。

## 8. Card 4 — `PAPER-PROSPECTIVE-V1`

### 目的

凍結 survivor 後，從未來到達的 production closed bars 產生 Shadow／Sandbox Paper 證據；這是每個 trading-eligible strategy 必經段，不能跳過。

### 預期檔案

- Create: `src/nautilus_quant/live_strategy.py`
- Create: `src/nautilus_quant/paper_runtime.py`
- Create: `tests/test_live_strategy.py`
- Create: `tests/test_paper_runtime.py`
- Create: `config/strategy_paper_policy.json`
- Create: `config/strategy_risk_execution_policy.json`
- Create: `docs/contracts/strategy-paper-evidence-v1.md`
- Create: `docs/contracts/strategy-risk-execution-policy-v1.md`
- Modify: `src/nautilus_quant/strategy_lab.py`
- Modify: `tests/test_strategy_lab.py`
- Modify: `pyproject.toml`
- Create only after bounded smoke passes: `ops/ai.nautilus.quant.paper.plist`
- Modify only after bounded smoke passes: `tests/test_launchd_plist.py`

### Runtime 接線

```text
BinanceDataClientConfig(
    product_type=BinanceProductType.USD_M,
    environment=BinanceEnvironment.LIVE,
)
        ↓ production market data only
shared FamilyStrategy
        ↓
versioned Risk & Execution Policy + canonical order mapping
        ├─ SHADOW NODE
        │    └─ no execution client registered; signal/order-intent evidence only
        └─ PAPER NODE
             └─ SandboxExecutionClientConfig + active LiveRiskEngineConfig
                  → simulated orders/fills/portfolio/account
```

### 實作順序

1. `FamilyStrategy` 只吃 completed bars；維護 bounded warm-up window，呼叫 Card 1 的 position/order-independent kernel，產出 canonical `signal_id`、score、target intent、reason。持倉轉移、quantity、risk 與 order mapping 只存在外層。
2. 在看 prospective 結果前凍結獨立 `strategy-risk-execution-policy-v1`：position sizing、leverage cap、gross/net/per-symbol exposure、maximum loss、order mapping、reduce-only、fee/slippage/Funding treatment、stale-data/reconnect behavior、duplicate-order prevention、kill switch/circuit breaker。Policy 不含 credentials；content hash 形成 `risk_policy_id`。
3. 同一 strategy class 供 Shadow 與 Paper composition root 使用，不複製公式；Shadow **完全不註冊 execution client**，Paper 才註冊 sandbox execution 與 active risk engine。Alpha family 與 Risk/Execution policy 是兩個獨立 domain，任何一方 version 改變都建立新的 runtime evidence identity。
4. 寫 synthetic event tests：warm-up、late/revised bar、gap、duplicate bar、save/load、restart、同 signal 不重送 order、flat idempotency、stale-data circuit breaker、maximum-loss/kill switch 與 shutdown flatten policy。
5. 寫 historical/live parity fixture：同 canonical bars 的 batch/incremental/PyBroker/Signal Parity Gate/Nautilus live signal identity 必須一致；fill 不要求一致。
6. Prospective live feed 只寫 append-only runtime evidence，不呼叫 canonical sync、不寫 catalog/store。當同一批 completed bars 隨後由既有 D-1 writer 進 canonical store，再以 data snapshot 對齊 normalized bar bytes 與 signal IDs；不一致標 `FIX_TECHNICAL`，不能把 live payload 回填成 historical truth。
7. 新增 append-only `runtime_runs`／`runtime_verdicts`（或等價的同一深模組）：保存 strategy/family/kernel/runtime version、code commit、tier、environment、cohort、time range、data/paper-admission/cost/risk policy IDs、instrument/Mark Price/fee metadata identities、technical status、strategy outcome、reason codes與 artifact path/hash。不得用 `_hash_tree` 一次性靜態歷史 identity 表示持續到達的 live stream。
8. 先以 Shadow 接收 1～2 根**未來到達**的 closed bars：orders cache 必須保持空；記錄 live WebSocket closed-bar semantics、Mark Price channel semantics與 instrument specification/filter snapshot。Mark Price 與 metadata 只供 execution/risk evidence，不進 alpha kernel，也不擴 historical feature store；Shadow 不產生 PnL claim。
9. Paper 先跑 deterministic forced `LONG → FLAT` fixture，真實穿過 Strategy → Risk → Sandbox execution → order/fill events → portfolio/account；固定 max quantity/notional、balance、NETTING／MARGIN、tracked fee/slippage/Funding policy、reduce-only 與 flatten-on-exit。這條縱切通過後才允許 prospective strategy cohort。
10. 先完成 bounded CLI smoke；只有 live data subscription、bar close、Mark Price/metadata readback、signal artifact、sandbox account/order readback 都成功，才加一個原生 launchd plist 長跑。不得用 Hermes cron 承擔 trading runtime。
11. Prospective cohort 開始前，先在 `strategy-paper-evidence-v1` 寫 content-addressed Strategy Freeze admission record：strategy/family/kernel/code/parameters、已通過的 historical/robustness verdict IDs、inspected data boundary、runtime、risk/paper-admission policy 與 instrument metadata snapshot。Freeze hash 定義 cohort；策略或任一 execution identity 修改後，舊 Paper 窗口不得 reuse，必須建立新 freeze record 並重新計時。不要另造 mutable promotion state machine。
12. Evidence 至少含：cohort start/end、completed/missing/revised bars、signal IDs、intent/order mapping、Mark Price與 instrument/fee metadata IDs、sandbox fills/fees/positions/account、restart/reconnect、duplicate suppression、technical/economic status、artifact hashes。
13. Paper technical fail → `FIX_TECHNICAL`；修好後先重跑 parity，再為同 strategy logic 開新 prospective window。Paper economic fail → child/new family，完整重跑 historical + robustness + Paper。

### 驗收

- Shadow LiveNode 使用 production data 真實啟動，沒有 execution client、orders cache 全程為空，且 1～2 根未來 closed bars 與 canonical REST bar semantics 一致。
- Paper LiveNode + production data + active risk + sandbox execution 真實啟動，不是 mock-only；forced LONG → FLAT fixture 與 prospective cohort 分開留證。
- Shadow 與 Paper 對同 bars 的 signal bytes 一致。
- Live feed 沒有 canonical-store write path；同 bars 經 D-1 ingestion 後，normalized bytes + signal IDs 可 reconciliation。
- Risk & Execution Policy required 欄位與 identity 完整；任一 policy byte、Mark Price semantics、instrument/filter snapshot 或 fee schedule identity 改變都會阻止舊 Paper verdict reuse。
- Strategy Freeze record 可由 hash 讀回完整 admission chain；缺失、被改寫或與 runtime identity 不一致時 Paper 不得啟動。
- 至少完成一次受控 restart/reconnect，無 duplicate signal/order。
- 每次 bounded Paper 結束時 terminal flat、無 open orders，fills/fees/positions/account 可 reconciliation；任何 gap、timestamp mismatch、state mismatch、unreconciled position 都 fail closed。
- Runtime run/verdict 可 append-only readback，technical status 與 strategy outcome 分欄，不覆寫 historical experiment identity。
- Paper 仍不會讓 `promotion_eligible=true`；還缺 Demo execution verdict。
- Launchd 與資料同步互不依賴，外接碟掛載不新增第二排程或複雜 wrapper。

### 停止線

若 production data 與 canonical historical bar semantics 無法證明 parity、Risk & Execution Policy／Mark Price／instrument metadata identity 缺失、sandbox state 無法 restart/reconcile，或 runtime 依賴 Hermes session 才能維持，就不能開始長期 Paper。

## 9. Card 5 — `BINANCE-DEMO-EXEC-V1`

### 目的

以專用模擬 credentials 驗證 Binance USD-M 真實 adapter 的 order lifecycle、user stream、unknown outcome 與 reconciliation；不拿 Demo 流動性做 alpha 優化。

### 預期檔案

- Create: `src/nautilus_quant/binance_demo_runtime.py`
- Create: `tests/test_binance_demo_runtime.py`
- Create: `config/binance_demo_policy.json`
- Create: `docs/contracts/binance-demo-evidence-v1.md`
- Modify: `src/nautilus_quant/live_strategy.py`
- Modify: `src/nautilus_quant/strategy_lab.py`
- Modify: `tests/test_strategy_lab.py`
- Modify: `pyproject.toml`

### 實作順序

1. Config trust boundary只接受 `DEMO`，必要且經重新查證時才接受 `TESTNET`；明確拒絕 `LIVE`、production URL override、真金 account ID、缺失 `risk_policy_id` 與缺失 risk limits。Demo 與 Testnet 必須是不同 credential profile、runtime/environment identity 與 evidence cohort，不能混成一級。
2. Credentials 只從執行時環境取得，預設使用官方 adapter 的 `BINANCE_DEMO_API_KEY`／`BINANCE_DEMO_API_SECRET`。專用 Demo key 的建立、交易權限、IP 限制與模擬餘額是 operator prerequisite；若另驗 Testnet，使用獨立的 Testnet credentials；key/secret 永不寫 artifact。
3. 先做 signed connectivity／server time／account readback，並建立帶 as-of/hash 的 execution metadata snapshot：instrument specification、tick size、step size、min qty/notional、Binance filters、leverage/margin constraints、Mark Price semantics、account適用的 actual fee schedule／execution-cost policy；缺任一 required identity 不允許 order。這些是 execution/risk truth，不加入 historical alpha features。
4. 用 bounded runtime、低 notional、固定最大 orders／position／timeout／flatten-on-exit 的 deterministic lifecycle suite 驗證：
   - passive GTC accept → modify → cancel；
   - 最小 market open → fill；
   - reduce-only close → fill；
   - conditional order（adapter 支援面內）；
   - user-stream order/fill/position/account event；
   - 接受一張 passive order 後進行 reconnect + process restart，reconciliation 必須啟用；
   - deterministic client-order ID 與 duplicate suppression。
5. Binance `503` unknown execution outcome 不得直接重送；先以 user stream／order query reconcile，再決定下一步。[2]
6. Infrastructure lifecycle PASS 後，再讓一個 Paper survivor 使用同 `FamilyStrategy`、同一 frozen Risk & Execution Policy 與 venue-specific Demo envelope 跑 bounded window，核對 signal/order identity。不得因看到 Demo fill 後放寬 Paper 的 risk policy；任何 policy/metadata變更形成新 evidence identity。
7. Execution defect → `FIX_TECHNICAL`；signal mismatch → 回 Card 1 並重跑 parity/Paper/Demo；不得因 Demo fill 好壞修改 alpha。
8. Evidence 保存 request intent、client/venue IDs、ack/fill/cancel/reconciliation、runtime/environment、endpoint class、Risk & Execution Policy ID、Mark Price與 instrument/filter/fee snapshot IDs、timestamps、reason codes與 hashes，但移除 credentials／signatures／敏感 headers。

### 驗收

- 實際 environment readback 為 `DEMO`（或經記錄理由的 `TESTNET`），絕非 `LIVE`。
- Demo／Testnet 使用不同 credential profile、runtime ID 與 cohort；任一環境證據不得冒充另一個環境。
- Instrument/filter/leverage/margin/Mark Price/actual-fee snapshot 可由 artifact hash 讀回；修改任一 byte 會讓既有 Demo verdict失效。
- Demo evidence 綁定 frozen `risk_policy_id`；缺失或與 Paper cohort 不一致時 fail closed。
- 上述 lifecycle 每一條都有 venue/user-stream evidence。
- 受控斷線與 process restart 後，已接受的 passive order、positions/account reconciliation 一致，沒有 duplicate client-order ID。
- 每次 bounded suite 結束 terminal flat、沒有 open orders；超出 max orders／position／timeout 時自動 fail closed 並 flatten。
- Unknown outcome 不產生 duplicate order。
- 一個 Paper survivor 的 signal/order mapping 在 Demo 通過；Demo PnL 不進 alpha promotion metric。

### 停止線

沒有 dedicated simulated credentials、environment 無法讀回、Risk & Execution Policy／Mark Price／instrument/filter/fee identity 缺失、帳戶已有未知 open position/order、或任一 production endpoint guard 失效時，禁止送出 order。

## 10. Card 6 — `PROMOTION-PROJECTION-V1`

### 目的

把完整證據鏈變成一個可查詢的 promotion verdict 與簡潔 operator view；不先建 Dashboard。

### 預期檔案

- Create: `src/nautilus_quant/strategy_promotion.py`
- Create: `tests/test_strategy_promotion.py`
- Create: `config/strategy_promotion_policy.json`
- Create: `docs/contracts/strategy-promotion-v1.md`
- Create: `docs/contracts/strategy-funnel-v2.md`
- Modify: `src/nautilus_quant/strategy_lab.py`
- Modify: `tests/test_strategy_lab.py`

### 實作順序

1. Promotion policy 在讀 survivor 結果前凍結，至少含 required tier/version、Paper cohort要求、signal mismatch上限、technical status、Demo lifecycle requirements，以及 screen/robustness/cost/risk/promotion policy IDs、data snapshot/as-of、family/kernel/code/runtime/environment identities。
2. `promotion_eligible` 只能由 ledger 查得的完整 chain 導出：family golden tests → Candidate v2 → formal Signal Parity Gate → research screen + complete trial census → Nautilus historical → robustness → Strategy Freeze → Paper → Demo。
3. 缺少 stage、artifact、hash、policy identity 或 prospective boundary 一律 `N/A/BLOCKED`，不得當成零或 PASS。
4. 產出 `latest-operator-summary.json`／`.md`：survivors、attrition、technical-invalid count、top reason codes、current tier、Paper/Demo health、next action、完整 data/policy/kernel/runtime/environment IDs。
5. 產出 canonical `strategy-funnel-v2`，所有層級只讀 ledger/runtime evidence；後兩級移除 hard-coded `(0,0,0)`，technical invalid 另列且不算 strategy attrition。
6. 增加 canonical ledger/artifact manifest，供外部備份工具驗 hash；備份目的地與 retention 另由 operator 指定，不把 runtime artifacts塞進 Git。
7. 完成後仍只得到「可進入另立 Runtime Qualification／Live plan 的候選」，不自動升級 Nautilus、不建立 Live config、不部署真金。

### 驗收

- 缺任一 tier 時 promotion fail closed。
- 修改任何 artifact byte、policy/data/kernel/runtime/environment ID、family version 或 prospective cohort 會使既有 promotion verdict失效。
- Funnel、operator summary 與 direct SQLite query一致。
- Live authorization欄位不存在或固定為 `NOT_AUTHORIZED`。

## 11. Post-plan gate — `RUNTIME-QUALIFICATION`（不是目前功能卡）

這個 gate 位於 `promotion_eligible` 之後、HUMAN/LIVE POLICY BOUNDARY 之下；只有 operator 另行授權真金 Live 規劃時才建立施工卡。前六卡不等待它，也不能借它升級目前 pin 的 NautilusTrader `2.0.0rc2`。

未來 qualification 至少必須：

1. 重新查證並選定適合真金使用的 stable NautilusTrader；任何升級建立全新 `runtime_identity`。
2. 在新 runtime 跑完整 root/research regression、family golden vectors、batch/incremental/Signal Parity Gate parity。
3. 重驗 Nautilus historical fills/fees/Funding/positions/PnL/accounting parity，不能無條件沿用 rc2 verdict。
4. 重跑 Paper prospective regression與 Binance Demo lifecycle suite。
5. 通過 restart、reconciliation、unknown-order outcome、duplicate-order prevention、terminal-flat與 fail-closed `LIVE` environment binding tests。
6. 由新 runtime identity 重新產生 promotion projection；缺任一證據即 `N/A/BLOCKED`。

通過 Runtime Qualification 仍不等於取得真金額度或 Live 授權；資金、風險、值守、kill switch與啟用程序仍須另立 operator-approved Live contract。

## 12. 尚缺的 operator／外部輸入

下列不是程式碼能自說自話補出的資料；必須在對應卡開始實跑前一次性凍結：

1. **Research/campaign budget**：每 cohort 最大 strategy 數、可用 instruments/bar types、search/parameter policy、最大 CPU／wall time。
2. **Screen/robustness policy**：最低活動量、最大 drawdown/turnover、window 定義、cost/slippage/delay/neighborhood stress；必須在看結果前固定。
3. **Risk & Execution Policy envelope**：position sizing、leverage/exposure/max-loss、order mapping、stale/reconnect、duplicate prevention、kill switch，以及 fee/slippage/Funding treatment。
4. **Paper admission policy**：最短 prospective wall-clock／completed-bar 數、允許 gap/revised bar、至少幾次 restart/reconnect、technical/economic rejection條件。
5. **Demo venue envelope**：專用模擬帳戶、最大 notional、leverage/margin mode、允許 order types、order-rate上限、metadata/fee snapshot freshness 與 emergency flatten規則。
6. **Dedicated Demo credentials**：由 operator 在 Binance Demo 建立與執行時注入；目前 repo 中沒有、也不應有。若另用 legacy Testnet，必須是不同 profile/cohort，且不是 Card 1～4 的 prerequisite。
7. **Runtime backup target**：ledger/artifact 備份位置、retention、restore drill頻率；Git只保存程式與 contract，不保存實跑帳務／交易 artifacts。
8. **Notification policy**：Paper/Demo technical failure、strategy attrition、promotion candidate 要送到哪個 Discord channel，以及哪些只記 ledger 不打擾 operator。
9. **Live policy**：不在本計畫內；未來需另決定真金額度、風險、kill switch、值守與授權。

小蒨可自主決定新 family／formula、提出上述 policy 建議並執行已凍結規則；但不能在看過結果後偷偷放寬 gate，也不能替 operator 建立或猜測 credentials。

## 13. 每卡共同驗證

每張功能卡完成最後一次 write 後都執行：

```bash
# Card-specific focused tests
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest <focused modules> -v

# Entire root suite
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q

# Entire isolated research suite
PYTHONDONTWRITEBYTECODE=1 research/.venv/bin/python -m unittest \
  discover -s research -p 'test*.py' -q

# Canonical data health
.venv/bin/nautilus-data status --config config/market_data.json

# Tracked diff before staging
git diff --check
git status --short
```

接著：

```bash
git add <card-scoped tracked files>
.venv/bin/python scripts/check_secrets.py --staged
git diff --cached --check
git commit -m "<card-specific message>"
git push origin main
git fetch origin main
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
git status --short
```

不得使用 `--no-verify`。`data/`、`var/`、`.venv/`、research runtime output、credentials 都不得 staged。

Paper 與 Demo 卡另須附真實 runtime evidence；mock/unit tests只能證明 trust boundary，不能代替 live market data、sandbox account 或 venue adapter實跑。

## 14. 建置 readiness 結論

- **Card 1 `FAMILY-KERNEL-V2` 已在本次變更實作**；不需要 credentials，也沒有碰 Live／Paper order。
- **下一個規劃 target 是 Card 2 `CAMPAIGN-SCREEN-V1`，但尚未因 Card 1 完成而自動授權。**獲得明確授權後才依序施工；先保存完整 trial census，再做 Card 3 robustness，不跳階。
- **Card 4 程式與 synthetic tests：前 3 卡通過後即可施工**；正式 prospective Paper evidence 需先凍結 Risk & Execution／Paper admission policy，並經過真實 production-data smoke。
- **Card 5 程式與 fail-closed tests：Card 4 通過後即可施工**；venue 驗活會卡在 dedicated Binance Demo credentials、metadata/fee snapshot 與 Demo envelope，這是正當外部 gate。
- **Card 6：必須等 Paper + Demo 真證據存在**，否則只能產生 `N/A/BLOCKED`，不得造假完成度。
- **Runtime Qualification：不在目前六卡授權內；不阻塞 Card 1～6，也不允許現在任意升級 rc2。**
- **Live：未授權、未規劃、不可因前六卡成功而自動發生。**

## 15. Sources

[1] NautilusTrader Binance integration — environments、Demo/Testnet 建議與 adapter能力：<https://nautilustrader.io/docs/latest/integrations/binance>

[2] Binance USD-M General Info — simulated endpoints、503 unknown-outcome reconciliation：<https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info>

[3] NautilusTrader Live Trading — live node lifecycle、startup reconciliation與 coordinated shutdown：<https://nautilustrader.io/docs/latest/concepts/live>

本機安裝介面證據：

- `.venv/lib/python3.13/site-packages/nautilus_trader/live/__init__.pyi`
- `.venv/lib/python3.13/site-packages/nautilus_trader/adapters/sandbox/__init__.pyi`
- `.venv/lib/python3.13/site-packages/nautilus_trader/adapters/binance/__init__.pyi`
