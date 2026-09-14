"""The reader-facing pages: the per-newspaper dashboard, its JSON API, and the
establishment page the dashboard links out to.

The API is the foundation, not an alternative to the page. Even a fully
server-rendered version would need it — otherwise sorting a column is a full page
reload and the map re-initialises on every interaction. Because it exists, the
same front-end component can be mounted on our own page or dropped straight into
a newspaper's template; see `design_note.md`.

Three read-only endpoints:

* `rows`     — one page of the table, with each row's violations for the dropdown.
* `map`      — every facility matching the current filters, as GeoJSON.
* `facility` — a single row, for when a reader clicks a pin whose row is on
               another page.

Deliberately plain `JsonResponse`. Three read-only GETs do not need a framework,
and adding one would cut against the dependency-light line held elsewhere.

**Sorting and paging never touch the map.** Sorting reorders rows without
changing which facilities match, and the map shows every match rather than the
current page — so the map only reloads when the filters change. That is what
makes rapid sorting free.
"""

import datetime as dt
import functools
import json
import logging

from django.conf import settings
from django.db.models import Prefetch, Q
from django.db.models.functions import Lower
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.cache import cache_control
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.gzip import gzip_page

from .models import Dashboard, Facility, PriorityLevel, Violation
from .output import BOILERPLATE

logger = logging.getLogger(__name__)

# Hard cap on the map payload. At 10,000 facilities the GeoJSON is ~242KB
# gzipped, which is the measured ceiling this design was sized for; beyond that
# the answer is vector tiles, not a bigger download. See design_note.md.
MAP_LIMIT = 20000

SORTS = {
    "name": [Lower("name"), "pk"],
    "-name": [Lower("name").desc(), "pk"],
    "date": ["latest_inspection_date", Lower("name")],
    "-date": ["-latest_inspection_date", Lower("name")],
    "violations": ["latest_violation_total", Lower("name")],
    "-violations": ["-latest_violation_total", Lower("name")],
    "city": [Lower("city"), Lower("name")],
    "-city": [Lower("city").desc(), Lower("name")],
}
DEFAULT_SORT = "-date"


def _date(value):
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _filtered(dashboard, params):
    """Apply the reader's filters. Anything unparseable is ignored, not fatal.

    A bad value in one filter must not silently widen the result set by dropping
    the others — so each is applied independently.
    """
    facilities = dashboard.facilities()

    query = (params.get("q") or "").strip()
    if query:
        facilities = facilities.filter(
            Q(name__icontains=query)
            | Q(street__icontains=query)
            | Q(city__icontains=query)
        )

    county = (params.get("county") or "").strip()
    # Only counties this dashboard covers; anything else would let a caller read
    # outside the publication's area.
    if county and county in (dashboard.counties or []):
        facilities = facilities.filter(county=county)

    date_from = _date(params.get("from"))
    if date_from:
        facilities = facilities.filter(latest_inspection_date__gte=date_from)
    date_to = _date(params.get("to"))
    if date_to:
        facilities = facilities.filter(latest_inspection_date__lte=date_to)

    kind = (params.get("type") or "").strip()
    if kind:
        facilities = facilities.filter(latest_inspection_type=kind)

    cited = (params.get("cited") or "").strip()
    if cited == "1":
        facilities = facilities.filter(latest_violation_total__gt=0)

    return facilities


def _row(facility, violations_by_inspection):
    return {
        "id": facility.pk,
        "name": facility.name,
        "slug": facility.slug,
        "url": facility.get_absolute_url(),
        "street": facility.street,
        "city": facility.city,
        "county": facility.county,
        "address": facility.address_display,
        "date": facility.latest_inspection_date.isoformat()
        if facility.latest_inspection_date else None,
        "type": facility.latest_inspection_type,
        # Violations at the most recent inspection only — never the history.
        "total": facility.latest_violation_total,
        "p": facility.latest_priority,
        "pf": facility.latest_priority_foundation,
        "c": facility.latest_core,
        "mapped": facility.location is not None,
        "violations": violations_by_inspection.get(facility.latest_inspection_id, []),
    }


def _violations_for(facilities):
    """The latest inspection's violations for a page of rows, in one query.

    Fetched with the page rather than on click: 25 rows of detail is a few
    kilobytes, and it makes the dropdown open instantly.
    """
    inspection_ids = [f.latest_inspection_id for f in facilities if f.latest_inspection_id]
    if not inspection_ids:
        return {}

    grouped = {}
    rows = (
        Violation.objects.filter(inspection_id__in=inspection_ids)
        .select_related("item")
        .order_by("ordinal")
    )
    labels = dict(PriorityLevel.choices)
    for violation in rows:
        grouped.setdefault(violation.inspection_id, []).append({
            "level": violation.priority_level,
            "label": labels.get(violation.priority_level, ""),
            "text": violation.inspector_comments.strip()
            or violation.short_description
            or violation.code_explanation,
            "code": violation.code,
        })
    return grouped


def _dashboard(slug):
    return get_object_or_404(Dashboard, slug=slug, is_active=True)


def cross_origin(view):
    """Let the mounted component read this endpoint from a newspaper's own page.

    `loader` mounts the dashboard straight into the paper's DOM rather than an
    iframe, so every fetch the component makes runs against the paper's origin,
    not ours, and a JSON response without this header is fetched and then thrown
    away by the browser. The `<script src>` that loads the snippet is exempt from
    this, which is what makes the failure look like it comes from nowhere: the
    loader arrives fine and only the data is blocked.

    A wildcard rather than an allow-list of papers. These three endpoints are
    public, read-only and unauthenticated, and the component fetches them with
    `credentials: "same-origin"`, so there is nothing here that an origin check
    would protect. It also keeps the responses cacheable: echoing the origin back
    would need `Vary: Origin`, which would fragment the shared cache in front of
    `map` and give anything that ignored the header a way to serve one paper's
    response to another.
    """
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        response["Access-Control-Allow-Origin"] = "*"
        return response

    return wrapper


@cross_origin
@gzip_page
def rows(request, slug):
    """One page of the table."""
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    dashboard = _dashboard(slug)
    facilities = _filtered(dashboard, request.GET)

    sort = request.GET.get("sort") or DEFAULT_SORT
    if sort not in SORTS:
        sort = DEFAULT_SORT
    facilities = facilities.order_by(*SORTS[sort])

    per_page = dashboard.rows_per_page or 25
    total = facilities.count()
    pages = max(1, -(-total // per_page))
    try:
        page = max(1, min(pages, int(request.GET.get("page", 1))))
    except (TypeError, ValueError):
        page = 1

    window = list(facilities[(page - 1) * per_page : page * per_page])
    violations = _violations_for(window)

    return JsonResponse({
        "rows": [_row(f, violations) for f in window],
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "sort": sort,
        # Geocoding misses about 1-2%. A reader who notices the map showing
        # fewer pins than the table says will assume the map is broken.
        "unmapped": facilities.filter(location__isnull=True).count(),
    })


# Compressed here rather than by global middleware: this is a large, public,
# read-only payload with no secrets in it, whereas gzipping every response —
# admin pages carrying a CSRF token beside reflected search terms — is the setup
# BREACH needs. Production nginx would also have to list application/json in
# gzip_types, which it does not by default.
@cross_origin
@gzip_page
@cache_control(public=True, max_age=60)
def map_data(request, slug):
    """Every facility matching the current filters, as GeoJSON.

    Every match, not the current page — a map showing only page one would be
    useless. Sorting and paging are deliberately ignored here, which is why
    reordering the table costs no request at all.
    """
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    dashboard = _dashboard(slug)
    facilities = _filtered(dashboard, request.GET).filter(location__isnull=False)

    features = []
    rows_ = facilities.values_list(
        "pk", "name", "street", "city", "county", "latest_violation_total",
        "latest_inspection_date", "location",
    )[:MAP_LIMIT]
    for pk, name, street, city, county, total, date, point in rows_:
        features.append({
            "type": "Feature",
            "id": pk,
            "geometry": {"type": "Point", "coordinates": [round(point.x, 6), round(point.y, 6)]},
            "properties": {
                "id": pk,
                "name": name,
                # Address is in the payload so labels can render without a
                # second request; it is the bulk of the payload's size.
                "address": ", ".join(p for p in [street, city] if p),
                "county": county,
                "total": total,
                "date": date.isoformat() if date else None,
            },
        })

    return JsonResponse(
        {"type": "FeatureCollection", "features": features},
        json_dumps_params={"separators": (",", ":")},
    )


@cross_origin
def facility_row(request, slug, facility_id):
    """One row, for a pin click whose row is not on the current page.

    Returning it separately means a pin click never has to move the reader's
    pagination or disturb their filters.
    """
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    dashboard = _dashboard(slug)
    facility = get_object_or_404(dashboard.facilities(), pk=facility_id)
    return JsonResponse(_row(facility, _violations_for([facility])))


def _config(dashboard, request):
    """Everything the front-end component needs to mount itself."""
    kinds = (
        dashboard.facilities()
        .exclude(latest_inspection_type="")
        .values_list("latest_inspection_type", flat=True)
        .distinct()
        .order_by("latest_inspection_type")
    )
    return {
        "apiBase": request.build_absolute_uri(
            reverse("dashboard-rows", args=[dashboard.slug])
        ).rsplit("/", 1)[0],
        "heading": dashboard.heading,
        "counties": list(dashboard.counties or []),
        "types": list(kinds),
        "mapStyle": settings.DASHBOARD_MAP_STYLE,
        "fonts": dict(settings.DASHBOARD_MAP_FONTS),
    }


@xframe_options_exempt
def page(request, slug):
    """The canonical full-page dashboard.

    Framed by newspapers that prefer an iframe to mounting the component, so
    `X-Frame-Options` has to be lifted — Django's middleware sends DENY by
    default and would silently break every embed.
    """
    dashboard = _dashboard(slug)
    return render(request, "inspections/dashboard.html", {
        "dashboard": dashboard,
        "config": _config(dashboard, request),
    })


# Short, explicit, and revalidated: this is a dynamic document — it carries the
# dashboard's configuration and the hashed URL of the current bundle — but it ends
# in `.js`, and a CDN with no Cache-Control to go on will happily treat it as a
# static asset and hold it for hours. The assets it points at are immutable
# (`ForgivingManifestStaticFilesStorage` hashes them), so this pointer is the only
# thing that has to turn over quickly for a deploy to reach readers.
@cache_control(public=True, max_age=60)
def loader(request, slug):
    """The one-line snippet: mount the component into the newspaper's own page.

    The component is the default and the iframe the fallback — WEHCO controls the
    papers' CSS and CSP, which removes both arguments against mounting directly.
    """
    dashboard = _dashboard(slug)
    config = _config(dashboard, request)
    config["fullscreenUrl"] = request.build_absolute_uri(dashboard.get_absolute_url())

    script = render_to_string(
        "inspections/dashboard_loader.js",
        {
            "dashboard": dashboard,
            # Serialised here rather than through template filters: this is a
            # JavaScript response, not HTML, so the values have to arrive as
            # literals. `ensure_ascii` keeps anything non-ASCII escaped.
            "config_json": json.dumps(config, ensure_ascii=True),
            "module_url": request.build_absolute_uri(
                static("inspections/dashboard/dashboard.js")),
            "css_urls_json": json.dumps([
                request.build_absolute_uri(static("inspections/vendor/maplibre-gl.css")),
                request.build_absolute_uri(static("inspections/dashboard/dashboard.css")),
            ]),
        },
        request=request,
    )
    return HttpResponse(script, content_type="application/javascript")


# Violations with no priority level still happened, so they are shown under their
# own heading rather than dropped. Mirrors what the dashboard's row dropdown does
# in JavaScript, so a reader sees the same shape in both places.
_CATEGORIES = [
    (PriorityLevel.PRIORITY, "Priority"),
    (PriorityLevel.PRIORITY_FOUNDATION, "Priority Foundation"),
    (PriorityLevel.CORE, "Core"),
]


def _grouped(violations):
    """[(label, [violation, ...])] for the categories actually cited."""
    buckets = {code: [] for code, _ in _CATEGORIES}
    other = []
    for violation in violations:
        buckets.get(violation.priority_level, other).append(violation)

    groups = [(label, buckets[code]) for code, label in _CATEGORIES if buckets[code]]
    if other:
        groups.append(("Other observations", other))
    return groups


def establishment(request, slug):
    """One establishment's inspection history, for readers.

    A separate view and template from the staff `facility_detail` rather than one
    template with `{% if %}` around the sensitive parts. Conditionals are how
    internal detail leaks: this template cannot accidentally render the
    coordinates or the geocoder used, because that markup does not exist in it.
    It also carries no site navigation, since the staff pages are going behind a
    VPN or a login and a reader must not be shown links they cannot follow.

    Inspections whose details have never been retrieved are left out entirely.
    Publishing "1 violation" under a named business when the state recorded five
    and we simply have not fetched them yet would be worse than saying nothing.
    """
    facility = get_object_or_404(Facility, slug=slug)

    inspections = (
        facility.inspections.exclude(
            observation_count__gt=0,
            details_scraped_at__isnull=True,
            report_parsed_at__isnull=True,
        )
        .prefetch_related(
            # select_related on the item keeps the short descriptions from
            # costing one query per violation.
            Prefetch("violations", queryset=Violation.objects.select_related("item"))
        )
        .order_by("-date", "-sequence_within_day", "-pk")
    )

    history = []
    for inspection in inspections:
        violations = list(inspection.violations.all())
        history.append({
            "inspection": inspection,
            "groups": _grouped(violations),
            "count": len(violations),
            "report_url": inspection.report_pdf.url if inspection.report_pdf
                          else inspection.report_source_url,
        })

    return render(request, "inspections/establishment.html", {
        "facility": facility,
        "history": history,
        # Reused from the export so there is one wording for what the categories
        # mean, not two that can drift apart.
        "boilerplate": BOILERPLATE,
        "hidden_count": facility.inspections.count() - len(history),
    })
