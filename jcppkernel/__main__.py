from ipykernel.kernelbase import Kernel
from os import path, close as fsclose, name as ostype
from tempfile import mkstemp
from uuid import uuid4
import html as html_mod
import subprocess

from .realtime_subprocess import RealTimeSubprocess
from .code_processing import CPPCodeProcessingUnit
from .temp_file_processing import CPPTempFileProcessing
from .python_quiz_cells import is_quiz_cell, QuizPythonRuntime
from .toolchain import (
    find_toolchain, ensure_toolchain, subprocess_env, ToolchainError,
    format_progress, GCC_VERSION_SHORT,
)


class ToolchainProgressView:
    """在 notebook 裡用一個會原地更新的 <progress> 條顯示下載進度。

    第一次送 display_data 並帶上 display_id，之後都送 update_display_data
    更新同一個 display_id —— JupyterLab 4 原生支援，不需要 ipywidgets。
    """

    def __init__(self, kernel):
        self._kernel = kernel
        self._display_id = "jcpp-toolchain-" + uuid4().hex[:12]
        self._opened = False

    def __call__(self, event):
        self._render(self._html(event), format_progress(event).rstrip())

    def finish(self, message):
        self._render(self._box("\u2713 " + message, "#0b7285"), "\u2713 " + message)

    def fail(self, message):
        self._render(self._box("\u2717 " + message, "#c92a2a"), "\u2717 " + message)

    def _render(self, html, text):
        self._kernel._publish_display(
            {"text/html": html, "text/plain": text},
            self._display_id, update=self._opened,
        )
        self._opened = True

    @staticmethod
    def _box(text, color):
        return ("<div style=\"font-family:var(--jp-code-font-family,monospace);color:%s\">%s</div>" % (color, html_mod.escape(text)))

    def _html(self, event):
        phase = event.get("phase", "")
        if phase == "download":
            total = event.get("total_bytes") or 1
            done = event.get("done_bytes") or 0
            pct = int(done * 100 / total)
            head = "%s \u4e0b\u8f09\u4e2d" % (event.get("source_label")
                                          or "GCC %s" % GCC_VERSION_SHORT)
            detail = ("%d%% \u00b7 %.1f/%.1f MB \u00b7 %.1f MB/s \u00b7 \u5269\u9918 %s"
                      % (pct, done / 1048576.0, total / 1048576.0,
                         (event.get("speed_bps") or 0) / 1048576.0,
                         self._clock(event.get("eta_s"))))
        elif phase == "extract":
            total = event.get("total_items") or 1
            done = event.get("done_items") or 0
            pct = int(done * 100 / total)
            head = "GCC %s \u89e3\u58d3\u4e2d" % GCC_VERSION_SHORT
            detail = "%d%% \u00b7 %d/%d \u6a94" % (pct, done, total)
        elif phase == "wait":
            pct = None
            head = "\u53e6\u4e00\u500b kernel \u6b63\u5728\u4e0b\u8f09\uff0c\u7b49\u5f85\u4e2d\u2026"
            detail = "\u5df2\u7b49 %s" % self._clock(event.get("elapsed_s"))
        else:
            pct = None
            head = event.get("message") or ""
            detail = ""

        bar = (
            "<progress value=\"%d\" max=\"100\" style=\"width:100%%;height:1.1em\"></progress>"
            % pct if pct is not None else
            "<progress style=\"width:100%;height:1.1em\"></progress>"
        )
        return (
            "<div style=\"font-family:var(--jp-ui-font-family,sans-serif);max-width:44em\">"
            "<div style=\"margin-bottom:.25em\">%s</div>%s"
            "<div style=\"font-family:var(--jp-code-font-family,monospace);font-size:90%%;opacity:.8;margin-top:.2em\">%s</div></div>"
            % (html_mod.escape(head), bar, html_mod.escape(detail))
        )

    @staticmethod
    def _clock(seconds):
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return "--:--"
        if seconds <= 0 or seconds > 359999:
            return "--:--"
        minutes, sec = divmod(seconds, 60)
        return "%d:%02d" % (minutes, sec)

class CPPKernel(Kernel):
    implementation = "jupyter_cpp_kernel"
    implementation_version = "1.0"
    language = "C++"
    language_version = "C++"
    help_links = [
        {
            "text": "License",
            "url": "https://raw.githubusercontent.com/shiroinekotfs/jupyter-cpp-kernel/refs/heads/master/LICENSE",
        },
        {
            "text": "Notebook tutorial",
            "url": "https://github.com/shiroinekotfs/jupyter-cpp-kernel-doc",
        },
        {
            "text": "Reporting the issue",
            "url": "https://github.com/shiroinekotfs/jupyter-cpp-kernel/issues",
        },
    ]
    language_info = {
        "name": "C++",
        "version": "1.0.0a9.post2",
        "mimetype": "text/markdown",
        "file_extension": ".cpp",
    }

    def __init__(self, *args, **kwargs):
        super(CPPKernel, self).__init__(*args, **kwargs)
        self._allow_stdin = True
        self.files = []

        # NSYSU MATH208: 課程講義穿插 jupyterquiz / jupytercards 的 Python 測驗 cell，
        # 這個 runtime 讓它們在 C++ kernel 裡也能執行（見 python_quiz_cells.py）。
        self._py_runtime = QuizPythonRuntime(
            self._publish_display_data, self._write_to_stderr, self._write_to_stdout_raw
        )

        if ostype == "nt":
            self._end_line_sys = "\r\n"
        else:
            self._end_line_sys = "\n"

        self.resDir = path.join(path.dirname(path.realpath(__file__)), "resources")

        # NSYSU MATH208: 工具鏈可能還不存在（Windows 教室機每次重置）。
        # 這裡只「找」，不下載 —— __init__ 沒有輸出管道，在這裡卡幾分鐘
        # Jupyter 會以為 kernel 啟動失敗。真正的下載延到第一個 C++ cell。
        self.toolchain = find_toolchain()
        self.master_path = None
        if self.toolchain:
            self._build_master()

    def _build_master(self):
        """用目前的工具鏈編出 master 執行檔（載入並執行每個 cell 編出的 .so）。"""
        master_temp = mkstemp(suffix=".out")
        fsclose(master_temp[0])
        self.master_path = master_temp[1]
        self.files.append(self.master_path)
        filepath = path.join(self.resDir, "master.cpp")
        rc = subprocess.call(
            [
                self.toolchain.gxx,
                filepath,
                f"-std={self.standard}",
                "-Wno-unused-but-set-variable",
                "-Wno-unused-parameter",
                "-Wno-unused-variable",
                "-ldl",
                "-w",
                "-o",
                self.master_path,
            ],
            env=subprocess_env(self.toolchain.bin_dir),
        )
        if rc != 0:
            self.master_path = None
        return rc == 0

    _TOOLCHAIN_HELP = (
        "\n[C++ kernel] 取得 C++ 編譯器失敗。\n"
        "  可以改用手動安裝（教材 00B 的 PART 04 有完整步驟），\n"
        "  或是已經有 g++ 的話，設定環境變數 JCPP_GXX 指向 g++ 的完整路徑，\n"
        "  例如 JCPP_GXX=C:\\msys64\\ucrt64\\bin\\g++.exe，然後重啟 kernel。\n"
    )

    def _ensure_ready(self):
        """確保 master 執行檔就緒；Windows 首次使用會在這裡下載工具鏈。"""
        if self.master_path:
            return True

        if not self.toolchain:
            view = ToolchainProgressView(self)
            try:
                self.toolchain = ensure_toolchain(progress=view)
            except ToolchainError as exc:
                view.fail(str(exc).splitlines()[0])
                self._write_to_stderr("\n[C++ kernel] " + str(exc) + "\n" + self._TOOLCHAIN_HELP)
                return False
            except Exception as exc:   # 網路等意外，kernel 不該掛掉
                view.fail("下載編譯器時發生非預期錯誤：%r" % (exc,))
                self._write_to_stderr(
                    "\n[C++ kernel] 下載編譯器時發生非預期錯誤：%r\n" % (exc,) + self._TOOLCHAIN_HELP
                )
                return False
            view.finish("GCC %s 已安裝到 %s" % (GCC_VERSION_SHORT, self.toolchain.gxx))

        if not self._build_master():
            self._write_to_stderr(
                "\n[C++ kernel] 編譯器可用（%s），但 master 執行檔編譯失敗。\n"
                % self.toolchain.gxx + self._TOOLCHAIN_HELP
            )
            return False
        return True

    @property
    def banner(self):
        return (
            f"C++ kernel (Standard: {self.standard}) for Jupyter (master), version 1.0.0a9.post2\n\n"
            "Copyright (C) Brendan Rius\n"
            "Copyright (C) Shiroi Neko\n"
            "Copyright (C) Vo Luu Tuong Anh\n\n"
            "Project Main Page: https://github.com/shiroinekotfs/jupyter-cpp-kernel\n"
            "Track Project Status: https://github.com/users/shiroinekotfs/projects/1\n"
            "Reporting the issue: https://github.com/shiroinekotfs/jupyter-cpp-kernel/issues\n"
            "Legal information: https://github.com/shiroinekotfs/jupyter-cpp-kernel/blob/master/LICENSE\n\n"
            "Notebook tutorial: https://github.com/shiroinekotfs/jupyter-cpp-kernel-doc"
        )

    def _write_to_stdout(self, contents):
        # NSYSU MATH208: 上游把每個換行加倍後以 text/markdown 送出，結果每一行都變成
        # 一個段落（看起來多一個空行），而且 * _ < 等字元會被 markdown 吃掉。
        # 改成標準 stream 純文字輸出：換行一比一、內容原樣呈現；Windows 的 \r\n 統一成 \n。
        contents = contents.replace("\r\n", "\n")
        self.send_response(
            self.iopub_socket, "stream", {"name": "stdout", "text": contents}
        )

    def _write_to_stdout_raw(self, contents):
        self.send_response(
            self.iopub_socket, "stream", {"name": "stdout", "text": contents}
        )

    def _publish_display_data(self, bundle):
        self.send_response(
            self.iopub_socket, "display_data", {"data": bundle, "metadata": {}}
        )

    def _publish_display(self, bundle, display_id, update=False):
        """帶 display_id 的輸出；update=True 就地更新同一塊，用來做進度條。"""
        self.send_response(
            self.iopub_socket,
            "update_display_data" if update else "display_data",
            {"data": bundle, "metadata": {}, "transient": {"display_id": display_id}},
        )

    def _write_to_stderr(self, contents):
        self.send_response(
            self.iopub_socket, "stream", {"name": "stderr", "text": contents}
        )

    def _read_from_stdin(self):
        return self.raw_input()

    def _create_jupyter_subprocess(self, cmd):
        bin_dir = self.toolchain.bin_dir if self.toolchain else None
        return RealTimeSubprocess(
            cmd, self._write_to_stdout, self._write_to_stderr, self._read_from_stdin,
            env=subprocess_env(bin_dir),
        )

    def _compile_with_gpp(self, source_filename, binary_filename):
        return self._create_jupyter_subprocess(
            [
                self.toolchain.gxx,
                source_filename,
                # NSYSU MATH208: 讓 cell 裡的 #include "dscpp/xxx.hpp" 能以
                # 「編譯當下的工作目錄」為基準解析。kernel 會把 cell 寫進暫存檔再編譯，
                # 沒有這一行的話，帶引號的相對 include 只會去暫存目錄找，一定失敗。
                "-I.",
                # NSYSU MATH208: 課程標頭（pythonds3/cppds/*.hpp）直接內建在 kernel 套件裡，
                # 學生只要下載 .ipynb 就能 #include "pythonds3/cppds/stack.hpp"，不必另外 clone。
                # -I. 放前面，工作目錄若有自己的 pythonds3/ 會優先。
                "-I" + self.resDir,
                "-pedantic",
                "-fPIC",
                f"-std={self.standard}",
                "-w",
                "-shared",
                "-Wno-unused-but-set-variable",
                "-Wno-unused-parameter",
                "-Wno-unused-variable",
                "-lm",
                "-Wall",
                "-DBUFFERED_OUTPUT",
                "-o",
                binary_filename,
            ]
        )

    def do_execute(
        self, code, silent, store_history=True, user_expressions=None, allow_stdin=True
    ):
        # NSYSU MATH208: Python 測驗 cell 走另一條路，不進 g++。
        # is_quiz_cell() 是保守白名單，任何不確定的 cell 都會落回下面的 C++ 流程。
        if is_quiz_cell(code):
            reply = self._py_runtime.execute(code, silent)
            reply["execution_count"] = self.execution_count
            return reply

        # NSYSU MATH208: 以下都需要編譯器。Python 測驗 cell 已在上面處理完，
        # 不會被首次下載擋住。
        if not self._ensure_ready():
            return {
                "status": "error",
                "ename": "ToolchainError",
                "evalue": "C++ 編譯器不可用",
                "traceback": [],
                "execution_count": self.execution_count,
            }

        cpp_res_path = f'"{self.resDir}/gcpph.hpp"'
        code = CPPCodeProcessingUnit()._add_code_compat(code, cpp_res_path)
        
        with CPPTempFileProcessing._new_temp_file(
            CPPTempFileProcessing, self.files, suffix=".cpp"
        ) as source_file, CPPTempFileProcessing._new_temp_file(
            CPPTempFileProcessing, self.files, suffix=".out"
        ) as binary_file:
            source_file.write(code)
            source_file.flush()

            p = self._compile_with_gpp(source_file.name, binary_file.name)
            while p.poll() is None:
                p.write_contents()
            p.write_contents()

            if p.returncode != 0:
                self._write_to_stderr(
                    f"\n[C++ kernel] Error: Unable to compile the source code. Return error: {hex(p.returncode)}."
                )
                return {
                    "status": "ok",
                    "execution_count": self.execution_count,
                    "payload": [],
                    "user_expressions": {},
                }

        p = self._create_jupyter_subprocess([self.master_path, binary_file.name])
        while p.poll() is None:
            p.write_contents()

        p._stdout_thread.join()
        p._stderr_thread.join()
        p.write_contents()

        if p.returncode != 0:
            self._write_to_stderr(
                f"\n[C++ kernel] Error: Executable exited with code {hex(p.returncode)}."
            )

        return {
            "status": "ok",
            "execution_count": self.execution_count,
            "payload": [],
            "user_expressions": {},
        }

    def do_shutdown(self, restart):
        CPPTempFileProcessing._cleanup_files(CPPTempFileProcessing, self.master_path, self.files)
