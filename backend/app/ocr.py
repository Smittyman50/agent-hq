"""OCR for scanned PDFs, with Tesseract.

`to_markdown` (knowledge.py) calls `pdf_text` for every PDF, so the knowledge library
(uploads, Git, shares, web pages) and chat attachments all get it. A page counts as
scanned when it holds an image and next to no text layer. A PDF without such pages is
left to markitdown, unchanged. Otherwise the text is rebuilt page by page under
"## Page N" headings: the text layer where there is one, OCR where there isn't. The
headings also let chunk_markdown cut sections and citations point at a page.

Pages are rendered with pypdfium2 (installed with markitdown[pdf]) and read by the
`tesseract` binary (apt install tesseract-ocr, plus tesseract-ocr-<lang> for languages
other than English). Settings: AHQ_OCR (auto / on / off), AHQ_OCR_LANGUAGES (eng, or
eng+deu), AHQ_OCR_DPI (300) and AHQ_OCR_MAX_PAGES (200).
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
from dataclasses import dataclass

from .config import settings

log = logging.getLogger(__name__)

# A page with fewer text-layer characters than this, and an image on it, is OCR'd.
MIN_PAGE_CHARS = 25
PAGE_TIMEOUT = 120  # seconds for Tesseract on one page


class OcrUnavailable(RuntimeError):
    pass


@dataclass
class PdfPages:
    texts: list[str]      # text layer, per page
    scanned: list[int]    # indexes of pages that need OCR


def tesseract() -> str | None:
    """Path of the tesseract binary, or None if OCR is off or not installed."""
    mode = settings.ocr_mode
    if mode == "off":
        return None
    path = shutil.which("tesseract")
    if not path and mode == "on":
        log.warning("AHQ_OCR=on but tesseract is not installed")
    return path


def _has_image(page) -> bool:
    import pypdfium2.raw as pdfium_c

    for obj in page.get_objects(max_depth=3):
        if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
            return True
    return False


def inspect(data: bytes) -> PdfPages:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        texts, scanned = [], []
        for i in range(len(pdf)):
            page = pdf[i]
            try:
                tp = page.get_textpage()
                text = (tp.get_text_bounded() or "").replace("\r\n", "\n").strip()
                tp.close()
                texts.append(text)
                if len("".join(text.split())) < MIN_PAGE_CHARS and _has_image(page):
                    scanned.append(i)
            finally:
                page.close()
        return PdfPages(texts, scanned)
    finally:
        pdf.close()


def _ocr_page(binary: str, png: bytes) -> str:
    r = subprocess.run([binary, "stdin", "stdout", "-l", settings.ocr_languages, "--psm", "3"],
                       input=png, capture_output=True, timeout=PAGE_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"tesseract failed: {r.stderr.decode('utf-8', 'ignore').strip()[:200]}")
    return _tidy(r.stdout.decode("utf-8", "ignore"))


def _tidy(text: str) -> str:
    """Tesseract output as paragraphs: drop form feeds, join wrapped lines, keep blank lines."""
    text = text.replace("\f", "").replace("\r", "")
    paras = []
    for block in text.split("\n\n"):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        out = lines[0]
        for ln in lines[1:]:
            out = out[:-1] + ln if out.endswith("-") and out[-2:-1].isalpha() else f"{out} {ln}"
        paras.append(out)
    return "\n\n".join(paras)


def ocr_pages(data: bytes, pages: list[int], binary: str) -> dict[int, str]:
    import pypdfium2 as pdfium

    scale = settings.ocr_dpi / 72
    out: dict[int, str] = {}
    pdf = pdfium.PdfDocument(data)
    try:
        for i in pages:
            page = pdf[i]
            try:
                img = page.render(scale=scale, grayscale=True).to_pil()
            finally:
                page.close()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            out[i] = _ocr_page(binary, buf.getvalue())
    finally:
        pdf.close()
    return out


def pdf_text(data: bytes) -> str | None:
    """Text for a PDF with scanned pages (OCR'd), or None to use markitdown as usual.

    Raises OcrUnavailable when every page is scanned and OCR is off or not installed,
    so the caller can say so instead of storing an empty document.
    """
    try:
        info = inspect(data)
    except Exception as e:  # pdfium can't open it: let markitdown try and report
        log.info("ocr: couldn't inspect PDF (%s)", e)
        return None
    if not info.scanned:
        return None
    binary = tesseract()
    if not binary:
        if len(info.scanned) == len(info.texts) or not any(info.texts):
            why = "OCR is turned off (AHQ_OCR=off)" if settings.ocr_mode == "off" \
                else "Tesseract isn't installed"
            raise OcrUnavailable(f"scanned PDF with no text layer, and {why}")
        return None  # some text: markitdown's is better than nothing
    todo = info.scanned[: settings.ocr_max_pages]
    if len(info.scanned) > len(todo):
        log.warning("ocr: %d scanned pages, reading the first %d (AHQ_OCR_MAX_PAGES)",
                    len(info.scanned), len(todo))
    read = ocr_pages(data, todo, binary)
    log.info("ocr: read %d of %d pages", len(read), len(info.texts))
    parts = []
    for i, text in enumerate(info.texts):
        body = read.get(i, text).strip()
        if body:
            parts.append(f"## Page {i + 1}\n\n{body}")
    return "\n\n".join(parts)
