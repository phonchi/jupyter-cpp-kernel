Maze GUI for course notebooks

Use the course C++17 kernel and the existing notebook code:
  Maze myMaze("maze2.txt", true);
  searchFrom(myMaze, myMaze.startRow, myMaze.startCol);
  myMaze.print();
  myMaze.waitForClose();

Close the desktop window to finish the cell. Maze(filename) or
Maze(filename, false) selects text-only execution. Both modes use the same
searchFrom algorithm. No precompiled maze DLL/SO or absolute path is needed.

The course kernel enables CPPDS_MAZE_ENABLE_GUI and supplies native graphics
linker flags. Windows uses GDI32; Linux uses X11 and requires development files
and a usable display. Without GUI build support, text-only mode still works.
For a standalone C++17 program, define CPPDS_MAZE_ENABLE_GUI and link the
platform graphics library (-lgdi32 -luser32 -pthread on MinGW Windows;
-lX11 -pthread on Linux). GUI libraries are not needed for text-only builds.

maze.hpp looks for the requested file in the working directory first. For a
bare filename such as maze2.txt, it then checks cppds/data beside the header.
The notebook, headers, and sample maze can therefore travel independently of
the teacher's computer. Packaged sample data never overwrites a student's file.

Bundled CTurtle and CImg are unchanged copies from HW3/02_FILE/Cpp. Their
original license notices are retained. Window colors and geometry follow HW3.
