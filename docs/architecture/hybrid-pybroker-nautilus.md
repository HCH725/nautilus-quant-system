# PyBroker × NautilusTrader 責任邊界

## 決策

PyBroker 是可移除的上游策略研究前端；NautilusTrader 保持 canonical data、正式回測、成交、費用、Funding、部位、帳務與未來 execution 的唯一真值。

```text
Nautilus canonical bars（唯讀）
        ↓
PyBroker research：研究、試跑、候選產生
        ↓
純資料 candidate
        ↓
NautilusTrader：後續正式驗證
```

這是責任分工，不是逐階段授權鏈。既有 FundingObservation 修正已完成，不再阻擋 PyBroker 研究前端導入。

## 執行環境

| 環境 | 責任 | 邊界 |
|---|---|---|
| Root runtime | 資料同步、canonical storage、Nautilus 正式驗證 | 不加入 PyBroker；不接受 provisional metrics 當正式績效 |
| `research/` | PyBroker 策略研究與 candidate 產生 | 唯讀市場資料；無憑證、訂單、Testnet 或 live 權限 |

刪除 `research/` 必須不影響 root runtime 或每日資料同步。兩側不共享 framework object、cache、pickle、帳本或 credentials。

## Domain ownership

| Domain | Owner |
|---|---|
| Binance public ingestion、D-1 completeness、canonical bars／Funding | Nautilus data foundation |
| 策略研究、PyBroker execution、provisional screening | PyBroker research frontend |
| Candidate 格式與來源識別 | 純資料 contract |
| Fills、fees、Funding、positions、PnL、accounting | NautilusTrader |
| Risk、sizing、OMS、execution、credentials | Nautilus runtime；本次不修改 |

## Candidate 邊界

第一版 candidate 只需符合 [`pybroker-candidate-v1.md`](../contracts/pybroker-candidate-v1.md)：普通 canonical JSON、可由 Python stdlib 讀取、不含程式碼或可執行序列化。它表達「研究前端提出了什麼」，不宣稱 Nautilus 已驗證，也不攜帶正式帳務結果。

## 永久紅線

- PyBroker 不改寫 canonical catalog、Funding store 或同步狀態。
- PyBroker 不持有 API key、交易權限或訂單指令。
- PyBroker 輸出一律標記 provisional。
- Candidate 不含 pickle、joblib、import path 或 framework object。
- Shadow、Testnet、live 與商業化不在本次導入範圍。
- Hermes、LLM 或 research environment 故障不得影響既有資料同步。

## 何時才擴充

只有實測出現需求，才考慮多檔 capsule、immutable snapshot service、cross-framework parity、promotion state machine、sealed holdout、批量 benchmark 或研究 Dashboard；它們不是第一條縱切的前置條件。
