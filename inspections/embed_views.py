"""Public embed endpoints.

Two views, both driven entirely by an `Embed` row:

* `embed_loader`  — a small script a newsroom pastes in one line. It injects the
  iframe and keeps its height in step with the content.
* `embed_page`    — the table itself: one server-rendered, self-contained,
  cached HTML document. Paging, sorting and searching all happen in the reader's
  browser, so the whole embed stays a single cacheable document no matter how a
  reader interacts with it.

Both are framed by third parties by definition, so `X-Frame-Options` has to be
lifted — Django's middleware sends DENY by default and would silently break every
embed.
"""

import hashlib
import os

from django.db.models import Count, Max
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.cache import cache_control
from django.views.decorators.clickjacking import xframe_options_exempt

from .models import Embed, Inspection, Violation

EMBED_TEMPLATE = "inspections/embed.html"


def _template_version():
    """Changes when the embed template does, so a deploy can't serve stale HTML.

    Without this, cached markup would outlive a template fix by the cache TTL —
    the sort of thing that reads as "I deployed but nothing changed".
    """
    try:
        from django.template.loader import get_template

        return str(int(os.path.getmtime(get_template(EMBED_TEMPLATE).origin.name)))
    except Exception:
        return "0"

# How long a rendered embed may be reused. The content only changes when a scrape
# runs, and the cache key carries a data fingerprint, so this can be generous:
# a CDN absorbs the traffic and new data still appears promptly.
BROWSER_CACHE_SECONDS = 300
SHARED_CACHE_SECONDS = 900


def _data_fingerprint(county, start, end):
    """Changes whenever the underlying data does, so cached HTML expires itself."""
    inspections = Inspection.objects.filter(
        facility__county=county, date__gte=start, date__lte=end
    )
    stats = inspections.aggregate(latest=Max("updated_at"), n=Count("pk"))
    violations = Violation.objects.filter(inspection__in=inspections).count()
    raw = f"{stats['latest']}-{stats['n']}-{violations}-{start}-{end}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _rows_for(embed, start, end):
    inspections = (
        Inspection.objects.filter(
            facility__county=embed.county, date__gte=start, date__lte=end
        )
        .select_related("facility")
        .prefetch_related("violations__item")
        .order_by("-date", "facility__name")
    )
    if embed.row_limit:
        inspections = inspections[: embed.row_limit]

    rows = []
    for inspection in inspections:
        violations = list(inspection.violations.all())
        facility = inspection.facility
        rows.append(
            {
                "inspection": inspection,
                "facility": facility,
                "violations": violations,
                "violation_count": len(violations),
                # Lower-cased once here so the browser's search filter stays trivial.
                "search_blob": " ".join(
                    filter(None, [facility.name, facility.street, facility.city])
                ).lower(),
                "sort_name": facility.name.lower(),
                "sort_date": inspection.date.isoformat(),
            }
        )
    return rows


@xframe_options_exempt
@cache_control(public=True, max_age=BROWSER_CACHE_SECONDS, s_maxage=SHARED_CACHE_SECONDS)
def embed_page(request, slug):
    """The embedded table. Self-contained: no external CSS, JS, fonts or images."""
    embed = get_object_or_404(Embed, slug=slug)
    if not embed.is_active:
        # Disabled centrally: render an empty shell rather than a 404, so a live
        # article degrades to blank space instead of a browser error page.
        return HttpResponse(
            render_to_string("inspections/embed_disabled.html", {"embed": embed}),
            content_type="text/html",
        )

    today = timezone.localtime(timezone.now()).date()
    start, end = embed.resolved_window(today)

    from django.core.cache import cache

    fingerprint = _data_fingerprint(embed.county, start, end)
    cache_key = (
        f"embed:{embed.slug}:{embed.updated_at.timestamp()}"
        f":{fingerprint}:{_template_version()}"
    )
    html = cache.get(cache_key)

    if html is None:
        rows = _rows_for(embed, start, end)
        html = render_to_string(
            EMBED_TEMPLATE,
            {
                "embed": embed,
                "rows": rows,
                "date_range": embed.date_range_label(today),
                "generated_at": timezone.localtime(timezone.now()),
                "rows_per_page": embed.rows_per_page or 0,
                "total_rows": len(rows),
            },
            request=request,
        )
        cache.set(cache_key, html, SHARED_CACHE_SECONDS)

    return HttpResponse(html, content_type="text/html")


@cache_control(public=True, max_age=BROWSER_CACHE_SECONDS)
def embed_loader(request, slug):
    """The one-line snippet's script: inject the iframe, then track its height."""
    embed = get_object_or_404(Embed, slug=slug)
    script = render_to_string(
        "inspections/embed_loader.js",
        {
            "embed": embed,
            "embed_url": request.build_absolute_uri(embed.get_absolute_url()),
            "loader_path": request.path,
        },
    )
    return HttpResponse(script, content_type="application/javascript")
