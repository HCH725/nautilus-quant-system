from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile


INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
BAR_TYPE = f"{INSTRUMENT_ID}-1-HOUR-LAST-EXTERNAL"
SYMBOL = INSTRUMENT_ID
SEED = 42
WINDOW = 24
FIXED_SCALAR = 10**16


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def decode_fixed(value: bytes) -> float:
    return int.from_bytes(value, "little", signed=True) / FIXED_SCALAR


def write_candidate(candidate: dict[str, object], output: Path) -> str:
    payload = canonical_json(candidate)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
    try:
        if tmp_path.read_bytes() != payload:
            raise OSError("candidate temporary-file readback mismatch")
        if json.loads(tmp_path.read_bytes()) != candidate:
            raise OSError("candidate temporary-file JSON readback mismatch")
        os.replace(tmp_path, output)
        if output.read_bytes() != payload:
            raise OSError("candidate published-file readback mismatch")
    finally:
        tmp_path.unlink(missing_ok=True)
    return sha256(payload).hexdigest()


def _catalog_digest(catalog: Path) -> tuple[str, list[Path]]:
    paths = sorted((Path(catalog) / "data" / "bars" / BAR_TYPE).glob("*.parquet"))
    if not paths:
        raise FileNotFoundError(f"no canonical bars found for {BAR_TYPE}")
    digest = sha256()
    for path in paths:
        digest.update(path.relative_to(catalog).as_posix().encode())
        digest.update(b"\0")
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest(), paths


def load_bars(catalog: Path):
    import pandas as pd
    import pyarrow.parquet as parquet

    source_hash, paths = _catalog_digest(catalog)
    frames = []
    for path in paths:
        table = parquet.read_table(path)
        metadata = table.schema.metadata or {}
        if metadata.get(b"bar_type", b"").decode() != BAR_TYPE:
            raise ValueError(f"unexpected bar_type metadata in {path}")
        columns = table.to_pydict()
        ts_event = columns["ts_event"]
        frames.append(
            pd.DataFrame(
                {
                    "date": pd.to_datetime(ts_event, unit="ns", utc=True),
                    "symbol": SYMBOL,
                    **{
                        field: [decode_fixed(value) for value in columns[field]]
                        for field in ("open", "high", "low", "close")
                    },
                    "volume": [decode_fixed(value) for value in columns["volume"]],
                    "ts_event_ns": ts_event,
                },
            ),
        )
    data = pd.concat(frames, ignore_index=True).sort_values("ts_event_ns", kind="stable").reset_index(drop=True)
    if data.empty or data["ts_event_ns"].duplicated().any() or not data["ts_event_ns"].is_monotonic_increasing:
        raise ValueError("canonical bars must be non-empty, unique, and ordered")
    return data, source_hash


def run(catalog: Path, output: Path) -> dict[str, object]:
    import pybroker
    from pybroker import Strategy, StrategyConfig

    pybroker.disable_logging()
    pybroker.register_columns("ts_event_ns")
    data, source_hash = load_bars(catalog)
    signals: list[dict[str, object]] = []

    def execute(ctx) -> None:
        if len(ctx.close) < WINDOW:
            return
        score = round(float(ctx.close[-1] / ctx.close[-WINDOW] - 1), 12)
        wants_long = score > 0
        is_long = ctx.long_pos() is not None
        if wants_long == is_long:
            return
        signals.append(
            {
                "intent": "LONG" if wants_long else "FLAT",
                "score": score,
                "ts_event_ns": int(ctx.ts_event_ns[-1]),
            },
        )
        if wants_long:
            ctx.buy_shares = 1
        else:
            ctx.sell_all_shares()

    strategy_data = data[["date", "symbol", "open", "high", "low", "close", "volume", "ts_event_ns"]]
    strategy = Strategy(
        strategy_data,
        start_date=data.iloc[0]["date"].tz_localize(None).to_pydatetime(),
        end_date=data.iloc[-1]["date"].tz_localize(None).to_pydatetime(),
        config=StrategyConfig(initial_cash=1_000_000, exit_on_last_bar=True),
    )
    strategy.add_execution(execute, SYMBOL)
    result = strategy.backtest(seed=SEED, disable_parallel=True, calc_bootstrap=False)
    if result.portfolio.empty:
        raise RuntimeError("PyBroker returned an empty portfolio")

    candidate = {
        "bar_type": BAR_TYPE,
        "instrument_id": INSTRUMENT_ID,
        "runtime": {
            "pybroker_version": pybroker.__version__,
            "python_version": sys.version.split()[0],
            "seed": SEED,
        },
        "schema_version": "pybroker-candidate-v1",
        "signals": signals,
        "source": {
            "first_ts_event_ns": int(data.iloc[0]["ts_event_ns"]),
            "last_ts_event_ns": int(data.iloc[-1]["ts_event_ns"]),
            "row_count": len(data),
            "sha256": source_hash,
        },
        "strategy": {
            "decision_timing": "bar-close; effective no earlier than next event",
            "name": "lookback-momentum-long-flat",
            "parameters": {"lookback_bars": WINDOW},
        },
        "truth_status": "provisional",
    }
    candidate_id = write_candidate(candidate, output)
    return {"candidate_id": candidate_id, "orders": len(result.orders), "signals": len(signals)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the isolated PyBroker research candidate generator")
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.catalog.resolve(), args.output.resolve()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())