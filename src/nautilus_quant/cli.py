from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import traceback

from .config import load_config
from .service import AlreadyRunning, run_sync, single_writer, write_report

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config/market_data.json"


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


def _latest_report(run_dir: Path) -> dict[str, object] | None:
    reports = sorted(run_dir.glob("sync-*.json"))
    if not reports:
        return None
    return json.loads(reports[-1].read_text(encoding="utf-8"))


def _emit_failure(exc: BaseException, run_dir: Path, *, persist: bool) -> int:
    report = {
        "status": "FAIL",
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
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

    if args.command == "status":
        latest = _latest_report(run_dir)
        _print_event({
            "status": "NO_RUN" if latest is None else latest.get("status"),
            "latest_report": latest,
            "catalog_path": str(config.catalog_path),
            "funding_path": str(config.funding_path),
        })
        return 0

    try:
        with single_writer(project_root / "var/locks/data-sync.lock"):
            report = run_sync(config, now=args.now, max_chunks=args.max_chunks, progress=_print_event)
    except AlreadyRunning as exc:
        _print_event({"status": "BUSY", "message": str(exc)})
        return 0
    except BaseException as exc:
        return _emit_failure(exc, run_dir, persist=True)

    report_path = write_report(report, run_dir)
    _print_event({"status": report["status"], "report_path": str(report_path)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
