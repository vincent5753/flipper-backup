from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .progress import Progress
from .storage import Entry, FlipperCli, StorageError

logger = logging.getLogger(__name__)


@dataclass
class Report:
    planned_files: int = 0
    planned_bytes: int = 0
    files: int = 0
    bytes: int = 0
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    timing: dict = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.skipped and not self.errors


@dataclass
class Plan:
    dirs: list[str] = field(default_factory=list)
    files: list[Entry] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size or 0 for f in self.files)

    def totals(self, root: str) -> tuple[int, int]:
        """回傳 root 底下的 (檔案數, 位元組數)。"""
        prefix = root.rstrip("/") + "/"
        files = [f for f in self.files if f.path.startswith(prefix)]
        return len(files), sum(f.size or 0 for f in files)


def _md5_local(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while chunk := f.read(1 << 16):
            h.update(chunk)
    return h.hexdigest()


def scan(
    cli: FlipperCli,
    roots: list[str],
    report: Report,
    on_dir: Callable[[str, Plan], None] | None = None,
) -> Plan:
    """遞迴列出 roots 底下所有目錄與檔案，不下載任何內容。

    StorageError 只記錄後繼續；ProtocolError 直接往上丟，因為 CLI 可能已失去同步。
    """
    plan = Plan()
    for root in roots:
        pending = [root]
        while pending:
            remote_dir = pending.pop()
            try:
                entries, skipped = cli.list_dir(remote_dir)
            except StorageError as e:
                logger.error("%s", e)
                report.errors.append({"path": remote_dir, "op": "list", "error": str(e)})
                continue

            plan.dirs.append(remote_dir)
            for s in skipped:
                logger.warning("Skipped %s/%s (%s)", remote_dir, s.name_hint, s.reason)
                report.skipped.append(
                    {"parent": remote_dir, "name_hint": s.name_hint, "raw_hex": s.raw.hex(), "reason": s.reason}
                )

            entries.sort(key=lambda e: e.path)
            # 反序推入，讓目錄依字母順序處理
            pending.extend(e.path for e in reversed(entries) if e.is_dir)
            plan.files.extend(e for e in entries if not e.is_dir)
            if on_dir:
                on_dir(remote_dir, plan)

    report.planned_files = len(plan.files)
    report.planned_bytes = plan.total_bytes
    return plan


def download(
    cli: FlipperCli,
    plan: Plan,
    dest: Path,
    report: Report,
    verify: bool = False,
    progress: Progress | None = None,
) -> None:
    for remote_dir in plan.dirs:
        (dest / remote_dir.lstrip("/")).mkdir(parents=True, exist_ok=True)

    for entry in plan.files:
        size = entry.size or 0
        if progress:
            progress.start_file(size)
        ok = _download(cli, entry.path, size, dest, report, verify, progress.advance if progress else None)
        if progress:
            progress.finish_file(ok)


def _download(
    cli: FlipperCli,
    remote: str,
    expected: int,
    dest: Path,
    report: Report,
    verify: bool,
    on_chunk: Callable[[int], None] | None,
) -> bool:
    local = dest / remote.lstrip("/")
    part = local.with_name(local.name + ".part")
    logger.debug("%s (%d bytes)", remote, expected)

    try:
        with part.open("wb") as f:
            size = cli.read_file(remote, f, on_chunk)
        if size != expected:
            raise StorageError(f"{remote}: size mismatch, listed {expected}, read {size}")
        if verify:
            remote_md5, local_md5 = cli.md5(remote), _md5_local(part)
            if remote_md5 != local_md5:
                raise StorageError(f"{remote}: md5 mismatch, device {remote_md5}, local {local_md5}")
    except StorageError as e:
        logger.error("%s", e)
        report.errors.append({"path": remote, "op": "read", "error": str(e)})
        part.unlink(missing_ok=True)
        return False
    except BaseException:
        part.unlink(missing_ok=True)
        raise

    part.replace(local)
    report.files += 1
    report.bytes += size
    return True
