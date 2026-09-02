"""NSYSU MATH208: 安裝 kernelspec（並在 Windows 順便備妥工具鏈）。

用法：`python -m jcppkernel.setup_cli`，或 pip 裝好後的 `jupyter-cpp-kernel-setup`。

為什麼需要這支：

* 原本 kernelspec 走 setup.py 的 `data_files`。`pip install --user` 時它會落到
  `%APPDATA%\\Python\\share\\jupyter\\kernels`，而 Jupyter **不會**搜這個路徑，
  結果就是裝完看不到 kernel。這裡改用 `KernelSpecManager.install_kernel_spec(user=True)`，
  一定寫到 Jupyter 真的會搜的 `%APPDATA%\\jupyter\\kernels`。
* kernel.json 的 argv[0] 原本寫死 `python3`，但 Windows 的 conda 通常只有
  `python.exe`。這裡一律換成 `sys.executable`，綁定到當下這個直譯器。

`data_files` 保留不動 —— conda 環境正常安裝時那條路本來就會成功。
"""

import json
import os
import shutil
import sys
import tempfile

# kernelspec 目錄名 → 套件目錄名
KERNELS = [
    ("cpp98", "jupyter-cpp-kernel-98"),
    ("cpp03", "jupyter-cpp-kernel-03"),
    ("cpp11", "jupyter-cpp-kernel-11"),
    ("cpp14", "jupyter-cpp-kernel-14"),
    ("cpp17", "jupyter-cpp-kernel-17"),
    ("cpp20", "jupyter-cpp-kernel-20"),
    ("cpp23", "jupyter-cpp-kernel-23"),
]

ASSETS = ("logo-32x32.png", "logo-64x64.png", "logo-svg.svg")


def _repo_root():
    """套件安裝後，jupyter-cpp-kernel-XX 會和 jcppkernel 並排在 site-packages。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def install_kernelspecs(prefix=None, user=True):
    from jupyter_client.kernelspec import KernelSpecManager

    ksm = KernelSpecManager()
    root = _repo_root()
    installed = []

    for kernel_name, pkg_dir in KERNELS:
        src = os.path.join(root, pkg_dir, "kernel_spec")
        spec_file = os.path.join(src, "kernel.json")
        if not os.path.isfile(spec_file):
            print("  跳過 %s：找不到 %s" % (kernel_name, spec_file))
            continue

        with open(spec_file, "r", encoding="utf-8") as fh:
            spec = json.load(fh)
        # argv[0] 換成當下的直譯器（Windows conda 常常沒有 python3.exe）
        spec["argv"][0] = sys.executable
        env = spec.get("env") or {}
        env.setdefault("PYTHONUTF8", "1")
        spec["env"] = env

        with tempfile.TemporaryDirectory() as staging:
            with open(os.path.join(staging, "kernel.json"), "w", encoding="utf-8") as fh:
                json.dump(spec, fh, indent=4, ensure_ascii=False)
            for asset in ASSETS:
                a = os.path.join(src, asset)
                if os.path.isfile(a):
                    shutil.copy2(a, staging)
            # 不傳 replace：新版 jupyter_client 一律覆蓋，傳了只會噴 DeprecationWarning
            dest = ksm.install_kernel_spec(
                staging, kernel_name=kernel_name, user=user, prefix=prefix
            )
        installed.append((kernel_name, dest))
        print("  已安裝 %-6s → %s" % (kernel_name, dest))

    return installed


def main(argv=None):
    print("=== jupyter-cpp-kernel 安裝 ===")
    print("Python   ：%s" % sys.executable)
    print()
    print("1) 安裝 kernelspec")
    try:
        installed = install_kernelspecs()
    except Exception as exc:
        print("安裝 kernelspec 失敗：%r" % (exc,), file=sys.stderr)
        return 1
    if not installed:
        print("沒有安裝任何 kernelspec。", file=sys.stderr)
        return 1

    print()
    print("2) C++ 編譯器")
    from .toolchain import (
        find_toolchain, ensure_toolchain, ToolchainError, managed_root, subprocess_env,
    )
    import subprocess

    tc = find_toolchain()
    if tc is None and managed_root():
        try:
            tc = ensure_toolchain(progress=lambda s: print("  " + s))
        except ToolchainError as exc:
            print("  取得編譯器失敗：%s" % exc, file=sys.stderr)
            tc = None

    print()
    print("=== 完成 ===")
    print("kernelspec：%s" % os.path.dirname(installed[0][1]))
    if tc:
        print("g++       ：%s（%s）" % (tc.gxx, {
            "JCPP_GXX": "JCPP_GXX 環境變數",
            "PATH": "系統上的現成安裝",
            "managed": "kernel 自行下載",
        }[tc.source]))
        try:
            out = subprocess.run([tc.gxx, "--version"], capture_output=True, text=True,
                                 timeout=60, env=subprocess_env(tc.bin_dir))
            print("版本      ：%s" % (out.stdout or "").splitlines()[0])
        except Exception:
            pass
        print()
        print("現在可以打開 JupyterLab，選 C++ 17 kernel。")
        if tc.source == "managed":
            print("（g++／gdb 也已加入使用者 PATH，新開的終端機或 VS Code 就能用。）")
    else:
        print("g++       ：找不到")
        print()
        print("請依教材 00B 的 PART 04 手動安裝 g++，")
        print("或設定環境變數 JCPP_GXX 指向 g++ 的完整路徑。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
