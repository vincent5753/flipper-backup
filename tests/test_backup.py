"""用假的序列埠模擬裝置端 storage CLI（輸出格式依 storage_cli.c）。"""

import hashlib
import io
import json
import re
import shutil
from pathlib import Path

import pytest

from flipper_backup import cli as cli_mod
from flipper_backup import storage
from flipper_backup.backup import Report, download, scan
from flipper_backup.progress import EtaEstimator, Progress
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
    # 實機上看到的情況：無法表示的字元被裝置換成 ?
    b"/ext/Se^2": None,
    b"/ext/Se^2/?": None,
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


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeStream(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


def run_backup(tmp_path, tree=TREE, verify=False, progress=None, **kw):
    fake = FakeFlipper(tree, **kw)
    report = Report()
    with FlipperCli(fake, timeout=1, chunk_size=4096) as cli:
        plan = scan(cli, ["/int", "/ext"], report)
        if progress is not None:
            progress = progress(plan)
        download(cli, plan, tmp_path, report, verify=verify, progress=progress)
    return report, fake, plan


def run_main(monkeypatch, fake, *args):
    monkeypatch.setattr(cli_mod, "open_port", lambda _: fake)
    return cli_mod.main(["-p", "/dev/fake", *map(str, args)])


# --- 掃描與下載 ---


def test_backup_int_and_ext(tmp_path):
    report, fake, _ = run_backup(tmp_path)

    assert (tmp_path / "int/.desktop.settings").read_bytes() == b"desk"
    assert (tmp_path / "int/.bt.keys").read_bytes() == b"\x00\x01keys"
    assert (tmp_path / "ext/subghz/garage door.sub").read_bytes() == BIG
    assert (tmp_path / "ext/subghz/empty.sub").read_bytes() == b""
    assert (tmp_path / "ext/empty_dir").is_dir()
    assert not list(tmp_path.rglob("*.part"))

    assert report.files == report.planned_files == 4
    assert report.bytes == report.planned_bytes == 4 + 6 + len(BIG)
    assert report.errors == []
    reasons = {s["name_hint"]: s["reason"] for s in report.skipped}
    assert reasons.keys() == {"中文.txt", "資料夾", "?"}
    assert reasons["中文.txt"] == reasons["資料夾"] == "non-ascii name"
    assert "?" in reasons["?"]
    # 含 ? 的目錄不應該再被 list
    assert 'storage list "/ext/Se^2/?"' not in fake.commands


def test_scan_totals_per_root(tmp_path):
    fake = FakeFlipper(TREE)
    report = Report()
    with FlipperCli(fake, timeout=1) as cli:
        plan = scan(cli, ["/int", "/ext"], report)

    assert plan.totals("/int") == (2, 10)
    assert plan.totals("/ext") == (2, len(BIG))
    assert plan.dirs == ["/int", "/ext", "/ext/Se^2", "/ext/empty_dir", "/ext/subghz"]
    assert not any(c.startswith("storage read_chunks") for c in fake.commands)


def test_missing_sd_card_is_recorded(tmp_path):
    report, _, _ = run_backup(tmp_path, list_errors={b"/ext"})

    assert report.files == 2
    assert report.errors == [
        {"path": "/ext", "op": "list", "error": "/ext: Storage error: filesystem not ready"}
    ]


def test_verify_uses_device_md5(tmp_path):
    report, fake, _ = run_backup(tmp_path, verify=True)

    assert report.errors == []
    assert sum(c.startswith("storage md5") for c in fake.commands) == 4


# --- 進度 ---


def test_progress_tracks_every_chunk(tmp_path):
    advanced = []

    def make_progress(plan):
        progress = Progress(len(plan.files), plan.total_bytes, stream=FakeStream(tty=False), interval=1e9)
        original = progress.advance
        progress.advance = lambda n: (advanced.append(n), original(n))
        return progress

    holder = {}

    def capture(plan):
        holder["p"] = make_progress(plan)
        return holder["p"]

    report, _, _ = run_backup(tmp_path, progress=capture)
    progress = holder["p"]

    assert advanced == [6, 4, 4096, 4096, 2048]  # 依掃描順序：.bt.keys、.desktop.settings、garage door.sub
    assert progress.files_done == progress.total_files == 4
    assert progress.bytes_done == progress.total_bytes == report.bytes
    assert progress.status().startswith("[100%]")


def test_progress_counts_failed_file_as_done():
    progress = Progress(2, 300, stream=FakeStream(tty=False), clock=FakeClock())
    progress.start_file(100)
    progress.advance(40)
    progress.finish_file(ok=False)

    assert progress.files_done == 1
    assert progress.bytes_done == 100
    assert progress.estimator.n == 0  # 失敗的檔案不拿來估速度


def test_progress_tty_overwrites_line():
    stream, clock = FakeStream(tty=True), FakeClock()
    progress = Progress(2, 200, stream=stream, clock=clock)
    for _ in range(2):
        progress.start_file(100)
        clock.now += 1
        progress.advance(100)
        progress.finish_file(ok=True)
    progress.close()

    out = stream.getvalue()
    assert out.count("\r\x1b[K") >= 2
    assert out.endswith("ETA 0s\n") or out.endswith("ETA --\n")
    assert out.count("\n") == 1


def test_progress_non_tty_prints_lines_at_interval():
    stream, clock = FakeStream(tty=False), FakeClock()
    progress = Progress(100, 100, stream=stream, clock=clock, interval=10)
    for _ in range(100):
        progress.start_file(1)
        clock.now += 1
        progress.advance(1)
        progress.finish_file(ok=True)
    progress.close()

    lines = stream.getvalue().splitlines()
    assert "\r" not in stream.getvalue()
    assert 10 <= len(lines) <= 12  # 每 10 秒一行，加上第一次與 close()
    assert lines[-1].startswith("[100%]")


# --- ETA ---


def test_eta_needs_samples():
    est = EtaEstimator()
    est.add(100, 1.0)
    est.add(200, 1.0)
    assert est.remaining(10, 1000) is None


def test_eta_separates_overhead_and_throughput():
    est = EtaEstimator()
    # 每檔 0.05 秒開銷 + 100 KB/s
    for size in (10, 500, 2_000, 50_000, 300_000, 1_000_000):
        est.add(size, 0.05 + size / 100_000)

    overhead, per_byte = est.params()
    assert overhead == pytest.approx(0.05)
    assert 1 / per_byte == pytest.approx(100_000)
    # 1000 個小檔案：只看位元組會估成 1 秒，實際主要是 50 秒開銷
    assert est.remaining(1000, 100_000) == pytest.approx(51.0)


def test_eta_with_identical_sizes_falls_back_to_average_speed():
    est = EtaEstimator()
    for _ in range(5):
        est.add(1000, 0.5)

    assert est.remaining(4, 4000) == pytest.approx(2.0)


def test_eta_clamps_negative_overhead():
    est = EtaEstimator()
    # 大檔反而比預期快的雜訊資料，直線擬合會得到負的開銷
    for size, t in ((100, 0.0), (1000, 0.02), (10_000, 0.1), (100_000, 1.0)):
        est.add(size, t)

    overhead, per_byte = est.params()
    assert overhead >= 0 and per_byte > 0


# --- CLI ---


def test_main_writes_report(tmp_path, monkeypatch):
    out = tmp_path / "out"

    code = run_main(monkeypatch, FakeFlipper(TREE), "-o", out)

    assert code == 1  # 有跳過的項目
    report = json.loads((out / "backup-report.json").read_text())
    assert report["aborted"] is None
    assert report["files"] == report["planned_files"] == 4
    assert len(report["skipped"]) == 3
    assert {"scan_seconds", "download_seconds", "per_file_overhead_seconds"} <= report["timing"].keys()


def test_scan_only_downloads_nothing(tmp_path, monkeypatch, capsys):
    fake = FakeFlipper(TREE)
    out = tmp_path / "out"

    code = run_main(monkeypatch, fake, "--scan-only", "-o", out)

    assert code == 1
    assert not out.exists()
    assert not any(c.startswith("storage read_chunks") for c in fake.commands)
    # main() 會換掉 root logger 的 handler，所以從 stderr 讀
    messages = capsys.readouterr().err
    assert "/int: 2 files, 10 B" in messages
    assert "/ext: 2 files, 10.0 KiB" in messages


def test_scan_only_clean_exit_code(tmp_path, monkeypatch):
    tree = {b"/int": None, b"/int/a": b"x", b"/ext": None}
    assert run_main(monkeypatch, FakeFlipper(tree), "--scan-only") == 0


def test_not_enough_disk_space(tmp_path, monkeypatch):
    fake = FakeFlipper(TREE)
    out = tmp_path / "out"
    monkeypatch.setattr(cli_mod.shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(100, 99, 1))

    assert run_main(monkeypatch, fake, "-o", out) == 2
    assert not out.exists()
    assert not any(c.startswith("storage read_chunks") for c in fake.commands)


class Silent(FakeFlipper):
    def __init__(self, tree, silent_prefix):
        super().__init__(tree)
        self.silent_prefix = silent_prefix

    def _handle(self, line):
        if line.startswith(self.silent_prefix):
            return  # 裝置沒回應
        super()._handle(line)


def test_timeout_during_scan_aborts_without_output(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.FlipperCli.__init__, "__defaults__", (0.3, 8192))
    out = tmp_path / "out"

    assert run_main(monkeypatch, Silent(TREE, "storage list"), "-o", out) == 2
    assert not out.exists()


def test_timeout_during_download_writes_report(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.FlipperCli.__init__, "__defaults__", (0.3, 8192))
    out = tmp_path / "out"

    assert run_main(monkeypatch, Silent(TREE, "storage read_chunks"), "-o", out) == 2
    report = json.loads((out / "backup-report.json").read_text())
    assert "Timed out" in report["aborted"]
    assert report["files"] == 0
    assert not list(out.rglob("*.part"))


def test_default_output_under_flipper_backups(tmp_path, monkeypatch):
    # 不要寫進真正的專案目錄
    monkeypatch.setattr(cli_mod, "default_backup_root", lambda: tmp_path / "flipper-backups")
    tree = {b"/int": None, b"/int/a": b"x", b"/ext": None}

    assert run_main(monkeypatch, FakeFlipper(tree)) == 0
    (dest,) = (tmp_path / "flipper-backups").iterdir()
    assert re.fullmatch(r"\d{8}-\d{6}", dest.name)
    assert (dest / "int/a").read_bytes() == b"x"
    assert (dest / "backup-report.json").exists()


def test_project_root_found_from_source_file():
    # 測試環境是 editable 安裝，cli.py 位於 <專案>/src/flipper_backup/
    root = cli_mod.find_project_root(Path(cli_mod.__file__))
    assert root == Path(__file__).resolve().parents[1]
    assert cli_mod.default_backup_root() == root / "flipper-backups"


def test_project_root_ignores_other_projects(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "something-else"\n')
    module = tmp_path / "venv/site-packages/flipper_backup/cli.py"
    module.parent.mkdir(parents=True)
    module.touch()

    assert cli_mod.find_project_root(module) is None


def test_default_backup_root_falls_back_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_mod, "find_project_root", lambda _: None)

    assert cli_mod.default_backup_root() == tmp_path / "flipper-backups"
