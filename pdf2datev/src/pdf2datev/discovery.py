"""Find the statement PDFs to process.

Convention: statements are named after their month (``Januar.pdf``) or placed in a
month folder (``Januar/*.pdf``). ``<Monat>_Rechnung.pdf`` files are invoice lists,
not statements (see :mod:`pdf2datev.invoices`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from pathlib import Path

log = logging.getLogger(__name__)

MONTH_NAMES = frozenset(
    {
        "january", "januar", "february", "februar", "march", "märz", "marz", "april", "may", "mai",
        "june", "juni", "july", "juli", "august", "september", "october", "oktober", "november",
        "december", "dezember",
    }
)  # fmt: skip

INVOICE_SUFFIX = "_rechnung"


def is_month_name(name: str) -> bool:
    return name.lower().rstrip("/") in MONTH_NAMES


def month_of(path: Path) -> str | None:
    """Month name a statement belongs to, from its file name or its folder."""
    for part in (path.stem, path.parent.name):
        candidate = part.split("_")[0].split("-")[0].strip()
        if candidate and is_month_name(candidate):
            return candidate
    return None


def is_pdf(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".pdf"


def is_invoice_pdf(path: Path) -> bool:
    return path.stem.lower().endswith(INVOICE_SUFFIX)


def _statements_in(folder: Path) -> Iterator[Path]:
    for path in sorted(folder.iterdir(), key=lambda p: p.name):
        if is_pdf(path) and not is_invoice_pdf(path):
            yield path


def _candidates(inputs: Sequence[Path], workdir: Path) -> Iterator[Path]:
    if inputs:
        for raw in inputs:
            path = raw if raw.is_absolute() else workdir / raw
            if is_pdf(path):
                yield path  # explicitly requested, even if named like an invoice list
            elif path.is_dir():
                yield from _statements_in(path)
            else:
                log.warning("Not a PDF file or folder, skipped: %s", raw)
        return

    found_month = False
    for path in sorted(workdir.iterdir(), key=lambda p: p.name):
        if not is_month_name(path.stem):
            continue
        if is_pdf(path):
            found_month = True
            yield path
        elif path.is_dir():
            found_month = True
            yield from _statements_in(path)
    if not found_month:
        yield from _statements_in(workdir)


def discover_statements(inputs: Sequence[Path], workdir: Path) -> list[Path]:
    """Statement PDFs from the given files/folders, or month-named ones in ``workdir``."""
    seen: set[Path] = set()
    statements = []
    for path in _candidates(inputs, workdir):
        key = path.resolve()
        if key in seen:
            continue
        seen.add(key)
        if path.stat().st_size == 0:
            log.warning("Empty file, skipped: %s", path.name)
            continue
        statements.append(path)
    return statements
