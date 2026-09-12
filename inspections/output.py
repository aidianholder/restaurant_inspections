"""Plain-HTML export of inspection results, for pasting into a story.

What comes out is a fragment, not a document: a newsroom CMS wants paragraphs
and lists it can drop straight into an article body, so there is no <html>
wrapper, no styling and no classes.

One heading, the explanatory paragraphs, then every cited establishment in one
alphabetical run. Inspections are not grouped by date and carry no date of their
own — the range in the heading is the only date the reader gets.

Only establishments with at least one *categorised* violation appear. The
boilerplate promises that "only categories in which an establishment was cited
are listed", and the three categories come from the report PDF — a violation
scraped from the website overlay alone carries no priority level and so has no
category to file it under. `Export.uncategorised` counts those so the omission
is visible on screen rather than silent.
"""

from django.db.models.functions import Lower
from django.utils.html import escape

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


class Export:
    """The rendered fragment plus the counts the screen reports back."""

    def __init__(self, html, establishments=0, days=0, uncategorised=0):
        self.html = html
        self.establishments = establishments
        self.days = days
        self.uncategorised = uncategorised

    def __str__(self):
        return self.html


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

    lines = [f"<h1>{escape(_heading(county, date_from, date_to))}</h1>", ""]
    lines += [f"<p>{escape(paragraph)}</p>" for paragraph in BOILERPLATE]

    establishments = uncategorised = 0
    dates = set()

    for inspection in inspections:
        violations = list(inspection.violations.all())
        categories = _by_category(violations)
        if not categories:
            if violations:
                uncategorised += 1
            continue

        facility = inspection.facility
        establishments += 1
        dates.add(inspection.date)
        lines += ["", f"<h3>{escape(facility.name)}</h3>"]

        detail = [escape(part) for part in (facility.address_display, inspection.inspection_type) if part]
        if detail:
            lines.append("<p>" + "<br>\n".join(detail) + "</p>")

        for label, cited in categories:
            lines.append(f"<p><strong>{escape(label)}</strong></p>")
            lines.append("<ul>")
            lines += [f"  <li>{escape(_observation_text(v))}</li>" for v in cited]
            lines.append("</ul>")

    return Export(
        "\n".join(lines).strip() + "\n",
        establishments=establishments,
        days=len(dates),
        uncategorised=uncategorised,
    )
