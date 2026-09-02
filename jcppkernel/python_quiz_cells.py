"""NSYSU MATH208: 讓 C++ kernel 也能跑 notebook 裡的 Python 測驗 cell。

課程講義（Slides/*.ipynb）用 jupyterquiz / jupytercards 穿插測驗與字卡，
但 notebook 的 kernel 是這支 C++ kernel，那些 cell 本來會被當成 C++ 送去 g++ 編譯而失敗。

這個模組做三件事：

1. `is_quiz_cell()` — 用非常保守的白名單規則判斷一個 cell 是不是「Python 測驗 cell」。
   只要有任何一行不在白名單內，就一律交還給 C++ 路徑，避免誤判毀掉正常的程式碼 cell。
2. `QuizPythonRuntime` — 在 kernel process 裡維持一個跨 cell 持久的 Python namespace，
   用 `exec` 執行這些 cell。
3. 把 jupyterquiz / jupytercards 模組層的 `IPython.display.display` 換掉，
   改成直接送 `display_data` IOPub 訊息；同時包一層 `display_quiz` / `display_flashcards`，
   讀本機 JSON 檔時強制 UTF-8（原始套件用不帶 encoding 的 open()，在繁中 Windows 會踩 cp950）。
"""

import io
import json
import os
import re
import sys
import traceback
from contextlib import redirect_stdout

# --- cell 辨識規則 -------------------------------------------------------

# 允許出現的行；任何一行不符合就不走 Python 路徑
_ALLOWED_LINE_PATTERNS = (
    re.compile(r"^#"),                                              # 註解（含被註解掉的 path=）
    re.compile(r"^(?:from|import)\s+(?:jupyterquiz|jupytercards)\b"),
    re.compile(r"""^[A-Za-z_]\w*\s*=\s*(?:"[^"\\]*"|'[^'\\]*')\s*$"""),  # path = "questions/ch1/"
    re.compile(r"^display_(?:quiz|flashcards)\s*\(.*\)\s*$"),
)

# 至少要有一行是這兩種之一，cell 才算數（避免純註解／`#include` 之類被誤判）
_ANCHOR_PATTERNS = (
    re.compile(r"^(?:from|import)\s+(?:jupyterquiz|jupytercards)\b"),
    re.compile(r"^display_(?:quiz|flashcards)\s*\(.*\)\s*$"),
)


def is_quiz_cell(code):
    """保守判斷：整個 cell 的每一行都在白名單內，且至少有一行是 import 或 display 呼叫。"""
    if not code or ("jupyterquiz" not in code and "jupytercards" not in code
                    and "display_quiz" not in code and "display_flashcards" not in code):
        return False

    has_anchor = False
    saw_line = False
    for raw in code.splitlines():
        line = raw.strip()
        if not line:
            continue
        saw_line = True
        if not any(p.match(line) for p in _ALLOWED_LINE_PATTERNS):
            return False
        if any(p.match(line) for p in _ANCHOR_PATTERNS):
            has_anchor = True
    return saw_line and has_anchor


# --- UTF-8 安全的檔案讀取（Windows 相容） --------------------------------

def _looks_like_local_file(ref):
    if not isinstance(ref, str):
        return False
    s = ref.strip()
    if not s or s.startswith("#") or s.startswith("["):
        return False          # DOM id / inline JSON，交給原本的邏輯
    if s.lower().startswith("http"):
        return False          # URL，交給原本的邏輯
    return os.path.exists(s)


def _preload_json(ref):
    """本機 JSON 檔一律用 UTF-8 讀成 list 再交給套件，繞過套件內不指定 encoding 的 open()。"""
    if not _looks_like_local_file(ref):
        return ref
    with open(ref, "r", encoding="utf-8") as f:
        return json.load(f)


def _wrap_utf8(func):
    def wrapper(ref, *args, **kwargs):
        return func(_preload_json(ref), *args, **kwargs)
    wrapper.__name__ = getattr(func, "__name__", "wrapper")
    wrapper.__doc__ = getattr(func, "__doc__", None)
    wrapper.__wrapped__ = func
    return wrapper


# --- runtime -------------------------------------------------------------

class QuizPythonRuntime:
    """跨 cell 持久的 Python namespace，並把 display 導向 kernel 的 IOPub。"""

    _PACKAGES = ("jupyterquiz", "jupytercards")

    def __init__(self, publish_display_data, write_stderr, write_stdout):
        self.ns = {"__name__": "__main__", "__builtins__": __builtins__}
        self._publish = publish_display_data
        self._stderr = write_stderr
        self._stdout = write_stdout
        self._patched = False
        self._silent = False

    # -- display 攔截 --

    def _display(self, *objs, **kwargs):
        for obj in objs:
            self._publish(self._to_bundle(obj))

    @staticmethod
    def _to_bundle(obj):
        """把 IPython.display 的物件轉成 mime bundle。

        用 _repr_*_ 而不是 isinstance，避免綁死 IPython 版本。
        Javascript 額外附一份 text/html <script>：JupyterLab 只會挑一種 mimetype
        來 render，所以多附一份是保險而不是重複執行。
        """
        if hasattr(obj, "_repr_javascript_"):
            js = obj._repr_javascript_()
            return {
                "application/javascript": js,
                "text/html": "<script type=\"text/javascript\">\n%s\n</script>" % js,
                "text/plain": "<IPython.core.display.Javascript object>",
            }
        if hasattr(obj, "_repr_html_"):
            html = obj._repr_html_()
            return {
                "text/html": html,
                "text/plain": "<IPython.core.display.HTML object>",
            }
        if isinstance(obj, str):
            return {"text/plain": obj}
        return {"text/plain": repr(obj)}

    # -- monkey patch --

    def _ensure_patched(self):
        if self._patched:
            return
        import jupyterquiz              # noqa: F401
        import jupyterquiz.dynamic      # noqa: F401
        import jupytercards             # noqa: F401
        import jupytercards.dynamic     # noqa: F401
        try:
            import jupyterquiz.dynamic.display   # noqa: F401
            import jupyterquiz.dynamic.capture   # noqa: F401
        except ImportError:
            pass  # 舊版 jupyterquiz 是單一 dynamic.py

        # 兩個套件都是 `from IPython.display import display`，綁在模組 namespace，
        # 所以必須逐一替換模組屬性，patch IPython.display.display 沒有用。
        for name, mod in list(sys.modules.items()):
            if not name.split(".")[0] in self._PACKAGES:
                continue
            if mod is not None and hasattr(mod, "display"):
                mod.display = self._display

        # 再包一層 UTF-8 讀檔；使用者可能從三種名字之一 import，全部換掉。
        for pkg, fname in (("jupyterquiz", "display_quiz"),
                           ("jupytercards", "display_flashcards")):
            for name, mod in list(sys.modules.items()):
                if name.split(".")[0] != pkg or mod is None:
                    continue
                func = getattr(mod, fname, None)
                if func is not None and not hasattr(func, "__wrapped__"):
                    setattr(mod, fname, _wrap_utf8(func))

        self._patched = True

    # -- 執行 --

    def execute(self, code, silent=False):
        """執行一個 Python 測驗 cell，回傳 do_execute 用的 reply dict。"""
        self._silent = silent
        try:
            self._ensure_patched()
        except ImportError as exc:
            self._stderr(
                "\n[C++ kernel] 這是 Python 測驗 cell，但缺少必要套件：%s\n"
                "請執行：pip install jupyterquiz jupytercards\n" % exc
            )
            return {
                "status": "error",
                "ename": type(exc).__name__,
                "evalue": str(exc),
                "traceback": [],
            }

        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                exec(compile(code, "<quiz-cell>", "exec"), self.ns)
        except BaseException as exc:   # noqa: BLE001 — 絕不能讓 kernel 掛掉
            out = buf.getvalue()
            if out and not silent:
                self._stdout(out)
            tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
            # 去掉本模組這一層 frame，讓訊息聚焦在使用者的 cell
            msg = "".join(tb)
            if isinstance(exc, FileNotFoundError):
                msg += (
                    "\n[C++ kernel] 找不到測驗 JSON 檔。相對路徑是以 notebook 所在目錄為基準，"
                    "請確認 questions/ 或 flashcards/ 目錄存在。\n"
                )
            self._stderr("\n" + msg)
            return {
                "status": "error",
                "ename": type(exc).__name__,
                "evalue": str(exc),
                "traceback": tb,
            }

        out = buf.getvalue()
        if out and not silent:
            self._stdout(out)
        return {"status": "ok", "payload": [], "user_expressions": {}}
