from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile, mkdtemp
from typing import Callable, Iterable, Protocol, cast
import hashlib
import json
import os
import shutil


OFFICIAL = "official"
MODELED_FUNDING = "modeled_funding"
FUNDING_PRICE_SOURCE = "binance_funding_history_mark_price"
SCHEMA_VERSION = 1
READY_POINTER = "funding-observations.v1.ready.json"
GENERATIONS_DIRECTORY = "funding-observations.v1.generations"
MANIFEST_NAME = "manifest.json"
ROLLBACK_NAME = "rollback.json"
MAX_FUNDING_GAP_NS = 8 * 60 * 60 * 1_000_000_000
HEAD_TOLERANCE_NS = 30 * 1_000_000_000


class FundingClient(Protocol):
    def funding(self, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, object]]: ...


def _decimal(field: str, value: object, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{field} must be numeric")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        qualifier = "positive and finite" if positive else "finite"
        raise ValueError(f"{field} must be {qualifier}")
    return parsed


def _canonical_decimal(value: Decimal) -> str:
    fixed = format(value, "f")
    if "." in fixed:
        fixed = fixed.rstrip("0").rstrip(".")
    return "0" if fixed in {"", "-0"} else fixed


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
        _fsync_directory(path.parent)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class FundingObservation:
    instrument_id: str
    funding_time_ns: int
    rate: Decimal
    mark_price: Decimal | None
    rate_type: str | None
    truth_status: str
    funding_price_source: str | None

    def __post_init__(self) -> None:
        symbol, separator, venue = self.instrument_id.partition("-PERP.")
        if not separator or venue != "BINANCE" or not symbol.isalnum() or symbol != symbol.upper():
            raise ValueError("instrument_id must identify a Binance perpetual")
        if isinstance(self.funding_time_ns, bool) or not isinstance(self.funding_time_ns, int) or self.funding_time_ns < 0:
            raise ValueError("funding_time_ns must be a non-negative integer")
        if not self.rate.is_finite():
            raise ValueError("rate must be finite")
        if self.rate_type is not None and not isinstance(self.rate_type, str):
            raise ValueError("rate_type must be a string or null")
        if self.mark_price is None:
            if self.truth_status != MODELED_FUNDING or self.funding_price_source is not None:
                raise ValueError("missing mark_price requires modeled_funding truth")
        elif (
            not self.mark_price.is_finite()
            or self.mark_price <= 0
            or self.truth_status != OFFICIAL
            or self.funding_price_source != FUNDING_PRICE_SOURCE
        ):
            raise ValueError("mark_price must be positive official Binance funding-history truth")

    @classmethod
    def from_api_row(cls, instrument_id: str, row: dict[str, object]) -> FundingObservation:
        if not isinstance(row, dict):
            raise ValueError("funding row must be an object")
        expected_symbol = instrument_id.partition("-PERP.")[0]
        if "symbol" in row and row["symbol"] != expected_symbol:
            raise ValueError("funding row symbol does not match instrument_id")
        if "markPrice" not in row:
            raise ValueError("markPrice is required")
        funding_time = row.get("fundingTime")
        if isinstance(funding_time, bool) or not isinstance(funding_time, int) or funding_time < 0:
            raise ValueError("fundingTime must be a non-negative integer")
        if "fundingRate" not in row:
            raise ValueError("fundingRate is required")
        rate_type = row.get("rateType")
        if rate_type is not None and not isinstance(rate_type, str):
            raise ValueError("rateType must be a string")
        raw_mark = row.get("markPrice")
        if raw_mark is None or raw_mark == "":
            mark_price = None
            truth_status = MODELED_FUNDING
            source = None
        else:
            mark_price = _decimal("markPrice", raw_mark, positive=True)
            truth_status = OFFICIAL
            source = FUNDING_PRICE_SOURCE
        return cls(
            instrument_id=instrument_id,
            funding_time_ns=funding_time * 1_000_000,
            rate=_decimal("fundingRate", row["fundingRate"]),
            mark_price=mark_price,
            rate_type=rate_type,
            truth_status=truth_status,
            funding_price_source=source,
        )

    @classmethod
    def from_jsonl(cls, line: bytes) -> FundingObservation:
        try:
            raw = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid FundingObservation JSON") from exc
        expected = {
            "instrument_id",
            "funding_time_ns",
            "rate",
            "mark_price",
            "rate_type",
            "truth_status",
            "funding_price_source",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ValueError("invalid FundingObservation fields")
        if not isinstance(raw["instrument_id"], str):
            raise ValueError("instrument_id must be a string")
        if isinstance(raw["funding_time_ns"], bool) or not isinstance(raw["funding_time_ns"], int):
            raise ValueError("funding_time_ns must be an integer")
        if not isinstance(raw["rate"], str):
            raise ValueError("rate must be a decimal string")
        if raw["mark_price"] is not None and not isinstance(raw["mark_price"], str):
            raise ValueError("mark_price must be a decimal string or null")
        if raw["rate_type"] is not None and not isinstance(raw["rate_type"], str):
            raise ValueError("rate_type must be a string or null")
        if not isinstance(raw["truth_status"], str):
            raise ValueError("truth_status must be a string")
        if raw["funding_price_source"] is not None and not isinstance(raw["funding_price_source"], str):
            raise ValueError("funding_price_source must be a string or null")
        observation = cls(
            instrument_id=raw["instrument_id"],
            funding_time_ns=raw["funding_time_ns"],
            rate=_decimal("rate", raw["rate"]),
            mark_price=None if raw["mark_price"] is None else _decimal("mark_price", raw["mark_price"], positive=True),
            rate_type=raw["rate_type"],
            truth_status=raw["truth_status"],
            funding_price_source=raw["funding_price_source"],
        )
        if observation.to_jsonl().rstrip(b"\n") != line:
            raise ValueError("FundingObservation JSON is not canonical")
        return observation

    def to_jsonl(self) -> bytes:
        return _canonical_json({
            "instrument_id": self.instrument_id,
            "funding_time_ns": self.funding_time_ns,
            "rate": _canonical_decimal(self.rate),
            "mark_price": None if self.mark_price is None else _canonical_decimal(self.mark_price),
            "rate_type": self.rate_type,
            "truth_status": self.truth_status,
            "funding_price_source": self.funding_price_source,
        })


def observations_from_api_rows(
    instrument_id: str,
    rows: object,
) -> list[FundingObservation]:
    if not isinstance(rows, list):
        raise ValueError("funding response must be a list")
    observations = sorted(
        (FundingObservation.from_api_row(instrument_id, row) for row in rows),
        key=lambda item: item.funding_time_ns,
    )
    seen: dict[int, FundingObservation] = {}
    official_seen = False
    for observation in observations:
        current = seen.get(observation.funding_time_ns)
        if current is not None:
            kind = "duplicate" if current == observation else "conflicting"
            raise ValueError(f"{kind} FundingObservation at {observation.funding_time_ns}")
        seen[observation.funding_time_ns] = observation
        if observation.truth_status == OFFICIAL:
            official_seen = True
        elif official_seen:
            raise ValueError("modeled_funding row after first official markPrice")
    return observations


class FundingObservationStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> list[FundingObservation]:
        if not self.path.exists():
            return []
        data = self.path.read_bytes()
        if data and not data.endswith(b"\n"):
            raise ValueError("FundingObservation JSONL must end with LF")
        observations = [
            FundingObservation.from_jsonl(line)
            for line in data.splitlines()
            if line
        ]
        return _validate_observations(observations)

    def write(self, observations: Iterable[FundingObservation]) -> str:
        ordered = _validate_observations(sorted(observations, key=lambda item: item.funding_time_ns))
        data = b"".join(item.to_jsonl() for item in ordered)
        _atomic_write(self.path, data)
        if self.path.read_bytes() != data or self.load() != ordered:
            raise RuntimeError(f"FundingObservation readback mismatch: {self.path}")
        return _sha256(data)


def _validate_observations(observations: list[FundingObservation]) -> list[FundingObservation]:
    seen: dict[int, FundingObservation] = {}
    official_seen = False
    for observation in observations:
        current = seen.get(observation.funding_time_ns)
        if current is not None:
            kind = "duplicate" if current == observation else "conflicting"
            raise ValueError(f"{kind} FundingObservation at {observation.funding_time_ns}")
        seen[observation.funding_time_ns] = observation
        if observation.truth_status == OFFICIAL:
            official_seen = True
        elif official_seen:
            raise ValueError("modeled_funding row after first official markPrice")
    if observations != sorted(observations, key=lambda item: item.funding_time_ns):
        raise ValueError("FundingObservation rows must be strictly ordered")
    return observations


def validate_coverage(
    observations: list[FundingObservation],
    *,
    instrument_id: str,
    start_ms: int,
    end_ms: int,
) -> None:
    if not observations:
        raise ValueError(f"no funding observations for {instrument_id}")
    _validate_observations(observations)
    if any(item.instrument_id != instrument_id for item in observations):
        raise ValueError("FundingObservation instrument mismatch")
    start_ns = start_ms * 1_000_000
    end_ns = end_ms * 1_000_000
    head_gap_ns = observations[0].funding_time_ns - start_ns
    if not 0 <= head_gap_ns < HEAD_TOLERANCE_NS:
        raise ValueError(f"funding observation head gap for {instrument_id}: {head_gap_ns}ns")
    for current, following in zip(observations, observations[1:], strict=False):
        gap_ns = following.funding_time_ns - current.funding_time_ns
        if not 0 < gap_ns < MAX_FUNDING_GAP_NS + HEAD_TOLERANCE_NS:
            raise ValueError(
                f"funding observation coverage gap for {instrument_id}: "
                f"{current.funding_time_ns} -> {following.funding_time_ns}",
            )
    tail_gap_ns = end_ns - observations[-1].funding_time_ns
    if not 0 < tail_gap_ns < MAX_FUNDING_GAP_NS + HEAD_TOLERANCE_NS:
        raise ValueError(f"funding observation tail gap for {instrument_id}: {tail_gap_ns}ns")


def _legacy_manifest(funding_path: Path) -> dict[str, object]:
    legacy = []
    for path in sorted(funding_path.glob("*.jsonl")):
        data = path.read_bytes()
        legacy.append({"path": path.name, "bytes": len(data), "sha256": _sha256(data)})
    return {"schema_version": SCHEMA_VERSION, "status": "ROLLBACK_READY", "legacy": legacy}


def _observation_entry(path: Path, observations: list[FundingObservation]) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "instrument_id": observations[0].instrument_id,
        "path": path.name,
        "rows": len(observations),
        "first_ns": observations[0].funding_time_ns,
        "last_ns": observations[-1].funding_time_ns,
        "sha256": _sha256(data),
        "modeled_rows": sum(item.truth_status == MODELED_FUNDING for item in observations),
        "official_rows": sum(item.truth_status == OFFICIAL for item in observations),
    }


def _manifest(
    *,
    start_ms: int,
    end_ms: int,
    entries: list[dict[str, object]],
    rollback_sha256: str,
) -> dict[str, object]:
    ordered = sorted(entries, key=lambda item: str(item["instrument_id"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "READY",
        "start_ms": start_ms,
        "end_ms": end_ms,
        "observations": ordered,
        "rollback_sha256": rollback_sha256,
        "truth_counts": {
            "modeled_funding": sum(cast(int, item["modeled_rows"]) for item in ordered),
            "official": sum(cast(int, item["official_rows"]) for item in ordered),
        },
    }


def _tree_bytes(path: Path) -> dict[str, bytes]:
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def _publish_generation(
    *,
    funding_path: Path,
    stage_path: Path,
    manifest: dict[str, object],
    symbols: tuple[str, ...],
    publication_hook: Callable[[str], None] | None,
) -> dict[str, object]:
    manifest_bytes = _canonical_json(manifest)
    generation = _sha256(manifest_bytes)
    generations_path = funding_path / GENERATIONS_DIRECTORY
    generations_path.mkdir(parents=True, exist_ok=True)
    _fsync_directory(funding_path)
    destination = generations_path / generation
    _fsync_directory(stage_path)
    if destination.exists():
        if _tree_bytes(destination) != _tree_bytes(stage_path):
            raise RuntimeError("FundingObservation generation collision")
    else:
        os.replace(stage_path, destination)
        _fsync_directory(generations_path)
    pointer = {
        "schema_version": SCHEMA_VERSION,
        "status": "READY",
        "generation": generation,
        "manifest_sha256": _sha256(manifest_bytes),
    }
    if publication_hook is not None:
        publication_hook("before_pointer")
    loaded_pointer, loaded_manifest, _observations = _load_ready_generation(
        funding_path,
        symbols=symbols,
        candidate_pointer=pointer,
    )
    if loaded_pointer != pointer or loaded_manifest != manifest:
        raise RuntimeError("FundingObservation candidate generation readback mismatch")
    _atomic_write(funding_path / READY_POINTER, _canonical_json(pointer))
    if publication_hook is not None:
        publication_hook("after_pointer")
    loaded_pointer, loaded_manifest, _observations = _load_ready_generation(funding_path, symbols=symbols)
    if loaded_pointer != pointer or loaded_manifest != manifest:
        raise RuntimeError("FundingObservation ready pointer readback mismatch")
    return pointer


def _stage_generation(
    *,
    funding_path: Path,
    observations_by_symbol: dict[str, list[FundingObservation]],
    start_ms: int,
    end_ms: int,
    symbols: tuple[str, ...],
    publication_hook: Callable[[str], None] | None,
) -> dict[str, object]:
    stage_path = Path(mkdtemp(dir=funding_path, prefix=".funding-observations.v1.stage."))
    try:
        entries = []
        for symbol in sorted(symbols):
            observations = observations_by_symbol[symbol]
            instrument_id = f"{symbol}-PERP.BINANCE"
            validate_coverage(observations, instrument_id=instrument_id, start_ms=start_ms, end_ms=end_ms)
            path = stage_path / f"{instrument_id}.observations.v1.jsonl"
            FundingObservationStore(path).write(observations)
            readback = FundingObservationStore(path).load()
            if readback != observations:
                raise RuntimeError(f"FundingObservation generation readback mismatch: {instrument_id}")
            entries.append(_observation_entry(path, readback))
        rollback_bytes = _canonical_json(_legacy_manifest(funding_path))
        manifest = _manifest(
            start_ms=start_ms,
            end_ms=end_ms,
            entries=entries,
            rollback_sha256=_sha256(rollback_bytes),
        )
        _atomic_write(stage_path / MANIFEST_NAME, _canonical_json(manifest))
        _atomic_write(stage_path / ROLLBACK_NAME, rollback_bytes)
        return _publish_generation(
            funding_path=funding_path,
            stage_path=stage_path,
            manifest=manifest,
            symbols=symbols,
            publication_hook=publication_hook,
        )
    finally:
        shutil.rmtree(stage_path, ignore_errors=True)


def migrate_funding_observations(
    *,
    client: FundingClient,
    funding_path: Path,
    symbols: tuple[str, ...],
    start_ms: int,
    end_ms: int,
    publication_hook: Callable[[str], None] | None = None,
) -> dict[str, object]:
    funding_path = Path(funding_path)
    funding_path.mkdir(parents=True, exist_ok=True)
    if not symbols or len(symbols) != len(set(symbols)):
        raise ValueError("migration symbols must be non-empty and unique")
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("invalid migration boundaries")
    observations_by_symbol = {
        symbol: observations_from_api_rows(
            f"{symbol}-PERP.BINANCE",
            client.funding(symbol, start_ms, end_ms),
        )
        for symbol in sorted(symbols)
    }
    return _stage_generation(
        funding_path=funding_path,
        observations_by_symbol=observations_by_symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        symbols=symbols,
        publication_hook=publication_hook,
    )


def _merge_observations(
    existing: list[FundingObservation],
    incoming: list[FundingObservation],
) -> list[FundingObservation]:
    merged = {item.funding_time_ns: item for item in existing}
    for observation in incoming:
        current = merged.get(observation.funding_time_ns)
        if current is not None and current != observation:
            raise ValueError(f"conflicting FundingObservation at {observation.funding_time_ns}")
        merged[observation.funding_time_ns] = observation
    return _validate_observations([merged[key] for key in sorted(merged)])


def sync_funding_generation(
    *,
    client: FundingClient,
    funding_path: Path,
    symbols: tuple[str, ...],
    start_ms: int,
    end_ms: int,
    publication_hook: Callable[[str], None] | None = None,
) -> dict[str, object]:
    funding_path = Path(funding_path)
    _pointer, manifest, existing = _load_ready_generation(funding_path, symbols=symbols)
    if manifest["start_ms"] != start_ms or not isinstance(manifest["end_ms"], int) or end_ms < manifest["end_ms"]:
        raise ValueError("daily FundingObservation boundaries must extend the ready generation")
    if end_ms == manifest["end_ms"]:
        return _pointer
    observations_by_symbol = {}
    for symbol in sorted(symbols):
        current = existing[symbol]
        cursor_ms = current[-1].funding_time_ns // 1_000_000
        incoming = observations_from_api_rows(
            f"{symbol}-PERP.BINANCE",
            client.funding(symbol, cursor_ms, end_ms),
        )
        observations_by_symbol[symbol] = _merge_observations(current, incoming)
    return _stage_generation(
        funding_path=funding_path,
        observations_by_symbol=observations_by_symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        symbols=symbols,
        publication_hook=publication_hook,
    )


def _load_ready_generation(
    funding_path: Path,
    *,
    symbols: tuple[str, ...],
    candidate_pointer: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, list[FundingObservation]]]:
    funding_path = Path(funding_path)
    pointer_path = funding_path / READY_POINTER
    if candidate_pointer is None:
        try:
            pointer_bytes = pointer_path.read_bytes()
            pointer = json.loads(pointer_bytes)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("FundingObservation ready pointer is unavailable or invalid") from exc
    else:
        pointer = candidate_pointer
        pointer_bytes = _canonical_json(pointer)
    pointer_keys = {"schema_version", "status", "generation", "manifest_sha256"}
    if (
        not isinstance(pointer, dict)
        or set(pointer) != pointer_keys
        or not _non_negative_int(pointer["schema_version"])
        or pointer["schema_version"] != SCHEMA_VERSION
        or pointer["status"] != "READY"
        or not isinstance(pointer["generation"], str)
        or len(pointer["generation"]) != 64
        or any(character not in "0123456789abcdef" for character in pointer["generation"])
        or not isinstance(pointer["manifest_sha256"], str)
        or pointer_bytes != _canonical_json(pointer)
    ):
        raise ValueError("invalid FundingObservation ready pointer")
    generation = pointer["generation"]
    generation_path = funding_path / GENERATIONS_DIRECTORY / generation
    if generation_path.name != generation:
        raise ValueError("invalid FundingObservation generation path")
    manifest_path = generation_path / MANIFEST_NAME
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("FundingObservation generation manifest is unavailable or invalid") from exc
    manifest_keys = {
        "schema_version",
        "status",
        "start_ms",
        "end_ms",
        "observations",
        "rollback_sha256",
        "truth_counts",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != manifest_keys
        or not _non_negative_int(manifest["schema_version"])
        or manifest["schema_version"] != SCHEMA_VERSION
        or manifest["status"] != "READY"
        or not _non_negative_int(manifest["start_ms"])
        or not _non_negative_int(manifest["end_ms"])
        or manifest["end_ms"] <= manifest["start_ms"]
        or not isinstance(manifest["observations"], list)
        or not isinstance(manifest["rollback_sha256"], str)
        or not isinstance(manifest["truth_counts"], dict)
        or manifest_bytes != _canonical_json(manifest)
        or _sha256(manifest_bytes) != pointer["manifest_sha256"]
        or _sha256(manifest_bytes) != generation
    ):
        raise ValueError("invalid FundingObservation generation manifest")
    try:
        rollback_bytes = (generation_path / ROLLBACK_NAME).read_bytes()
    except OSError as exc:
        raise ValueError("FundingObservation rollback manifest is unavailable") from exc
    if _sha256(rollback_bytes) != manifest["rollback_sha256"]:
        raise ValueError("FundingObservation rollback manifest hash mismatch")
    expected_ids = {f"{symbol}-PERP.BINANCE" for symbol in symbols}
    observations_by_symbol: dict[str, list[FundingObservation]] = {}
    actual_ids = set()
    modeled_rows = 0
    official_rows = 0
    for entry in cast(list[object], manifest["observations"]):
        entry_keys = {
            "instrument_id",
            "path",
            "rows",
            "first_ns",
            "last_ns",
            "sha256",
            "modeled_rows",
            "official_rows",
        }
        if not isinstance(entry, dict) or set(entry) != entry_keys:
            raise ValueError("invalid FundingObservation manifest entry")
        if any(
            not _non_negative_int(entry[field])
            for field in ("rows", "first_ns", "last_ns", "modeled_rows", "official_rows")
        ):
            raise ValueError("invalid FundingObservation manifest entry")
        instrument_id = entry["instrument_id"]
        expected_name = f"{instrument_id}.observations.v1.jsonl"
        if (
            not isinstance(instrument_id, str)
            or instrument_id not in expected_ids
            or entry["path"] != expected_name
            or Path(expected_name).name != expected_name
        ):
            raise ValueError("invalid FundingObservation manifest path")
        payload_path = generation_path / expected_name
        data = payload_path.read_bytes()
        if _sha256(data) != entry["sha256"]:
            raise ValueError(f"FundingObservation manifest hash mismatch: {instrument_id}")
        observations = FundingObservationStore(payload_path).load()
        validate_coverage(
            observations,
            instrument_id=instrument_id,
            start_ms=manifest["start_ms"],
            end_ms=manifest["end_ms"],
        )
        entry_modeled = sum(item.truth_status == MODELED_FUNDING for item in observations)
        entry_official = sum(item.truth_status == OFFICIAL for item in observations)
        if (
            len(observations) != entry["rows"]
            or observations[0].funding_time_ns != entry["first_ns"]
            or observations[-1].funding_time_ns != entry["last_ns"]
            or entry_modeled != entry["modeled_rows"]
            or entry_official != entry["official_rows"]
        ):
            raise ValueError(f"FundingObservation manifest readback mismatch: {instrument_id}")
        symbol = instrument_id.removesuffix("-PERP.BINANCE")
        observations_by_symbol[symbol] = observations
        actual_ids.add(instrument_id)
        modeled_rows += entry_modeled
        official_rows += entry_official
    if actual_ids != expected_ids or len(actual_ids) != len(manifest["observations"]):
        raise ValueError("FundingObservation manifest symbol coverage mismatch")
    truth_counts = manifest["truth_counts"]
    if (
        set(truth_counts) != {"modeled_funding", "official"}
        or any(not _non_negative_int(value) for value in truth_counts.values())
        or truth_counts != {"modeled_funding": modeled_rows, "official": official_rows}
    ):
        raise ValueError("FundingObservation manifest truth counts mismatch")
    return pointer, manifest, observations_by_symbol


def read_funding_observations(
    funding_path: Path,
    *,
    symbols: tuple[str, ...],
) -> dict[str, list[FundingObservation]]:
    return _load_ready_generation(funding_path, symbols=symbols)[2]


def read_funding_status(
    funding_path: Path,
    *,
    symbols: tuple[str, ...],
) -> dict[str, object]:
    pointer, manifest, observations_by_symbol = _load_ready_generation(funding_path, symbols=symbols)
    return {
        "status": "READY",
        "generation": pointer["generation"],
        "start_ms": manifest["start_ms"],
        "end_ms": manifest["end_ms"],
        "truth_counts": manifest["truth_counts"],
        "streams": {
            symbol: {
                "rows": len(observations),
                "first_ns": observations[0].funding_time_ns,
                "last_ns": observations[-1].funding_time_ns,
            }
            for symbol, observations in observations_by_symbol.items()
        },
    }
