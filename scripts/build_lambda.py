"""Build the Lambda bundle in build/lambda without Docker.

pip fetches Linux ARM64 wheels for Python 3.12 (the Lambda runtime), then the
countersign package is copied next to them. boto3 is left out: the Lambda
runtime provides it.

    python scripts/build_lambda.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "build" / "lambda"
RUNTIME_DEPS = ["requests>=2.32", "cryptography>=43", "fastapi>=0.115", "mangum>=0.19", "pillow>=10.1",
                "tzdata>=2024.1"]


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--quiet", "--no-cache-dir",
        "--target", str(OUT),
        "--implementation", "cp", "--python-version", "3.12",
        "--platform", "manylinux2014_aarch64", "--platform", "manylinux_2_28_aarch64",
        "--only-binary=:all:",
        *RUNTIME_DEPS,
    ], check=True)
    shutil.copytree(ROOT / "countersign", OUT / "countersign", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for junk in OUT.glob("*.dist-info"):
        shutil.rmtree(junk)
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"built {OUT} ({size / 1e6:.1f} MB unzipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
