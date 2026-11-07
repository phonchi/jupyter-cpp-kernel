"""Platform selection and missing-development-files regression checks."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jcppkernel import gui_support


class GuiSupportTests(unittest.TestCase):
    def test_windows_include_forms(self):
        for include in ['"pythonds3/cppds/maze.hpp"', '<pythonds3/cppds/maze.hpp>',
                        '"pythonds3\\cppds\\maze.hpp"', '<CTurtle.hpp>']:
            with self.subTest(include=include):
                flags = gui_support.gui_compile_flags('# include ' + include, platform='win32')
                self.assertIn('-DCPPDS_MAZE_ENABLE_GUI', flags)
                self.assertIn('-lgdi32', flags)
                self.assertIn('-luser32', flags)
                self.assertNotIn('-lX11', flags)

    def test_ordinary_cell_does_not_acquire_gui_dependencies(self):
        code = '#include <iostream>\n// #include "maze.hpp"\nint main() {}'
        for platform in ['win32', 'linux', 'darwin']:
            self.assertEqual(gui_support.gui_compile_flags(code, platform=platform), [])

    def test_linux_requires_development_library_and_prefers_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, system = root / 'conda environment', root / 'system'
            for base in [prefix, system]:
                (base / 'include/X11').mkdir(parents=True)
                (base / 'include/X11/Xlib.h').touch()
                (base / 'lib').mkdir()
            (system / 'lib/libX11.so').touch()
            with patch.object(gui_support, '_X11_SYSTEM_ROOTS', (system,)):
                code = '#include "pythonds3/cppds/maze.hpp"'
                # A runtime-only library is insufficient: use system development files.
                (prefix / 'lib/libX11.so.6').touch()
                flags = gui_support.gui_compile_flags(code, platform='linux', prefix=prefix)
                self.assertIn('-I' + str(system / 'include'), flags)
                (prefix / 'lib/libX11.so').touch()
                flags = gui_support.gui_compile_flags(code, platform='linux', prefix=prefix)
                self.assertIn('-I' + str(prefix / 'include'), flags)
                self.assertIn('-L' + str(prefix / 'lib'), flags)
                self.assertIn('-Wl,-rpath,' + str(prefix / 'lib'), flags)
                self.assertIn('-lX11', flags)
                self.assertNotIn('-lgdi32', flags)

    def test_linux_without_x11_leaves_text_mode_available(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(gui_support, '_X11_SYSTEM_ROOTS', ()):
                self.assertEqual(gui_support.gui_compile_flags(
                    '#include "maze.hpp"', platform='linux', prefix=directory), [])


if __name__ == '__main__':
    unittest.main()
