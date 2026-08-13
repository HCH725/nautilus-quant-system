from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
import re


_TOP_LEVEL_FIELDS = {
    "bar_type",
    "instrument_id",
    "runtime",
    "schema_version",
    "signals",
    "source",
    "strategy",
    "truth_status",
}
_FORBIDDEN_PARAMETER_FIELDS = {
    "accounting",
    "cache",
    "callable",
    "code",
    "credential",
    "credentials",
    "executable",
    "import",
    "import_path",
    "joblib",
    "leverage",
    "order_type",
    "payload",
    "pickle",
    "pnl",
    "quantity",
}
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _integer(value: object, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < (1 if positive else 0):
        raise ValueError(f"{field} must be {'positive' if positive else 'non-negative'}")
    return value


def _fields(value: object, expected: set[str], field: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"invalid {field} fields")
    return value


def _plain_parameters(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower().replace("-", "_").replace(" ", "_")
            if normalized in _FORBIDDEN_PARAMETER_FIELDS:
                raise ValueError(f"forbidden candidate field: {key}")
            _plain_parameters(item)
        return
    elif isinstance(value, list):
        for item in value:
            _plain_parameters(item)
        return
    elif value is None or isinstance(value, (str, bool, int)):
        return
    elif isinstance(value, float):
        if math.isfinite(value):
            return
    raise ValueError("strategy.parameters must contain only finite JSON values")


def validate_pybroker_candidate(candidate: object) -> dict[str, object]:
    root = _fields(candidate, _TOP_LEVEL_FIELDS, "candidate")
    if root["schema_version"] != "pybroker-candidate-v1":
        raise ValueError("unsupported candidate schema")
    if root["truth_status"] != "provisional":
        raise ValueError("candidate truth_status must be provisional")

    instrument_id = root["instrument_id"]
    bar_type = root["bar_type"]
    if not isinstance(instrument_id, str) or not instrument_id:
        raise ValueError("instrument_id must be a non-empty string")
    if not isinstance(bar_type, str) or not bar_type.startswith(f"{instrument_id}-"):
        raise ValueError("bar_type must identify instrument_id")

    runtime = _fields(root["runtime"], {"pybroker_version", "python_version", "seed"}, "runtime")
    if not isinstance(runtime["pybroker_version"], str) or not runtime["pybroker_version"]:
        raise ValueError("runtime.pybroker_version must be a non-empty string")
    if not isinstance(runtime["python_version"], str) or not runtime["python_version"]:
        raise ValueError("runtime.python_version must be a non-empty string")
    _integer(runtime["seed"], "runtime.seed")

    source = _fields(
        root["source"],
        {"first_ts_event_ns", "last_ts_event_ns", "row_count", "sha256"},
        "source",
    )
    first = _integer(source["first_ts_event_ns"], "source.first_ts_event_ns")
    last = _integer(source["last_ts_event_ns"], "source.last_ts_event_ns")
    _integer(source["row_count"], "source.row_count", positive=True)
    if first > last:
        raise ValueError("source window is reversed")
    if not isinstance(source["sha256"], str) or _SHA256.fullmatch(source["sha256"]) is None:
        raise ValueError("source.sha256 must be lowercase SHA-256")

    strategy = _fields(root["strategy"], {"decision_timing", "name", "parameters"}, "strategy")
    if strategy["decision_timing"] != "bar-close; effective no earlier than next event":
        raise ValueError("invalid strategy.decision_timing")
    if not isinstance(strategy["name"], str) or not strategy["name"]:
        raise ValueError("strategy.name must be a non-empty string")
    if not isinstance(strategy["parameters"], dict):
        raise ValueError("strategy.parameters must be a JSON object")
    _plain_parameters(strategy["parameters"])

    signals = root["signals"]
    if not isinstance(signals, list):
        raise ValueError("signals must be an array")
    previous = -1
    for signal in signals:
        row = _fields(signal, {"intent", "score", "ts_event_ns"}, "signal")
        timestamp = _integer(row["ts_event_ns"], "signal.ts_event_ns")
        if timestamp <= previous:
            raise ValueError("signal timestamps must be strictly increasing")
        if not first <= timestamp <= last:
            raise ValueError("signal timestamp is outside source window")
        previous = timestamp
        if row["intent"] not in {"LONG", "FLAT"}:
            raise ValueError("signal intent must be LONG or FLAT")
        score = row["score"]
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or (isinstance(score, float) and not math.isfinite(score))
        ):
            raise ValueError("signal score must be finite")
    return root


def canonical_candidate_bytes(candidate: object) -> bytes:
    validated = validate_pybroker_candidate(candidate)
    return (
        json.dumps(validated, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def load_pybroker_candidate(path: Path) -> tuple[dict[str, object], str]:
    payload = Path(path).read_bytes()
    try:
        candidate = json.loads(
            payload,
            object_pairs_hook=_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}"),
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("candidate must be UTF-8 JSON") from exc
    validated = validate_pybroker_candidate(candidate)
    if payload != canonical_candidate_bytes(validated):
        raise ValueError("candidate must use canonical JSON encoding")
    return validated, sha256(payload).hexdigest()