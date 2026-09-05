"""Nautilus-native strategy candidate: plain canonical JSON strategy identity.

A strategy candidate is a *specification*, not a backtest. It carries only what
the Nautilus historical evaluator needs to reproduce intent deterministically
from canonical bars through the shared family kernel:

- strategy family / version / kernel / parameters (bounded plain JSON);
- instrument / bar type;
- canonical source window identity (digest + bounds);
- evaluation context binding the pinned data / policy / engine / runtime;
- root runtime versions needed for reproducibility.

It carries no signals, no metrics, no fills, no PnL, and no accounting truth.
There is no second engine and no cross-engine parity gate: the Nautilus
evaluator derives decisions directly from this spec with the same kernel that
serves live/paper runtimes. ``candidate_id`` remains the generic
strategy-candidate identity (sha256 of the canonical bytes).
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
import re


_SCHEMA_VERSION = "strategy-candidate-v1"
_DECISION_TIMING = "bar-close; effective no earlier than next event"

_TOP_LEVEL_FIELDS = {
    "bar_type",
    "evaluation_context_id",
    "instrument_id",
    "runtime",
    "schema_version",
    "source",
    "strategy",
    "truth_status",
}
_STRATEGY_FIELDS = {
    "decision_timing",
    "family_id",
    "family_version",
    "kernel_hash",
    "kernel_version",
    "parameters",
}
_SOURCE_FIELDS = {
    "data_as_of_ns",
    "data_snapshot_id",
    "first_ts_event_ns",
    "last_ts_event_ns",
    "row_count",
    "sha256",
}
_RUNTIME_FIELDS = {"nautilus_trader", "python_version"}
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


def validate_strategy_candidate(candidate: object) -> dict[str, object]:
    """Validate one Nautilus-native strategy candidate and return it."""
    if not isinstance(candidate, dict):
        raise ValueError("invalid candidate fields")
    root = _fields(candidate, _TOP_LEVEL_FIELDS, "candidate")
    if root["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("unsupported candidate schema")
    if root["truth_status"] != "provisional":
        raise ValueError("candidate truth_status must be provisional")
    instrument_id = _text(root["instrument_id"], "instrument_id")
    bar_type = root["bar_type"]
    if not isinstance(bar_type, str) or not bar_type.startswith(f"{instrument_id}-"):
        raise ValueError("bar_type must identify instrument_id")
    _sha256(root["evaluation_context_id"], "evaluation_context_id")
    source = _fields(root["source"], _SOURCE_FIELDS, "source")
    first = _integer(source["first_ts_event_ns"], "source.first_ts_event_ns")
    last = _integer(source["last_ts_event_ns"], "source.last_ts_event_ns")
    if first > last:
        raise ValueError("source window is reversed")
    _integer(source["row_count"], "source.row_count", positive=True)
    source_hash = _sha256(source["sha256"], "source.sha256")
    if _sha256(source["data_snapshot_id"], "source.data_snapshot_id") != source_hash:
        raise ValueError("source.data_snapshot_id must equal source.sha256")
    if _integer(source["data_as_of_ns"], "source.data_as_of_ns") != last:
        raise ValueError("source.data_as_of_ns must equal source.last_ts_event_ns")
    runtime = _fields(root["runtime"], _RUNTIME_FIELDS, "runtime")
    _text(runtime["nautilus_trader"], "runtime.nautilus_trader")
    _text(runtime["python_version"], "runtime.python_version")
    strategy = _fields(root["strategy"], _STRATEGY_FIELDS, "strategy")
    if strategy["decision_timing"] != _DECISION_TIMING:
        raise ValueError("invalid strategy.decision_timing")
    _text(strategy["family_id"], "strategy.family_id")
    _text(strategy["family_version"], "strategy.family_version")
    _text(strategy["kernel_version"], "strategy.kernel_version")
    _sha256(strategy["kernel_hash"], "strategy.kernel_hash")
    parameters = strategy["parameters"]
    if not isinstance(parameters, dict):
        raise ValueError("strategy.parameters must be a JSON object")
    _plain_parameters(parameters)
    return root


def canonical_candidate_bytes(candidate: object) -> bytes:
    """Return the canonical bytes whose sha256 is the generic candidate_id."""
    validated = validate_strategy_candidate(candidate)
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


def load_strategy_candidate(path: Path) -> tuple[dict[str, object], str]:
    """Load one canonical candidate file and return it with its candidate_id."""
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
    validated = validate_strategy_candidate(candidate)
    if payload != canonical_candidate_bytes(validated):
        raise ValueError("candidate must use canonical JSON encoding")
    return validated, sha256(payload).hexdigest()
