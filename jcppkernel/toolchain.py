"""NSYSU MATH208: Windows 上自動取得 GCC 工具鏈。

電腦教室每次關機整台重置，學生每堂課都要重裝環境。這個模組讓 kernel 在
Windows 首次執行時，自己把 winlibs 的 GCC 12.1 抓到使用者資料夾
（`%LOCALAPPDATA%\\jupyter-cpp-kernel`），之後就一直沿用。

找工具鏈的順序（`find_toolchain`）：

1. `JCPP_GXX` 環境變數 —— 指到 g++ 完整路徑，任何自動化出錯時的逃生口。
2. `shutil.which("g++")` —— 已經有現成的就用現成的，不重複下載。
3. 受管安裝 `<MANAGED_ROOT>\\mingw64\\bin\\g++.exe`。

非 Windows 只做第 1、2 步；找不到就維持原本的錯誤行為（不會自動下載）。

另外一個重點是 `subprocess_env()`：所有 g++ 呼叫與 master 執行都把工具鏈的
bin 目錄插到 PATH 最前面。Windows 上 Anaconda 會用自己那份較舊的
`libstdc++-6.dll` 蓋掉 MinGW 的，編出來的執行檔會載入失敗；PATH 前置就是
在修這個 DLL 解析順序。

CLI：`python -m jcppkernel.toolchain [--status|--install|--force]`
"""

import concurrent.futures
import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import zipfile

# winlibs GitHub release 是唯一官方來源。tag 與檔名寫死，確保每台機器拿到同一份。
GITHUB_ZIP_URL = (
    "https://github.com/brechtsanders/winlibs_mingw/releases/download/"
    "12.1.0-14.0.6-10.0.0-ucrt-r3/"
    "winlibs-x86_64-posix-seh-gcc-12.1.0-mingw-w64ucrt-10.0.0-r3.zip"
)
DEFAULT_ZIP_URL = GITHUB_ZIP_URL      # 舊名保留

# 依序嘗試；HEAD 失敗或大小不符就換下一個。
# TODO: 校內網對 GitHub 只有 < 1 MB/s，之後在最前面補上 Cloudflare R2 的公開 URL。
TOOLCHAIN_URLS = [
    GITHUB_ZIP_URL,
]

# 分段下載的連線數。校內網單連線太慢，多開幾條可以把頻寬吃滿。
DEFAULT_CONNECTIONS = 8
MAX_CONNECTIONS = 16
SEGMENT_RETRIES = 3
ZIP_NAME = "winlibs-x86_64-posix-seh-gcc-12.1.0-mingw-w64ucrt-10.0.0-r3.zip"
ZIP_SIZE = 208612121
# 2026-09-02 從上述 URL 下載一次後算出並釘死（GitHub release 沒有提供官方 digest）。
ZIP_SHA256 = "6b957b84f5432b500b72999e47082e1728acecd6733a15e5120a498c6c8a3aaa"
GCC_VERSION = "12.1.0"
GCC_VERSION_SHORT = "12.1"

# zip 解開後頂層就是 mingw64/
TOOLCHAIN_DIRNAME = "mingw64"

# 同一台機器上可能同時開好幾個 notebook，每個 kernel 都是獨立行程。
# 沒有鎖的話它們會同時寫同一個 .part：sha256 各自對自己的網路串流算，
# 兩邊都會通過，但磁碟上是交錯的垃圾。所以下載＋解壓整段要互斥。
LOCK_NAME = "install.lock"
LOCK_STALE_SECONDS = 30 * 60      # 鎖檔超過這麼久沒更新就視為前一個行程死掉了
LOCK_WAIT_SECONDS = 40 * 60       # 等待方的上限
LOCK_POLL_SECONDS = 2
LOCK_NOTICE_SECONDS = 15


# --- 進度回報 -------------------------------------------------------------
#
# progress 是一個吃 dict 的 callable，dict 長這樣：
#   {phase, done_bytes, total_bytes, elapsed_s, speed_bps, eta_s, message}
# phase 是 download / verify / extract / wait / done / info / error。
# extract 另外帶 done_items / total_items（以檔案數計）。
# 呼叫端自己決定怎麼呈現：終端機用 format_progress() 印一行，
# notebook 則畫成 <progress> 元件。

BAR_WIDTH = 20
LINE_WIDTH = 86          # 固定欄寬，讓終端機用 \r 原地更新時不會留下殘影
                         # （所有 phase 都補到同寬，跨階段切換也不留殘影）


def _display_width(text):
    """終端機顯示寬度：全形字算兩欄。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _pad(text, width=LINE_WIDTH):
    return text + " " * max(0, width - _display_width(text))


def _bar(frac):
    filled = int(round(max(0.0, min(1.0, frac)) * BAR_WIDTH))
    return "\u2588" * filled + "\u2591" * (BAR_WIDTH - filled)


def _fmt_clock(seconds):
    """秒數轉 m:ss / h:mm:ss；無法估計時回 --:--。"""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "--:--"
    if seconds <= 0 or seconds > 359999:
        return "--:--"
    minutes, sec = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return "%d:%02d:%02d" % (hours, minutes, sec)
    return "%d:%02d" % (minutes, sec)


def format_progress(event):
    """把 progress event 排成一行固定寬度的文字（給終端機／text-plain 備援）。"""
    phase = event.get("phase", "")
    message = event.get("message") or ""

    if phase == "download":
        total = event.get("total_bytes") or ZIP_SIZE
        done = event.get("done_bytes") or 0
        frac = (done / total) if total else 0.0
        line = ("GCC %s \u4e0b\u8f09\u4e2d %3d%%  %s  %5.1f/%5.1f MB  %5.1f MB/s  \u5269\u9918 %-7s"
                % (GCC_VERSION_SHORT, int(frac * 100), _bar(frac),
                   done / 1048576.0, total / 1048576.0,
                   (event.get("speed_bps") or 0) / 1048576.0,
                   _fmt_clock(event.get("eta_s"))))
    elif phase == "extract":
        total = event.get("total_items") or 0
        done = event.get("done_items") or 0
        frac = (done / total) if total else 0.0
        line = ("GCC %s \u89e3\u58d3\u4e2d %3d%%  %s  %5d/%5d \u6a94  \u5df2\u7528 %-7s"
                % (GCC_VERSION_SHORT, int(frac * 100), _bar(frac), done, total,
                   _fmt_clock(event.get("elapsed_s"))))
    elif phase == "verify":
        line = "GCC %s \u6821\u9a57\u4e2d       %s" % (GCC_VERSION_SHORT, message or "\u6bd4\u5c0d sha256\u2026")
    elif phase == "wait":
        line = "\u7b49\u5f85\u5176\u4ed6 kernel      %s\uff08\u5df2\u7b49 %s\uff09" % (
            message or "\u53e6\u4e00\u500b kernel \u6b63\u5728\u4e0b\u8f09", _fmt_clock(event.get("elapsed_s")))
    else:
        line = message

    return _pad(line)


class TerminalProgress:
    """終端機的進度呈現：TTY 用 \\r 原地更新，非 TTY（被重導）退回每 10% 一行。"""

    LIVE_PHASES = ("download", "extract", "wait")

    def __init__(self, stream=None, indent="  "):
        self.stream = stream if stream is not None else sys.stdout
        self.indent = indent
        try:
            self.isatty = bool(self.stream.isatty())
        except Exception:
            self.isatty = False
        self._line_open = False
        self._last_pct = {}

    def __call__(self, event):
        phase = event.get("phase", "")
        line = format_progress(event)

        if phase in self.LIVE_PHASES:
            if self.isatty:
                self.stream.write("\r" + self.indent + line)
                self.stream.flush()
                self._line_open = True
                return
            # 非 TTY：只在跨過 10% 時印一行，免得 log 被洗版
            pct = self._percent(event)
            last = self._last_pct.get(phase, -10)
            if pct < last + 10 and pct < 100:
                return
            self._last_pct[phase] = pct - (pct % 10)

        self._break_line()
        self.stream.write(self.indent + line.rstrip() + "\n")
        self.stream.flush()

    @staticmethod
    def _percent(event):
        total = event.get("total_items") or event.get("total_bytes") or 0
        done = event.get("done_items") or event.get("done_bytes") or 0
        return int(done * 100 / total) if total else 0

    def _break_line(self):
        if self._line_open:
            self.stream.write("\n")
            self._line_open = False

    def close(self):
        self._break_line()
        self.stream.flush()


def _emit(progress, phase, done=0, total=0, started=None, message="", **extra):
    """組出 event 並送給 progress。UI 出錯不該讓安裝失敗，所以整段包起來。"""
    if not progress:
        return
    elapsed = (time.monotonic() - started) if started else 0.0
    speed = (done / elapsed) if (elapsed > 0 and done) else 0.0
    eta = ((total - done) / speed) if (speed > 0 and total > done) else 0.0
    event = {
        "phase": phase,
        "done_bytes": done,
        "total_bytes": total,
        "elapsed_s": elapsed,
        "speed_bps": speed,
        "eta_s": eta,
        "message": message,
    }
    event.update(extra)
    try:
        progress(event)
    except Exception:
        pass


class ToolchainError(RuntimeError):
    """工具鏈取得失敗；訊息會直接顯示給學生，請寫成看得懂的中文。"""


class Toolchain:
    __slots__ = ("gxx", "bin_dir", "source")

    def __init__(self, gxx, bin_dir, source):
        self.gxx = gxx
        self.bin_dir = bin_dir
        self.source = source      # "JCPP_GXX" / "PATH" / "managed"

    def __repr__(self):
        return "Toolchain(gxx=%r, source=%r)" % (self.gxx, self.source)


# --- 路徑 -----------------------------------------------------------------

def zip_url():
    """第一順位的下載網址（保留舊介面）。"""
    return zip_urls()[0]


def zip_urls():
    """要依序嘗試的下載來源。JCPP_TOOLCHAIN_URL 可覆寫成單一 URL。"""
    override = os.environ.get("JCPP_TOOLCHAIN_URL")
    if override:
        return [override]
    return list(TOOLCHAIN_URLS)


def _connection_count():
    try:
        n = int(os.environ.get("JCPP_DOWNLOAD_CONNECTIONS", DEFAULT_CONNECTIONS))
    except ValueError:
        n = DEFAULT_CONNECTIONS
    return max(1, min(MAX_CONNECTIONS, n))


def managed_root():
    """受管安裝的根目錄；回傳 None 表示這台機器不做自動下載。

    JCPP_MANAGED_ROOT 主要給測試用（可在 Linux 上驗證下載／校驗／解壓）。
    """
    override = os.environ.get("JCPP_MANAGED_ROOT")
    if override:
        return os.path.abspath(override)
    if os.name != "nt":
        return None
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return os.path.join(base, "jupyter-cpp-kernel")


# 受管安裝一律是 winlibs 的 Windows 工具鏈，所以檔名永遠是 g++.exe
# （即使在 Linux 上跑測試時也一樣）。
MANAGED_GXX_NAME = "g++.exe"

# Windows 上常見的手動安裝位置。學生照教材 00B PART 04 裝過 MSYS2／WinLibs，
# 但如果沒把 bin 加進 PATH，which("g++") 就找不到 —— 與其重抓 200 MB，
# 不如先來這裡看一眼。受管目錄放最後（前面都沒有才用自己下載的那份）。
COMMON_BIN_DIR_TEMPLATES = [
    r"C:\mingw64\bin",
    r"C:\msys64\ucrt64\bin",
    r"C:\msys64\mingw64\bin",
    r"C:\w64devkit\bin",
    r"C:\TDM-GCC-64\bin",
    r"%LOCALAPPDATA%\Programs\mingw64\bin",
    r"%USERPROFILE%\mingw64\bin",
    r"%LOCALAPPDATA%\jupyter-cpp-kernel\mingw64\bin",
]


def managed_bin_dir():
    root = managed_root()
    return os.path.join(root, TOOLCHAIN_DIRNAME, "bin") if root else None


# --- 尋找 -----------------------------------------------------------------

def _expand_env(text):
    """展開 %VAR%。os.path.expandvars 只在 Windows 認 %VAR%，這裡自己來，方便測試。"""
    return re.sub(r"%([^%]+)%",
                  lambda m: os.environ.get(m.group(1)) or m.group(0), text)


def common_bin_dirs():
    """常見安裝位置，已展開環境變數且去掉重複。"""
    seen, result = set(), []
    for template in COMMON_BIN_DIR_TEMPLATES:
        path = _expand_env(template)
        if "%" in path:          # 變數展不開（例如在 Linux 上），跳過
            continue
        key = os.path.normcase(os.path.normpath(path))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def registry_path_dirs():
    r"""讀 HKCU\Environment\Path。

    使用者剛裝好編譯器、PATH 已經寫進登錄檔，但目前這個行程是從舊環境繼承的，
    os.environ["PATH"] 還看不到 —— 這時直接讀登錄檔就能找到。
    """
    if os.name != "nt":
        return []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, "Path")
    except Exception:
        return []
    dirs = []
    for entry in str(value).split(os.pathsep):
        entry = _expand_env(entry.strip())
        if entry and "%" not in entry:
            dirs.append(entry)
    return dirs


def _probe_bin_dirs(dirs, source):
    managed = managed_bin_dir()
    managed_key = os.path.normcase(os.path.normpath(managed)) if managed else None
    for bin_dir in dirs:
        candidate = os.path.join(bin_dir, MANAGED_GXX_NAME)
        if os.path.isfile(candidate):
            key = os.path.normcase(os.path.normpath(bin_dir))
            label = "managed" if managed_key and key == managed_key else source
            return Toolchain(candidate, bin_dir, label)
    return None


def find_toolchain():
    """依序找 JCPP_GXX → PATH → 常見安裝位置 → 使用者登錄檔 PATH；都沒有回傳 None。"""
    override = os.environ.get("JCPP_GXX")
    if override and os.path.isfile(override):
        return Toolchain(override, os.path.dirname(override), "JCPP_GXX")

    found = shutil.which("g++")
    if found:
        return Toolchain(found, os.path.dirname(found), "PATH")

    # 常見手動安裝位置（受管目錄也在清單最後）
    hit = _probe_bin_dirs(common_bin_dirs(), "common")
    if hit:
        return hit

    # 登錄檔裡的使用者 PATH：剛裝好但這個行程還沒繼承到
    hit = _probe_bin_dirs(registry_path_dirs(), "registry")
    if hit:
        return hit

    # 保險：managed_root 被 JCPP_MANAGED_ROOT 覆寫時不在上面的清單裡
    bin_dir = managed_bin_dir()
    if bin_dir:
        candidate = os.path.join(bin_dir, MANAGED_GXX_NAME)
        if os.path.isfile(candidate):
            return Toolchain(candidate, bin_dir, "managed")

    return None


def subprocess_env(bin_dir):
    """os.environ 的複本，把 bin_dir 插到 PATH 最前面。

    Windows 上這是 DLL 解析順序的關鍵（見模組說明）；其他平台無害。
    """
    env = os.environ.copy()
    if bin_dir:
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return env


# --- 使用者層 PATH（僅 Windows） -------------------------------------------

def add_to_user_path(bin_dir):
    """把 bin_dir 前置寫進 HKCU\\Environment\\Path，讓 VS Code／終端機也找得到。

    刻意不用 setx：setx 會在 1024 字元處截斷使用者 PATH，是有名的資料損毀來源。
    冪等 —— 已經在裡面就不動。回傳是否真的寫入。
    """
    if os.name != "nt":
        return False
    import winreg     # noqa: PLC0415 — 只有 Windows 有

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, kind = "", winreg.REG_EXPAND_SZ
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            kind = winreg.REG_EXPAND_SZ

        entries = [p for p in current.split(os.pathsep) if p.strip()]
        if any(os.path.normcase(os.path.normpath(p)) ==
               os.path.normcase(os.path.normpath(bin_dir)) for p in entries):
            return False

        new_value = os.pathsep.join([bin_dir] + entries)
        # 一律寫成 REG_EXPAND_SZ，保住其他程式放在 PATH 裡的 %VAR% 展開
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_value)

    _broadcast_setting_change()
    return True


def _broadcast_setting_change():
    """廣播 WM_SETTINGCHANGE，讓之後新開的行程讀到新的 PATH。"""
    try:
        import ctypes
        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x1A, 0x0002
        result = ctypes.c_long()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0,
            ctypes.c_wchar_p("Environment"), SMTO_ABORTIFHUNG, 5000,
            ctypes.byref(result),
        )
    except Exception:
        pass      # 廣播失敗只是要重開終端機，不該讓安裝失敗


# --- 下載與安裝 ------------------------------------------------------------

def _pid_alive(pid):
    """這個 pid 還活著嗎？判斷鎖檔是不是前一個當掉的行程留下的。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return True      # 查不出來就當它還活著，交給 mtime 逾時處理
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 別人的行程，但確實存在
    except OSError:
        return True
    return True


def _lock_is_stale(lock_path):
    """鎖檔是不是已經沒有主人了（行程不在，或放太久）。"""
    try:
        age = time.time() - os.path.getmtime(lock_path)
    except OSError:
        return False         # 剛好被別人刪掉了，不算 stale，重跑迴圈即可
    if age > LOCK_STALE_SECONDS:
        return True
    try:
        with open(lock_path, "r", encoding="utf-8") as fh:
            pid = int((fh.readline() or "0").strip() or 0)
    except (OSError, ValueError):
        return False         # 可能正在被寫入，下一輪再看
    return not _pid_alive(pid)


def _try_acquire_lock(lock_path):
    """O_EXCL 建檔即取得鎖；拿不到回傳 False。"""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, ("%d\n%f\n" % (os.getpid(), time.time())).encode("ascii"))
    finally:
        os.close(fd)
    return True


def _probe(url):
    """HEAD 取得檔案大小與是否支援 Range。回傳 (total, accepts_ranges, final_url)。"""
    import urllib.request

    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        accepts = "bytes" in (resp.headers.get("Accept-Ranges") or "").lower()
        final = resp.geturl() or url
    return total, accepts, final


def _range_ok(url):
    """真的送一次 Range 請求確認伺服器回 206（有些伺服器 HEAD 說支援其實不支援）。"""
    import urllib.request

    req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return getattr(resp, "status", resp.getcode()) == 206
    except Exception:
        return False


def _download_single(url, part, progress, started):
    """單連線下載，也是分段下載失敗時的退路。"""
    import urllib.request

    written = 0
    last_report = 0.0
    last_pct = -1
    with urllib.request.urlopen(url, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or ZIP_SIZE)
        _emit(progress, "download", 0, total, started)
        with open(part, "wb") as fh:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
                now = time.monotonic()
                pct = int(written * 100 / total) if total else 0
                if now - last_report >= 0.5 or pct > last_pct:
                    last_report, last_pct = now, pct
                    _emit(progress, "download", written, total, started)
    _emit(progress, "download", written, total, started)
    return written


def _download_segmented(url, part, total, connections, progress, started):
    """把檔案切成等份，多條連線各抓一段寫進同一個檔的對應 offset。

    每個執行緒自己 open 一個 handle 用 seek+write，不共用 file object。
    任一段重試 3 次仍失敗就往外拋，由呼叫端退回單連線。
    """
    # 先把 .part 撐到全長，這樣各段 seek 到自己的 offset 就能直接寫
    with open(part, "wb") as fh:
        fh.truncate(total)

    span = total // connections
    segments = []
    for i in range(connections):
        begin = i * span
        finish = total - 1 if i == connections - 1 else (i + 1) * span - 1
        segments.append((begin, finish))

    done = 0
    lock = threading.Lock()

    def fetch(begin, finish):
        import urllib.request

        nonlocal done
        expected = finish - begin + 1
        last_error = None
        for attempt in range(SEGMENT_RETRIES):
            got = 0
            try:
                req = urllib.request.Request(
                    url, headers={"Range": "bytes=%d-%d" % (begin, finish)})
                # 重新 urlopen 會重新跟隨 302，換到新的 CDN 節點
                with urllib.request.urlopen(req, timeout=120) as resp:
                    if getattr(resp, "status", resp.getcode()) != 206:
                        raise IOError("伺服器沒有回應 206 Partial Content")
                    with open(part, "r+b") as fh:
                        fh.seek(begin)
                        while True:
                            chunk = resp.read(1024 * 256)
                            if not chunk:
                                break
                            fh.write(chunk)
                            got += len(chunk)
                            with lock:
                                done += len(chunk)
                if got != expected:
                    raise IOError("段長度不符：拿到 %d，預期 %d" % (got, expected))
                return
            except Exception as exc:
                last_error = exc
                with lock:
                    done -= got          # 這一輪白做了，把計數扣回去
                if attempt < SEGMENT_RETRIES - 1:
                    time.sleep(1.0 + attempt)
        raise IOError("分段 %d-%d 重試 %d 次仍失敗：%s"
                      % (begin, finish, SEGMENT_RETRIES, last_error))

    _emit(progress, "download", 0, total, started)
    reported = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=connections) as pool:
        futures = [pool.submit(fetch, a, b) for a, b in segments]
        while not all(f.done() for f in futures):
            time.sleep(0.4)
            with lock:
                current = done
            # 重試會讓計數往回跳，但對外的進度必須單調
            reported = max(reported, current)
            _emit(progress, "download", reported, total, started)
        for f in futures:
            f.result()               # 有例外就在這裡拋出

    _emit(progress, "download", total, total, started)
    return total


def _sha256_file(path):
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def _download(url, dest, progress):
    """從單一 URL 下載並校驗。大小不符或 HEAD 失敗會拋例外，由呼叫端換下一個來源。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    started = time.monotonic()
    connections = _connection_count()

    total, accepts, final_url = _probe(url)
    if total and total != ZIP_SIZE:
        raise ToolchainError(
            "來源檔案大小不對（%d bytes，預期 %d），換下一個來源。" % (total, ZIP_SIZE))

    segmented = False
    if connections > 1 and total and accepts and _range_ok(final_url):
        try:
            _download_segmented(final_url, part, total, connections, progress, started)
            segmented = True
        except Exception as exc:
            _emit(progress, "info",
                  message="分段下載失敗（%s），改用單連線重試。" % exc)
    if not segmented:
        _download_single(final_url, part, progress, started)

    _emit(progress, "verify", os.path.getsize(part), total or ZIP_SIZE, started,
          message="比對檔案大小與 sha256…")

    written = os.path.getsize(part)
    if written != ZIP_SIZE:
        os.remove(part)
        raise ToolchainError(
            "下載的檔案大小不對（拿到 %d bytes，預期 %d）。"
            "可能是網路中斷或被代理伺服器攔截，請重試。" % (written, ZIP_SIZE)
        )
    digest = _sha256_file(part)
    if ZIP_SHA256 and not ZIP_SHA256.startswith("__") and digest != ZIP_SHA256:
        os.remove(part)
        raise ToolchainError(
            "下載的檔案 sha256 不符（拿到 %s，預期 %s）。"
            "請重試；若持續失敗請改用手動安裝。" % (digest, ZIP_SHA256)
        )
    os.replace(part, dest)
    return digest


def _download_with_mirrors(dest, progress):
    """依序嘗試每個來源，全部失敗才放棄。"""
    failures = []
    urls = zip_urls()
    for index, url in enumerate(urls, 1):
        if index > 1:
            _emit(progress, "info", message="換下一個下載來源（第 %d 個）…" % index)
        try:
            return _download(url, dest, progress)
        except Exception as exc:
            failures.append("  %s\n    -> %s" % (url, exc))
    raise ToolchainError("所有下載來源都失敗：\n" + "\n".join(failures))


def _extract(zip_path, root, progress):
    """解壓到 tmp-<pid> 再原子改名，半途失敗不會留下壞掉的 mingw64。"""
    tmp_dir = os.path.join(root, "tmp-%d" % os.getpid())
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)

    started = time.monotonic()
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        total = len(members)
        last_report = 0.0
        last_pct = -1
        _emit(progress, "extract", started=started, done_items=0, total_items=total)
        for index, member in enumerate(members, 1):
            zf.extract(member, tmp_dir)
            now = time.monotonic()
            pct = int(index * 100 / total) if total else 0
            if now - last_report >= 0.5 or pct > last_pct:
                last_report, last_pct = now, pct
                _emit(progress, "extract", started=started,
                      done_items=index, total_items=total)
        _emit(progress, "extract", started=started,
              done_items=total, total_items=total)

    staged = os.path.join(tmp_dir, TOOLCHAIN_DIRNAME)
    if not os.path.isdir(staged):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ToolchainError("壓縮檔內容和預期不符：找不到頂層的 %s/ 目錄。"
                             % TOOLCHAIN_DIRNAME)

    final = os.path.join(root, TOOLCHAIN_DIRNAME)
    if os.path.exists(final):
        trash = os.path.join(root, "old-%d" % os.getpid())
        os.replace(final, trash)
        shutil.rmtree(trash, ignore_errors=True)
    os.replace(staged, final)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return final


def _download_and_extract(root, progress):
    """實際的下載→校驗→解壓→原子改名。呼叫端必須已經持有鎖。"""
    zip_path = os.path.join(root, "download", ZIP_NAME)

    if os.path.isfile(zip_path) and os.path.getsize(zip_path) == ZIP_SIZE:
        _emit(progress, "info", message="已有下載好的壓縮檔，跳過下載。")
    else:
        _emit(progress, "info",
              message="第一次使用：正在下載 GCC %s（198 MB）到 %s，"
                      "約 2–5 分鐘，之後不會再出現。" % (GCC_VERSION, root))
        _download_with_mirrors(zip_path, progress)

    _extract(zip_path, root, progress)

    if not os.environ.get("JCPP_KEEP_ZIP"):
        try:
            os.remove(zip_path)
        except OSError:
            pass


def _install_under_lock(root, progress, allow_skip=True):
    """取得 install.lock 後安裝；拿不到鎖就等另一個 kernel 裝完。

    allow_skip=False（--force）表示就算已經裝好了也要重裝一次。
    """
    lock_path = os.path.join(root, LOCK_NAME)
    gxx = os.path.join(root, TOOLCHAIN_DIRNAME, "bin", MANAGED_GXX_NAME)
    deadline = time.time() + LOCK_WAIT_SECONDS
    wait_started = time.monotonic()
    last_notice = 0.0
    waited = False

    while True:
        # 先看是不是已經裝好了 —— 等待方醒來時鎖多半已經釋放，
        # 這個檢查要排在搶鎖之前，否則它會搶到鎖再重下載一次。
        if allow_skip and os.path.isfile(gxx) and not os.path.exists(lock_path):
            if waited:
                _emit(progress, "info", message="另一個 kernel 已完成安裝，直接使用。")
            return

        if _try_acquire_lock(lock_path):
            try:
                # 拿到鎖後再確認一次：可能在搶鎖的空檔對方剛裝完
                if allow_skip and os.path.isfile(gxx):
                    if waited:
                        _emit(progress, "info", message="另一個 kernel 已完成安裝，直接使用。")
                    return
                _download_and_extract(root, progress)
            finally:
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
            return

        # 鎖在別人手上。先確認那個「別人」還在。
        waited = True
        if _lock_is_stale(lock_path):
            _emit(progress, "info", message="發現前一次安裝殘留的鎖檔，清除後重試。")
            try:
                os.remove(lock_path)
            except OSError:
                pass
            continue

        if time.time() > deadline:
            raise ToolchainError(
                "等待另一個 kernel 完成安裝超過 %d 分鐘。\n"
                "如果確定沒有其他 kernel 正在下載，請手動刪除這個檔案後重試：\n"
                "  %s" % (LOCK_WAIT_SECONDS // 60, lock_path)
            )

        now = time.time()
        if now - last_notice >= LOCK_NOTICE_SECONDS:
            last_notice = now
            _emit(progress, "wait", started=wait_started,
                  message="另一個 kernel 正在下載")
        time.sleep(LOCK_POLL_SECONDS)


def ensure_toolchain(progress=None, force=False):
    """確保有可用的 g++；必要時下載安裝。回傳 Toolchain。

    progress 是一個吃字串的 callable，kernel 會把它接到 stderr 讓學生看到進度。
    """
    if not force:
        existing = find_toolchain()
        if existing:
            return existing

    root = managed_root()
    if not root:
        raise ToolchainError(
            "找不到 g++，而且這個平台不支援自動下載。\n"
            "請自行安裝 g++，或用 JCPP_GXX 環境變數指向 g++ 的完整路徑。"
        )

    os.makedirs(root, exist_ok=True)
    _install_under_lock(root, progress, allow_skip=not force)

    bin_dir = os.path.join(root, TOOLCHAIN_DIRNAME, "bin")
    gxx = os.path.join(bin_dir, MANAGED_GXX_NAME)
    if not os.path.isfile(gxx):
        raise ToolchainError("解壓完成但找不到 %s。" % gxx)

    if os.name == "nt":
        try:
            out = subprocess.run([gxx, "--version"], capture_output=True,
                                 text=True, timeout=60,
                                 env=subprocess_env(bin_dir))
        except Exception as exc:
            raise ToolchainError("無法執行剛安裝的 g++：%s" % exc)
        if out.returncode != 0 or GCC_VERSION not in (out.stdout or ""):
            raise ToolchainError(
                "剛安裝的 g++ 無法正常執行或版本不符。\n輸出：%s%s"
                % ((out.stdout or "")[:300], (out.stderr or "")[:300])
            )
        if add_to_user_path(bin_dir):
            _emit(progress, "info",
                  message="已把 %s 加入使用者 PATH（新開的終端機才會生效）。" % bin_dir)

    _emit(progress, "done", message="GCC %s 已安裝到 %s" % (GCC_VERSION, gxx))
    return Toolchain(gxx, bin_dir, "managed")


# --- CLI ------------------------------------------------------------------

_SOURCE_LABELS = {
    "JCPP_GXX": "JCPP_GXX 環境變數",
    "PATH": "系統 PATH 上的現成安裝",
    "common": "常見安裝位置",
    "registry": "使用者登錄檔 PATH",
    "managed": "kernel 自行下載的受管安裝",
}


def _describe(tc):
    if not tc:
        print("g++：找不到")
        return 1
    print("g++      ：%s" % tc.gxx)
    print("來源     ：%s" % _SOURCE_LABELS.get(tc.source, tc.source))
    print("bin 目錄 ：%s" % tc.bin_dir)
    try:
        out = subprocess.run([tc.gxx, "--version"], capture_output=True,
                             text=True, timeout=60, env=subprocess_env(tc.bin_dir))
        print("版本     ：%s" % (out.stdout or "").splitlines()[0])
    except Exception as exc:
        print("版本     ：無法取得（%s）" % exc)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    root = managed_root()
    print("受管路徑 ：%s" % (root or "（此平台不啟用自動下載）"))

    if "--force" in argv or "--install" in argv:
        reporter = TerminalProgress()
        try:
            tc = ensure_toolchain(progress=reporter, force="--force" in argv)
        except ToolchainError as exc:
            reporter.close()
            print("失敗：%s" % exc, file=sys.stderr)
            return 1
        reporter.close()
        return _describe(tc)

    return _describe(find_toolchain())


if __name__ == "__main__":
    sys.exit(main())
