from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import math
from pathlib import Path
import re

from .strategy_families import derive_signal_id


_V1_TOP_LEVEL_FIELDS = {
    "bar_type",
    "instrument_id",
    "runtime",
    "schema_version",
    "signals",
    "source",
    "strategy",
    "truth_status",
}
_V2_TOP_LEVEL_FIELDS = _V1_TOP_LEVEL_FIELDS | {"evaluation_context_id"}
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
_CANONICAL_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?")


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


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be lowercase SHA-256")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _plain_parameters(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower().replace("-", "_").replace(" ", "_")
            if normalized in _FORBIDDEN_PARAMETER_FIELDS:
                raise ValueError(f"forbidden candidate field: {key}")
            _plain_parameters(item)
        return
    if isinstance(value, list):
        for item in value:
            _plain_parameters(item)
        return
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    raise ValueError("strategy.parameters must contain only finite JSON values")


def _common_root(root: dict[str, object]) -> tuple[int, int]:
    if root["truth_status"] != "provisional":
        raise ValueError("candidate truth_status must be provisional")
    instrument_id = _text(root["instrument_id"], "instrument_id")
    bar_type = root["bar_type"]
    if not isinstance(bar_type, str) or not bar_type.startswith(f"{instrument_id}-"):
        raise ValueError("bar_type must identify instrument_id")
    source = root["source"]
    if not isinstance(source, dict):
        raise ValueError("invalid source fields")
    first = _integer(source.get("first_ts_event_ns"), "source.first_ts_event_ns")
    last = _integer(source.get("last_ts_event_ns"), "source.last_ts_event_ns")
    if first > last:
        raise ValueError("source window is reversed")
    return first, last


def _validate_v1(root: dict[str, object]) -> dict[str, object]:
    if set(root) != _V1_TOP_LEVEL_FIELDS:
        raise ValueError("invalid candidate fields")
    first, last = _common_root(root)
    runtime = _fields(
        root["runtime"], {"pybroker_version", "python_version", "seed"}, "runtime"
    )
    _text(runtime["pybroker_version"], "runtime.pybroker_version")
    _text(runtime["python_version"], "runtime.python_version")
    _integer(runtime["seed"], "runtime.seed")
    source = _fields(
        root["source"],
        {"first_ts_event_ns", "last_ts_event_ns", "row_count", "sha256"},
        "source",
    )
    _integer(source["row_count"], "source.row_count", positive=True)
    _sha256(source["sha256"], "source.sha256")
    strategy = _fields(
        root["strategy"], {"decision_timing", "name", "parameters"}, "strategy"
    )
    if strategy["decision_timing"] != "bar-close; effective no earlier than next event":
        raise ValueError("invalid strategy.decision_timing")
    _text(strategy["name"], "strategy.name")
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


def _canonical_decimal(value: object) -> str:
    if not isinstance(value, str) or _CANONICAL_DECIMAL.fullmatch(value) is None:
        raise ValueError("signal.score must be a canonical decimal string")
    if value == "-0":
        raise ValueError("signal.score must be a canonical decimal string")
    try:
        if not Decimal(value).is_finite():
            raise ValueError("signal.score must be a canonical decimal string")
    except InvalidOperation as error:
        raise ValueError("signal.score must be a canonical decimal string") from error
    return value


def _validate_v2(root: dict[str, object]) -> dict[str, object]:
    if set(root) != _V2_TOP_LEVEL_FIELDS:
        raise ValueError("invalid candidate v2 fields")
    first, last = _common_root(root)
    _sha256(root["evaluation_context_id"], "evaluation_context_id")
    runtime = _fields(
        root["runtime"],
        {"environment_id", "pybroker_version", "python_version", "seed"},
        "runtime v2",
    )
    _sha256(runtime["environment_id"], "runtime.environment_id")
    _text(runtime["pybroker_version"], "runtime.pybroker_version")
    _text(runtime["python_version"], "runtime.python_version")
    _integer(runtime["seed"], "runtime.seed")
    source = _fields(
        root["source"],
        {
            "data_as_of_ns",
            "data_snapshot_id",
            "first_ts_event_ns",
            "last_ts_event_ns",
            "row_count",
            "sha256",
        },
        "source v2",
    )
    _integer(source["row_count"], "source.row_count", positive=True)
    source_hash = _sha256(source["sha256"], "source.sha256")
    if _sha256(source["data_snapshot_id"], "source.data_snapshot_id") != source_hash:
        raise ValueError("source.data_snapshot_id must equal source.sha256")
    if _integer(source["data_as_of_ns"], "source.data_as_of_ns") != last:
        raise ValueError("source.data_as_of_ns must equal source.last_ts_event_ns")

    strategy = _fields(
        root["strategy"],
        {
            "decision_timing",
            "family_id",
            "family_version",
            "kernel_hash",
            "kernel_version",
            "parameters",
        },
        "strategy v2",
    )
    if strategy["decision_timing"] != "bar-close; effective no earlier than next event":
        raise ValueError("invalid strategy.decision_timing")
    family_id = _text(strategy["family_id"], "strategy.family_id")
    family_version = _text(strategy["family_version"], "strategy.family_version")
    kernel_version = _text(strategy["kernel_version"], "strategy.kernel_version")
    kernel_hash = _sha256(strategy["kernel_hash"], "strategy.kernel_hash")
    parameters = strategy["parameters"]
    if not isinstance(parameters, dict):
        raise ValueError("strategy.parameters must be a JSON object")
    _plain_parameters(parameters)

    signals = root["signals"]
    if not isinstance(signals, list):
        raise ValueError("signals must be an array")
    previous = -1
    signal_fields = {
        "family_id",
        "family_version",
        "kernel_hash",
        "kernel_version",
        "reason",
        "score",
        "signal_id",
        "target_intent",
        "ts_event_ns",
    }
    for signal in signals:
        row = _fields(signal, signal_fields, "signal v2")
        timestamp = _integer(row["ts_event_ns"], "signal.ts_event_ns", positive=True)
        if timestamp <= previous:
            raise ValueError("signal timestamps must be strictly increasing")
        if not first <= timestamp <= last:
            raise ValueError("signal timestamp is outside source window")
        previous = timestamp
        score = _canonical_decimal(row["score"])
        target_intent = row["target_intent"]
        if target_intent not in {"LONG", "FLAT"}:
            raise ValueError("signal target_intent must be LONG or FLAT")
        reason = _text(row["reason"], "signal.reason")
        if (
            row["family_id"] != family_id
            or row["family_version"] != family_version
            or row["kernel_version"] != kernel_version
            or row["kernel_hash"] != kernel_hash
        ):
            raise ValueError("signal kernel identity does not match strategy identity")
        signal_id = _sha256(row["signal_id"], "signal.signal_id")
        expected = derive_signal_id(
            family_id=family_id,
            family_version=family_version,
            kernel_hash=kernel_hash,
            kernel_version=kernel_version,
            parameters=parameters,
            reason=reason,
            score=score,
            target_intent=str(target_intent),
            ts_event_ns=timestamp,
        )
        if signal_id != expected:
            raise ValueError("signal_id mismatch")
    return root


def validate_pybroker_candidate(candidate: object) -> dict[str, object]:
    if not isinstance(candidate, dict):
        raise ValueError("invalid candidate fields")
    schema_version = candidate.get("schema_version")
    if schema_version == "pybroker-candidate-v1":
        return _validate_v1(candidate)
    if schema_version == "pybroker-candidate-v2":
        return _validate_v2(candidate)
    raise ValueError("unsupported candidate schema")


def canonical_candidate_bytes(candidate: object) -> bytes:
    validated = validate_pybroker_candidate(candidate)
    return (
        json.dumps(
            validated,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def load_pybroker_candidate(path: Path) -> tuple[dict[str, object], str]:
    payload = Path(path).read_bytes()
    try:
        candidate = json.loads(
            payload,
            object_pairs_hook=_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("candidate must be UTF-8 JSON") from exc
    validated = validate_pybroker_candidate(candidate)
    if payload != canonical_candidate_bytes(validated):
        raise ValueError("candidate must use canonical JSON encoding")
    return validated, sha256(payload).hexdigest()
