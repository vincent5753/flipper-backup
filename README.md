# flipper-backup
## 介紹 / Intro
透過 USB 一次備份 Flipper Zero 的內部儲存（`/int`）和 SD 卡（`/ext`）。使用裝置內建的 CLI `storage` 指令，不需要 qFlipper。\
Back up both the internal storage (`/int`) and the SD card (`/ext`) of a Flipper Zero over USB in one run, using the device's built-in CLI `storage` commands. qFlipper is not required.

## 功能 / Features
- 先掃描再下載，開始前就知道總檔案數和大小 / Scans first, so totals are known before downloading
- 進度列與 ETA（依「每檔固定開銷 + 傳輸速度」估算，小檔案多也不會低估）/ Progress bar with ETA that models per-file overhead plus throughput
- `--scan-only` 只列出總量不下載 / List totals without downloading
- 下載前檢查本機剩餘空間 / Checks local free space before downloading
- 每個檔案下載後比對大小，可選擇用 `--verify` 比對 md5 / Size check for every file, optional md5 verification
- 無法備份的檔案會記錄在報告中，不會默默略過 / Entries that cannot be backed up are recorded, never silently skipped

## 需求 / Requirements
- [uv](https://docs.astral.sh/uv/)（會自動安裝所需的 Python 版本 / installs the required Python automatically）
- 以 USB 連接、已開機的 Flipper Zero / A Flipper Zero connected over USB and powered on
- 備份時請關閉 qFlipper，否則序列埠會被佔用 / Close qFlipper while backing up, it holds the serial port

## 安裝 / Setup
```sh
git clone git@github.com:vincent5753/flipper-backup.git
cd flipper-backup
uv sync
```

## 使用方式 / Usage
```sh
# 先看看要備份多少東西 / See how much there is to back up
uv run flipper-backup --scan-only

# 完整備份 /int 和 /ext / Full backup of /int and /ext
uv run flipper-backup

# 只備份 SD 卡，指定輸出目錄 / SD card only, custom output directory
uv run flipper-backup --only ext -o ~/backups/flipper
```

|參數 / Option|說明 / Description|
|-|-|
|`-p`, `--port`|序列埠，預設自動偵測 / Serial port, auto-detected by default|
|`-o`, `--output`|輸出目錄，預設為 `<專案>/flipper-backups/<時間戳記>` / Output directory, defaults to `<project>/flipper-backups/<timestamp>`|
|`--only {int,ext}`|只備份其中一個儲存區 / Back up only one storage|
|`--scan-only`|只掃描並列出總量 / Scan and print totals only|
|`--verify`|逐檔比對 md5，速度慢很多 / Compare md5 of every file, much slower|
|`--chunk-size`|每次讀取的大小，預設 8192 / Read chunk size, default 8192|
|`-v`, `--verbose`|列出每個下載的檔案 / Log every downloaded file|

預設輸出目錄是相對於專案位置，不是目前所在目錄，所以在哪裡執行都會放到同一個地方。若以一般 wheel 安裝（找不到專案目錄），則改用目前目錄並顯示警告。\
The default output directory is resolved relative to the project, not the current directory. With a regular wheel install, where the project cannot be found, it falls back to the current directory with a warning.

## 輸出 / Output
```
flipper-backups/20261008-144141/
├── int/                  # /int 的內容 / contents of /int
├── ext/                  # SD 卡的內容 / contents of the SD card
└── backup-report.json    # 檔案數、大小、略過與錯誤項目、耗時 / counts, skipped entries, errors, timing
```

> [!WARNING]
> `/int` 包含藍牙配對金鑰等裝置資料，請妥善保管備份。`flipper-backups/` 已列入 `.gitignore`。\
> `/int` contains device data such as Bluetooth pairing keys, keep backups private. `flipper-backups/` is already in `.gitignore`.

|Exit code|意義 / Meaning|
|:-:|-|
|0|全部完成 / Everything backed up|
|1|完成，但有略過或錯誤的項目，詳見報告 / Finished with skipped entries or errors, see the report|
|2|中途停止（找不到裝置、逾時、空間不足等）/ Aborted (no device, timeout, not enough space, ...)|

## 已知限制 / Known limitations
- 名稱含非 ASCII 字元（例如中文）的檔案或目錄無法備份。裝置的 CLI 會把無法表示的字元換成 `?`，而 `?` 不是合法的 FAT 檔名，所以無法用這個名稱讀取。這些項目會列在報告的 `skipped` 中。\
  Files and directories with non-ASCII names cannot be backed up. The device CLI replaces unrepresentable characters with `?`, which is not a valid FAT name, so the entry cannot be read. They are listed under `skipped` in the report.
- 文字 CLI 傳輸不快。實測一台裝置約 110 KiB/s，35 MiB、1456 個檔案約 5 分半。\
  The text CLI is not fast: one device measured about 110 KiB/s, 35 MiB in 1456 files took about 5.5 minutes.
- 目前只有備份，沒有還原功能。\
  Backup only, restore is not implemented.

## 開發 / Development
```sh
uv run pytest
```
測試使用模擬裝置 CLI 的假序列埠，不需要接上 Flipper。\
Tests use a fake serial port that emulates the device CLI, no Flipper needed.

## 授權 / License
[GPL-3.0-only](LICENSE)

`storage` 指令的處理方式參考了 [flipperzero-firmware](https://github.com/flipperdevices/flipperzero-firmware) 的 `scripts/flipper/storage.py` 與 `applications/services/storage/storage_cli.c`（GPLv3）。\
The `storage` command handling was written with reference to `scripts/flipper/storage.py` and `applications/services/storage/storage_cli.c` from [flipperzero-firmware](https://github.com/flipperdevices/flipperzero-firmware) (GPLv3).
