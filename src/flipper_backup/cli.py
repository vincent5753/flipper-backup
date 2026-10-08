from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .backup import Report, backup_tree
from .storage import FlipperCli, FlipperError, find_port, open_port

logger = logging.getLogger("flipper_backup")

ROOTS = ("/int", "/ext")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Back up Flipper Zero /int and /ext over USB CLI")
    parser.add_argument("-p", "--port", default="auto", help="serial port (default: auto-detect)")
    parser.add_argument("-o", "--output", type=Path, help="output directory (default: ./flipper-backup-<timestamp>)")
    parser.add_argument("--only", choices=[r.strip("/") for r in ROOTS], help="back up only one storage")
    parser.add_argument("--verify", action="store_true", help="compare md5 of every file with the device (slow)")
    parser.add_argument("--chunk-size", type=int, default=8192)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    try:
        port = args.port if args.port != "auto" else find_port()
    except FlipperError as e:
        logger.error("%s", e)
        return 2

    started = datetime.now()
    dest: Path = args.output or Path(f"flipper-backup-{started:%Y%m%d-%H%M%S}")
    if dest.exists() and any(dest.iterdir()):
        logger.error("Output directory %s is not empty", dest)
        return 2
    dest.mkdir(parents=True, exist_ok=True)

    roots = [f"/{args.only}"] if args.only else list(ROOTS)
    report = Report()
    aborted: str | None = None

    try:
        with FlipperCli(open_port(port), chunk_size=args.chunk_size) as cli:
            for root in roots:
                logger.info("Backing up %s", root)
                backup_tree(cli, root, dest, report, verify=args.verify)
    except (FlipperError, OSError) as e:
        # ProtocolError、序列埠斷線等：CLI 狀態不可信，停止整個備份
        aborted = str(e)
        logger.error("Aborted: %s", e)
    except KeyboardInterrupt:
        aborted = "interrupted by user"
        logger.error("Aborted: interrupted")
    finally:
        summary = {
            "started": started.isoformat(timespec="seconds"),
            "finished": datetime.now().isoformat(timespec="seconds"),
            "roots": roots,
            "verify": args.verify,
            "aborted": aborted,
            **asdict(report),
        }
        (dest / "backup-report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    logger.info(
        "%d files, %d bytes, %d skipped, %d errors -> %s",
        report.files, report.bytes, len(report.skipped), len(report.errors), dest,
    )
    if report.skipped:
        logger.warning("Skipped entries (not backed up), see backup-report.json:")
        for s in report.skipped:
            logger.warning("  %s/%s (%s)", s["parent"], s["name_hint"], s["reason"])

    if aborted:
        return 2
    return 0 if report.clean else 1


if __name__ == "__main__":
    sys.exit(main())
