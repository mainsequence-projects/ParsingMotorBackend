"""Turn input files (PDF / JPG / PNG) into page images, hashing every input and page."""
from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf as fitz
from PIL import Image

from .config import RENDER_DPI

SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg"}
MAX_SIDE = 2000  # keep Bedrock/Textract payloads small
MAX_FILES = int(os.getenv("EXTRACTOR_MAX_FILES", "12"))
MAX_PAGES = int(os.getenv("EXTRACTOR_MAX_PAGES", "20"))        # per package: cost + Lambda time guard
MAX_FILE_MB = float(os.getenv("EXTRACTOR_MAX_FILE_MB", "15"))


class InputError(ValueError):
    """The upload itself is unusable. `status` is the HTTP code the API returns."""

    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status


def _kind(name: str, data: bytes) -> str:
    """Trust the bytes, not only the extension ('blob' from a browser, renamed files)."""
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff":
        return "image"
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED:
        raise InputError(f"unsupported file type: {name} (PDF, PNG or JPG only)", 415)
    raise InputError(f"{name}: content is not a valid {ext[1:].upper()} file", 422)


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
    if not data:
        raise InputError(f"empty file: {name}", 400)
    if len(data) > MAX_FILE_MB * 1024 * 1024:
        raise InputError(f"file too large ({MAX_FILE_MB:g} MB max): {name}", 413)
    src = sha256(data)
    kind = _kind(name, data)
    if kind == "pdf":
        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as e:
            raise InputError(f"{name}: cannot open PDF ({e})", 422) from e
        if doc.needs_pass and not doc.authenticate(""):
            raise InputError(f"{name}: PDF is password-protected; send an unencrypted copy", 422)
        if doc.page_count == 0:
            raise InputError(f"{name}: PDF has no pages", 422)
        if doc.page_count > MAX_PAGES:
            raise InputError(f"{name}: {doc.page_count} pages > {MAX_PAGES} per package", 413)
        pages = []
        for i, p in enumerate(doc):
            pix = p.get_pixmap(dpi=RENDER_DPI)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pages.append(Page(name, i + 1, to_png(img), source_sha256=src))
        return pages
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Image.DecompressionBombError as e:
        raise InputError(f"{name}: image too large ({e})", 413) from e
    except Exception as e:
        raise InputError(f"{name}: cannot read image ({type(e).__name__})", 422) from e
    return [Page(name, 1, to_png(img), source_sha256=src)]


def check_package(pages: list, n_files: int) -> None:
    if n_files == 0:
        raise InputError("upload at least one PDF/PNG/JPG", 400)
    if n_files > MAX_FILES:
        raise InputError(f"{n_files} files > {MAX_FILES} per package", 413)
    if len(pages) > MAX_PAGES:
        raise InputError(f"{len(pages)} pages > {MAX_PAGES} per package", 413)


def safe_name(name: str | None, seen: set[str] | None = None) -> str:
    """Basename only, no control chars, max 120 chars; duplicates get '__2', '__3'... .

    Two uploads called 'scan.pdf' must stay two different sources: the splitter groups pages
    by source_file, so equal names would merge two different documents into one.
    """
    raw = str(name or "upload.bin").replace("\\", "/").split("/")[-1]
    raw = "".join(ch for ch in raw if ch.isprintable()).strip() or "upload.bin"
    stem, suffix = Path(raw).stem[:100] or "upload", Path(raw).suffix[:10]
    out, n = stem + suffix, 1
    if seen is not None:
        while out in seen:
            n += 1
            out = f"{stem}__{n}{suffix}"
        seen.add(out)
    return out


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
    pages, inputs, seen = [], [], set()
    for f in _files(paths):
        data = f.read_bytes()
        name = safe_name(f.name, seen)
        inputs.append({"name": name, "original_name": f.name, "bytes": len(data), "sha256": sha256(data)})
        pages += load_bytes(name, data)
        check_package(pages, len(inputs))
    check_package(pages, len(inputs))
    return pages, inputs


def load_path(path) -> list[Page]:
    return load_inputs(path)[0]
