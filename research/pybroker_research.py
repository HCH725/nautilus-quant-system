from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# ponytail: the isolated research venv imports only the pure shared kernel from src;
# installing the full Nautilus package here would couple the two runtimes.
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
from nautilus_quant.runtime_attestation import (  # pyright: ignore[reportMissingImports]  # noqa: E402
    research_runtime_identity,
)
from nautilus_quant.strategy_families import (  # noqa: E402
    DEFAULT_REGISTRY,
    KERNEL_HASH,
    KERNEL_VERSION,
    ClosedBar,
    FamilyKernelError,
    evaluate_batch,
)


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
HYPOTHESIS_FIELDS_V2 = HYPOTHESIS_FIELDS | {"family_version"}
PARAMETER_FIELDS = {"entry_threshold", "lookback_bars"}
HEX_DIGITS = frozenset("0123456789abcdef")
REPOSITORY_DATA = Path(__file__).resolve().parents[1] / "data"


def _require_isolated_runtime() -> None:
    """Require the controller's dedicated interpreter and no ambient import path."""
    if not sys.flags.isolated:
        raise RuntimeError("research runtime must run in isolated mode")
    environment = (REPOSITORY_ROOT / "research/.venv").resolve()
    if Path(sys.executable).resolve() != (environment / "bin/python").resolve():
        raise RuntimeError("research executable is not the attested interpreter")
    if Path(sys.prefix).resolve() != environment:
        raise RuntimeError("research prefix is not the attested environment")
    if "PYTHONPATH" in os.environ:
        raise RuntimeError("research runtime received PYTHONPATH")


def _require_trusted_origins(pybroker_module: object) -> None:
    """Ensure imports resolve only to the attested research env and shared source."""
    if not sys.flags.isolated:
        return
    import nautilus_quant.strategy_families as family_kernel

    environment = (REPOSITORY_ROOT / "research/.venv").resolve()
    pybroker_origin = getattr(pybroker_module, "__file__", None)
    kernel_origin = getattr(family_kernel, "__file__", None)
    if not isinstance(pybroker_origin, str) or not isinstance(kernel_origin, str):
        raise RuntimeError("research module origin is missing")
    try:
        Path(pybroker_origin).resolve().relative_to(environment)
        Path(kernel_origin).resolve().relative_to(REPOSITORY_ROOT / "src")
    except ValueError as error:
        raise RuntimeError("research module origin is untrusted") from error


@dataclass(frozen=True, slots=True)
class HypothesisSpec:
    schema_version: str
    family_id: str
    family_version: str
    parameters: dict[str, object]


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


def load_hypothesis(path: Path) -> HypothesisSpec:
    payload = Path(path).read_bytes()
    try:
        root = json.loads(payload, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("hypothesis must be UTF-8 JSON") from error
    if not isinstance(root, dict):
        raise ValueError("invalid hypothesis fields")
    schema_version = root.get("schema_version")
    if schema_version == "strategy-hypothesis-v1":
        expected_fields = HYPOTHESIS_FIELDS
        family_version = "lookback-momentum-long-flat-v1"
    elif schema_version == "strategy-hypothesis-v2":
        expected_fields = HYPOTHESIS_FIELDS_V2
        family_version = root.get("family_version")
        if not isinstance(family_version, str) or not family_version:
            raise ValueError("family_version must be a non-empty string")
    else:
        raise ValueError("unsupported hypothesis schema_version")
    if set(root) != expected_fields:
        raise ValueError("invalid hypothesis fields")
    family_id = root["strategy_family"]
    if not isinstance(family_id, str) or not family_id:
        raise ValueError("unsupported strategy family")
    if schema_version == "strategy-hypothesis-v1" and family_id != "lookback-momentum-long-flat":
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
    if not isinstance(parameters, dict):
        raise ValueError("invalid parameters fields")
    if schema_version == "strategy-hypothesis-v1" and set(parameters) != PARAMETER_FIELDS:
        raise ValueError("invalid parameters fields")
    try:
        definition = DEFAULT_REGISTRY.resolve(family_id, family_version)
        normalized_parameters = definition.validate_parameters(parameters)
    except FamilyKernelError as error:
        raise ValueError(str(error)) from error
    normalized = dict(
        root,
        parameters=normalized_parameters,
    )
    if payload != canonical_json(normalized):
        raise ValueError("hypothesis must use canonical JSON encoding")
    return HypothesisSpec(
        schema_version=str(schema_version),
        family_id=family_id,
        family_version=family_version,
        parameters=normalized_parameters,
    )


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


def _provisional_metrics(result, signal_count: int) -> dict[str, object]:
    """Derive finite, bounded screen metrics from one PyBroker result."""
    equity = [float(value) for value in result.portfolio["equity"].tolist()]
    if not equity or any(not math.isfinite(value) for value in equity):
        raise RuntimeError("PyBroker equity series must be finite and non-empty")
    initial_equity = equity[0]
    if initial_equity <= 0:
        raise RuntimeError("PyBroker initial equity must be positive")
    peak = initial_equity
    drawdowns: list[float] = []
    for value in equity:
        peak = max(peak, value)
        drawdowns.append(max(0.0, (peak - value) / peak))
    notional = 0.0
    for order in result.orders.itertuples():
        notional += abs(float(order.shares) * float(order.fill_price))
    metrics = {
        "max_drawdown": round(max(drawdowns), 12),
        "signal_count": signal_count,
        "total_return": round(equity[-1] / initial_equity - 1.0, 12),
        "trade_count": len(result.trades),
        "turnover": round(notional / initial_equity, 12),
    }
    if any(
        isinstance(value, float) and not math.isfinite(value)
        for value in metrics.values()
    ):
        raise RuntimeError("PyBroker provisional metrics must be finite")
    return metrics


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


def _content_id(value: str | None, field: str) -> str:
    if value is None or len(value) != 64 or not set(value) <= HEX_DIGITS:
        raise ValueError(f"{field} must be lowercase SHA-256")
    return value


def run(
    catalog: Path,
    output: Path,
    *,
    hypothesis: Path,
    evaluation_context_id: str | None = None,
    environment_id: str | None = None,
) -> dict[str, object]:
    specification = load_hypothesis(hypothesis)
    is_v2 = specification.schema_version == "strategy-hypothesis-v2"
    if is_v2:
        evaluation_context_id = _content_id(evaluation_context_id, "evaluation_context_id")
        expected_environment_id = _content_id(environment_id, "environment_id")
        environment_id = research_runtime_identity(REPOSITORY_ROOT, require_active=True)
        if environment_id != expected_environment_id:
            raise ValueError("environment attestation mismatch")
    catalog_path = Path(catalog)
    catalog = catalog_path.resolve()
    output = validate_output_path(catalog_path, output)

    import pybroker
    from pybroker import Strategy, StrategyConfig

    _require_trusted_origins(pybroker)

    pybroker.disable_logging()
    pybroker.register_columns("ts_event_ns")
    data, source_hash = load_bars(catalog)
    closed_bars = tuple(
        ClosedBar(
            ts_event_ns=int(row.ts_event_ns),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
        )
        for row in data.itertuples(index=False)
    )
    decisions = evaluate_batch(
        family_id=specification.family_id,
        family_version=specification.family_version,
        parameters=specification.parameters,
        bars=closed_bars,
    )
    decisions_by_time = {decision.ts_event_ns: decision for decision in decisions}
    signals: list[dict[str, object]] = (
        [asdict(decision) for decision in decisions] if is_v2 else []
    )

    def execute(ctx) -> None:
        decision = decisions_by_time.get(int(ctx.ts_event_ns[-1]))
        if decision is None:
            return
        wants_long = decision.target_intent == "LONG"
        is_long = ctx.long_pos() is not None
        if wants_long == is_long:
            return
        if not is_v2:
            signals.append(
                {
                    "intent": decision.target_intent,
                    "score": float(decision.score),
                    "ts_event_ns": decision.ts_event_ns,
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

    runtime: dict[str, object] = {
        "pybroker_version": pybroker.__version__,
        "python_version": sys.version.split()[0],
        "seed": SEED,
    }
    source: dict[str, object] = {
        "first_ts_event_ns": int(data.iloc[0]["ts_event_ns"]),
        "last_ts_event_ns": int(data.iloc[-1]["ts_event_ns"]),
        "row_count": len(data),
        "sha256": source_hash,
    }
    strategy_contract: dict[str, object]
    candidate: dict[str, object] = {
        "bar_type": BAR_TYPE,
        "instrument_id": INSTRUMENT_ID,
        "runtime": runtime,
        "schema_version": "pybroker-candidate-v2" if is_v2 else "pybroker-candidate-v1",
        "signals": signals,
        "source": source,
        "truth_status": "provisional",
    }
    if is_v2:
        runtime["environment_id"] = environment_id
        source.update(
            {
                "data_as_of_ns": source["last_ts_event_ns"],
                "data_snapshot_id": source_hash,
            },
        )
        strategy_contract = {
            "decision_timing": "bar-close; effective no earlier than next event",
            "family_id": specification.family_id,
            "family_version": specification.family_version,
            "kernel_hash": KERNEL_HASH,
            "kernel_version": KERNEL_VERSION,
            "parameters": specification.parameters,
        }
        candidate["evaluation_context_id"] = evaluation_context_id
    else:
        strategy_contract = {
            "decision_timing": "bar-close; effective no earlier than next event",
            "name": specification.family_id,
            "parameters": specification.parameters,
        }
    candidate["strategy"] = strategy_contract
    candidate_id = write_candidate(candidate, output)
    provisional_metrics = (
        _provisional_metrics(result, len(signals))
        if is_v2
        else {"orders": len(result.orders), "signals": len(signals)}
    )
    research_result: dict[str, object] = {
        "candidate_id": candidate_id,
        "provisional_metrics": provisional_metrics,
    }
    if is_v2:
        research_result.update(
            {
                "schema_version": "research-result-v2",
                "truth_status": "provisional",
            },
        )
    return research_result


def main() -> int:
    _require_isolated_runtime()
    parser = argparse.ArgumentParser(description="Run the isolated PyBroker research candidate generator")
    parser.add_argument("--hypothesis", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evaluation-context-id")
    parser.add_argument("--environment-id")
    args = parser.parse_args()
    result = run(
        args.catalog,
        args.output,
        hypothesis=args.hypothesis,
        evaluation_context_id=args.evaluation_context_id,
        environment_id=args.environment_id,
    )
    if result.get("schema_version") == "research-result-v2":
        print(canonical_json(result).decode(), end="")
    else:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
