"""Markdown export of inspection results, for pasting into a story.

Markdown rather than HTML because a person reads and edits this before it is
published — it is the canonical artefact, not an intermediate. HTML for the web
is rendered from it on demand (`markdown_render`), and the plain text an
InDesign operator places is the Markdown itself.

One heading, the explanatory paragraphs, then every cited establishment in one
alphabetical run. Inspections are not grouped by date and carry no date of their
own — the range in the heading is the only date the reader gets.

The document is also handed to the summariser, so it is built in pieces: a
preamble that is ours and must never change, and one block per establishment.
Only the blocks are ever sent to a model.
"""

import re

from django.db.models.functions import Lower

from .models import Inspection, PriorityLevel

BOILERPLATE = [
    "Violations marked as priority contribute directly to the elimination, "
    "prevention or reduction in the hazards associated with foodborne illness. "
    "Priority violations include prevention of contamination, cooking, reheating, "
    "cooling and handwashing.",
    "Priority foundation rules support, facilitate or enable one or more priority items.",
    "Core violations include items that usually relate to general sanitation, "
    "operational controls, equipment design or general maintenance.",
    "Only categories in which an establishment was cited are listed.",
]

# Most serious first, which is the order the categories are introduced in the
# boilerplate above.
CATEGORY_ORDER = [
    PriorityLevel.PRIORITY,
    PriorityLevel.PRIORITY_FOUNDATION,
    PriorityLevel.CORE,
]

# An establishment's name is an h3. Both the summariser and its reconciliation
# check read this, so the convention lives here with the code that writes it.
ESTABLISHMENT_HEADING = "### "
_HEADING_RE = re.compile(r"^### (.+)$", re.M)

# The characters CommonMark reads as markup inline. Deliberately not the full
# escapable set — backslashing every "." and "-" would make the document
# unreadable, which would defeat the point of using Markdown at all. `<` and `>`
# earn their place here specifically: inspectors write "held <41F" all day long.
_INLINE_SPECIAL = re.compile(r"([\\`*_\[\]<>])")

# At the start of a paragraph these open a block instead. Addresses really do
# begin "#5 Highway 65".
_BLOCK_OPENER = re.compile(r"\A([#>+\-=])")
_ORDERED_OPENER = re.compile(r"\A(\d+)([.)])")


class Export:
    """The document in parts, plus the counts the screen reports back."""

    def __init__(self, preamble, blocks, establishments=0, days=0, uncategorised=0):
        # Ours, fixed, never sent to a model: the heading and the four
        # explanatory paragraphs.
        self.preamble = preamble
        # One Markdown block per cited inspection, in the order they appear.
        self.blocks = blocks
        self.establishments = establishments
        self.days = days
        self.uncategorised = uncategorised

    @property
    def markdown(self):
        return "\n\n".join([self.preamble, *self.blocks]).strip() + "\n"

    def __str__(self):
        return self.markdown


def establishment_names(markdown):
    """Every establishment heading in a document, in order.

    Used on both sides of the summariser — what was sent and what came back —
    so a mismatch between the two means something actually went missing.
    """
    return [match.group(1).strip() for match in _HEADING_RE.finditer(markdown)]


def _one_line(text):
    """Collapse a value to a single line with single spaces.

    Not cosmetic: two spaces at the end of a Markdown line is a hard break, and
    a stray newline inside an observation would split the bullet. Rendered HTML
    collapsed both anyway, so nothing a reader saw is lost.
    """
    return " ".join((text or "").split())


def _inline(text):
    """Escape a value that sits inside a line."""
    return _INLINE_SPECIAL.sub(r"\\\1", _one_line(text))


def _paragraph(text):
    """Escape a value that begins its own paragraph."""
    escaped = _inline(text)
    if _BLOCK_OPENER.match(escaped):
        return "\\" + escaped
    ordered = _ORDERED_OPENER.match(escaped)
    if ordered:
        return f"{ordered.group(1)}\\{ordered.group(2)}"
    return escaped


def _format_date(value):
    """"9/04/26" — month unpadded, day padded, two-digit year."""
    return f"{value.month}/{value.day:02d}/{value:%y}"


def _heading(county, date_from, date_to):
    return (
        f"{county} County health inspections "
        f"{_format_date(date_from)} - {_format_date(date_to)}"
    )


def _observation_text(violation):
    """The inspector's own wording, with the sparest of fallbacks.

    Comments are what the export is for, so a violation carrying none is worth
    showing as its item description rather than as an empty bullet.
    """
    return (
        violation.inspector_comments.strip()
        or violation.short_description
        or violation.code_explanation
    )


def _by_category(violations):
    """[(label, [violation, ...])] for the categories actually cited."""
    buckets = {level: [] for level in CATEGORY_ORDER}
    for violation in violations:
        if violation.priority_level in buckets and _observation_text(violation):
            buckets[violation.priority_level].append(violation)
    return [
        (PriorityLevel(level).label, buckets[level]) for level in CATEGORY_ORDER if buckets[level]
    ]


def _block_for(inspection, categories):
    """One establishment: heading, address, inspection type, then its groups."""
    facility = inspection.facility
    parts = [f"{ESTABLISHMENT_HEADING}{_inline(facility.name)}"]

    # Address and inspection type are separate paragraphs. They used to share one
    # with a <br> between them, which in Markdown means two trailing spaces —
    # invisible whitespace that the first person to edit this would destroy
    # without noticing.
    if facility.address_display:
        parts.append(_paragraph(facility.address_display))
    if inspection.inspection_type:
        parts.append(_paragraph(inspection.inspection_type))

    for label, cited in categories:
        parts.append(f"**{label}**")
        parts.append("\n".join(f"- {_inline(_observation_text(v))}" for v in cited))

    return "\n\n".join(parts)


def build_export(county, date_from, date_to):
    """Render every cited inspection in `county` between the two dates."""
    inspections = (
        Inspection.objects.filter(
            facility__county=county, date__gte=date_from, date__lte=date_to
        )
        .select_related("facility")
        .prefetch_related("violations__item")
        # One alphabetical run across the whole range, case-folded: the state's
        # names arrive in a mix of upper and title case, which a raw sort would
        # interleave badly. Date only breaks ties, so an establishment inspected
        # twice reads in the order it happened.
        .order_by(Lower("facility__name"), "date", "pk")
    )

    preamble = "\n\n".join(
        [f"# {_inline(_heading(county, date_from, date_to))}"]
        + [_paragraph(paragraph) for paragraph in BOILERPLATE]
    )

    blocks = []
    uncategorised = 0
    dates = set()

    for inspection in inspections:
        violations = list(inspection.violations.all())
        categories = _by_category(violations)
        if not categories:
            if violations:
                uncategorised += 1
            continue

        dates.add(inspection.date)
        blocks.append(_block_for(inspection, categories))

    return Export(
        preamble,
        blocks,
        establishments=len(blocks),
        days=len(dates),
        uncategorised=uncategorised,
    )
