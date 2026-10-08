from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .backup import Plan, Report, download, scan
from .progress import Progress, ScanStatus, StatusLine, fmt_bytes, fmt_duration
from .storage import FlipperCli, FlipperError, find_port, open_port

logger = logging.getLogger("flipper_backup")

ROOTS = ("/int", "/ext")


class _StatusAwareHandler(logging.StreamHandler):
    """輸出 log 前先清掉狀態列，輸出後補回，避免兩者擠在同一行。"""

    status: StatusLine | None = None

    def emit(self, record: logging.LogRecord) -> None:
        status = self.status
        if status:
            status.clear_line()
        super().emit(record)
        if status and status.tty:
            status.draw()


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Back up Flipper Zero /int and /ext over USB CLI")
    parser.add_argument("-p", "--port", default="auto", help="serial port (default: auto-detect)")
    parser.add_argument("-o", "--output", type=Path, help="output directory (default: ./flipper-backups/<timestamp>)")
    parser.add_argument("--only", choices=[r.strip("/") for r in ROOTS], help="back up only one storage")
    parser.add_argument("--scan-only", action="store_true", help="list files and print totals without downloading")
    parser.add_argument("--verify", action="store_true", help="compare md5 of every file with the device (slow)")
    parser.add_argument("--chunk-size", type=int, default=8192)
    parser.add_argument("-v", "--verbose", action="store_true", help="log every file")
    return parser.parse_args(argv)


def _free_space(path: Path) -> int:
    # 輸出目錄還沒建立時，往上找第一個存在的目錄
    path = path.absolute()
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def _log_scan_summary(plan: Plan, roots: list[str], report: Report, seconds: float) -> None:
    for root in roots:
        files, size = plan.totals(root)
        logger.info("%s: %d files, %s", root, files, fmt_bytes(size))
    logger.info(
        "Scan finished in %s: %d dirs, %d files, %s total, %d skipped, %d errors",
        fmt_duration(seconds), len(plan.dirs), len(plan.files), fmt_bytes(plan.total_bytes),
        len(report.skipped), len(report.errors),
    )


def _log_problems(report: Report) -> None:
    if report.skipped:
        logger.warning("Skipped entries (not backed up):")
        for s in report.skipped:
            logger.warning("  %s/%s (%s)", s["parent"], s["name_hint"], s["reason"])
    if report.errors:
        logger.warning("Errors:")
        for e in report.errors:
            logger.warning("  %s", e["error"])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    handler = _StatusAwareHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=[handler], force=True)

    try:
        port = args.port if args.port != "auto" else find_port()
    except FlipperError as e:
        logger.error("%s", e)
        return 2

    started = datetime.now()
    dest: Path = args.output or Path("flipper-backups", f"{started:%Y%m%d-%H%M%S}")
    if not args.scan_only and dest.exists() and any(dest.iterdir()):
        logger.error("Output directory %s is not empty", dest)
        return 2

    roots = [f"/{args.only}"] if args.only else list(ROOTS)
    report = Report()
    aborted: str | None = None
    dest_created = False

    try:
        with FlipperCli(open_port(port), chunk_size=args.chunk_size) as cli:
            scan_status = handler.status = ScanStatus()
            t0 = time.monotonic()
            plan = scan(
                cli, roots, report,
                on_dir=lambda d, p: scan_status.update(d, len(p.dirs), len(p.files), p.total_bytes),
            )
            scan_status.close()
            handler.status = None
            report.timing["scan_seconds"] = round(time.monotonic() - t0, 1)
            _log_scan_summary(plan, roots, report, report.timing["scan_seconds"])

            if args.scan_only:
                _log_problems(report)
                return 0 if report.clean else 1

            free = _free_space(dest)
            if plan.total_bytes > free:
                logger.error("Not enough disk space: need %s, %s free", fmt_bytes(plan.total_bytes), fmt_bytes(free))
                return 2

            dest.mkdir(parents=True, exist_ok=True)
            dest_created = True
            progress = handler.status = Progress(len(plan.files), plan.total_bytes)
            try:
                download(cli, plan, dest, report, verify=args.verify, progress=progress)
            finally:
                progress.close()
                handler.status = None
                report.timing["download_seconds"] = round(progress.elapsed(), 1)
                if params := progress.estimator.params():
                    overhead, per_byte = params
                    report.timing["per_file_overhead_seconds"] = round(overhead, 4)
                    report.timing["bytes_per_second"] = round(1 / per_byte) if per_byte else None
    except (FlipperError, OSError) as e:
        # ProtocolError、序列埠斷線等：CLI 狀態不可信，停止整個備份
        aborted = str(e)
        logger.error("Aborted: %s", e)
    except KeyboardInterrupt:
        aborted = "interrupted by user"
        logger.error("Aborted: interrupted")
    finally:
        if handler.status:
            handler.status.close()
            handler.status = None
        if dest_created:
            summary = {
                "started": started.isoformat(timespec="seconds"),
                "finished": datetime.now().isoformat(timespec="seconds"),
                "roots": roots,
                "verify": args.verify,
                "aborted": aborted,
                **asdict(report),
            }
            (dest / "backup-report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    if not dest_created:
        # 掃描階段就中止，沒有下載任何東西
        return 2

    logger.info(
        "%d/%d files, %s downloaded in %s -> %s",
        report.files, report.planned_files, fmt_bytes(report.bytes),
        fmt_duration(report.timing.get("download_seconds", 0)), dest,
    )
    _log_problems(report)

    if aborted:
        return 2
    return 0 if report.clean else 1


if __name__ == "__main__":
    sys.exit(main())
