> ℹ️
> * For C/C++ header add-on development, [try this template](https://github.com/shiroinekotfs/jupyter-cpp-header-template)
> * [Security issue with Jupyter Notebooks](https://github.com/shiroinekotfs/jupyter-cpp-kernel/discussions/20)
> * [Track `jupyter-cpp-kernel` on PePY](https://www.pepy.tech/projects/jupyter-cpp-kernel)

[![CodeQL](https://github.com/shiroinekotfs/jupyter-cpp-kernel/actions/workflows/codeql.yml/badge.svg)](https://github.com/shiroinekotfs/jupyter-cpp-kernel/actions/workflows/codeql.yml)

![GitHub repo size](https://img.shields.io/github/repo-size/shiroinekotfs/jupyter-cpp-kernel)
![GitHub Repo stars](https://img.shields.io/github/stars/shiroinekotfs/jupyter-cpp-kernel)
[![Total Downloads](https://static.pepy.tech/badge/jupyter-cpp-kernel)](https://pepy.tech/project/jupyter-cpp-kernel)
[![Downloads](https://static.pepy.tech/badge/jupyter-cpp-kernel/month)](https://pepy.tech/project/jupyter-cpp-kernel)

# C++ (General) kernel for Jupyter

## Installation

> :warning:
>
> If you want to use it on Windows, please install the [GNU Compiler Collection for Windows](https://github.com/shiroinekotfs/jupyter-cpp-kernel/blob/master/INSTALL_ON_WINDOWS.md)

Normally, your target machine must meet these requirement packages before installing and using `jupyter-cpp-kernel`.

* `g++`
* `python3`, `python3-pip`
* `jupyter` (recommend `jupyterlab`)

### Install from PyPI

> :warning:
>
> For Windows User: Please follow [this instruction](https://github.com/shiroinekotfs/jupyter-cpp-kernel/blob/master/INSTALL_ON_WINDOWS.md) to install GCC properly.

```shell
pip install jupyter-cpp-kernel
```

### Install from the GitHub repo


> :warning:
>
> For Windows User: Please follow [this instruction](https://github.com/shiroinekotfs/jupyter-cpp-kernel/blob/master/INSTALL_ON_WINDOWS.md) to install GCC properly.

```shell
pip install git+https://github.com/shiroinekotfs/jupyter-cpp-kernel.git
```

## 課程 ArrayList 更新（NSYSU MATH208 fork）

`1.0.0a9.post1` 同步第三章使用的 `ArrayList`，提供 `push_back()`、
`capacity()`、const 索引及深層複製。內建標頭取自
[pythonds3 的 3b4a200](https://github.com/phonchi/pythonds3/blob/3b4a20038b327e424b1f21f07ea42b8d6cee3f2c/cppds/arraylist.hpp)。

已安裝課程版的使用者，請在啟動 Jupyter 的同一個 Python 環境執行：

```shell
python -m pip install --upgrade --no-cache-dir https://github.com/phonchi/jupyter-cpp-kernel/archive/refs/heads/nsysu-math208.zip
python -m jcppkernel.setup_cli
```

更新後重啟 C++ kernel。若 notebook 所在目錄另有 `pythonds3/`，編譯器會優先
使用那裡的標頭，也須確認該副本已更新。

維護者可用以下命令檢查第三章原始範例；需要 Python 與 g++：

```shell
python tools/check_arraylist.py
# 檢查安裝包內的標頭時，指定 jcppkernel/resources 的實際位置：
python tools/check_arraylist.py --include-root /path/to/jcppkernel/resources
```

## 工具鏈來源與重建方式（NSYSU MATH208 fork）

Windows 上如果找不到 g++（`JCPP_GXX` → PATH → 常見安裝位置 → 使用者登錄檔 PATH
都落空），kernel 會自己把工具鏈下載到 `%LOCALAPPDATA%\jupyter-cpp-kernel\mingw64`。
來源定義在 `jcppkernel/toolchain.py` 的 `TOOLCHAIN_SOURCES`，依序嘗試：

| 順位 | 內容 | 大小 | 說明 |
|---|---|---|---|
| 1 | [`mingw64-gcc-12.1.0-ucrt-r3-nsysu.zip`](https://github.com/phonchi/jupyter-cpp-kernel/releases/download/toolchain-12.1.0-ucrt-r3-nsysu1/mingw64-gcc-12.1.0-ucrt-r3-nsysu.zip) | 100,474,179 bytes | 課程精簡版，放在本 fork 的 Release |
| 2 | [`winlibs-...-r3.zip`](https://github.com/brechtsanders/winlibs_mingw/releases/download/12.1.0-14.0.6-10.0.0-ucrt-r3/winlibs-x86_64-posix-seh-gcc-12.1.0-mingw-w64ucrt-10.0.0-r3.zip) | 208,612,121 bytes | winlibs 官方原版，精簡版拿不到時的退路 |

兩份的頂層都是 `mingw64/`，解壓後都能通過 `g++ --version` 含 `12.1.0` 的驗證。
每個來源自帶 `size` 與 `sha256`（兩份內容不同，摘要當然不同），HEAD 大小或
下載後的摘要不符就自動換下一個來源。

### 重建精簡版

```shell
# 1) 抓 winlibs 原版
curl -L -o winlibs.zip \
  https://github.com/brechtsanders/winlibs_mingw/releases/download/12.1.0-14.0.6-10.0.0-ucrt-r3/winlibs-x86_64-posix-seh-gcc-12.1.0-mingw-w64ucrt-10.0.0-r3.zip

# 2) 瘦身（會印出產物的大小與 sha256）
python3 tools/build_trimmed_toolchain.py winlibs.zip mingw64-gcc-12.1.0-ucrt-r3-nsysu.zip

# 3) 上傳到 Release
gh release create toolchain-12.1.0-ucrt-r3-nsysu1 mingw64-gcc-12.1.0-ucrt-r3-nsysu.zip
```

精簡版移除 Fortran／Objective-C 前端與函式庫、LTO 除錯工具、libgccjit、doxygen、
gcc plugin 開發檔、gdb 內嵌 Python 的 test/ensurepip/idlelib，以及 locale／info／man
文件；**保留**所有 DLL、`lib/gcc`、`x86_64-w64-mingw32` 整包、標頭檔與 gdb。
解壓後約 413 MB（原版約 951 MB）。

### 換新版本時要改哪些常數

都在 `jcppkernel/toolchain.py`：

- `TOOLCHAIN_SOURCES` — 每個來源的 `url` / `size` / `sha256` / `label`
  （`label` 會顯示在下載進度條上）
- `TRIMMED_ZIP_URL`、`WINLIBS_ZIP_URL` — 兩個網址常數
- `GCC_VERSION`（如 `"12.1.0"`，用於解壓後的 `g++ --version` 驗證）與
  `GCC_VERSION_SHORT`（如 `"12.1"`，顯示用）
- `ZIP_SIZE` / `ZIP_SHA256` — 舊介面保留的別名，指向 winlibs 原版
- 若 gcc 大版號變了，`tools/build_trimmed_toolchain.py` 裡寫死 `12.1.0` 的路徑也要一起改

### 相關環境變數

| 變數 | 用途 |
|---|---|
| `JCPP_GXX` | 直接指定 g++ 完整路徑，跳過所有偵測（逃生口） |
| `JCPP_TOOLCHAIN_URL` | 覆寫成單一下載來源（例如校內鏡像） |
| `JCPP_TOOLCHAIN_SHA256` / `JCPP_TOOLCHAIN_SIZE` | 搭配上面使用；沒給 sha 就只驗大小並印警告 |
| `JCPP_DOWNLOAD_CONNECTIONS` | 分段下載的連線數，預設 8，設 1 就是單連線 |
| `JCPP_DOWNLOAD_CHUNK_MB` | 每個工作分塊的大小，預設 4（MB） |
| `JCPP_DOWNLOAD_STALL_S` | 連線多久沒收到資料就判定停滯並重抓，預設 10（秒） |
| `JCPP_MANAGED_ROOT` | 覆寫受管安裝根目錄（測試用） |
| `JCPP_KEEP_ZIP` | 安裝完保留壓縮檔 |

用 `python -m jcppkernel.toolchain --status` 可以看目前用的是哪一套 g++、來源與版本。

## Contributing

You can clone, create a fork, or import this repo whenever possible.

Please follow the GitHub standards and the license

## Guides (notebook)

<p align="center">
    <b>See more at </b><a href="https://github.com/shiroinekotfs/jupyter-cpp-kernel-doc">here</a>
    <br><br>
    <img src="https://github.com/shiroinekotfs/jupyter-cpp-kernel/assets/115929530/201d3f51-fa4c-44d4-bc2b-4ea2a252f13c" />
</p>
