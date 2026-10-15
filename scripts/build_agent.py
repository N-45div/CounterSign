"""Bundle the dispute agent for AgentCore Runtime direct code deployment.

Linux ARM64 wheels for Python 3.12 (AgentCore runs on Graviton), the
agentcore/app.py entry point, and only the countersign modules the agent needs:
no store, no lambda handlers, nothing that could sign.

    python scripts/build_agent.py   -> build/agent.zip
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "build" / "agent"
ZIP = ROOT / "build" / "agent.zip"
DEPS = ["strands-agents>=1.0", "bedrock-agentcore>=1.0", "cryptography>=43"]
MODULES = ["__init__.py", "dispute_agent.py", "disputes.py", "receipt.py"]


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--quiet", "--no-cache-dir", "--target", str(OUT),
        "--implementation", "cp", "--python-version", "3.12",
        "--platform", "manylinux2014_aarch64", "--platform", "manylinux_2_28_aarch64",
        "--only-binary=:all:", *DEPS,
    ], check=True)
    (OUT / "countersign").mkdir()
    for name in MODULES:
        shutil.copy(ROOT / "countersign" / name, OUT / "countersign" / name)
    shutil.copy(ROOT / "agentcore" / "app.py", OUT / "app.py")
    for junk in list(OUT.rglob("__pycache__")):
        shutil.rmtree(junk, ignore_errors=True)
    if ZIP.exists():
        ZIP.unlink()
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(OUT.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(OUT).as_posix())
    print(f"built {ZIP} ({ZIP.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
