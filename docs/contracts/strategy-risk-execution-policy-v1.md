# Strategy Risk & Execution Policy v1

This tracked, canonical JSON policy is independent from the alpha family and contains no credentials. Its SHA-256 is `risk_policy_id`; changing any byte creates a new Paper execution identity and invalidates reuse.

Required fail-closed fields cover allowed position intents, quantity/notional/leverage and gross/net/per-symbol exposure caps, maximum loss, order mapping, reduce-only exit, fee/slippage/Funding treatment, stale-data and reconnect behavior, signal-ID duplicate prevention, order-rate limit, kill switch, and flatten-on-exit.

`allow_live_execution` is always `false`. Card 4 permits production Binance USD-M market data only. SHADOW registers no execution client. PAPER registers only Nautilus sandbox execution with active risk; real-money credentials, Binance LIVE execution, and production order endpoints are outside authorization.

Every accepted policy must use canonical finite JSON bytes, `FLAT`/`LONG` intents, MARKET mapping, reduce-only exits, positive bounded limits, and an exposure envelope no smaller than maximum notional. Missing, noncanonical, conflicting, or changed policy evidence blocks Strategy Freeze and Paper reuse.
