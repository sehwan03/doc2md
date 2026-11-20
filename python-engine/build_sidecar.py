"""Build the converter engine with PyInstaller and place it in src-tauri/binaries/.

Run with the venv interpreter:  venv/Scripts/python build_sidecar.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN_DIR = HERE.parent / "src-tauri" / "binaries"
NAME = "converter-engine"

# Optional MarkItDown extras we do not need (audio, cloud, youtube ...).
EXCLUDES = [
    "azure", "speech_recognition", "pydub", "youtube_transcript_api",
    "tkinter", "matplotlib", "IPython", "pytest",
]


def target_triple() -> str:
    out = subprocess.check_output(["rustc", "-vV"], text=True)
    for line in out.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError("rustc -vV 에서 host triple 을 찾지 못했습니다.")


def main() -> None:
    triple = target_triple()
    ext = ".exe" if os.name == "nt" else ""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--noconsole",
        "--name", NAME,
        "--distpath", str(HERE / "dist"),
        "--workpath", str(HERE / "build"),
        "--specpath", str(HERE / "build"),
        "--paths", str(HERE),
        "--hidden-import", "hwp_converters",
        "--hidden-import", "md_images",
        "--collect-all", "markitdown",
        "--collect-all", "magika",
        "--collect-data", "pdfminer",
        "--copy-metadata", "markitdown",
        "--copy-metadata", "magika",
    ]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    cmd.append(str(HERE / "converter_cli.py"))

    print(">", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, cwd=HERE)

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    src = HERE / "dist" / f"{NAME}{ext}"
    dst = BIN_DIR / f"{NAME}-{triple}{ext}"
    shutil.copy2(src, dst)
    print(f"sidecar -> {dst} ({dst.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
