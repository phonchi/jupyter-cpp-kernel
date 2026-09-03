#!/usr/bin/env python3
"""把 winlibs GCC 12.1.0 UCRT r3 的 zip 瘦身成課程用的精簡版（只保留 C/C++ 編譯、連結與 gdb）。

用法：python3 tools/build_trimmed_toolchain.py <winlibs-*.zip> <輸出.zip>
輸出仍以 mingw64/ 為頂層，可直接被 jcppkernel.toolchain 解壓使用。
移除：Fortran／Objective-C 前端與函式庫、LTO 除錯工具、libgccjit、doxygen、
gcc plugin 開發檔、libpython 靜態庫、gdb 內嵌 Python 的 test/ensurepip/idlelib、
locale／info／man 文件。保留所有 DLL、lib/gcc、x86_64-w64-mingw32 整包與標頭。
"""
import sys, zipfile, hashlib, os, fnmatch

DROP_EXACT = {
    "libexec/gcc/x86_64-w64-mingw32/12.1.0/f951.exe",
    "libexec/gcc/x86_64-w64-mingw32/12.1.0/lto1.exe",
    "libexec/gcc/x86_64-w64-mingw32/12.1.0/cc1obj.exe",
    "libexec/gcc/x86_64-w64-mingw32/12.1.0/cc1objplus.exe",
    "bin/gfortran.exe", "bin/x86_64-w64-mingw32-gfortran.exe",
    "bin/lto-dump.exe", "bin/libgccjit-0.dll", "bin/doxygen.exe", "bin/libgfortran-5.dll",
    "lib/libgfortran.a", "lib/libgfortran.spec", "lib/libgfortran.dll.a",
    "lib/libgccjit.dll.a", "lib/libpython3.9.a",
    "include/libgccjit.h", "include/libgccjit++.h",
    "lib/gcc/x86_64-w64-mingw32/12.1.0/include/ISO_Fortran_binding.h",
}
DROP_PREFIX = (
    "lib/gcc/x86_64-w64-mingw32/12.1.0/plugin/",
    "share/locale/", "share/info/", "share/man/",
    "lib/python3.9/test/", "lib/python3.9/ensurepip/", "lib/python3.9/idlelib/",
    "lib/python3.9/tkinter/", "lib/python3.9/turtledemo/",
)

def keep(rel):
    if rel in DROP_EXACT: return False
    if rel.startswith(DROP_PREFIX): return False
    if "/__pycache__/" in rel: return False
    return True

def main(src, dst):
    zin = zipfile.ZipFile(src)
    kept = dropped = 0; kept_raw = dropped_raw = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
        for info in zin.infolist():
            name = info.filename
            assert name.startswith("mingw64/"), name
            rel = name[len("mingw64/"):]
            if not keep(rel):
                dropped += 1; dropped_raw += info.file_size; continue
            data = zin.read(name)
            zi = zipfile.ZipInfo(name, date_time=info.date_time)
            zi.external_attr = info.external_attr; zi.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(zi, data)
            kept += 1; kept_raw += info.file_size
    h = hashlib.sha256(open(dst, "rb").read()).hexdigest()
    print(f"kept {kept} files ({kept_raw/1e6:.0f} MB raw), dropped {dropped} ({dropped_raw/1e6:.0f} MB raw)")
    print(f"output {os.path.getsize(dst)} bytes = {os.path.getsize(dst)/1e6:.1f} MB")
    print(f"sha256 {h}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
