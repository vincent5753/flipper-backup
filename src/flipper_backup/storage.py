"""透過 Flipper Zero 的 USB CLI（`storage` 指令）列目錄與讀檔。

協定行為參考 flipperzero-firmware：
- scripts/flipper/storage.py（官方 Python 用戶端）
- applications/services/storage/storage_cli.c（裝置端輸出格式）
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO, Protocol

import serial
import serial.tools.list_ports

CLI_PROMPT = b">: "
CLI_EOL = b"\r\n"
STORAGE_ERROR = b"Storage error:"

logger = logging.getLogger(__name__)


class FlipperError(Exception):
    pass


class StorageError(FlipperError):
    """裝置回報 `Storage error: ...`；CLI 狀態仍同步，可以繼續下一個指令。"""


class ProtocolError(FlipperError):
    """逾時或輸出格式不符；CLI 狀態可能已經錯亂，不應再繼續傳輸。"""


class Stream(Protocol):
    in_waiting: int

    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int | None: ...
    def reset_input_buffer(self) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Entry:
    path: str
    is_dir: bool
    size: int | None = None


@dataclass(frozen=True)
class Skipped:
    """`storage list` 輸出中無法安全處理的項目。"""

    parent: str
    raw: bytes
    reason: str

    @property
    def name_hint(self) -> str:
        # 裝置端編碼未確認，UTF-8 只是猜測，僅供人工辨識
        return self.raw.decode("utf-8", errors="replace")


def find_port() -> str:
    ports = list(serial.tools.list_ports.grep("flip_"))
    if len(ports) == 1:
        logger.info("Using %s on %s", ports[0].serial_number, ports[0].device)
        return ports[0].device
    if not ports:
        raise FlipperError("No Flipper found, use --port to specify one")
    names = ", ".join(p.device for p in ports)
    raise FlipperError(f"More than one Flipper attached ({names}), use --port")


def open_port(name: str) -> serial.Serial:
    port = serial.Serial()
    port.port = name
    port.baudrate = 115200  # VCP 不看鮑率
    # 短 timeout，讓 _read_until 有機會檢查自己的 deadline
    port.timeout = 0.2
    port.write_timeout = 60
    port.open()
    return port


def _join(parent: str, name: str) -> str:
    return f"{parent.rstrip('/')}/{name}"


class FlipperCli:
    def __init__(self, stream: Stream, timeout: float = 30.0, chunk_size: int = 8192):
        self._stream = stream
        self._buffer = bytearray()
        self.timeout = timeout
        self.chunk_size = chunk_size

    def __enter__(self) -> FlipperCli:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stream.close()

    def start(self) -> None:
        # 與官方 FlipperStorage.start() 相同的同步流程
        time.sleep(0.5)
        self._read_until(CLI_PROMPT)
        self._stream.reset_input_buffer()
        self._buffer.clear()
        self._send("device_info")
        self._read_until(b"hardware_model")
        self._read_until(CLI_PROMPT)

    # --- 低階讀寫 ---

    def _send(self, line: str) -> None:
        self._stream.write(line.encode("ascii") + b"\r")

    def _fill(self, deadline: float, waiting_for: bytes) -> None:
        data = self._stream.read(max(1, self._stream.in_waiting))
        if data:
            self._buffer.extend(data)
        elif time.monotonic() > deadline:
            raise ProtocolError(f"Timed out waiting for {waiting_for!r} from Flipper")

    def _read_until(self, token: bytes, timeout: float | None = None) -> bytes:
        timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while (i := self._buffer.find(token)) < 0:
            before = len(self._buffer)
            self._fill(deadline, token)
            if len(self._buffer) != before:
                deadline = time.monotonic() + timeout
        data = bytes(self._buffer[:i])
        del self._buffer[: i + len(token)]
        return data

    def _read_exact(self, size: int) -> bytes:
        deadline = time.monotonic() + self.timeout
        while len(self._buffer) < size:
            before = len(self._buffer)
            self._fill(deadline, f"{size} bytes".encode())
            if len(self._buffer) != before:
                deadline = time.monotonic() + self.timeout
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def _command(self, line: str) -> None:
        self._send(line)
        self._read_until(CLI_EOL)  # 指令回顯

    # --- storage 指令 ---

    def list_dir(self, path: str) -> tuple[list[Entry], list[Skipped]]:
        self._command(f'storage list "{path}"')
        output = self._read_until(CLI_PROMPT)

        entries: list[Entry] = []
        skipped: list[Skipped] = []
        for raw in output.split(CLI_EOL):
            line = raw.strip()
            if not line or line == b"Empty":
                continue
            if line.startswith(STORAGE_ERROR):
                raise StorageError(f"{path}: {line.decode('ascii', 'replace')}")

            kind, _, info = line.partition(b" ")
            if kind == b"[D]":
                name, size = info, None
            elif kind == b"[F]":
                name, _, size_text = info.rpartition(b" ")
                try:
                    size = int(size_text.removesuffix(b"b"))
                except ValueError:
                    skipped.append(Skipped(path, line, "unparsable list line"))
                    continue
            else:
                skipped.append(Skipped(path, line, "unparsable list line"))
                continue

            try:
                text = name.decode("ascii")
            except UnicodeDecodeError:
                skipped.append(Skipped(path, name, "non-ascii name"))
                continue
            # FAT 不允許 ? 出現在檔名，出現代表裝置把無法表示的字元替換掉了，
            # 用這個名稱讀取一定回 invalid name/path
            if "?" in text:
                skipped.append(Skipped(path, name, "name contains '?' (unrepresentable characters)"))
                continue
            # 雙引號無法放進 CLI 參數；/ 與 . .. 會讓本機路徑跳出備份目錄
            if '"' in text or "/" in text or text in ("", ".", ".."):
                skipped.append(Skipped(path, name, "unsafe name"))
                continue

            entries.append(Entry(_join(path, text), kind == b"[D]", size))
        return entries, skipped

    def read_file(self, path: str, out: BinaryIO, on_chunk: Callable[[int], None] | None = None) -> int:
        self._command(f'storage read_chunks "{path}" {self.chunk_size}')
        header = self._read_until(CLI_EOL)
        if header.startswith(STORAGE_ERROR):
            self._read_until(CLI_PROMPT)
            raise StorageError(f"{path}: {header.decode('ascii', 'replace')}")
        if not header.startswith(b"Size: "):
            raise ProtocolError(f"{path}: unexpected read_chunks header {header!r}")

        remaining = size = int(header[len(b"Size: ") :])
        while remaining:
            self._read_until(b"Ready?" + CLI_EOL)
            self._stream.write(b"y")
            chunk = self._read_exact(min(remaining, self.chunk_size))
            out.write(chunk)
            remaining -= len(chunk)
            if on_chunk:
                on_chunk(len(chunk))
        self._read_until(CLI_PROMPT)
        return size

    def md5(self, path: str, timeout: float = 600.0) -> str:
        self._command(f'storage md5 "{path}"')
        line = self._read_until(CLI_EOL, timeout=timeout)
        self._read_until(CLI_PROMPT)
        if line.startswith(STORAGE_ERROR):
            raise StorageError(f"{path}: {line.decode('ascii', 'replace')}")
        return line.decode("ascii").strip()
