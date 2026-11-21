"""Booking rules learned from the DATEV booking history (``Base/Book*.xlsx``).

Every history line "Buchungstext -> Gegenkonto" becomes a rule. A statement line
matches a rule if it contains the whole Buchungstext, or enough of its keywords.
Matching is done on whole words: "Bahn" does not match "Bahnhofsapotheke". A line
without a match goes to the fallback account for manual review, which is safer
than booking it to a wrong account.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

log = logging.getLogger(__name__)

TEXT_COLUMN = "Buchungstext"
ACCOUNT_COLUMN = "Gegenkonto"
#: DATEV exports put a metadata line above the header, so look a few rows down for it.
_HEADER_SEARCH_ROWS = 5

_KEYWORD_STOPWORDS = frozenset(
    {"gmbh", "ag", "kg", "mbh", "co", "und", "zahlung", "rechnung", "sepa", "lastschrift", "karte", "ec", "de", "eu"}
)
_NON_WORD_RE = re.compile(r"\W+")

#: Salary lines are matched via these words if their text matches nothing else.
SALARY_KEYWORDS = ("gehalt", "lohn", "entgelt")

_EXACT_MATCH_BONUS = 1000


def _tokens(text_lower: str) -> list[str]:
    return [t for t in _NON_WORD_RE.split(text_lower) if t]


@dataclass(frozen=True, slots=True)
class Rule:
    text: str  # Buchungstext as written in the history; used as output text
    gegenkonto: int | str
    phrase: str  # lower-cased text
    keywords: tuple[str, ...]
    _phrase_re: re.Pattern[str]

    @classmethod
    def from_history(cls, text: str, gegenkonto: int | str) -> Rule:
        phrase = text.lower()
        keywords = tuple(
            dict.fromkeys(
                w
                for w in _tokens(phrase)
                if len(w) >= 4 and w not in _KEYWORD_STOPWORDS and any(c.isalpha() for c in w)
            )
        )
        phrase_re = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)")
        return cls(text, gegenkonto, phrase, keywords, phrase_re)

    def score(self, text_lower: str, tokens: frozenset[str]) -> int | None:
        """Higher is better; None if the rule does not apply."""
        if self.phrase in text_lower and self._phrase_re.search(text_lower):
            return _EXACT_MATCH_BONUS + len(self.phrase)
        if not self.keywords:
            return None
        hits = sum(1 for kw in self.keywords if kw in tokens)
        needed = 1 if len(self.keywords) == 1 else 2
        if hits < needed:
            return None
        return hits * 10 + min(len(self.phrase), 200)


class RuleMatcher:
    """Finds the best rule for a text.

    A rule can only match a text that contains one of its keywords (a whole-phrase
    match contains all of the rule's words, a keyword match at least one keyword).
    An index keyword -> rules therefore limits scoring to a few candidates instead
    of the whole history. Rules without keywords are indexed by all their words.
    """

    def __init__(self, rules: Sequence[Rule] = ()) -> None:
        self._rules = tuple(rules)
        index: defaultdict[str, list[int]] = defaultdict(list)
        self._without_words: list[int] = []  # e.g. "---": can only match as a phrase
        for position, rule in enumerate(self._rules):
            words = set(rule.keywords or _tokens(rule.phrase))
            for word in words:
                index[word].append(position)
            if not words:
                self._without_words.append(position)
        self._index = dict(index)

    def __len__(self) -> int:
        return len(self._rules)

    def match(self, text: str) -> Rule | None:
        """Best-scoring rule for ``text``; on a tie the rule loaded first wins."""
        text_lower = text.lower().strip()
        if not text_lower or not self._rules:
            return None
        tokens = frozenset(_tokens(text_lower))
        candidates = set(self._without_words)
        for token in tokens:
            candidates.update(self._index.get(token, ()))
        best: Rule | None = None
        best_score = -1
        for position in sorted(candidates):  # load order decides ties
            rule = self._rules[position]
            score = rule.score(text_lower, tokens)
            if score is not None and score > best_score:
                best, best_score = rule, score
        return best


def find_history_files(base_dir: Path) -> list[Path]:
    """``Book*.xlsx`` files in ``base_dir``, sorted by name."""
    return sorted(
        (p for p in base_dir.iterdir() if p.is_file() and p.name.startswith("Book") and p.suffix == ".xlsx"),
        key=lambda p: p.name,
    )


def _normalize_account(value: object) -> int | str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    s = str(value).strip()
    return int(s) if s.isdigit() else s


def read_history(path: Path) -> list[tuple[str, int | str]]:
    """(Buchungstext, Gegenkonto) pairs from the first sheet of a DATEV export."""
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = workbook.worksheets[0].iter_rows(values_only=True)
        columns: tuple[int, int] | None = None
        for row in islice(rows, _HEADER_SEARCH_ROWS):
            names = [str(v).strip() if v is not None else "" for v in row]
            if TEXT_COLUMN in names and ACCOUNT_COLUMN in names:
                columns = names.index(TEXT_COLUMN), names.index(ACCOUNT_COLUMN)
                break
        if columns is None:
            raise ValueError(f"{path.name}: no header row with '{TEXT_COLUMN}' and '{ACCOUNT_COLUMN}'")
        i_text, i_account = columns
        pairs = []
        for row in rows:
            if len(row) <= max(i_text, i_account):
                continue
            text, account = row[i_text], row[i_account]
            if text is None or account is None:
                continue
            text = str(text).strip()
            if len(text) >= 3:
                pairs.append((text, _normalize_account(account)))
        return pairs
    finally:
        workbook.close()


def load_rules(paths: Iterable[Path]) -> list[Rule]:
    """Rules from all history files, without duplicates, in file order."""
    rules: list[Rule] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        try:
            pairs = read_history(path)
        except (OSError, ValueError, BadZipFile, InvalidFileException) as exc:
            log.warning("Skipping booking history %s: %s", path.name, exc)
            continue
        for text, account in pairs:
            key = (text, str(account))
            if key not in seen:
                seen.add(key)
                rules.append(Rule.from_history(text, account))
    return rules
