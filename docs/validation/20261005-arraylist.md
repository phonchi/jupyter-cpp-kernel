# ArrayList bundled-header validation (2026-10-05)

The Chapter 3 notebook uses `push_back`, `capacity`, and const access through
`print(const ArrayList&)`. The course kernel previously bundled the older
`append` / `isEmpty` implementation, so a fresh install failed to compile that cell.

## Source

- Header synchronized byte-for-byte from `phonchi/pythonds3` commit
  `3b4a20038b327e424b1f21f07ea42b8d6cee3f2c`, `cppds/arraylist.hpp`.
- Header SHA-256: `7821a7d7844a3570e2be8b686882862775233ec55b44bfa85a45b38fd863f4cc`.
- Fixture is cell 47 (zero-based) of the public `03_Arrays.ipynb` under
  `phonchi/nsysu-math208/main/static_files/presentations/`.
- Notebook snapshot SHA-256: `f8b49486d47a00ebd3b0a8d1d1660412c462a7cc4542bd1317b8f5b817ba0847`.

## Checks

1. Before updating the header, `python tools/check_arraylist.py` failed with
   missing `push_back` / `capacity` and discarded-const-qualifier errors.
2. After synchronization, the same command compiled and matched all output lines.
3. Created a second clean venv without system site packages; installed the fixed
   repository as a package (`1.0.0a9.post1`), plus ipykernel and nbformat.
4. Registered kernelspecs inside that venv and launched the actual C++17 kernel
   through Jupyter KernelManager, with an empty working directory. There was no
   local `pythonds3/` directory to override the bundled header.
5. The installed header hash matched the canonical source. The original notebook
   cell returned the exact expected output. The compiler regression also passed
   against the installed `jcppkernel/resources` include directory.

Execution output is preserved in [20261005-arraylist.ipynb](20261005-arraylist.ipynb).
Validation ran on Linux with C++17; Windows was not directly tested.

Expected output:

```text
31 77 17 93 (size 4, capacity 8)
93 4
77 17 20 93 (size 4, capacity 8)
50 17 93 (size 3, capacity 8)
```

The kernel currently reports `execute_reply.status=ok` even after compiler errors.
These checks therefore assert actual stdout and absence of compiler errors;
`status=ok` alone was not used as a pass signal.
