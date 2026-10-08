from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import TextIO


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} GiB"


def fmt_duration(seconds: float) -> str:
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


class EtaEstimator:
    """每個檔案的下載時間建模為 overhead + size / throughput。

    小檔案多的目錄主要耗在每個指令的往返，大檔案則受傳輸速度限制，
    只用「剩餘位元組 ÷ 速度」會在小檔案多時嚴重低估。
    兩個參數用已完成檔案的 (size, seconds) 做最小平方法擬合。
    """

    MIN_SAMPLES = 3

    def __init__(self) -> None:
        self.n = 0
        self._sx = self._sy = self._sxx = self._sxy = 0.0

    def add(self, size: int, seconds: float) -> None:
        self.n += 1
        self._sx += size
        self._sy += seconds
        self._sxx += size * size
        self._sxy += size * seconds

    def params(self) -> tuple[float, float] | None:
        """回傳 (每檔固定開銷秒數, 每位元組秒數)；樣本不足時回傳 None。"""
        if self.n < self.MIN_SAMPLES:
            return None
        n, sx, sy, sxx, sxy = self.n, self._sx, self._sy, self._sxx, self._sxy
        denom = n * sxx - sx * sx
        # 檔案大小都差不多時無法分離兩個參數，退回只用平均速度
        if denom <= 1e-9 * max(1.0, n * sxx):
            return (0.0, sy / sx) if sx else (sy / n, 0.0)
        per_byte = (n * sxy - sx * sy) / denom
        overhead = (sy - per_byte * sx) / n
        # 任一參數擬合成負值時，固定為 0 後重新估另一個
        if per_byte < 0:
            return sy / n, 0.0
        if overhead < 0:
            return 0.0, sxy / sxx
        return overhead, per_byte

    def remaining(self, files: int, size: int) -> float | None:
        p = self.params()
        if p is None:
            return None
        overhead, per_byte = p
        return overhead * files + per_byte * size


class StatusLine:
    """TTY 用單行狀態列（\r 覆寫），否則每隔 interval 輸出一行。"""

    def __init__(
        self,
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
        interval: float | None = None,
    ) -> None:
        self.stream = stream or sys.stderr
        self.clock = clock
        self.tty = self.stream.isatty()
        self.interval = interval if interval is not None else (0.2 if self.tty else 10.0)
        self._last_draw: float | None = None
        self._line_visible = False

    def status(self) -> str:
        raise NotImplementedError

    def _maybe_draw(self) -> None:
        now = self.clock()
        if self._last_draw is None or now - self._last_draw >= self.interval:
            self.draw()

    def draw(self) -> None:
        self._last_draw = self.clock()
        if self.tty:
            self.stream.write("\r\x1b[K" + self.status())
            self._line_visible = True
        else:
            self.stream.write(self.status() + "\n")
        self.stream.flush()

    def clear_line(self) -> None:
        """讓其他 log 輸出前先清掉狀態列，輸出完再呼叫 draw() 補回。"""
        if self.tty and self._line_visible:
            self.stream.write("\r\x1b[K")
            self.stream.flush()
            self._line_visible = False

    def close(self) -> None:
        self.draw()
        if self.tty and self._line_visible:
            self.stream.write("\n")
            self.stream.flush()
        self._line_visible = False


class ScanStatus(StatusLine):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.dirs = self.files = self.bytes = 0
        self.current = ""

    def update(self, current: str, dirs: int, files: int, size: int) -> None:
        self.current, self.dirs, self.files, self.bytes = current, dirs, files, size
        self._maybe_draw()

    def status(self) -> str:
        return f"Scanning {self.current}  {self.dirs} dirs  {self.files} files  {fmt_bytes(self.bytes)}"


class Progress(StatusLine):
    """下載進度與 ETA。"""

    def __init__(self, total_files: int, total_bytes: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.estimator = EtaEstimator()

        self.files_done = 0
        self.bytes_done = 0  # 已完成（含失敗）檔案的大小總和
        self._current_size = 0
        self._current_received = 0
        self._file_started = 0.0
        self._started = self.clock()

    def start_file(self, size: int) -> None:
        self._current_size = size
        self._current_received = 0
        self._file_started = self.clock()
        self._maybe_draw()

    def advance(self, received: int) -> None:
        self._current_received += received
        self._maybe_draw()

    def finish_file(self, ok: bool) -> None:
        if ok:
            self.estimator.add(self._current_size, self.clock() - self._file_started)
        self.files_done += 1
        self.bytes_done += self._current_size
        self._current_size = self._current_received = 0
        self._maybe_draw()

    def elapsed(self) -> float:
        return self.clock() - self._started

    def eta(self) -> float | None:
        remaining_files = self.total_files - self.files_done
        remaining_bytes = self.total_bytes - self.bytes_done - self._current_received
        return self.estimator.remaining(remaining_files, max(0, remaining_bytes))

    def status(self) -> str:
        elapsed = self.elapsed()
        received = self.bytes_done + self._current_received
        eta = self.eta()
        if self.files_done >= self.total_files:
            percent = 100.0
        elif eta is not None and elapsed + eta > 0:
            # 以時間估計進度，小檔案多時比位元組比例準
            percent = 100.0 * elapsed / (elapsed + eta)
        elif self.total_bytes:
            percent = 100.0 * received / self.total_bytes
        else:
            percent = 100.0 * self.files_done / self.total_files
        speed = received / elapsed if elapsed > 0 else 0.0
        return (
            f"[{percent:3.0f}%] {fmt_bytes(received)}/{fmt_bytes(self.total_bytes)}"
            f"  {self.files_done}/{self.total_files} files"
            f"  {fmt_bytes(speed)}/s"
            f"  ETA {fmt_duration(eta) if eta is not None else '--'}"
        )
