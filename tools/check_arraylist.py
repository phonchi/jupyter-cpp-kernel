"""Compile and execute the Chapter 3 example against bundled or installed headers."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = "31 77 17 93 (size 4, capacity 8)\n93 4\n77 17 20 93 (size 4, capacity 8)\n50 17 93 (size 3, capacity 8)\n"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-root", type=Path, default=ROOT / "jcppkernel/resources")
    args = parser.parse_args()
    compiler = shutil.which("g++")
    if not compiler:
        raise SystemExit("g++ is required")
    with tempfile.TemporaryDirectory(prefix="arraylist-regression-") as temp:
        binary = Path(temp) / "example.exe"
        subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-pedantic",
                        "-I" + str(args.include_root.resolve()),
                        str(ROOT / "tests/fixtures/chapter3_arraylist.cpp"),
                        "-o", str(binary)], check=True, timeout=60)
        result = subprocess.run([str(binary)], capture_output=True, text=True, check=True, timeout=10)
        if result.stdout != EXPECTED or result.stderr:
            raise AssertionError((result.stdout, result.stderr))
    print("Chapter 3 ArrayList: compilation and exact output passed")

if __name__ == "__main__":
    main()
