"""Korean Hangul document converters (.hwpx / .hwp) for MarkItDown.

Both converters are registered with priority -10.0 so they are tried before
any built-in MarkItDown converter.
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, BinaryIO, List, Optional

from markitdown import DocumentConverter, DocumentConverterResult, StreamInfo

# ---------------------------------------------------------------------------
# Shared markdown table helpers
# ---------------------------------------------------------------------------


def _escape_cell(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = text.replace("|", "\\|")
    return "<br>".join(line.strip() for line in text.split("\n"))


@dataclass
class Cell:
    row: Optional[int] = None
    col: Optional[int] = None
    row_span: int = 1
    col_span: int = 1
    paragraphs: List[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(p for p in self.paragraphs if p.strip())


def render_table(cells: List[Cell], n_rows: int = 0, n_cols: int = 0) -> str:
    """Render cells (with optional coordinates/spans) as a markdown table."""
    if not cells:
        return ""

    have_coords = all(c.row is not None and c.col is not None for c in cells)
    if have_coords:
        n_rows = max(n_rows, max(c.row + max(c.row_span, 1) for c in cells))
        n_cols = max(n_cols, max(c.col + max(c.col_span, 1) for c in cells))
        placed = [(c.row, c.col, c) for c in cells]
    else:
        # Fallback: sequential placement honouring spans.
        if n_cols <= 0:
            n_cols = max(1, len(cells))
        occupied: set = set()
        placed = []
        r = c_ = 0
        for cell in cells:
            while (r, c_) in occupied or c_ >= n_cols:
                c_ += 1
                if c_ >= n_cols:
                    r += 1
                    c_ = 0
            placed.append((r, c_, cell))
            for dr in range(max(cell.row_span, 1)):
                for dc in range(max(cell.col_span, 1)):
                    occupied.add((r + dr, c_ + dc))
        n_rows = max(n_rows, max(rr + max(cl.row_span, 1) for rr, _, cl in placed))

    if n_rows <= 0 or n_cols <= 0:
        return ""

    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for r, c, cell in placed:
        if 0 <= r < n_rows and 0 <= c < n_cols:
            grid[r][c] = _escape_cell(cell.text)

    # Drop trailing fully-empty rows.
    while len(grid) > 1 and not any(grid[-1]):
        grid.pop()

    lines = ["| " + " | ".join(grid[0]) + " |", "| " + " | ".join(["---"] * n_cols) + " |"]
    for row in grid[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _join_blocks(blocks: List[str]) -> str:
    out = "\n\n".join(b.strip("\n") for b in blocks if b and b.strip())
    return re.sub(r"\n{3,}", "\n\n", out).strip() + "\n"


# ---------------------------------------------------------------------------
# HWPX (OWPML, zip + XML)
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_child(el: ET.Element, name: str) -> Optional[ET.Element]:
    for ch in el:
        if _local(ch.tag) == name:
            return ch
    return None


class HwpxConverter(DocumentConverter):
    """Converts .hwpx (Hancom OWPML) documents."""

    def accepts(self, file_stream: BinaryIO, stream_info: StreamInfo, **kwargs: Any) -> bool:
        ext = (stream_info.extension or "").lower()
        return ext == ".hwpx"

    def convert(self, file_stream: BinaryIO, stream_info: StreamInfo, **kwargs: Any) -> DocumentConverterResult:
        data = file_stream.read()
        blocks: List[str] = []
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            sections = []
            for name in zf.namelist():
                m = re.match(r"Contents/section(\d+)\.xml$", name, re.IGNORECASE)
                if m:
                    sections.append((int(m.group(1)), name))
            if not sections:
                raise ValueError("HWPX 문서에서 Contents/section*.xml 을 찾을 수 없습니다.")
            for _, name in sorted(sections):
                root = ET.fromstring(zf.read(name))
                for child in root:
                    if _local(child.tag) == "p":
                        self._paragraph(child, blocks)
        return DocumentConverterResult(markdown=_join_blocks(blocks))

    # -- paragraph / run handling -------------------------------------------------

    def _paragraph(self, p: ET.Element, blocks: List[str]) -> None:
        """Emit paragraph text and any tables (in document order)."""
        buf: List[str] = []

        def flush() -> None:
            text = "".join(buf).strip()
            if text:
                blocks.append(text)
            buf.clear()

        for run in p:
            if _local(run.tag) != "run":
                continue
            for item in run:
                name = _local(item.tag)
                if name == "t":
                    buf.append(self._t_text(item))
                elif name == "tbl":
                    flush()
                    md = self._table(item)
                    if md:
                        blocks.append(md)
                else:
                    # Other inline objects (pictures, shapes, ...) may contain text.
                    for sub in item.iter():
                        if _local(sub.tag) == "drawText":
                            for sp in sub.iter():
                                if _local(sp.tag) == "p":
                                    t = self._plain_text(sp)
                                    if t:
                                        buf.append(t + "\n")
                            break
        flush()

    @staticmethod
    def _t_text(t: ET.Element) -> str:
        parts = [t.text or ""]
        for ch in t:
            n = _local(ch.tag)
            if n == "tab":
                parts.append("\t")
            elif n in ("lineBreak",):
                parts.append("\n")
            elif n in ("nbSpace", "fwSpace"):
                parts.append(" ")
            elif n == "hyphen":
                parts.append("-")
            parts.append(ch.tail or "")
        return "".join(parts)

    def _plain_text(self, p: ET.Element) -> str:
        """Flatten a paragraph (including nested tables) into plain text."""
        out: List[str] = []
        for run in p:
            if _local(run.tag) != "run":
                continue
            for item in run:
                n = _local(item.tag)
                if n == "t":
                    out.append(self._t_text(item))
                elif n == "tbl":
                    out.append(" ".join(self._flatten_table(item)))
        return "".join(out).strip()

    def _flatten_table(self, tbl: ET.Element) -> List[str]:
        texts: List[str] = []
        for tc in tbl.iter():
            if _local(tc.tag) == "tc":
                cell = self._cell_paragraphs(tc)
                if cell:
                    texts.append(" ".join(cell))
        return texts

    def _cell_paragraphs(self, tc: ET.Element) -> List[str]:
        sub = _find_child(tc, "subList")
        container = sub if sub is not None else tc
        paras = []
        for p in container:
            if _local(p.tag) == "p":
                t = self._plain_text(p)
                if t:
                    paras.append(t)
        return paras

    def _table(self, tbl: ET.Element) -> str:
        n_rows = int(tbl.get("rowCnt", "0") or 0)
        n_cols = int(tbl.get("colCnt", "0") or 0)
        cells: List[Cell] = []
        for tr in tbl:
            if _local(tr.tag) != "tr":
                continue
            for tc in tr:
                if _local(tc.tag) != "tc":
                    continue
                cell = Cell(paragraphs=self._cell_paragraphs(tc))
                addr = _find_child(tc, "cellAddr")
                if addr is not None:
                    cell.row = int(addr.get("rowAddr", "0"))
                    cell.col = int(addr.get("colAddr", "0"))
                span = _find_child(tc, "cellSpan")
                if span is not None:
                    cell.row_span = int(span.get("rowSpan", "1"))
                    cell.col_span = int(span.get("colSpan", "1"))
                cells.append(cell)
        return render_table(cells, n_rows, n_cols)


# ---------------------------------------------------------------------------
# HWP 5.0 (OLE compound file, binary records)
# ---------------------------------------------------------------------------

HWPTAG_BEGIN = 0x010
HWPTAG_PARA_HEADER = HWPTAG_BEGIN + 50  # 66
HWPTAG_PARA_TEXT = HWPTAG_BEGIN + 51  # 67
HWPTAG_CTRL_HEADER = HWPTAG_BEGIN + 55  # 71
HWPTAG_LIST_HEADER = HWPTAG_BEGIN + 56  # 72
HWPTAG_TABLE = HWPTAG_BEGIN + 61  # 77


def _ctrl_id(s: str) -> int:
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


CTRL_TABLE = _ctrl_id("tbl ")
CTRL_SKIP = {_ctrl_id("head"), _ctrl_id("foot")}  # headers / footers

# Control characters occupying 8 WCHARs (inline + extended controls).
_WIDE_CTRL = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}


def _parse_para_text(data: bytes) -> str:
    n = len(data) // 2
    chars = struct.unpack(f"<{n}H", data[: n * 2])
    out: List[str] = []
    i = 0
    while i < n:
        c = chars[i]
        if c >= 32:
            out.append(chr(c))
            i += 1
        elif c in _WIDE_CTRL:
            if c == 9:
                out.append("\t")
            i += 8
        else:
            if c == 10:
                out.append("\n")
            elif c in (30, 31):
                out.append(" ")
            elif c == 24:
                out.append("-")
            # 0, 13 and other 1-wchar controls are dropped.
            i += 1
    # Join surrogate pairs that were split by chr() of each half.
    text = "".join(out)
    try:
        text = text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        pass
    return text


def _iter_records(data: bytes):
    pos, size_total = 0, len(data)
    while pos + 4 <= size_total:
        (header,) = struct.unpack_from("<I", data, pos)
        pos += 4
        tag = header & 0x3FF
        level = (header >> 10) & 0x3FF
        size = (header >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > size_total:
                break
            (size,) = struct.unpack_from("<I", data, pos)
            pos += 4
        yield tag, level, data[pos : pos + size]
        pos += size


@dataclass
class _TableCtx:
    level: int
    rows: int = 0
    cols: int = 0
    cells: List[Cell] = field(default_factory=list)
    current: Optional[Cell] = None


class HwpConverter(DocumentConverter):
    """Converts legacy binary .hwp (HWP 5.x) documents."""

    def accepts(self, file_stream: BinaryIO, stream_info: StreamInfo, **kwargs: Any) -> bool:
        ext = (stream_info.extension or "").lower()
        return ext == ".hwp"

    def convert(self, file_stream: BinaryIO, stream_info: StreamInfo, **kwargs: Any) -> DocumentConverterResult:
        import olefile

        data = file_stream.read()
        if not olefile.isOleFile(io.BytesIO(data)):
            raise ValueError("HWP 5.0 형식(OLE)이 아닙니다. HWP 3.x 등 구형 문서는 지원하지 않습니다.")

        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            header = ole.openstream("FileHeader").read()
            props = struct.unpack_from("<I", header, 36)[0] if len(header) >= 40 else 0
            compressed = bool(props & 0x01)
            if props & 0x02:
                raise ValueError("암호가 설정된 HWP 문서는 변환할 수 없습니다.")
            if props & 0x04:
                raise ValueError("배포용(DRM) HWP 문서는 변환할 수 없습니다.")

            sections = []
            for entry in ole.listdir(streams=True, storages=False):
                if len(entry) == 2 and entry[0] == "BodyText":
                    m = re.match(r"Section(\d+)$", entry[1])
                    if m:
                        sections.append((int(m.group(1)), "/".join(entry)))

            blocks: List[str] = []
            for _, name in sorted(sections):
                raw = ole.openstream(name).read()
                if compressed:
                    raw = zlib.decompress(raw, -15)
                self._parse_section(raw, blocks)

            if not any(b.strip() for b in blocks) and ole.exists("PrvText"):
                prv = ole.openstream("PrvText").read().decode("utf-16-le", errors="ignore")
                blocks.append(prv)

        return DocumentConverterResult(markdown=_join_blocks(blocks))

    def _parse_section(self, raw: bytes, blocks: List[str]) -> None:
        stack: List[_TableCtx] = []
        skip_level: Optional[int] = None

        def add_text(text: str) -> None:
            if stack:
                top = stack[-1]
                if top.current is None:
                    top.current = Cell()
                    top.cells.append(top.current)
                top.current.paragraphs.append(text)
            elif text.strip():
                blocks.append(text.strip())

        def close_top() -> None:
            ctx = stack.pop()
            if stack:
                # Nested table: flatten into the parent cell.
                flat = " ".join(c.text.replace("\n", " ") for c in ctx.cells if c.text)
                if flat:
                    add_text(flat)
            else:
                md = self._render(ctx)
                if md:
                    blocks.append(md)

        for tag, level, data in _iter_records(raw):
            while stack and level <= stack[-1].level:
                close_top()

            if skip_level is not None:
                if level > skip_level:
                    continue
                skip_level = None

            if tag == HWPTAG_CTRL_HEADER and len(data) >= 4:
                ctrl = struct.unpack_from("<I", data, 0)[0]
                if ctrl == CTRL_TABLE:
                    stack.append(_TableCtx(level=level))
                elif ctrl in CTRL_SKIP:
                    skip_level = level
            elif tag == HWPTAG_TABLE and stack and level == stack[-1].level + 1:
                if len(data) >= 8:
                    stack[-1].rows, stack[-1].cols = struct.unpack_from("<HH", data, 4)
            elif tag == HWPTAG_LIST_HEADER and stack and level == stack[-1].level + 1:
                cell = Cell()
                if len(data) >= 16:
                    col, row, cspan, rspan = struct.unpack_from("<HHHH", data, 8)
                    cell.col, cell.row = col, row
                    cell.col_span, cell.row_span = max(cspan, 1), max(rspan, 1)
                stack[-1].cells.append(cell)
                stack[-1].current = cell
            elif tag == HWPTAG_PARA_TEXT:
                add_text(_parse_para_text(data))

        while stack:
            close_top()

    @staticmethod
    def _render(ctx: _TableCtx) -> str:
        cells = ctx.cells
        valid = ctx.rows > 0 and ctx.cols > 0 and all(
            c.row is not None and c.col is not None and c.row < ctx.rows and c.col < ctx.cols for c in cells
        )
        if not valid:
            for c in cells:
                c.row = c.col = None
        return render_table(cells, ctx.rows, ctx.cols)
