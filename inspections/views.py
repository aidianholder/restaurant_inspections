import logging
from collections import defaultdict

from django.conf import settings
from django.db.models import Count, IntegerField, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django_q.tasks import async_task

from .forms import FacilityFilterForm, OutputForm, ScrapeRequestForm
from .markdown_render import markdown_to_html
from .models import Facility, Inspection, ScrapeRun, Violation
from .output import build_export
from .summarize import SummaryError, summarize

logger = logging.getLogger(__name__)

# A month of a large county runs to a few hundred KB. Well clear of that, but
# short of letting an open endpoint render something enormous.
MAX_RENDER_CHARS = 2_000_000


def scrape_request(request):
    """The retrieval screen: pick a county and a date range, queue a run."""
    if request.method == "POST":
        form = ScrapeRequestForm(request.POST)
        if form.is_valid():
            run = form.save()
            run.task_id = async_task(
                "inspections.tasks.scrape_run_task", run.pk, task_name=f"scrape-{run.pk}"
            )
            run.save(update_fields=["task_id"])
            return redirect("scrape-detail", pk=run.pk)
    else:
        form = ScrapeRequestForm()

    return render(
        request,
        "inspections/scrape_request.html",
        {"form": form, "recent_runs": ScrapeRun.objects.all()[:10]},
    )


def scrape_detail(request, pk):
    """Progress page for one run; polls the status endpoint while it's active."""
    run = get_object_or_404(ScrapeRun, pk=pk)
    return render(request, "inspections/scrape_detail.html", {"run": run})


def scrape_status(request, pk):
    run = get_object_or_404(ScrapeRun, pk=pk)
    return JsonResponse(
        {
            "status": run.status,
            "status_label": run.get_status_display(),
            "active": run.is_active,
            "note": run.progress_note,
            "pages": run.pages_fetched,
            "facilities": run.facilities_created,
            "inspections": run.inspections_created,
            "violations": run.violations_created,
            "reports": run.reports_downloaded,
            "error": run.error,
        }
    )


def _latest_inspection_for_facility():
    """Subquery source: a facility's inspections, newest first.

    Ordered by sequence and pk as well as date so two inspections on the same day
    resolve deterministically rather than arbitrarily.
    """
    return Inspection.objects.filter(facility=OuterRef("pk")).order_by(
        "-date", "-sequence_within_day", "-pk"
    )


def _attach_latest_violation_counts(facilities):
    """Fill in P / PF / C counts for each facility's most recent inspection.

    Done as one grouped query over the page's inspections rather than as more
    subqueries: it stays a single round trip, and it's far easier to read than
    nesting OuterRefs three deep.
    """
    latest_ids = [f.latest_inspection_id for f in facilities if f.latest_inspection_id]
    counts = defaultdict(lambda: {"P": 0, "PF": 0, "C": 0, "total": 0})

    if latest_ids:
        rows = (
            Violation.objects.filter(inspection_id__in=latest_ids)
            .values("inspection_id", "priority_level")
            .annotate(n=Count("pk"))
        )
        for row in rows:
            bucket = counts[row["inspection_id"]]
            bucket["total"] += row["n"]
            # A violation with no priority level still counts toward the total.
            if row["priority_level"] in bucket:
                bucket[row["priority_level"]] = row["n"]

    for facility in facilities:
        bucket = counts[facility.latest_inspection_id]
        facility.latest_priority = bucket["P"]
        facility.latest_priority_foundation = bucket["PF"]
        facility.latest_core = bucket["C"]
        facility.latest_violation_total = bucket["total"]
    return facilities


def facility_list(request):
    """Reader-facing index of everything retrieved so far."""
    counties = (
        Facility.objects.exclude(county="")
        .values_list("county", flat=True)
        .distinct()
        .order_by("county")
    )
    form = FacilityFilterForm(request.GET or None, counties=counties)

    latest = _latest_inspection_for_facility()
    facilities = Facility.objects.annotate(
        # All subqueries, no aggregates: mixing the two in one queryset drags a
        # GROUP BY across the subqueries and gets fragile.
        inspection_count=Coalesce(
            Subquery(
                Inspection.objects.filter(facility=OuterRef("pk"))
                .values("facility")
                .annotate(n=Count("pk"))
                .values("n")[:1],
                output_field=IntegerField(),
            ),
            0,
        ),
        latest_inspection=Subquery(latest.values("date")[:1]),
        latest_inspection_id=Subquery(latest.values("pk")[:1]),
        latest_inspection_type=Subquery(latest.values("inspection_type")[:1]),
    ).filter(inspection_count__gt=0)

    if form.is_bound:
        # Run validation for its side effect of populating cleaned_data, then
        # apply whichever filters cleaned successfully. A single bad field
        # shouldn't quietly widen the result set by dropping the good ones.
        form.is_valid()
        cleaned = form.cleaned_data
        if cleaned.get("county"):
            facilities = facilities.filter(county=cleaned["county"])
        if cleaned.get("q"):
            facilities = facilities.filter(
                Q(name__icontains=cleaned["q"]) | Q(city__icontains=cleaned["q"])
            )
        # Both bounds are optional and apply to the latest inspection date.
        if cleaned.get("date_from"):
            facilities = facilities.filter(latest_inspection__gte=cleaned["date_from"])
        if cleaned.get("date_to"):
            facilities = facilities.filter(latest_inspection__lte=cleaned["date_to"])

    facilities = facilities.order_by("-latest_inspection", "name")
    total = facilities.count()
    page = _attach_latest_violation_counts(list(facilities[:200]))

    return render(
        request,
        "inspections/facility_list.html",
        {"form": form, "facilities": page, "total": total},
    )


def facility_detail(request, slug):
    facility = get_object_or_404(
        Facility.objects.prefetch_related(
            Prefetch(
                "inspections",
                queryset=Inspection.objects.prefetch_related(
                    # select_related on the item keeps the short descriptions from
                    # costing one query per violation.
                    Prefetch("violations", queryset=Violation.objects.select_related("item"))
                ),
            )
        ),
        slug=slug,
    )
    return render(request, "inspections/facility_detail.html", {"facility": facility})


def output_data(request):
    """Render stored inspections as HTML a newsroom can paste into a story."""
    form = OutputForm(request.GET or None)
    export = None
    if form.is_bound and form.is_valid():
        export = build_export(
            form.cleaned_data["county"],
            form.cleaned_data["date_from"],
            form.cleaned_data["date_to"],
        )

    return render(
        request,
        "inspections/output_data.html",
        # The button explains itself rather than failing on click when no token
        # has been configured.
        {"form": form, "export": export, "summary_available": bool(settings.OPENAI_TOKEN)},
    )


def output_summary(request):
    """Shorten an export's observations via OpenAI. Posted to, answers JSON.

    Takes the same county and date range the export was built from and rebuilds
    it here rather than accepting Markdown from the browser. Two reasons: the
    summary is then provably of our own data, and the endpoint can't be used to
    spend the newsroom's OpenAI account on arbitrary text someone posts at it.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    form = OutputForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"error": _first_error(form)}, status=400)

    export = build_export(
        form.cleaned_data["county"],
        form.cleaned_data["date_from"],
        form.cleaned_data["date_to"],
    )
    if not export.establishments:
        return JsonResponse({"error": "There is nothing to summarise."}, status=400)

    try:
        summary = summarize(export)
    except SummaryError as exc:
        return JsonResponse({"error": str(exc)}, status=502)

    logger.info(
        "Summarised %s establishments for %s with %s using prompt '%s'",
        export.establishments, form.cleaned_data["county"], summary.model, summary.prompt,
    )
    return JsonResponse({
        "markdown": summary.markdown,
        "model": summary.model,
        # Which wording produced this, so two summaries can be told apart.
        "prompt": summary.prompt,
        "truncated": summary.truncated,
        # Named on screen: an establishment quietly dropped from a roundup is
        # the one failure a reader would never catch.
        "missing": summary.missing,
        "unexpected": summary.unexpected,
    })


def output_render(request):
    """Markdown in, HTML out. Backs the live preview and the Copy HTML button.

    Both go through here so the preview is by construction what gets copied —
    a preview that can drift from the real output is worse than none.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    markdown = request.POST.get("markdown", "")
    if len(markdown) > MAX_RENDER_CHARS:
        return JsonResponse(
            {"error": "That's larger than this page will render. Narrow the date range."},
            status=400,
        )
    return JsonResponse({"html": markdown_to_html(markdown)})


def _first_error(form):
    """One sentence a person can act on, rather than Django's error dict."""
    for errors in [form.non_field_errors(), *form.errors.values()]:
        if errors:
            return errors[0]
    return "That request wasn't valid."
