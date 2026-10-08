from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .storage import FlipperCli, StorageError

logger = logging.getLogger(__name__)


@dataclass
class Report:
    files: int = 0
    bytes: int = 0
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.skipped and not self.errors


def _md5_local(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while chunk := f.read(1 << 16):
            h.update(chunk)
    return h.hexdigest()


def backup_tree(cli: FlipperCli, root: str, dest: Path, report: Report, verify: bool = False) -> None:
    """把裝置上的 root（如 /int）遞迴下載到 dest/<root>。

    StorageError 只記錄後繼續；ProtocolError 直接往上丟，因為 CLI 可能已失去同步。
    """
    pending = [root]
    while pending:
        remote_dir = pending.pop()
        try:
            entries, skipped = cli.list_dir(remote_dir)
        except StorageError as e:
            logger.error("%s", e)
            report.errors.append({"path": remote_dir, "op": "list", "error": str(e)})
            continue

        for s in skipped:
            logger.warning("Skipped %s/%s (%s)", remote_dir, s.name_hint, s.reason)
            report.skipped.append(
                {"parent": remote_dir, "name_hint": s.name_hint, "raw_hex": s.raw.hex(), "reason": s.reason}
            )

        local_dir = dest / remote_dir.lstrip("/")
        local_dir.mkdir(parents=True, exist_ok=True)

        entries.sort(key=lambda e: e.path)
        # 反序推入，讓目錄依字母順序處理
        pending.extend(e.path for e in reversed(entries) if e.is_dir)
        for entry in entries:
            if not entry.is_dir:
                _download(cli, entry.path, entry.size, dest, report, verify)


def _download(cli: FlipperCli, remote: str, expected: int | None, dest: Path, report: Report, verify: bool) -> None:
    local = dest / remote.lstrip("/")
    part = local.with_name(local.name + ".part")
    logger.info("%s (%s bytes)", remote, expected if expected is not None else "?")

    try:
        with part.open("wb") as f:
            size = cli.read_file(remote, f)
        if expected is not None and size != expected:
            raise StorageError(f"{remote}: size mismatch, listed {expected}, read {size}")
        if verify:
            remote_md5, local_md5 = cli.md5(remote), _md5_local(part)
            if remote_md5 != local_md5:
                raise StorageError(f"{remote}: md5 mismatch, device {remote_md5}, local {local_md5}")
    except StorageError as e:
        logger.error("%s", e)
        report.errors.append({"path": remote, "op": "read", "error": str(e)})
        part.unlink(missing_ok=True)
        return
    except BaseException:
        part.unlink(missing_ok=True)
        raise

    part.replace(local)
    report.files += 1
    report.bytes += size
