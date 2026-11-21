"""OCR fallback for scanned statements. Needs the optional ``easyocr`` dependency."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import Any

from pdf2datev.models import RawTransaction, Side, Word
from pdf2datev.normalize import DATE_RE, OCR_AMOUNT_RE, parse_ocr_amount
from pdf2datev.pdf.layout import Line, Metrics, TransactionCollector, cluster_lines, line_text
from pdf2datev.pdf.ocr_text import GLUED_DATE, Kind, repair_token
from pdf2datev.pdf.table import OCR_AMOUNTS, TableLayout, parse_table_page

DEFAULT_DPI, MIN_DPI, MAX_DPI = 200, 200, 400
#: Text below this confidence is dropped (mostly specks and stamps on the scan).
MIN_CONFIDENCE = 0.2
#: Amounts and dates are kept down to this confidence; repair_token validates their shape.
MIN_VALUE_CONFIDENCE = 0.05
LINE_SMOOTHING = 0.3  # OCR boxes of one line wobble vertically
_LABEL_WORDS = frozenset({"soll", "haben", "saldo", "vorgang"})

# Column boundaries for Soll/Haben scans, as a share of the page width.
DATE_COLUMN_END = 0.22
AMOUNT_COLUMN_START = 0.70
CREDIT_COLUMN_START = 0.84


class OcrUnavailableError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _reader() -> Any:
    """The EasyOCR model takes seconds to load, so it is created once per run."""
    try:
        import easyocr  # noqa: PLC0415 - optional dependency, loaded only when needed
    except ImportError as exc:
        raise OcrUnavailableError("OCR needs easyocr: pip install 'pdf2datev[ocr]'") from exc
    try:
        return easyocr.Reader(["de", "en"], gpu=False, verbose=False)
    except TypeError:  # older easyocr versions have no "verbose"
        return easyocr.Reader(["de", "en"], gpu=False)


def scan_resolution(page: Any, default: int) -> int:
    """The resolution of the scanned image on a page, so OCR sees it without resampling."""
    widths = [img["srcsize"][0] for img in page.images if img.get("srcsize")]
    if not widths or not page.width:
        return default
    native = round(max(widths) / (float(page.width) / 72))
    return max(MIN_DPI, min(MAX_DPI, native))


def parse_pdf(pdf: Any, resolution: int | None = None) -> list[RawTransaction]:
    """Run OCR on every page of an open pdfplumber document.

    Without an explicit ``resolution``, each page is rendered at the resolution of
    its scan (e.g. 300 dpi), which recognises noticeably more than downsampling.
    """
    reader = _reader()  # raises OcrUnavailableError before numpy is needed
    import numpy as np  # noqa: PLC0415 - installed together with easyocr

    transactions = []
    layout: TableLayout | None = None
    for page in pdf.pages:
        dpi = resolution or scan_resolution(page, DEFAULT_DPI)
        image = page.to_image(resolution=dpi).original
        results = reader.readtext(np.array(image), detail=1, paragraph=False)
        page_transactions, layout = parse_page(results_to_words(results), image.size[0], layout)
        transactions.extend(page_transactions)
    return transactions


def results_to_words(results: Iterable[tuple[Sequence[Sequence[float]], str, float | None]]) -> list[Word]:
    """OCR boxes -> words with repaired amounts and dates.

    EasyOCR returns phrases ("J03.02.2026 Überweisung"); they are split into words
    whose positions are estimated from the character offsets, so that a date and
    the text next to it land in their own columns. Low-confidence text is dropped,
    but low-confidence amounts and dates are kept: on poor scans the numbers are
    exactly what OCR is least sure about.
    """
    words = []
    for bbox, text, confidence in results:
        xs, ys = [float(p[0]) for p in bbox], [float(p[1]) for p in bbox]
        reliable = confidence is None or confidence >= MIN_CONFIDENCE
        for word, kind in _split_box(str(text), min(xs), min(ys), max(xs), max(ys)):
            if reliable or (kind is not None and confidence >= MIN_VALUE_CONFIDENCE):
                words.append(word)
    return words


def _split_box(text: str, x0: float, top: float, x1: float, bottom: float) -> list[tuple[Word, Kind | None]]:
    text = GLUED_DATE.sub(lambda m: f" {m.group(0)} ", text).strip()
    char_width = (x1 - x0) / max(len(text), 1)
    words = []
    for m in re.finditer(r"\S+", text):
        repaired, kind = repair_token(m.group(0))
        words.append((Word(repaired, x0 + m.start() * char_width, top, x0 + m.end() * char_width, bottom), kind))
    return words


def parse_page(
    words: Sequence[Word], page_width: float, previous: TableLayout | None = None
) -> tuple[list[RawTransaction], TableLayout | None]:
    metrics = Metrics.of(words, default=25.0)
    lines = cluster_lines(words, metrics.line_tolerance, smoothing=LINE_SMOOTHING)
    transactions, layout = parse_table_page(lines, metrics, previous, amounts=OCR_AMOUNTS)
    if layout is None:
        transactions = _parse_fixed_columns(lines, page_width)
    return transactions, layout


def _parse_fixed_columns(lines: Sequence[Line], page_width: float) -> list[RawTransaction]:
    """Soll/Haben scans: columns are found at fixed shares of the page width."""
    date_end = page_width * DATE_COLUMN_END
    amount_start = page_width * AMOUNT_COLUMN_START
    credit_start = page_width * CREDIT_COLUMN_START

    start = 0
    for index, line in enumerate(lines):
        low = line_text(line).lower()
        if "vorgang" in low and ("soll" in low or "haben" in low):
            start = index + 1
            break

    collector = TransactionCollector()
    current_date: str | None = None
    for line in lines[start:]:
        full_text = line_text(line)
        if not full_text:
            continue
        if date := DATE_RE.search(full_text):
            current_date = date.group(0)
        hits: list[tuple[float, RawTransaction]] = []
        for word in line:
            if word.x0 < amount_start:
                continue
            m = OCR_AMOUNT_RE.search(word.text)
            amount = parse_ocr_amount(m.group(0)) if m else None
            if amount is not None:
                side = Side.CREDIT if word.x0 >= credit_start else Side.DEBIT
                hits.append((word.x0, RawTransaction(current_date, amount=amount, side=side)))
        description = " ".join(
            w.text.strip()
            for w in line
            if date_end < w.x0 < amount_start and w.text.strip().lower() not in _LABEL_WORDS
        ).strip()
        if not hits:
            collector.add_text(description)
            continue
        transaction = max(hits, key=lambda hit: hit[0])[1]
        transaction.text = description
        collector.start(transaction)
    return collector.flush()
