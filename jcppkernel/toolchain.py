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

import hashlib
import os
import shutil
import subprocess
import sys
import time
import zipfile

# winlibs GitHub release 是唯一官方來源。tag 與檔名寫死，確保每台機器拿到同一份。
DEFAULT_ZIP_URL = (
    "https://github.com/brechtsanders/winlibs_mingw/releases/download/"
    "12.1.0-14.0.6-10.0.0-ucrt-r3/"
    "winlibs-x86_64-posix-seh-gcc-12.1.0-mingw-w64ucrt-10.0.0-r3.zip"
)
ZIP_NAME = "winlibs-x86_64-posix-seh-gcc-12.1.0-mingw-w64ucrt-10.0.0-r3.zip"
ZIP_SIZE = 208612121
# 2026-09-02 從上述 URL 下載一次後算出並釘死（GitHub release 沒有提供官方 digest）。
ZIP_SHA256 = "6b957b84f5432b500b72999e47082e1728acecd6733a15e5120a498c6c8a3aaa"
GCC_VERSION = "12.1.0"

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
    """允許老師用 JCPP_TOOLCHAIN_URL 指向校內鏡像（40 人同時抓 GitHub 會塞）。"""
    return os.environ.get("JCPP_TOOLCHAIN_URL") or DEFAULT_ZIP_URL


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


def managed_bin_dir():
    root = managed_root()
    return os.path.join(root, TOOLCHAIN_DIRNAME, "bin") if root else None


# --- 尋找 -----------------------------------------------------------------

def find_toolchain():
    """依序找 JCPP_GXX → PATH → 受管安裝；都沒有回傳 None。"""
    override = os.environ.get("JCPP_GXX")
    if override and os.path.isfile(override):
        return Toolchain(override, os.path.dirname(override), "JCPP_GXX")

    found = shutil.which("g++")
    if found:
        return Toolchain(found, os.path.dirname(found), "PATH")

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


def _download(url, dest, progress):
    import urllib.request

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    sha = hashlib.sha256()
    written = 0
    last_pct = 0

    with urllib.request.urlopen(url) as resp:
        total = int(resp.headers.get("Content-Length") or ZIP_SIZE)
        with open(part, "wb") as fh:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                sha.update(chunk)
                written += len(chunk)
                pct = int(written * 100 / total) if total else 0
                if progress and pct >= last_pct + 5:
                    last_pct = pct - (pct % 5)
                    progress("下載中… %d%%（%.0f/%.0f MB）"
                             % (pct, written / 1048576, total / 1048576))

    if written != ZIP_SIZE:
        os.remove(part)
        raise ToolchainError(
            "下載的檔案大小不對（拿到 %d bytes，預期 %d）。"
            "可能是網路中斷或被代理伺服器攔截，請重試。" % (written, ZIP_SIZE)
        )
    digest = sha.hexdigest()
    if ZIP_SHA256 and not ZIP_SHA256.startswith("__") and digest != ZIP_SHA256:
        os.remove(part)
        raise ToolchainError(
            "下載的檔案 sha256 不符（拿到 %s，預期 %s）。"
            "請重試；若持續失敗請改用手動安裝。" % (digest, ZIP_SHA256)
        )
    os.replace(part, dest)
    return digest


def _extract(zip_path, root, progress):
    """解壓到 tmp-<pid> 再原子改名，半途失敗不會留下壞掉的 mingw64。"""
    tmp_dir = os.path.join(root, "tmp-%d" % os.getpid())
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)

    if progress:
        progress("解壓中…（約 1 GB，需要一兩分鐘）")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(tmp_dir)

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
        if progress:
            progress("已有下載好的壓縮檔，跳過下載。")
    else:
        if progress:
            progress("第一次使用：正在下載 GCC %s（198 MB）到 %s，"
                     "約 2–5 分鐘，之後不會再出現。" % (GCC_VERSION, root))
        _download(zip_url(), zip_path, progress)

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
    last_notice = 0.0
    waited = False

    while True:
        # 先看是不是已經裝好了 —— 等待方醒來時鎖多半已經釋放，
        # 這個檢查要排在搶鎖之前，否則它會搶到鎖再重下載一次。
        if allow_skip and os.path.isfile(gxx) and not os.path.exists(lock_path):
            if progress and waited:
                progress("另一個 kernel 已完成安裝，直接使用。")
            return

        if _try_acquire_lock(lock_path):
            try:
                # 拿到鎖後再確認一次：可能在搶鎖的空檔對方剛裝完
                if allow_skip and os.path.isfile(gxx):
                    if progress and waited:
                        progress("另一個 kernel 已完成安裝，直接使用。")
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
            if progress:
                progress("發現前一次安裝殘留的鎖檔，清除後重試。")
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
        if progress and now - last_notice >= LOCK_NOTICE_SECONDS:
            last_notice = now
            progress("另一個 kernel 正在下載，等待中…")
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
        if add_to_user_path(bin_dir) and progress:
            progress("已把 %s 加入使用者 PATH（新開的終端機才會生效）。" % bin_dir)

    if progress:
        progress("GCC %s 安裝完成：%s" % (GCC_VERSION, gxx))
    return Toolchain(gxx, bin_dir, "managed")


# --- CLI ------------------------------------------------------------------

def _describe(tc):
    if not tc:
        print("g++：找不到")
        return 1
    print("g++      ：%s" % tc.gxx)
    print("來源     ：%s" % {"JCPP_GXX": "JCPP_GXX 環境變數",
                             "PATH": "系統 PATH 上的現成安裝",
                             "managed": "kernel 自行下載的受管安裝"}[tc.source])
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
        try:
            tc = ensure_toolchain(progress=lambda s: print("  " + s),
                                  force="--force" in argv)
        except ToolchainError as exc:
            print("失敗：%s" % exc, file=sys.stderr)
            return 1
        return _describe(tc)

    return _describe(find_toolchain())


if __name__ == "__main__":
    sys.exit(main())
