# PyBroker Candidate v1

## 用途

這是 PyBroker 研究前端交給 Nautilus 後續驗證的**最小純資料格式**。它不是正式績效、帳務結果或交易指令。

第一版每個 candidate 只有一個檔案：

```text
candidate.json
```

## Canonical encoding

- UTF-8；
- JSON object；
- keys 依字典序；
- separators 為 `,`、`:`，不加多餘空白；
- 檔尾一個 LF；
- 禁止 NaN、Infinity；
- candidate ID 為整個 `candidate.json` bytes 的 SHA-256，不寫回檔內，避免 self-hash cycle。

## 必填欄位

```json
{
  "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
  "instrument_id": "BTCUSDT-PERP.BINANCE",
  "runtime": {
    "pybroker_version": "1.2.14",
    "python_version": "3.12.x",
    "seed": 42
  },
  "schema_version": "pybroker-candidate-v1",
  "signals": [
    {
      "intent": "LONG",
      "score": 0.73,
      "ts_event_ns": 0
    }
  ],
  "source": {
    "first_ts_event_ns": 0,
    "last_ts_event_ns": 0,
    "row_count": 1,
    "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
  },
  "strategy": {
    "decision_timing": "bar-close; effective no earlier than next event",
    "name": "<strategy name>",
    "parameters": {}
  },
  "truth_status": "provisional"
}
```

## 驗證規則

- `schema_version` 必須完全相符，`truth_status` 必須是 `provisional`。
- `instrument_id`、`bar_type` 與 source window 必須明示。
- `source.row_count` 為正整數；source digest 為 64 位小寫 SHA-256。
- `signals` 按 `ts_event_ns` 嚴格遞增，不得重複。
- `intent` 第一版只允許 `LONG` 或 `FLAT`；`score` 必須 finite。
- `strategy.parameters` 只接受普通 JSON 值。
- 檔案不得含 Python、pickle、joblib、cache、import path、quantity、order type、leverage、credentials 或正式 PnL／accounting 宣稱。
- Writer 先寫暫存檔、readback 驗證後再原子發布；同一輸入、strategy、seed 重跑必須得到相同 bytes 與 hash。

Nautilus 端第一步只需用 Python stdlib 解析並驗證上述欄位。正式訊號重算、成交、費用、Funding 與帳務驗證屬後續工作，不阻擋研究前端完成第一條縱切。

> ponytail: v1 刻意使用單一 JSON。只有實測檔案大小或串流記憶體成為瓶頸時，才升級成 manifest + JSONL。
