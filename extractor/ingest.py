"""Turn input files (PDF / JPG / PNG) into page images, hashing every input and page."""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf as fitz
from PIL import Image

from .config import RENDER_DPI

SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg"}
MAX_SIDE = 2000  # keep Bedrock/Textract payloads small


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@dataclass
class Page:
    source_file: str
    page: int          # 1-based physical page index within the source file
    png: bytes
    source_sha256: str = ""
    sha256: str = ""   # of the rendered PNG sent to AWS
    rotation_applied: int = 0
    original_sha256: str = ""

    def __post_init__(self):
        self.sha256 = self.sha256 or sha256(self.png)
        self.original_sha256 = self.original_sha256 or self.sha256


def to_png(img: Image.Image) -> bytes:
    img = img.convert("RGB")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    b = io.BytesIO()
    img.save(b, "PNG", optimize=True)
    return b.getvalue()


def rotate_png(png: bytes, clockwise_degrees: int) -> bytes:
    """Rotate so the page becomes upright (PIL rotates counter-clockwise)."""
    if clockwise_degrees % 360 == 0:
        return png
    return to_png(Image.open(io.BytesIO(png)).rotate(-clockwise_degrees, expand=True))


def load_bytes(name: str, data: bytes) -> list[Page]:
    ext = Path(name).suffix.lower()
    src = sha256(data)
    if ext == ".pdf":
        doc = fitz.open(stream=data, filetype="pdf")
        pages = []
        for i, p in enumerate(doc):
            pix = p.get_pixmap(dpi=RENDER_DPI)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pages.append(Page(name, i + 1, to_png(img), source_sha256=src))
        return pages
    if ext in (".png", ".jpg", ".jpeg"):
        return [Page(name, 1, to_png(Image.open(io.BytesIO(data))), source_sha256=src)]
    raise ValueError(f"unsupported file type: {name}")


def _files(paths) -> list[Path]:
    out = []
    for path in paths:
        path = Path(path)
        if path.is_dir():
            out += sorted(p for p in path.iterdir() if p.suffix.lower() in SUPPORTED)
        else:
            out.append(path)
    return out


def load_inputs(paths) -> tuple[list[Page], list[dict]]:
    if isinstance(paths, (str, Path)):
        paths = [paths]
    pages, inputs = [], []
    for f in _files(paths):
        data = f.read_bytes()
        inputs.append({"name": f.name, "bytes": len(data), "sha256": sha256(data)})
        pages += load_bytes(f.name, data)
    return pages, inputs


def load_path(path) -> list[Page]:
    return load_inputs(path)[0]
