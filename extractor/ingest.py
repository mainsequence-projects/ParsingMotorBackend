"""Turn input files (PDF / JPG / PNG) into a list of page images."""
import io
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz
from PIL import Image

from .config import RENDER_DPI

SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg"}
MAX_SIDE = 2000  # keep Bedrock/Textract payloads small


@dataclass
class Page:
    source_file: str
    page: int          # 1-based page number within the source file
    png: bytes


def _to_png(img: Image.Image) -> bytes:
    img = img.convert("RGB")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    b = io.BytesIO()
    img.save(b, "PNG", optimize=True)
    return b.getvalue()


def load_bytes(name: str, data: bytes) -> list[Page]:
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        doc = fitz.open(stream=data, filetype="pdf")
        pages = []
        for i, p in enumerate(doc):
            pix = p.get_pixmap(dpi=RENDER_DPI)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pages.append(Page(name, i + 1, _to_png(img)))
        return pages
    if ext in (".png", ".jpg", ".jpeg"):
        return [Page(name, 1, _to_png(Image.open(io.BytesIO(data))))]
    raise ValueError(f"unsupported file type: {name}")


def load_path(path: str | Path) -> list[Page]:
    path = Path(path)
    files = sorted(p for p in path.iterdir() if p.suffix.lower() in SUPPORTED) if path.is_dir() else [path]
    pages = []
    for f in files:
        pages += load_bytes(f.name, f.read_bytes())
    return pages
