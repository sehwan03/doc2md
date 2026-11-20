"""doc2md converter engine CLI.

Usage: converter-engine <file-path> [--out <markdown-output-path>]

With --out, embedded images are written to "<stem>_images/" next to the given
Markdown path and linked relatively. Without it, images become short notes.

Writes a single UTF-8 JSON object to stdout:
  {"success": bool, "file_name": str, "markdown": str, "error": str}
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import traceback
import warnings


def _ensure_streams() -> None:
    # PyInstaller --noconsole builds may start with sys.stdout/stderr = None.
    if sys.stderr is None:
        sys.stderr = io.StringIO()
    warnings.filterwarnings("ignore")


def _write_stdout(payload: bytes) -> None:
    out = getattr(sys.stdout, "buffer", None) if sys.stdout is not None else None
    if out is not None:
        out.write(payload)
        out.flush()
        return
    fd = None
    if os.name == "nt":
        import ctypes
        import msvcrt

        handle = ctypes.windll.kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if handle and handle != ctypes.c_void_p(-1).value:
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
    else:
        fd = 1
    if fd is None:
        return
    view = memoryview(payload)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def convert(path: str, out_md: str | None = None) -> dict:
    result = {"success": False, "file_name": os.path.basename(path), "markdown": "", "error": ""}
    try:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"파일을 찾을 수 없습니다: {path}")

        from markitdown import MarkItDown
        from hwp_converters import HwpConverter, HwpxConverter

        md = MarkItDown(enable_plugins=False)
        md.register_converter(HwpxConverter(), priority=-10.0)
        md.register_converter(HwpConverter(), priority=-10.0)

        # Keep library chatter from corrupting the JSON on stdout.
        with contextlib.redirect_stdout(io.StringIO()):
            # keep_data_uris hands us the real image bytes; md_images moves them
            # out of the Markdown so no base64 ever reaches the output.
            converted = md.convert(path, keep_data_uris=True)
        markdown = converted.markdown or ""

        from md_images import add_smartart_text, extract_images

        if path.lower().endswith(".pptx"):
            markdown = add_smartart_text(markdown, path)
        markdown = extract_images(markdown, out_md)
        result["markdown"] = markdown
        result["success"] = True
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).strip() or exc.__class__.__name__
        result["error"] = f"{exc.__class__.__name__}: {msg}"
        if os.environ.get("DOC2MD_DEBUG"):
            result["error"] += "\n" + traceback.format_exc()
    return result


def main() -> int:
    _ensure_streams()
    if len(sys.argv) < 2:
        res = {"success": False, "file_name": "", "markdown": "", "error": "변환할 파일 경로가 필요합니다."}
    else:
        args = sys.argv[1:]
        out_md = None
        if "--out" in args:
            i = args.index("--out")
            if i + 1 < len(args):
                out_md = args[i + 1]
            del args[i : i + 2]
        res = convert(args[0], out_md)
    _write_stdout(json.dumps(res, ensure_ascii=False).encode("utf-8"))
    return 0 if res["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
