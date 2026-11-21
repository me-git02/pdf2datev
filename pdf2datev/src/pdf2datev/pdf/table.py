"""Header-driven parsing of statement tables.

Instead of fixed coordinates, the column header line ("Buchungstag | Vorgang |
Betrag", "Datum | Wert | Soll | Haben", ...) tells which column is which. All
distances are relative to the text height, so fonts, sizes and spacing do not
matter. Rules that hold for every bank layout:

* an entry's amount is the right-most number on its line (a balance column is
  recognised by its header and ignored);
* numbers inside the description ("Ursprungsbetrag 56,00 PLN") are text;
* the side comes from the Soll/Haben column, or from the sign or an S/H mark.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from pdf2datev.models import RawTransaction, Side, Word
from pdf2datev.normalize import AMOUNT_RE, DATE_RE, OCR_AMOUNT_RE, parse_amount, parse_ocr_amount
from pdf2datev.pdf.layout import Line, Metrics, TransactionCollector, line_text
from pdf2datev.textclean import looks_like_summary


class Role(str, Enum):
    DATE = "date"
    VALUE_DATE = "value_date"
    DESCRIPTION = "description"
    AMOUNT = "amount"  # signed, or with an S/H mark
    DEBIT = "debit"  # Soll column
    CREDIT = "credit"  # Haben column
    BALANCE = "balance"  # running balance, not a transaction amount


TEXT_ROLES = (Role.DATE, Role.VALUE_DATE, Role.DESCRIPTION)
AMOUNT_ROLES = (Role.AMOUNT, Role.DEBIT, Role.CREDIT, Role.BALANCE)
DATE_ROLES = (Role.DATE, Role.VALUE_DATE)

# Header words of German banks. The first role whose keyword occurs in a header cell wins,
# so "Buchung / Verwendungszweck" is a description and "Valutadatum" a value date.
_HEADER_KEYWORDS: tuple[tuple[Role, tuple[str, ...]], ...] = (
    (
        Role.DESCRIPTION,
        (
            "beschreib", "vorgang", "buchungstext", "verwendungszweck", "erläuterung", "erlaeuterung", "erlauterung",
            "umsatzart", "empfänger", "auftraggeber", "text",
        ),
    ),
    (Role.BALANCE, ("saldo", "kontostand")),
    (Role.DEBIT, ("soll", "belastung", "ausgang")),
    (Role.CREDIT, ("haben", "gutschrift", "eingang")),
    (Role.AMOUNT, ("betrag", "umsatz")),
    (Role.VALUE_DATE, ("valuta", "wert")),
    (Role.DATE, ("datum", "buchung", "tag", "ver.dat")),
)  # fmt: skip

#: Lines after which the transaction table of a page ends.
_TABLE_END = ("neuer kontostand", "neuer saldo", "endsaldo", "saldo neu")
#: Marks next to an amount: currency, sign, or S (Soll) / H (Haben).
_MARKERS = {"€", "eur", "s", "h", "+", "-", "−"}
_DEBIT_MARKS = {"s", "-", "−"}
_CREDIT_MARKS = {"h", "+"}
#: A header needs at least date, text and amount columns, so a text line that happens
#: to contain "Rechnungsdatum" and "Betrag" is not mistaken for one.
_MIN_HEADER_COLUMNS = 3


@dataclass(frozen=True, slots=True)
class AmountFormat:
    pattern: object  # compiled regex with fullmatch()
    parse: Callable[[str], Decimal | None]

    def value(self, token: str) -> Decimal | None:
        core = token.strip().replace("−", "-")
        if core.lower().endswith("eur"):
            core = core[:-3]
        core = core.strip(" €+-")
        return self.parse(core) if self.pattern.fullmatch(core) else None


TEXT_AMOUNTS = AmountFormat(AMOUNT_RE, parse_amount)
OCR_AMOUNTS = AmountFormat(OCR_AMOUNT_RE, parse_ocr_amount)


@dataclass(frozen=True, slots=True)
class HeaderCell:
    role: Role
    x0: float
    x1: float

    def distance(self, word: Word) -> float:
        """How well a value lines up under this header: left, right or centre aligned."""
        return min(
            abs(word.x1 - self.x1),
            abs(word.x0 - self.x0),
            abs((word.x0 + word.x1) / 2 - (self.x0 + self.x1) / 2),
        )


@dataclass(frozen=True, slots=True)
class TableLayout:
    columns: tuple[HeaderCell, ...]  # left to right, one per role

    def by_roles(self, roles: Sequence[Role]) -> list[HeaderCell]:
        return [c for c in self.columns if c.role in roles]


def _role_of(cell_text: str) -> Role | None:
    low = cell_text.lower()
    for role, keywords in _HEADER_KEYWORDS:
        if any(keyword in low for keyword in keywords):
            return role
    return None


def _cells(line: Line, gap: float) -> list[list[Word]]:
    """Split a line into cells at gaps wider than a normal space."""
    cells: list[list[Word]] = []
    for word in line:
        if cells and word.x0 - cells[-1][-1].x1 <= gap:
            cells[-1].append(word)
        else:
            cells.append([word])
    return cells


def header_layout(line: Line, metrics: Metrics, amounts: AmountFormat = TEXT_AMOUNTS) -> TableLayout | None:
    """The table layout if ``line`` is a column header line, otherwise None."""
    text = line_text(line)
    if not text or DATE_RE.search(text) or any(amounts.value(w.text) is not None for w in line):
        return None
    columns: dict[Role, HeaderCell] = {}
    for cell in _cells(line, metrics.word_gap):
        role = _role_of(line_text(cell))
        if role is not None and role not in columns:
            columns[role] = HeaderCell(role, cell[0].x0, cell[-1].x1)
    has_date = any(r in columns for r in DATE_ROLES)
    has_amount = any(r in columns for r in (Role.AMOUNT, Role.DEBIT, Role.CREDIT))
    if not (has_date and has_amount) or len(columns) < _MIN_HEADER_COLUMNS:
        return None
    return TableLayout(tuple(sorted(columns.values(), key=lambda c: c.x0)))


@dataclass(slots=True)
class _ParsedLine:
    date: str | None
    description: str
    amount: Decimal | None
    side: Side | None


def _side_from_marks(line: Line, index: int) -> Side | None:
    """Sign in the amount ("-49,99", "49,99-", "+1,00"), a mark after it ("49,99 S")
    or a detached sign before it ("- 49,99"); None if the amount is unmarked."""
    token = line[index].text.strip().replace("−", "-").removesuffix("€").strip()
    if token.startswith("-") or token.endswith("-"):
        return Side.DEBIT
    if token.startswith("+"):
        return Side.CREDIT
    after = line[index + 1].text.strip().lower() if index + 1 < len(line) else ""
    before = line[index - 1].text.strip() if index > 0 else ""
    if after in _DEBIT_MARKS or before in ("-", "−"):
        return Side.DEBIT
    if after in _CREDIT_MARKS or before == "+":
        return Side.CREDIT
    return None


def _trailing_amounts(
    line: Line, layout: TableLayout, metrics: Metrics, amounts: AmountFormat
) -> tuple[list[tuple[int, Decimal, Role]], int]:
    """Numbers at the end of the line (only marks after them) under an amount header.

    Returns (word index, value, column role) per amount, right to left, and the
    number of leading words that are text.
    """
    amount_columns = layout.by_roles(AMOUNT_ROLES)
    leftmost = min(c.x0 for c in amount_columns) - metrics.column_tolerance
    hits: list[tuple[int, Decimal, Role]] = []
    for i in range(len(line) - 1, -1, -1):
        token = line[i].text.strip()
        if token.lower() in _MARKERS:
            continue
        value = amounts.value(token)
        if value is None or line[i].x1 < leftmost:
            break
        hits.append((i, value, min(amount_columns, key=lambda c: c.distance(line[i])).role))
    if not hits:
        return [], len(line)
    end = hits[-1][0]
    while end > 0 and line[end - 1].text.strip().lower() in _MARKERS:
        end -= 1  # a detached sign before the amount
    return hits, end


def _dates_and_text(words: Sequence[Word], layout: TableLayout, metrics: Metrics) -> tuple[str | None, str]:
    """Booking date (or value date) and description; each word belongs to the right-most
    text column that starts left of it."""
    text_columns = layout.by_roles(TEXT_ROLES)
    dates: dict[Role, str] = {}
    description = []
    for word in words:
        column = text_columns[0]
        for candidate in text_columns[1:]:
            if word.x0 < candidate.x0 - metrics.column_tolerance:
                break
            column = candidate
        token = word.text.strip()
        if column.role in DATE_ROLES and DATE_RE.fullmatch(token.rstrip(".")):
            dates.setdefault(column.role, token.rstrip("."))
        else:
            description.append(token)
    return dates.get(Role.DATE) or dates.get(Role.VALUE_DATE), " ".join(description)


def _parse_line(line: Line, layout: TableLayout, metrics: Metrics, amounts: AmountFormat) -> _ParsedLine:
    hits, text_end = _trailing_amounts(line, layout, metrics, amounts)
    date, description = _dates_and_text(line[:text_end], layout, metrics)
    entry = next(((i, v, r) for i, v, r in hits if r is not Role.BALANCE), None)
    if entry is None:
        return _ParsedLine(date, description, None, None)
    index, value, role = entry
    side = {Role.DEBIT: Side.DEBIT, Role.CREDIT: Side.CREDIT}.get(role) or _side_from_marks(line, index)
    return _ParsedLine(date, description, value, side)


def parse_table_page(
    lines: Sequence[Line],
    metrics: Metrics,
    previous: TableLayout | None,
    amounts: AmountFormat = TEXT_AMOUNTS,
) -> tuple[list[RawTransaction], TableLayout | None]:
    """Transactions of one page, and the layout in effect at its end.

    A page without its own header continues the table of the previous page.
    An unmarked amount is a credit - unless the page marks credits with "+": then
    the sign was lost (OCR) and the side is left open for :mod:`pdf2datev.bookings`.
    """
    headers = [header_layout(line, metrics, amounts) for line in lines]
    credits_marked = any(w.text.strip().startswith("+") and amounts.value(w.text) for line in lines for w in line)
    layout = previous
    active = previous is not None and not any(headers)
    collector = TransactionCollector()
    current_date: str | None = None
    previous_top: float | None = None

    for line, header in zip(lines, headers, strict=True):
        top = min(w.top for w in line)
        if header is not None:
            layout, active, previous_top = header, True, top
            continue
        if not active or layout is None:
            continue
        if previous_top is not None and top - previous_top > metrics.block_gap:
            collector.end_block()  # text far below the last entry (e.g. the page footer) is not its continuation
        previous_top = top
        parsed = _parse_line(line, layout, metrics, amounts)
        if parsed.amount is not None and parsed.side is None and not credits_marked:
            parsed.side = Side.CREDIT
        current_date = parsed.date or current_date
        is_summary = looks_like_summary(parsed.description)

        if parsed.amount is not None:
            if parsed.date is None and collector.awaiting_amount and not is_summary:
                # Amount on a later line than the date and text of its entry.
                collector.current.set_amount(parsed.amount, parsed.side)
                collector.current.add_text(parsed.description)
            else:
                collector.start(RawTransaction(current_date, parsed.description, parsed.amount, parsed.side))
        elif parsed.date is not None:
            # A dated line without amount: an entry whose amount follows, or the value-date
            # line of the previous entry (then its text is merged back, see TransactionCollector).
            collector.start(RawTransaction(parsed.date, parsed.description))
        else:
            collector.add_text(parsed.description)

        if is_summary and any(end in parsed.description.lower() for end in _TABLE_END):
            active = False  # the rest of the page is footer text
            collector.flush()

    return collector.flush(), layout
