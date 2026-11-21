"""Page geometry: font-relative tolerances, lines, amounts and the transaction builder."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from pdf2datev.models import RawTransaction, Word
from pdf2datev.normalize import AMOUNT_RE, parse_amount

Line = list[Word]


@dataclass(frozen=True, slots=True)
class Metrics:
    """Tolerances derived from the page's own text size instead of fixed point values,
    so that small fonts with tight lines and large fonts both work."""

    text_height: float

    @classmethod
    def of(cls, words: Sequence[Word], default: float = 9.0) -> Metrics:
        heights = [w.height for w in words if w.height > 0]
        return cls(statistics.median(heights) if heights else default)

    @property
    def line_tolerance(self) -> float:
        """Max vertical offset of words on the same line (half a line of text)."""
        return max(1.0, 0.5 * self.text_height)

    @property
    def column_tolerance(self) -> float:
        """How far a cell may start left of its column header."""
        return self.text_height

    @property
    def block_gap(self) -> float:
        """A vertical gap larger than this ends a multi-line entry (about two empty lines)."""
        return 3.0 * self.text_height

    @property
    def word_gap(self) -> float:
        """Gaps wider than this separate header cells; normal spaces are narrower."""
        return 0.8 * self.text_height


def cluster_lines(words: Sequence[Word], y_tolerance: float, smoothing: float = 0.0) -> list[Line]:
    """Group words whose ``top`` lies within ``y_tolerance`` of the line's anchor.

    With ``smoothing`` > 0 the anchor follows the words of the line (useful for
    OCR boxes, which wobble vertically); with 0 it stays at the first word.
    """
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (w.top, w.x0))
    lines: list[Line] = [[ordered[0]]]
    anchor = ordered[0].top
    for word in ordered[1:]:
        if abs(word.top - anchor) <= y_tolerance:
            lines[-1].append(word)
            anchor += (word.top - anchor) * smoothing
        else:
            lines.append([word])
            anchor = word.top
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def line_text(words: Sequence[Word]) -> str:
    return " ".join(w.text.strip() for w in words).strip()


@dataclass(frozen=True, slots=True)
class AmountHit:
    value: Decimal
    x0: float


def amounts_in(words: Sequence[Word]) -> list[AmountHit]:
    """The first amount inside each word, ordered left to right."""
    hits = []
    for word in words:
        m = AMOUNT_RE.search(word.text.strip().strip("-"))
        if m and (value := parse_amount(m.group(0))) is not None:
            hits.append(AmountHit(value, word.x0))
    return sorted(hits, key=lambda h: h.x0)


def count_amount_tokens(words: Sequence[Word]) -> int:
    return sum(1 for w in words if AMOUNT_RE.fullmatch(w.text.strip()))


class TransactionCollector:
    """Holds the transaction being built and the finished ones.

    An entry that never received an amount (e.g. a value-date line printed under
    the booking line) is not a transaction of its own: its text is added to the
    transaction before it - but only within the same block of lines, so that a
    dated footer line ("Erstellt am 01.02.2026") never extends the last entry.
    """

    def __init__(self) -> None:
        self.done: list[RawTransaction] = []
        self.current: RawTransaction | None = None
        self._block_start = 0  # index in ``done`` where the current block of lines began

    @property
    def awaiting_amount(self) -> bool:
        return self.current is not None and not self.current.has_amount

    def start(self, transaction: RawTransaction) -> None:
        self.flush()
        self.current = transaction

    def add_text(self, text: str) -> None:
        if self.current is not None:
            self.current.add_text(text)

    def end_block(self) -> None:
        """Called at a large vertical gap: nothing below continues what is above."""
        self.flush()
        self._block_start = len(self.done)

    def flush(self) -> list[RawTransaction]:
        transaction, self.current = self.current, None
        if transaction is None:
            return self.done
        if transaction.has_amount:
            self.done.append(transaction)
        elif len(self.done) > self._block_start:
            self.done[-1].add_text(transaction.text)
        # else: an amount-less entry without a predecessor in its block is dropped
        return self.done
