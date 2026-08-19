from django.db.models import Count, Max, Prefetch, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django_q.tasks import async_task

from .forms import ScrapeRequestForm
from .models import Facility, Inspection, ScrapeRun


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


def facility_list(request):
    """Reader-facing index of everything retrieved so far."""
    facilities = (
        Facility.objects.annotate(
            inspection_count=Count("inspections", distinct=True),
            latest_inspection=Max("inspections__date"),
        )
        .filter(inspection_count__gt=0)
        .order_by("-latest_inspection", "name")
    )

    county = request.GET.get("county", "")
    query = request.GET.get("q", "")
    if county:
        facilities = facilities.filter(county=county)
    if query:
        facilities = facilities.filter(Q(name__icontains=query) | Q(city__icontains=query))

    counties = (
        Facility.objects.exclude(county="")
        .values_list("county", flat=True)
        .distinct()
        .order_by("county")
    )
    return render(
        request,
        "inspections/facility_list.html",
        {
            "facilities": facilities[:200],
            "total": facilities.count(),
            "counties": counties,
            "county": county,
            "query": query,
        },
    )


def facility_detail(request, slug):
    facility = get_object_or_404(
        Facility.objects.prefetch_related(
            Prefetch("inspections", queryset=Inspection.objects.prefetch_related("violations"))
        ),
        slug=slug,
    )
    return render(request, "inspections/facility_detail.html", {"facility": facility})
