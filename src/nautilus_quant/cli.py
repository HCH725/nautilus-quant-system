from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import traceback

from .config import MarketDataConfig, load_config, valid_binance_usdt_symbol
from .service import AlreadyRunning, run_sync, single_writer, write_report
from .timebound import interval_millis

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config/market_data.json"
_REPORT_STATUSES = {"PASS", "PARTIAL", "FAIL"}
_REPORT_DATASETS = {"trade", "mark", "index", "premium", "funding"}


def _utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nautilus-data")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="idempotently sync configured public Binance data")
    sync.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sync.add_argument("--now", type=_utc_datetime, help="override current UTC time for deterministic verification")
    sync.add_argument("--max-chunks", type=int, help="bound each bar stream (verification only)")

    status = subparsers.add_parser("status", help="show latest deterministic run evidence")
    status.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser


def _print_event(event: dict[str, object]) -> None:
    print(json.dumps(event, sort_keys=True), flush=True)


def _config_scope(config: MarketDataConfig) -> dict[str, object]:
    return {
        "base_url": config.base_url,
        "start": config.start.isoformat(),
        "symbols": list(config.symbols),
        "intervals": list(config.intervals),
        "datasets": list(config.datasets),
    }


def _validated_report_scope(value: object) -> dict[str, object]:
    string_fields = {"base_url", "start"}
    list_fields = {"symbols", "intervals", "datasets"}
    if not isinstance(value, dict) or set(value) != string_fields | list_fields:
        raise ValueError("invalid run report field: config_scope")
    if any(not isinstance(value[field], str) or not value[field] for field in string_fields):
        raise ValueError("invalid run report field: config_scope")
    for field in list_fields:
        items = value[field]
        if (
            not isinstance(items, list)
            or not items
            or any(not isinstance(item, str) or not item for item in items)
            or len(items) != len(set(items))
        ):
            raise ValueError("invalid run report field: config_scope")
    if value["base_url"] != "https://fapi.binance.com":
        raise ValueError("invalid run report field: config_scope")
    try:
        parsed_start = datetime.fromisoformat(value["start"])
    except ValueError as exc:
        raise ValueError("invalid run report field: config_scope") from exc
    if parsed_start.tzinfo is None or parsed_start.astimezone(timezone.utc).isoformat() != value["start"]:
        raise ValueError("invalid run report field: config_scope")
    symbols = value["symbols"]
    if any(not valid_binance_usdt_symbol(symbol) for symbol in symbols):
        raise ValueError("invalid run report field: config_scope")
    try:
        for interval in value["intervals"]:
            interval_millis(interval)
    except ValueError as exc:
        raise ValueError("invalid run report field: config_scope") from exc
    if set(value["datasets"]) - _REPORT_DATASETS:
        raise ValueError("invalid run report field: config_scope")
    return value


def _latest_report(
    run_dir: Path,
    catalog_path: Path,
    funding_path: Path,
    config_scope: dict[str, object],
) -> dict[str, object] | None:
    for path in reversed(sorted(run_dir.glob("sync-*.json"))):
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("run report must be a JSON object")
        for field in ("catalog_path", "funding_path"):
            if not isinstance(report.get(field), str) or not report[field]:
                raise ValueError(f"invalid run report field: {field}")
        if report.get("status") not in _REPORT_STATUSES:
            raise ValueError("invalid run report field: status")
        if "config_scope" not in report:
            continue
        report_scope = _validated_report_scope(report["config_scope"])
        if (
            report.get("catalog_path") == str(catalog_path)
            and report.get("funding_path") == str(funding_path)
            and report_scope == config_scope
        ):
            return report
    return None


def _emit_failure(
    exc: BaseException,
    run_dir: Path,
    *,
    persist: bool,
    catalog_path: Path | None = None,
    funding_path: Path | None = None,
    config_scope: dict[str, object] | None = None,
) -> int:
    report: dict[str, object] = {
        "status": "FAIL",
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    if catalog_path is not None and funding_path is not None:
        report.update({"catalog_path": str(catalog_path), "funding_path": str(funding_path)})
    if config_scope is not None:
        report["config_scope"] = config_scope
    evidence = getattr(exc, "sync_evidence", None)
    if isinstance(evidence, dict):
        report.update(evidence)
    if persist:
        report["report_path"] = str(write_report(report, run_dir))
    print(json.dumps(report, sort_keys=True), file=sys.stderr, flush=True)
    traceback.print_exc()
    return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = PROJECT_ROOT.resolve()
    config_path = args.config.resolve()
    run_dir = project_root / "var/runs"
    try:
        config = load_config(config_path, project_root)
    except BaseException as exc:
        return _emit_failure(exc, run_dir, persist=args.command == "sync")
    config_scope = _config_scope(config)

    if args.command == "status":
        try:
            latest = _latest_report(run_dir, config.catalog_path, config.funding_path, config_scope)
        except BaseException as exc:
            return _emit_failure(exc, run_dir, persist=False)
        _print_event({
            "status": "NO_RUN" if latest is None else latest.get("status"),
            "latest_report": latest,
            "catalog_path": str(config.catalog_path),
            "funding_path": str(config.funding_path),
            "config_scope": config_scope,
        })
        return 0

    try:
        with single_writer(project_root / "var/locks/data-sync.lock"):
            report = run_sync(config, now=args.now, max_chunks=args.max_chunks, progress=_print_event)
    except AlreadyRunning as exc:
        _print_event({"status": "BUSY", "message": str(exc)})
        return 0
    except BaseException as exc:
        return _emit_failure(
            exc,
            run_dir,
            persist=True,
            catalog_path=config.catalog_path,
            funding_path=config.funding_path,
            config_scope=config_scope,
        )

    report["config_scope"] = config_scope
    report_path = write_report(report, run_dir)
    _print_event({"status": report["status"], "report_path": str(report_path)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
