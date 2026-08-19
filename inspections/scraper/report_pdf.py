"""Read violations out of the inspection report PDF.

The web page's "Observations" overlay is incomplete: measured across 177 reports
it listed 173 violations where the PDFs contained 332. The omissions are mostly
Core-priority items but not exclusively — Priority and Priority Foundation items
go missing too — and some inspections that look clean online have violations in
the report. No online violation was ever absent from the PDF, so the PDF is a
strict superset and is treated as authoritative wherever one exists.

The reports are text PDFs with a properly ruled observations table, so this reads
real table cells rather than guessing at text layout. No OCR involved.

The PDF also carries three things the web view never exposes: the item number,
the priority level (P / PF / C), and the correct-by date.
"""

import logging
import re
from datetime import datetime

import pdfplumber

logger = logging.getLogger(__name__)

INSPECTION_ID_RE = re.compile(r"Inspection ID\s*:\s*(\S+)")
RISK_VIOLATION_COUNT_RE = re.compile(r"No\. Of Risk Factor/Intervention Violations\s*(\d+)")
REPEAT_VIOLATION_COUNT_RE = re.compile(r"No\. Of Repeat Factor/Intervention Violations\s*(\d+)")

PRIORITY_LEVELS = {"P", "PF", "C"}


def _text(cell):
    """Flatten a table cell to a single line."""
    return re.sub(r"\s+", " ", (cell or "").replace("\n", " ")).strip()


def _code(cell):
    """Join a wrapped code without inserting spaces.

    The cell arrives as "20 CAR 191-\\n201 (a)(1),(2),\\n(3)&(5)" and must come
    back as "20 CAR 191-201 (a)(1),(2),(3)&(5)" to match the web spelling.
    """
    return re.sub(r"\s+", " ", (cell or "").replace("\n", "")).strip()


def _date(value):
    value = _text(value)
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _is_observations_header(row):
    joined = " ".join(_text(c) for c in row)
    return "Item" in joined and "Priority Level" in joined and "Comment" in joined


def parse_report(path_or_file):
    """Parse one inspection report.

    Returns {"inspection_id", "risk_violation_count", "repeat_violation_count",
    "violations": [...]}. Raises nothing for an empty report — a clean inspection
    legitimately has no violation rows.
    """
    violations = []
    header_text = ""

    with pdfplumber.open(path_or_file) as pdf:
        for page_number, page in enumerate(pdf.pages):
            if page_number == 0:
                header_text = page.extract_text() or ""

            for table in page.extract_tables():
                header_idx = next(
                    (i for i, row in enumerate(table[:3]) if _is_observations_header(row)), None
                )
                if header_idx is None:
                    continue

                for row in table[header_idx + 1 :]:
                    if len(row) < 5:
                        continue
                    item, code, priority, comment, correct_by = row[:5]
                    item, code = _text(item), _code(code)

                    # A row with no item or code is the tail of the previous
                    # comment, spilling over a page or cell boundary.
                    if not item and not code:
                        if _text(comment) and violations:
                            violations[-1]["comment"] += " " + _text(comment)
                        continue

                    priority = _text(priority).upper()
                    violations.append(
                        {
                            "item_number": item,
                            "code": code,
                            "priority_level": priority if priority in PRIORITY_LEVELS else "",
                            "comment": _text(comment),
                            "correct_by": _date(correct_by),
                        }
                    )

    def _count(pattern):
        match = pattern.search(header_text)
        return int(match.group(1)) if match else None

    inspection_id = INSPECTION_ID_RE.search(header_text)
    return {
        "inspection_id": inspection_id.group(1).strip() if inspection_id else "",
        "risk_violation_count": _count(RISK_VIOLATION_COUNT_RE),
        "repeat_violation_count": _count(REPEAT_VIOLATION_COUNT_RE),
        "violations": violations,
    }
