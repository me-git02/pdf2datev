"""Read raw transactions from a bank statement PDF.

Strategies, tried in order:

1. Positional parsing of the text layer, page by page:
   a. a table whose column header is recognised (see :mod:`pdf2datev.pdf.table`);
   b. without a header: a heuristic for "Soll | Haben" columns.
2. Line-by-line regex parsing of the extracted text.
3. OCR, if the PDF has (almost) no text layer, i.e. is a scan.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

import pdfplumber

from pdf2datev.models import RawTransaction, Side, Word
from pdf2datev.normalize import AMOUNT_RE, DATE_RE, parse_amount, side_from_sign
from pdf2datev.pdf import ocr
from pdf2datev.pdf.layout import (
    AmountHit,
    Line,
    Metrics,
    TransactionCollector,
    amounts_in,
    cluster_lines,
    count_amount_tokens,
    line_text,
)
from pdf2datev.pdf.table import TableLayout, parse_table_page

log = logging.getLogger(__name__)

#: Letters closer than this share of the font size belong to the same word.
WORD_GAP_RATIO = 0.15
#: Fewer amount tokens than this in the whole text layer suggests a scanned PDF.
MIN_AMOUNT_TOKENS_FOR_TEXT_LAYER = 6
#: OCR output with fewer transactions than this is considered unusable.
MIN_OCR_TRANSACTIONS = 8
#: Default debit/credit boundary when the amounts do not reveal two columns.
DEFAULT_SPLIT_RATIO = 0.78
MIN_COLUMN_GAP = 25.0
MIN_AMOUNTS_FOR_SPLIT = 6

_LABEL_WORDS = frozenset({"soll", "haben", "saldo", "vorgang"})
_TEXT_LINE_HEADER_WORDS = ("verbuchungsdatum", "beschreibung", "betrag")
_STRICT_LINE_RE = re.compile(
    r"^\s*(?P<desc>.*?)\s+(?P<date>\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)\s+"
    r"(?P<amount>-?(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2}-?)\s*$"
)


def parse_statement(path: Path, *, use_ocr: bool = True, ocr_resolution: int | None = None) -> list[RawTransaction]:
    with pdfplumber.open(path) as pdf:
        transactions: list[RawTransaction] = []
        amount_tokens = 0
        layout: TableLayout | None = None
        for page in pdf.pages:
            words = _page_words(page)
            amount_tokens += count_amount_tokens(words)
            page_transactions, layout = parse_page(words, page.width, layout)
            transactions.extend(page_transactions)
        if transactions:
            return transactions

        transactions = parse_text_lines(page.extract_text() or "" for page in pdf.pages)
        if transactions or not use_ocr or amount_tokens >= MIN_AMOUNT_TOKENS_FOR_TEXT_LAYER:
            return transactions

        log.info("No text layer found, trying OCR (this can take a while).")
        try:
            transactions = ocr.parse_pdf(pdf, resolution=ocr_resolution)
        except ocr.OcrUnavailableError as exc:
            log.warning("%s", exc)
            return []
        if len(transactions) < MIN_OCR_TRANSACTIONS:
            log.warning("OCR found only %d transactions, ignoring the result.", len(transactions))
            return []
        return transactions


def _page_words(page: pdfplumber.page.Page) -> list[Word]:
    # Many PDFs contain no space characters; words are only separated by distance. A
    # gap relative to the font size (instead of pdfplumber's fixed 3 pt) splits them
    # correctly for small and large fonts alike.
    options = {"use_text_flow": True, "keep_blank_chars": False}
    try:
        raw = page.extract_words(x_tolerance_ratio=WORD_GAP_RATIO, **options) or []
    except TypeError:  # pdfplumber versions without x_tolerance_ratio
        raw = page.extract_words(**options) or []
    return [
        Word(str(w.get("text") or ""), float(w["x0"]), float(w["top"]), float(w["x1"]), float(w["bottom"])) for w in raw
    ]


def parse_page(
    words: Sequence[Word], page_width: float, previous: TableLayout | None = None
) -> tuple[list[RawTransaction], TableLayout | None]:
    """Transactions of one page and the table layout to carry over to the next page."""
    metrics = Metrics.of(words)
    lines = cluster_lines(words, metrics.line_tolerance)
    transactions, layout = parse_table_page(lines, metrics, previous)
    if layout is None:  # no table header recognised (yet)
        transactions = parse_debit_credit_columns(lines, split_x=debit_credit_split(words, page_width))
    return transactions, layout


# --- b. No header: Soll | Haben heuristic ---------------------------------------------------


def debit_credit_split(words: Sequence[Word], page_width: float) -> float:
    """x position between the Soll and Haben amount columns.

    Uses the widest horizontal gap between amount positions if it is wide enough,
    otherwise the median position, otherwise a fixed share of the page width.
    """
    xs = sorted(w.x0 for w in words if AMOUNT_RE.search(w.text.strip().strip("-")))
    if len(xs) < MIN_AMOUNTS_FOR_SPLIT:
        return page_width * DEFAULT_SPLIT_RATIO
    i = max(range(len(xs) - 1), key=lambda k: xs[k + 1] - xs[k])
    if xs[i + 1] - xs[i] >= MIN_COLUMN_GAP:
        return (xs[i] + xs[i + 1]) / 2
    return xs[len(xs) // 2]


def _side_by_position(hit: AmountHit, split_x: float) -> Side:
    return Side.CREDIT if hit.x0 >= split_x else Side.DEBIT


def _description(line: Line) -> str:
    """Words of a line without dates, amounts and column labels."""
    kept = []
    for word in line:
        token = word.text.strip()
        if not token or token.lower() in _LABEL_WORDS:
            continue
        if DATE_RE.fullmatch(token) or AMOUNT_RE.fullmatch(token.strip("-")):
            continue
        kept.append(token)
    return " ".join(kept)


def _header_end(lines: Sequence[Line]) -> int:
    for index, line in enumerate(lines):
        low = line_text(line).lower()
        if "vorgang" in low and ("soll" in low or "haben" in low):
            return index + 1
    return 0


def _date_segments(text: str, dates: list[re.Match[str]]) -> list[tuple[str, str]]:
    """Split a line with several dates into (date, text) segments.

    A date directly followed by another date ("01.02. 01.02. Lastschrift ...") is a
    booking date plus value date of the same transaction, not two transactions.
    """
    segments: list[tuple[str, str]] = []
    for i, m in enumerate(dates):
        end = dates[i + 1].start() if i + 1 < len(dates) else len(text)
        segment_text = re.sub(r"\s+", " ", AMOUNT_RE.sub(" ", text[m.end() : end])).strip(" .\t")
        if segments and not segments[-1][1]:
            segments[-1] = (segments[-1][0], segment_text)
        else:
            segments.append((m.group(0), segment_text))
    return segments


def parse_debit_credit_columns(lines: Sequence[Line], split_x: float) -> list[RawTransaction]:
    collector = TransactionCollector()
    current_date: str | None = None
    for line in lines[_header_end(lines) :]:
        full_text = line_text(line)
        if not full_text:
            continue
        amounts = amounts_in(line)
        dates = list(DATE_RE.finditer(full_text))

        if len(dates) >= 2:
            for i, (date_raw, text) in enumerate(_date_segments(full_text, dates)):
                transaction = RawTransaction(date_raw, text)
                if i < len(amounts):
                    transaction.set_amount(amounts[i].value, _side_by_position(amounts[i], split_x))
                collector.start(transaction)
            current_date = dates[-1].group(0)
            continue

        if dates:
            current_date = dates[0].group(0)
        description = _description(line)
        if not amounts:
            collector.add_text(description)
            continue

        chosen = amounts[-1]  # right-most amount; a balance column would be further left
        side = _side_by_position(chosen, split_x)
        if collector.awaiting_amount and (not dates or not description):
            # The amount belongs to the transaction started on a previous line.
            collector.current.set_amount(chosen.value, side)
            collector.current.add_text(description)
            continue
        collector.start(RawTransaction(current_date, description, chosen.value, side))
    return collector.flush()


# --- 2. Plain text lines ------------------------------------------------------------------


def parse_text_lines(page_texts: Iterable[str]) -> list[RawTransaction]:
    """Fallback: every text line with a date and an amount is a transaction."""
    transactions = []
    for page_text in page_texts:
        for raw_line in page_text.splitlines():
            line = raw_line.strip()
            if not line or any(word in line.lower() for word in _TEXT_LINE_HEADER_WORDS):
                continue
            if m := _STRICT_LINE_RE.match(line):
                date_raw, amount_token, description = m["date"], m["amount"], m["desc"].strip()
            else:
                date_match = DATE_RE.search(line)
                amount_matches = list(AMOUNT_RE.finditer(line))
                if not date_match or not amount_matches:
                    continue
                date_raw, amount_token = date_match.group(0), amount_matches[-1].group(0)
                description = AMOUNT_RE.sub(" ", line.replace(date_raw, " ", 1))
                description = re.sub(r"\s+", " ", description).strip()
            amount = parse_amount(amount_token)
            if amount is not None:
                transactions.append(RawTransaction(date_raw, description, amount, side_from_sign(amount_token)))
    return transactions
