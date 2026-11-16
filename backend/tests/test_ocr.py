"""OCR for scanned PDFs (app/ocr.py), through knowledge.to_markdown's PDF path."""

import io
import shutil

import pytest

pdfium = pytest.importorskip("pypdfium2")
fpdf = pytest.importorskip("fpdf")
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app import ocr  # noqa: E402
from app.config import settings  # noqa: E402

needs_tesseract = pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")


def _scan(text: str) -> bytes:
    """A page of text as a PNG, like a scanner would make."""
    img = Image.new("L", (1700, 2200), 255)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 48)
    except OSError:
        font = ImageFont.load_default(size=48)
    ImageDraw.Draw(img).multiline_text((150, 200), text, fill=0, font=font, spacing=20)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _pdf(*pages: tuple[str, str]) -> bytes:
    """A PDF with ("text", words) pages and ("scan", words) image pages."""
    doc = fpdf.FPDF(unit="pt", format="letter")
    for kind, words in pages:
        doc.add_page()
        if kind == "text":
            doc.set_font("helvetica", size=14)
            doc.multi_cell(0, 20, words)
        else:
            doc.image(io.BytesIO(_scan(words)), x=0, y=0, w=612, h=792)
    return bytes(doc.output())


SCAN = "The UPS battery was replaced\non the rack in the garage.\nRuntime is now 42 minutes."


def test_text_pdf_is_left_to_markitdown():
    data = _pdf(("text", "Proxmox backup runbook. Snapshots run nightly at two in the morning."))
    assert ocr.inspect(data).scanned == []
    assert ocr.pdf_text(data) is None


@needs_tesseract
def test_scanned_pdf_is_read():
    from app.knowledge import to_markdown

    text = to_markdown("scan.pdf", _pdf(("scan", SCAN)))
    assert text.startswith("## Page 1")
    assert "battery was replaced" in text and "42 minutes" in text


@needs_tesseract
def test_mixed_pdf_keeps_text_layer_and_reads_scans():
    data = _pdf(("text", "Network map for the homelab. The core switch is a MikroTik."),
                ("scan", SCAN))
    assert ocr.inspect(data).scanned == [1]
    text = ocr.pdf_text(data)
    page1, page2 = text.split("## Page 2")
    assert "MikroTik" in page1 and "garage" in page2


def test_scanned_pdf_without_ocr_says_why(monkeypatch):
    monkeypatch.setattr(settings, "ocr_mode", "off")
    with pytest.raises(ocr.OcrUnavailable, match="OCR is turned off"):
        ocr.pdf_text(_pdf(("scan", SCAN)))


@needs_tesseract
def test_max_pages(monkeypatch):
    monkeypatch.setattr(settings, "ocr_max_pages", 1)
    text = ocr.pdf_text(_pdf(("scan", SCAN), ("scan", "Second page about the NAS.")))
    assert "garage" in text and "NAS" not in text


def test_tidy_joins_wrapped_lines():
    assert ocr._tidy("The bat-\ntery was\nreplaced.\n\n\fNext para.\n") == \
        "The battery was replaced.\n\nNext para."
