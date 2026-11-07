"""Select native GUI flags for cells directly including maze.hpp or CTurtle.hpp.

No GUI dependency is added to ordinary cells. Linux installations without X11
development files retain the text-only Maze implementation.
"""
from pathlib import Path
import re
import sys


_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"\r\n]+)[>"]', re.MULTILINE)
_X11_SYSTEM_ROOTS = (Path('/usr'), Path('/usr/local'))


def _needs_gui_support(code):
    return any(
        name.replace('\\', '/').rsplit('/', 1)[-1].lower() in ('maze.hpp', 'cturtle.hpp')
        for name in _INCLUDE.findall(code)
    )


def _x11_flags(prefix):
    # Prefer the current environment, including conda's X11 headers/libraries.
    # Require the linker-facing .so as well as headers; a runtime-only .so.6
    # installation cannot satisfy -lX11.
    for root in (prefix,) + _X11_SYSTEM_ROOTS:
        include = root / 'include'
        if not (include / 'X11' / 'Xlib.h').is_file():
            continue
        candidates = [root / 'lib' / 'libX11.so', root / 'lib64' / 'libX11.so']
        candidates += sorted((root / 'lib').glob('*/libX11.so'))
        for library in candidates:
            if library.is_file():
                libdir = str(library.parent)
                return ['-DCPPDS_MAZE_ENABLE_GUI', '-I' + str(include),
                        '-L' + libdir, '-Wl,-rpath,' + libdir, '-lX11', '-pthread']
    return []


def gui_compile_flags(code, platform=None, prefix=None):
    """Return argument-list entries, never shell text or installation commands."""
    if not _needs_gui_support(code):
        return []
    platform = sys.platform if platform is None else platform
    if platform == 'win32':
        return ['-DCPPDS_MAZE_ENABLE_GUI', '-lgdi32', '-luser32', '-pthread']
    if platform.startswith('linux'):
        return _x11_flags(Path(sys.prefix if prefix is None else prefix))
    return []
