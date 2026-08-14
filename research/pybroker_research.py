from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile


INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
BAR_TYPE = f"{INSTRUMENT_ID}-1-HOUR-LAST-EXTERNAL"
SYMBOL = INSTRUMENT_ID
SEED = 42
FIXED_SCALAR = 10**16
HYPOTHESIS_FIELDS = {
    "bar_type",
    "based_on_verdict_id",
    "falsification",
    "instrument_id",
    "parameters",
    "parent_strategy_id",
    "schema_version",
    "strategy_family",
    "thesis",
}
PARAMETER_FIELDS = {"entry_threshold", "lookback_bars"}
HEX_DIGITS = frozenset("0123456789abcdef")
REPOSITORY_DATA = Path(__file__).resolve().parents[1] / "data"


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def decode_fixed(value: bytes) -> float:
    return int.from_bytes(value, "little", signed=True) / FIXED_SCALAR


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise ValueError(f"hypothesis must contain only finite JSON values: {value}")


def load_hypothesis(path: Path) -> tuple[int, float]:
    payload = Path(path).read_bytes()
    try:
        root = json.loads(payload, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("hypothesis must be UTF-8 JSON") from error
    if not isinstance(root, dict) or set(root) != HYPOTHESIS_FIELDS:
        raise ValueError("invalid hypothesis fields")
    if root["schema_version"] != "strategy-hypothesis-v1":
        raise ValueError("unsupported hypothesis schema_version")
    if root["strategy_family"] != "lookback-momentum-long-flat":
        raise ValueError("unsupported strategy family")
    if root["instrument_id"] != INSTRUMENT_ID:
        raise ValueError("unsupported instrument_id")
    if root["bar_type"] != BAR_TYPE:
        raise ValueError("unsupported bar_type")
    if not isinstance(root["thesis"], str) or not root["thesis"].strip():
        raise ValueError("thesis must be a non-empty string")
    if not isinstance(root["falsification"], str) or not root["falsification"].strip():
        raise ValueError("falsification must be a non-empty string")
    parent = root["parent_strategy_id"]
    verdict = root["based_on_verdict_id"]
    for field, value in (("parent_strategy_id", parent), ("based_on_verdict_id", verdict)):
        if value is not None and (
            not isinstance(value, str) or len(value) != 64 or not set(value) <= HEX_DIGITS
        ):
            raise ValueError(f"{field} must be null or lowercase SHA-256")
    if (parent is None) != (verdict is None):
        raise ValueError("lineage fields must both be null or both be populated")
    parameters = root["parameters"]
    if not isinstance(parameters, dict) or set(parameters) != PARAMETER_FIELDS:
        raise ValueError("invalid parameters fields")
    lookback_bars = parameters["lookback_bars"]
    if isinstance(lookback_bars, bool) or not isinstance(lookback_bars, int):
        raise ValueError("lookback_bars must be an integer")
    if not 1 <= lookback_bars <= 8_760:
        raise ValueError("lookback_bars must be between 1 and 8760")
    entry_threshold = parameters["entry_threshold"]
    if isinstance(entry_threshold, bool) or not isinstance(entry_threshold, (int, float)):
        raise ValueError("entry_threshold must be a number")
    if not math.isfinite(entry_threshold) or entry_threshold < 0:
        raise ValueError("entry_threshold must be finite and non-negative")
    normalized_threshold = 0.0 if entry_threshold == 0 else float(entry_threshold)
    normalized = dict(
        root,
        parameters={"entry_threshold": normalized_threshold, "lookback_bars": lookback_bars},
    )
    if payload != canonical_json(normalized):
        raise ValueError("hypothesis must use canonical JSON encoding")
    return lookback_bars, normalized_threshold


def validate_output_path(catalog: Path, output: Path) -> Path:
    """Resolve output and reject every canonical-data destination."""
    lexical_catalog = Path(catalog).absolute()
    resolved_catalog = lexical_catalog.resolve()
    output = Path(output).resolve()
    lexical_data = (
        lexical_catalog.parent if lexical_catalog.name == "catalog" else lexical_catalog
    ).resolve()
    resolved_data = (
        resolved_catalog.parent if resolved_catalog.name == "catalog" else resolved_catalog
    )
    for canonical_data in (REPOSITORY_DATA.resolve(), lexical_data, resolved_data):
        if output == canonical_data or canonical_data in output.parents:
            raise ValueError("candidate output must be outside canonical data")
    return output


def write_candidate(candidate: dict[str, object], output: Path) -> str:
    payload = canonical_json(candidate)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if output.read_bytes() == payload:
            return sha256(payload).hexdigest()
        raise OSError(f"immutable candidate conflict: {output}")
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


def run(catalog: Path, output: Path, *, hypothesis: Path) -> dict[str, object]:
    lookback_bars, entry_threshold = load_hypothesis(hypothesis)
    catalog_path = Path(catalog)
    catalog = catalog_path.resolve()
    output = validate_output_path(catalog_path, output)

    import pybroker
    from pybroker import Strategy, StrategyConfig

    pybroker.disable_logging()
    pybroker.register_columns("ts_event_ns")
    data, source_hash = load_bars(catalog)
    signals: list[dict[str, object]] = []

    def execute(ctx) -> None:
        if len(ctx.close) < lookback_bars:
            return
        score = round(float(ctx.close[-1] / ctx.close[-lookback_bars] - 1), 12)
        wants_long = score > entry_threshold
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
            "parameters": {
                "entry_threshold": entry_threshold,
                "lookback_bars": lookback_bars,
            },
        },
        "truth_status": "provisional",
    }
    candidate_id = write_candidate(candidate, output)
    return {
        "candidate_id": candidate_id,
        "provisional_metrics": {"orders": len(result.orders), "signals": len(signals)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the isolated PyBroker research candidate generator")
    parser.add_argument("--hypothesis", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(
        args.catalog,
        args.output,
        hypothesis=args.hypothesis,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
