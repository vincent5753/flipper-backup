"""用假的序列埠模擬裝置端 storage CLI（輸出格式依 storage_cli.c）。"""

import hashlib
import json
import re

import pytest

from flipper_backup import cli as cli_mod
from flipper_backup import storage
from flipper_backup.backup import Report, backup_tree
from flipper_backup.storage import FlipperCli

PROMPT = b"\r\n>: "

BIG = bytes(range(256)) * 40  # 10240 bytes，chunk_size=4096 時分三塊

TREE = {
    b"/int": None,
    b"/int/.desktop.settings": b"desk",
    b"/int/.bt.keys": b"\x00\x01keys",
    b"/ext": None,
    b"/ext/subghz": None,
    b"/ext/subghz/garage door.sub": BIG,
    b"/ext/subghz/empty.sub": b"",
    b"/ext/empty_dir": None,
    "/ext/中文.txt".encode(): b"skip me",
    "/ext/資料夾".encode(): None,
    "/ext/資料夾/a.txt".encode(): b"inside skipped dir",
}


class FakeFlipper:
    def __init__(self, tree, list_errors=()):
        self.tree = tree
        self.list_errors = set(list_errors)
        self.out = bytearray(b"Welcome to Flipper Zero" + PROMPT)
        self.inp = bytearray()
        self.chunks = None
        self.commands = []

    @property
    def in_waiting(self):
        return len(self.out)

    def read(self, size=1):
        data = bytes(self.out[:size])
        del self.out[:size]
        return data

    def reset_input_buffer(self):
        self.out.clear()

    def close(self):
        pass

    def write(self, data):
        if self.chunks is not None:
            assert data == b"y"
            self.out += self.chunks.pop(0)
            if self.chunks:
                self.out += b"\r\nReady?\r\n"
            else:
                self.chunks = None
                self.out += b"\r\n" + PROMPT
            return len(data)

        self.inp += data
        while b"\r" in self.inp:
            line, _, rest = self.inp.partition(b"\r")
            self.inp = bytearray(rest)
            self.commands.append(line.decode())
            self.out += line + b"\r\n"
            self._handle(line.decode())
        return len(data)

    def _children(self, path):
        prefix = path + b"/"
        return [k for k in self.tree if k.startswith(prefix) and b"/" not in k[len(prefix):]]

    def _handle(self, line):
        if line == "device_info":
            self.out += b"hardware_model      : Flipper Zero" + PROMPT
        elif m := re.fullmatch(r'storage list "(.*)"', line):
            path = m[1].encode()
            if path in self.list_errors or path not in self.tree:
                self.out += b"Storage error: filesystem not ready\r\n" + PROMPT
                return
            body = b""
            for child in self._children(path):
                name = child.rsplit(b"/", 1)[1]
                if self.tree[child] is None:
                    body += b"\t[D] " + name + b"\r\n"
                else:
                    body += b"\t[F] " + name + b" %db\r\n" % len(self.tree[child])
            self.out += (body or b"\tEmpty\r\n") + PROMPT
        elif m := re.fullmatch(r'storage read_chunks "(.*)" (\d+)', line):
            data, size = self.tree[m[1].encode()], int(m[2])
            self.out += b"Size: %d\r\n" % len(data)
            if data:
                self.chunks = [data[i : i + size] for i in range(0, len(data), size)]
                self.out += b"\r\nReady?\r\n"
            else:
                self.out += b"\r\n" + PROMPT
        elif m := re.fullmatch(r'storage md5 "(.*)"', line):
            self.out += hashlib.md5(self.tree[m[1].encode()]).hexdigest().encode() + b"\r\n" + PROMPT
        else:
            raise AssertionError(f"unexpected command {line!r}")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(storage.time, "sleep", lambda _: None)


def run_backup(tmp_path, tree=TREE, verify=False, **kw):
    fake = FakeFlipper(tree, **kw)
    report = Report()
    with FlipperCli(fake, timeout=1, chunk_size=4096) as cli:
        for root in ("/int", "/ext"):
            backup_tree(cli, root, tmp_path, report, verify=verify)
    return report, fake


def test_backup_int_and_ext(tmp_path):
    report, _ = run_backup(tmp_path)

    assert (tmp_path / "int/.desktop.settings").read_bytes() == b"desk"
    assert (tmp_path / "int/.bt.keys").read_bytes() == b"\x00\x01keys"
    assert (tmp_path / "ext/subghz/garage door.sub").read_bytes() == BIG
    assert (tmp_path / "ext/subghz/empty.sub").read_bytes() == b""
    assert (tmp_path / "ext/empty_dir").is_dir()
    assert not list(tmp_path.rglob("*.part"))

    assert report.files == 4
    assert report.bytes == 4 + 6 + len(BIG)
    assert report.errors == []
    assert sorted(s["name_hint"] for s in report.skipped) == ["中文.txt", "資料夾"]
    assert all(s["reason"] == "non-ascii name" and s["parent"] == "/ext" for s in report.skipped)


def test_missing_sd_card_is_recorded(tmp_path):
    report, _ = run_backup(tmp_path, list_errors={b"/ext"})

    assert report.files == 2
    assert report.errors == [
        {"path": "/ext", "op": "list", "error": "/ext: Storage error: filesystem not ready"}
    ]


def test_verify_uses_device_md5(tmp_path):
    report, fake = run_backup(tmp_path, verify=True)

    assert report.errors == []
    assert sum(c.startswith("storage md5") for c in fake.commands) == 4


def test_main_writes_report(tmp_path, monkeypatch):
    fake = FakeFlipper(TREE)
    monkeypatch.setattr(cli_mod, "open_port", lambda _: fake)
    out = tmp_path / "out"

    code = cli_mod.main(["-p", "/dev/fake", "-o", str(out)])

    assert code == 1  # 有跳過的項目
    report = json.loads((out / "backup-report.json").read_text())
    assert report["aborted"] is None
    assert report["files"] == 4
    assert len(report["skipped"]) == 2


def test_timeout_aborts(tmp_path, monkeypatch):
    class Silent(FakeFlipper):
        def _handle(self, line):
            if line.startswith("storage list"):
                return  # 裝置沒回應
            super()._handle(line)

    monkeypatch.setattr(cli_mod, "open_port", lambda _: Silent(TREE))
    monkeypatch.setattr(storage.FlipperCli.__init__, "__defaults__", (0.3, 8192))
    out = tmp_path / "out"

    assert cli_mod.main(["-p", "/dev/fake", "-o", str(out)]) == 2
    report = json.loads((out / "backup-report.json").read_text())
    assert "Timed out" in report["aborted"]
