# Strategy robustness V1 contract

This contract defines the bounded formal robustness tier. It is historical
evidence only; it does not write canonical market data, Paper, Demo/Testnet, or
Live state.

Every run binds hypothesis, strategy, experiment, candidate, data source and
snapshot, data-as-of, evaluation context, policy, engine, runtime, and code
commit. Card 3 request timestamps are UTC `Z` values, ordered, and data after
`data_as_of_ns` is never read.

The tracked matrix contains bounded expanding and rolling windows with
deterministic TREND/RANGE/HIGH_VOLATILITY labels and seven stress scenarios:
baseline, 2x fee, 2x funding, one-bar delay, low parameter, high parameter,
and one-tick slippage. Every window × scenario cell receives one formal
Nautilus evaluation. Fees remain Nautilus instrument metadata; Funding remains
the canonical FundingObservation generation. Parameter neighborhoods are
copies from the shared family kernel and never mutate the candidate.

DSR and PBO are explicitly `NOT_MODELED`. Missing cells, technical failures,
unclear Funding truth, or an unmodeled cost cannot pass robustness.
