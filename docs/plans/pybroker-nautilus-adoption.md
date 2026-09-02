# PyBroker 策略研究前端導入計畫

> **註記（2026-09-02，正典溯源）：** 本文為歷史實作計畫，原文的線性／縱切措辭（含上下游箭頭）為實作溯源而保留。當前正典詮釋為「Three Layers, Two Loops, One Gate」：Hermes Loop A（low-frequency）→ PyBroker Loop B（high-throughput attrition）→ fail-closed Signal-Parity Gate → Nautilus high-fidelity（survivors only）。歷史措辭不得被解讀為一對一、逐次回測皆呼叫 LLM 的流程（Loop B 為確定性 N-candidate 批次 attrition，無 LLM per candidate）。

## 目標

把 PyBroker 導入為 Nautilus Quant System 的**上游策略發源地**：它負責研究、試跑與產生候選；NautilusTrader 繼續負責後續正式驗證，並保持唯一的回測、成交、費用、Funding、部位與帳務真值。

```text
既有 Nautilus canonical bars（唯讀）
        ↓
隔離的 PyBroker research environment
        ↓
實跑一個最小策略
        ↓
純資料 candidate
        ↓
NautilusTrader 後續正式驗證
```

本計畫只完成這條縱切，不建立完整研究平台、promotion 系統或交易部署流程。

## 固定邊界

1. PyBroker 位於獨立 `research/` 環境，不加入正式 root runtime 的 dependency graph。
2. Research 只讀既有完整市場資料；不得改寫 canonical catalog、Funding store 或同步狀態。
3. PyBroker 不持有 API key、交易權限或訂單指令，也不接 Shadow、Testnet 或 live。
4. PyBroker 結果一律是 research/provisional；正式 PnL、fees、Funding 與 accounting 只由 NautilusTrader 判定。
5. Candidate 只含普通 JSON／JSONL 資料，不含 pickle、joblib、framework object、import path 或可執行 payload。
6. 既有 FundingObservation 修正是已完成的資料基礎，不再作為 PyBroker 導入的等待條件或額外驗收工作。
7. Hermes、LLM 或 research environment 故障不得影響既有 Nautilus 資料同步。

## 這次要做

以 `BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL` 為第一條研究路徑：

1. 建立可從 lock 重建的隔離 PyBroker environment。
2. 以唯讀 adapter 載入既有 bars，轉成 PyBroker 所需欄位。
3. 實跑一個 deterministic 的 long／flat 參考策略。
4. 輸出一份符合 [`pybroker-candidate-v1.md`](../contracts/pybroker-candidate-v1.md) 的純資料 candidate。
5. 用同一份輸入重跑，確認 canonical candidate bytes／hash 相同。
6. 確認 root Nautilus runtime、資料同步與既有測試不受影響。

策略是否有 alpha、PyBroker 與 Nautilus 的績效是否一致，都不是本次導入通過條件。這次只證明研究前端能安全產生可交棒的候選。

## 明確不做

- 不建立 Stage 0～6、逐階段批准、獨立 audit 或 closeout 儀式。
- 不建立共用 Strategy Core、七件套 Candidate Capsule、promotion state machine、sealed holdout 或 hash-chained lifecycle。
- 不要求 12 個雙路徑 benchmark、routing recall 或效率門檻。
- 不新增 Dashboard、MCP、queue、database、daemon、service、cron 或第二套排程。
- 不接 Qlib、AutoML、深度學習、多商品、多 timeframe 或 portfolio optimizer。
- 不修改正式 execution、risk、OMS、credentials 或 live wiring。

上述能力只有在日後有實際瓶頸或明確需求時，才另行設計；不能阻擋本次縱切。

## 最小交付物

- `research/pyproject.toml` 與 lock file；
- 一個唯讀資料 adapter；
- 一個最小 PyBroker strategy runner；
- 一個 candidate writer；
- 對應的窄測試；
- 一份 ignored 的實跑 candidate artifact；
- 簡短操作說明。

具體檔名以實作時的最小 coherent diff 為準；本計畫不預先規定 13 個 task 或大型目錄樹。

## 完成條件

以下全部有真實工具輸出，即稱為「PyBroker 策略研究前端已導入」：

- [ ] Research environment 可從 lock 重建，且 root environment 不含 PyBroker。
- [ ] BTCUSDT 1H 既有資料可唯讀載入，不修改 canonical bytes。
- [ ] 最小 PyBroker strategy 真實執行成功。
- [ ] 產出符合最小 contract 的純資料 candidate。
- [ ] 同輸入重跑產生相同 canonical candidate hash。
- [ ] Candidate 可由 Nautilus 端以 stdlib 讀取，足以進入後續正式驗證。
- [ ] 既有 root 測試與資料狀態檢查全綠。
- [ ] Tracked source 完成檢查、commit、push 與遠端讀回；`data/`、`var/` 和實跑 artifacts 不上傳。

## Kanban 執行原則

本次剩餘工作只使用**一張可執行縱切卡**。卡片直接引用本文件當前 tracked 版本，依上述完成條件施工；不再用 phase umbrella、GATE、CLOSE、approval placeholder、watchdog 或 plan-reference verifier 製造額外依賴。

先前已完成的 Funding 文件、契約、migration、cutover 與測試保留為歷史成果；舊 P0 尾閘與尚未執行的七階段卡片以 `scope superseded` 收束，不冒充技術 PASS，也不阻擋這張縱切卡。
