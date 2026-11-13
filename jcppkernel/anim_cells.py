"""NSYSU MATH208: 讓 C++ kernel 也能執行講義 ch7–ch9 的動畫 cell。

講義用兩種 Python cell 顯示動畫（cell tag `anim-widget`）：

* 初始化 cell：第一行是 `# === DS07 animation infrastructure ...` 或
  `# === DS08/DS09 animation loader. ===`，定義 `load_anim()`。
* 動畫 cell：整格只有一行 `load_anim("bubble")`。

這些 cell 原本會被當成 C++ 送去 g++ 而編譯失敗；Jupyter 前端在送出執行要求前就清掉
輸出，所以即使 kernel 什麼都不做，存好的動畫也會消失。因此這裡不跳過，而是用 kernel
內附的動畫檔（resources/animations/，與網站 extra/animations* 相同）重新產生**與存檔
輸出逐字相同**的 HTML，不讀 notebook 旁的檔案、不連網。

刻意不 exec cell 原始碼：ch7 讀相對路徑 animations/（學生沒有），ch8/ch9 會連 GitHub，
而且 `from IPython.display import display` 在這個 kernel 裡不會送到前端。
"""

import re
from html import escape
from os import path

_RES = path.join(path.dirname(path.realpath(__file__)), "resources", "animations")

# 章節 → 子目錄、樣式／腳本檔
_CHAPTERS = {
    "07": "animations",
    "08": "animations08",
    "09": "animations09",
}

_INFRA_RE = re.compile(r"^# === DS(0[789]) animation (?:infrastructure|loader)\b")
_ANIM_RE = re.compile(r"""^load_anim\(\s*(["'])([A-Za-z0-9_]+)\1\s*\)$""")


def _read(chapter, name):
    with open(path.join(_RES, _CHAPTERS[chapter], name), "r", encoding="utf-8") as f:
        return f.read()


def _chapter_of(name):
    for chapter, folder in _CHAPTERS.items():
        if path.isfile(path.join(_RES, folder, name + ".html")):
            return chapter
    return None


def classify(code):
    """回傳 ("infra", chapter)、("anim", name) 或 None（交回其他路徑）。保守判斷。"""
    if not code:
        return None
    lines = [l.strip() for l in code.splitlines() if l.strip()]
    if not lines:
        return None
    m = _INFRA_RE.match(lines[0])
    if m and "def load_anim" in code:
        return ("infra", m.group(1))
    if len(lines) == 1:
        m = _ANIM_RE.match(lines[0])
        if m:
            return ("anim", m.group(2))
    return None


def _ch07_infra():
    return "<style>" + _read("07", "ds07.css") + "</style><script>" + _read("07", "ds07.js") + "</script>"


def _iframe(chapter, name, height=500):
    # 與講義 ch8/ch9 loader 的 _anim_iframe() 逐字相同
    css = _read(chapter, "ds%s.css" % chapter)
    js = _read(chapter, "ds%s.js" % chapter).replace("</script>", "<\\/script>")
    panel = _read(chapter, name + ".html")
    srcdoc = f"""<!doctype html>
<html>
<head>
<meta charset=\"utf-8\">
<style>html,body{{margin:0;padding:0;background:transparent;}}</style>
<style>{css}</style>
</head>
<body class="rise-enabled">
{panel}
<script>{js}</script>
</body>
</html>"""
    return (
        f'<iframe title="DS{chapter} {escape(name)} animation" '
        f'srcdoc="{escape(srcdoc, quote=True)}" '
        f'style="width:100%;height:{height}px;border:0;display:block;" '
        f'loading="lazy"></iframe>'
    )


def render(kind, arg):
    """回傳要送出的 HTML 字串（None 表示不輸出）；找不到動畫時丟 KeyError。"""
    if kind == "infra":
        return _ch07_infra() if arg == "07" else None   # ch8/ch9 的 iframe 自帶 CSS/JS
    chapter = _chapter_of(arg)
    if chapter is None:
        raise KeyError(arg)
    if chapter == "07":
        return _read("07", arg + ".html")
    return _iframe(chapter, arg)


def execute(kind, arg, silent, publish, write_stderr):
    """執行一個動畫 cell，回傳 do_execute 用的 reply dict（不含 execution_count）。"""
    try:
        html = render(kind, arg)
    except (KeyError, OSError) as exc:
        write_stderr(
            "\n[C++ kernel] 找不到動畫 %r（kernel 內附的動畫檔：%s）。\n" % (arg, _RES)
        )
        return {"status": "error", "ename": type(exc).__name__, "evalue": str(exc), "traceback": []}
    if html is not None and not silent:
        publish({"text/html": html, "text/plain": "<IPython.core.display.HTML object>"})
    return {"status": "ok", "payload": [], "user_expressions": {}}
