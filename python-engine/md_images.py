"""Post-processing that keeps converted Markdown cheap for an AI reader.

MarkItDown is run with ``keep_data_uris=True`` so embedded images come back as
``![alt](data:...;base64,...)``. Base64 inside Markdown costs an enormous number
of text tokens, so every data URI is moved out to a sidecar folder and replaced
by a short relative link that carries enough metadata (slide, size, rough token
cost, alt text) for the reader to decide whether opening the image is worth it.

Token-saving rules applied per image:
  * identical images (logos, decorations) are saved once; repeats become a note
  * tiny images (icons) and blank images are dropped
  * uniform / transparent borders are trimmed
  * the long edge is capped at MAX_EDGE (image tokens ~= width * height / 750)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
from pathlib import Path

MAX_EDGE = 1568          # long-edge cap in pixels
ICON_EDGE = 64           # images whose long edge is below this are dropped
TRIM_TOLERANCE = 16      # per-channel difference treated as "same as border"
JPEG_QUALITY = 85
IMAGE_DIR_SUFFIX = "_images"

_DATA_IMG_RE = re.compile(
    r"!\[(?P<alt>(?:\\.|[^\]\\])*)\]"
    r"\(\s*data:(?P<mime>[\w.+/-]+)?(?:;[\w=.-]+)*;base64,(?P<data>[A-Za-z0-9+/=\s]+?)"
    r"(?:\s+\"[^\"]*\")?\s*\)"
)
_SLIDE_RE = re.compile(r"<!-- Slide number: (\d+) -->")
_OWN_FILE_RE = re.compile(r"^(s\d+_\d+|img\d+)\.(png|jpg|svg|bin|emf|wmf|gif|tif|tiff|bmp|webp)$")

_RAW_EXT = {
    "image/svg+xml": "svg",
    "image/x-emf": "emf",
    "image/emf": "emf",
    "image/x-wmf": "wmf",
    "image/wmf": "wmf",
}


def images_dir_name(md_path: Path) -> str:
    safe = re.sub(r"[\s()\[\]<>#%{}]+", "_", md_path.stem).strip("_") or "doc"
    return safe + IMAGE_DIR_SUFFIX


_NOISE_ALT_RE = re.compile(
    r"^(?:[\w\- ]+\.(?:png|jpe?g|gif|bmp|emf|wmf|tiff?|svg|webp)"
    r"|(?:그림|사진|이미지|picture|image|graphic|grafik)\s*\d*)$",
    re.IGNORECASE,
)


def _clean_alt(alt: str) -> str:
    alt = re.sub(r"[\r\n\[\]\\]", " ", alt)
    alt = re.sub(r"\s+", " ", alt).strip()
    # Auto-generated names ("image.png", "그림 3") carry no information.
    return "" if _NOISE_ALT_RE.match(alt) else alt


def _trim(im):
    """Crop transparent and uniform-colour borders. Returns None for blank images."""
    from PIL import Image, ImageChops

    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        alpha_box = im.getchannel("A").point(lambda p: 255 if p > 8 else 0).getbbox()
        if alpha_box is None:
            return None
        im = im.crop(alpha_box)
        rgb = Image.new("RGB", im.size, (255, 255, 255))
        rgb.paste(im, mask=im.getchannel("A"))
    else:
        im = im.convert("RGB")
        rgb = im

    bg = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
    diff = ImageChops.difference(rgb, bg).convert("L")
    box = diff.point(lambda p: 255 if p > TRIM_TOLERANCE else 0).getbbox()
    if box is None:
        return None
    pad = 4
    box = (max(box[0] - pad, 0), max(box[1] - pad, 0),
           min(box[2] + pad, im.width), min(box[3] + pad, im.height))
    return im.crop(box)


def _encode(im, src_mime: str) -> tuple[bytes, str]:
    """Pick PNG for flat graphics / transparency, JPEG for photo-like images."""
    has_alpha = im.mode == "RGBA" and im.getchannel("A").getextrema()[0] < 255
    out = io.BytesIO()
    if has_alpha:
        im.save(out, "PNG", optimize=True)
        return out.getvalue(), "png"
    rgb = im.convert("RGB")
    photo_like = src_mime in ("image/jpeg", "image/jpg") or rgb.getcolors(4096) is None
    if photo_like:
        rgb.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return out.getvalue(), "jpg"
    rgb.save(out, "PNG", optimize=True)
    return out.getvalue(), "png"


def _clear_previous(img_dir: Path) -> None:
    if not img_dir.is_dir():
        return
    for f in img_dir.iterdir():
        if f.is_file() and _OWN_FILE_RE.match(f.name):
            f.unlink(missing_ok=True)


def extract_images(markdown: str, md_path: str | None) -> str:
    """Move data-URI images out of ``markdown``.

    With ``md_path`` the images are written next to it; without it they are
    replaced by a short placeholder so the Markdown never carries base64.
    """
    if "data:" not in markdown:
        return markdown

    out_md = Path(md_path) if md_path else None
    dir_name = images_dir_name(out_md) if out_md else ""
    img_dir = out_md.parent / dir_name if out_md else None
    if img_dir is not None:
        _clear_previous(img_dir)

    slides = [(m.start(), int(m.group(1))) for m in _SLIDE_RE.finditer(markdown)]
    seen: dict[str, str] = {}      # sha1 -> replacement note for repeats
    per_slide: dict[int, int] = {}
    counter = 0

    def slide_at(pos: int) -> int | None:
        current = None
        for start, num in slides:
            if start > pos:
                break
            current = num
        return current

    def replace(m: re.Match) -> str:
        nonlocal counter
        alt = _clean_alt(m.group("alt"))
        mime = (m.group("mime") or "").lower()
        try:
            blob = base64.b64decode(re.sub(r"\s+", "", m.group("data")), validate=False)
        except (binascii.Error, ValueError):
            return f"(이미지 해석 실패{': ' + alt if alt else ''})"

        digest = hashlib.sha1(blob).hexdigest()
        if digest in seen:
            return seen[digest]

        slide = slide_at(m.start())
        if slide is not None:
            per_slide[slide] = per_slide.get(slide, 0) + 1
            stem = f"s{slide:02d}_{per_slide[slide]:02d}"
            label = f"슬라이드{slide} 그림{per_slide[slide]}"
        else:
            counter += 1
            stem = f"img{counter:03d}"
            label = f"그림{counter}"

        if img_dir is None:
            note = f"({label}{' · ' + alt if alt else ''})"
            seen[digest] = f"(반복 이미지{' · ' + alt if alt else ''})"
            return note

        data, ext, size = None, None, None
        if mime != "image/svg+xml":  # SVG cannot be rasterised without extra deps
            try:
                from PIL import Image

                im = Image.open(io.BytesIO(blob))
                im.load()
                if max(im.size) < ICON_EDGE:
                    seen[digest] = ""
                    return ""
                im = _trim(im)
                if im is None or max(im.size) < 16:
                    seen[digest] = ""
                    return ""
                im.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
                data, ext = _encode(im, mime)
                size = im.size
            except Exception:  # noqa: BLE001 - unreadable formats are kept raw
                data = None
        if data is None:
            data = blob
            ext = _RAW_EXT.get(mime) or (mime.split("/")[-1] if "/" in mime else "bin")
            ext = re.sub(r"[^a-z0-9]", "", ext)[:5] or "bin"

        img_dir.mkdir(parents=True, exist_ok=True)
        file_name = f"{stem}.{ext}"
        (img_dir / file_name).write_bytes(data)
        rel = f"{dir_name}/{file_name}"

        meta = [label]
        if size:
            meta.append(f"{size[0]}×{size[1]}")
            meta.append(f"~{max(1, size[0] * size[1] // 750)}토큰")
        else:
            meta.append(ext.upper())
        if alt:
            meta.append(alt)
        seen[digest] = f"(반복 이미지: {rel})"
        return f"![{' · '.join(meta)}]({rel})"

    result = _DATA_IMG_RE.sub(replace, markdown)
    # Dropped images leave empty lines behind; collapse runs of blank lines.
    return re.sub(r"\n{3,}", "\n\n", result)


# --------------------------------------------------------------------------
# SmartArt text (MarkItDown skips SmartArt graphic frames entirely)
# --------------------------------------------------------------------------

_DGM_URI = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "dgm": _DGM_URI,
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def _smartart_texts(slide) -> list[list[str]]:
    from lxml import etree

    found: list[list[str]] = []

    def walk(shapes):
        for shape in shapes:
            if hasattr(shape, "shapes"):  # group shape
                walk(shape.shapes)
                continue
            el = shape._element
            gdata = el.find(".//a:graphicData", _NS)
            if gdata is None or gdata.get("uri") != _DGM_URI:
                continue
            rel = gdata.find("dgm:relIds", _NS)
            rid = rel.get(f"{{{_NS['r']}}}dm") if rel is not None else None
            if not rid:
                continue
            try:
                part = slide.part.related_part(rid)
                root = etree.fromstring(part.blob)
            except Exception:  # noqa: BLE001
                continue
            items = []
            for pt in root.iterfind(".//dgm:pt", _NS):
                if pt.get("type") not in (None, "node"):
                    continue
                text = "".join(t.text or "" for t in pt.iterfind(".//a:t", _NS)).strip()
                if text:
                    items.append(text)
            if items:
                found.append(items)

    walk(slide.shapes)
    return found


def add_smartart_text(markdown: str, pptx_path: str) -> str:
    try:
        import pptx

        prs = pptx.Presentation(pptx_path)
    except Exception:  # noqa: BLE001
        return markdown

    extra: dict[int, str] = {}
    for num, slide in enumerate(prs.slides, start=1):
        blocks = _smartart_texts(slide)
        if blocks:
            lines = []
            for items in blocks:
                lines.append("[SmartArt]")
                lines.extend(f"- {t}" for t in items)
            extra[num] = "\n".join(lines)
    if not extra:
        return markdown

    parts = _SLIDE_RE.split(markdown)
    # parts = [preamble, num1, body1, num2, body2, ...]
    out = [parts[0]]
    for i in range(1, len(parts) - 1, 2):
        num, body = int(parts[i]), parts[i + 1]
        if num in extra:
            notes_at = body.find("\n### Notes:")
            insert = "\n" + extra[num] + "\n"
            if notes_at >= 0:
                body = body[:notes_at].rstrip() + "\n" + insert + body[notes_at:]
            else:
                body = body.rstrip() + "\n" + insert
        out.append(f"<!-- Slide number: {num} -->")
        out.append(body)
    return "".join(out)
