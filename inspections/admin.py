from django.contrib.gis import admin
from django.db import models as django_models
from django.forms import Textarea
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from .latest_inspection import refresh as refresh_latest
from .models import (
    Dashboard, Embed, Facility, GeocodeSource, Inspection, ScrapeRun, ScrapeSchedule,
    SummaryPrompt, Violation, ViolationItem,
)
from .widgets import VectorBasemapWidget

admin.site.site_header = "AR Health Inspections"
admin.site.site_title = "AR Health Inspections"


class InspectionInline(admin.TabularInline):
    model = Inspection
    extra = 0
    fields = ("date", "inspection_type", "observation_count", "details_scraped_at", "report_pdf")
    readonly_fields = fields
    show_change_link = True
    ordering = ("-date",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Facility)
class FacilityAdmin(admin.GISModelAdmin):
    # Django's default is OpenStreetMap's public tile server, which blocks
    # sustained use. See inspections/widgets.py.
    gis_widget = VectorBasemapWidget

    list_display = (
        "name", "city", "county", "phone", "source_key",
        "address_needs_review", "geocode_source", "coordinates",
    )
    list_filter = ("county", "address_needs_review", "geocode_source", "state")
    search_fields = ("name", "street", "city", "zip_code", "source_key", "raw_address")
    readonly_fields = (
        "fingerprint", "raw_address", "first_seen", "last_seen",
        "geocode_source", "geocode_matched_address", "geocoded_at",
    )
    inlines = [InspectionInline]
    # Addresses the parser couldn't resolve surface first for hand-correction.
    ordering = ("-address_needs_review", "name")

    def save_model(self, request, obj, form, change):
        """A point moved by hand in the admin is a manual location, not a guess."""
        if "location" in form.changed_data and obj.location is not None:
            obj.geocode_source = GeocodeSource.MANUAL
            obj.geocode_matched_address = ""
            obj.geocoded_at = timezone.now()
        super().save_model(request, obj, form, change)

    @admin.display(description="Coordinates")
    def coordinates(self, obj):
        if not obj.location:
            return "—"
        return f"{obj.latitude:.5f}, {obj.longitude:.5f}"


class ViolationInline(admin.TabularInline):
    model = Violation
    extra = 0
    fields = ("ordinal", "item_number", "item", "code", "priority_level", "inspector_comments", "correct_by", "source")
    autocomplete_fields = ("item",)


@admin.register(Inspection)
class InspectionAdmin(admin.ModelAdmin):
    list_display = (
        "facility", "date", "inspection_type", "observation_count", "violation_rows",
        "violations_source", "has_report",
    )
    list_filter = ("inspection_type", "violations_source", "date", "facility__county")
    search_fields = ("facility__name", "facility__city")
    date_hierarchy = "date"
    autocomplete_fields = ("facility",)
    inlines = [ViolationInline]

    @admin.display(description="violations")
    def violation_rows(self, obj):
        """Flag inspections where the website's count understates the report."""
        n = obj.violations.count()
        if obj.website_undercounted:
            return format_html('<strong>{}</strong> <span style="color:#9a6700">(site: {})</span>',
                               n, obj.observation_count)
        return n

    @admin.display(boolean=True, description="report")
    def has_report(self, obj):
        return bool(obj.report_pdf)

    # Facility's latest_* columns are rebuilt by scrape runs. Editing violations
    # inline, or deleting an inspection, changes the answer without a run — the
    # one realistic way those columns go stale in production. Rebuilding one
    # facility is a single indexed statement, so it is cheap to do here.
    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        refresh_latest([form.instance.facility_id])

    def delete_model(self, request, obj):
        facility_id = obj.facility_id
        super().delete_model(request, obj)
        refresh_latest([facility_id])

    def delete_queryset(self, request, queryset):
        facility_ids = list(queryset.values_list("facility_id", flat=True))
        super().delete_queryset(request, queryset)
        refresh_latest(facility_ids)


@admin.register(ScrapeRun)
class ScrapeRunAdmin(admin.ModelAdmin):
    list_display = (
        "county", "date_from", "date_to", "status", "schedule", "pages_fetched",
        "facilities_created", "inspections_created", "violations_created",
        "reports_downloaded", "created_at",
    )
    list_filter = ("status", "county")
    readonly_fields = tuple(
        f.name for f in ScrapeRun._meta.fields if f.name not in {"county", "date_from", "date_to"}
    )

    @admin.display(description="Error")
    def error_display(self, obj):
        return format_html("<pre>{}</pre>", obj.error)


@admin.register(ViolationItem)
class ViolationItemAdmin(admin.ModelAdmin):
    """Reference data. Numbers and official text come from the state's form;
    `plain_description` is ours to write and survives a re-seed."""

    list_display = ("number", "display_description", "section", "subsection", "violation_count")
    list_filter = ("section", "subsection")
    search_fields = ("number", "official_description", "plain_description", "subsection")
    readonly_fields = ("number", "section", "subsection", "official_description")
    ordering = ("number",)

    @admin.display(description="Reader-facing text")
    def display_description(self, obj):
        if obj.plain_description:
            return format_html("<strong>{}</strong>", obj.plain_description)
        return obj.official_description

    @admin.display(description="Times cited")
    def violation_count(self, obj):
        return obj.violations.count()

    def has_add_permission(self, request):
        return False   # the form defines these, not us

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ScrapeSchedule)
class ScrapeScheduleAdmin(admin.ModelAdmin):
    """Per-county standing scrape instructions.

    Editing any timing field clears `next_run_at` so it is recomputed on save —
    otherwise a schedule moved to a new day would keep its old next occurrence.
    """

    TIMING_FIELDS = {"cadence", "day_of_week", "day_of_month", "run_at", "is_active"}

    list_display = (
        "county", "schedule_description", "lookback_days", "is_active",
        "next_run_at", "last_run_status",
    )
    list_filter = ("cadence", "is_active")
    search_fields = ("county",)
    readonly_fields = ("next_run_at", "last_queued_at", "last_run", "created_at")
    actions = ["run_now", "recompute_next_run"]

    @admin.display(description="Last run")
    def last_run_status(self, obj):
        if not obj.last_run:
            return "—"
        run = obj.last_run
        colour = {"success": "#1f6f43", "failed": "#8c1d1d"}.get(run.status, "#9a6700")
        return format_html(
            '<span style="color:{}">{}</span> {}', colour, run.get_status_display(), run.date_to
        )

    def save_model(self, request, obj, form, change):
        if self.TIMING_FIELDS & set(form.changed_data):
            obj.next_run_at = None      # save() recomputes it
        super().save_model(request, obj, form, change)

    @admin.action(description="Run selected schedules now")
    def run_now(self, request, queryset):
        from django.utils import timezone

        from .scheduling import dispatch_due_scrapes

        # Pull the chosen schedules forward, then let the normal dispatcher run —
        # so the in-flight guard and window logic behave exactly as they do at 2am.
        queryset.update(next_run_at=timezone.now())
        result = dispatch_due_scrapes()
        self.message_user(
            request,
            f"Queued: {', '.join(result['queued']) or 'none'}. "
            f"Skipped (already running): {', '.join(result['skipped']) or 'none'}.",
        )

    @admin.action(description="Recompute next run time")
    def recompute_next_run(self, request, queryset):
        for schedule in queryset:
            schedule.reschedule()
            schedule.save(update_fields=["next_run_at"])
        self.message_user(request, f"Rescheduled {queryset.count()}.")


@admin.register(Embed)
class EmbedAdmin(admin.ModelAdmin):
    """The whole configuration surface for embeds, for now.

    A newsroom asks for a county; you fill this in and send them the snippet. No
    file, no template, no deploy — the public view reads this row at request time.
    """

    list_display = ("slug", "publication", "county", "window_label", "rows_per_page", "is_active")
    list_filter = ("is_active", "county", "publication")
    search_fields = ("slug", "publication", "county", "notes")
    readonly_fields = ("embed_snippets", "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("slug", "publication", "county", "is_active")}),
        ("What it covers", {
            "fields": ("window_days", "date_from", "date_to"),
            "description": "Use a rolling window wherever possible — a fixed range is "
                           "stale the day after you set it, and nobody notices for months.",
        }),
        ("Presentation", {
            "fields": ("title_override", "default_sort", "rows_per_page", "row_limit",
                       "search_enabled", "show_report_links"),
        }),
        ("Give this to the newsroom", {"fields": ("embed_snippets",)}),
        ("Internal", {"fields": ("notes", "created_at", "updated_at")}),
    )

    @admin.display(description="Covers")
    def window_label(self, obj):
        if obj.window_days:
            return f"last {obj.window_days} days"
        return f"{obj.date_from} – {obj.date_to}"

    @admin.display(description="Embed code")
    def embed_snippets(self, obj):
        if not obj.pk:
            return "Save the embed first, then the snippets appear here."

        # Built from the current request so the host is right in every environment.
        request = getattr(self, "_request", None)
        page_url = obj.get_absolute_url()
        loader_url = reverse("embed-loader", args=[obj.slug])
        if request is not None:
            page_url = request.build_absolute_uri(page_url)
            loader_url = request.build_absolute_uri(loader_url)

        script_tag = f'<script src="{loader_url}" async></script>'
        iframe_tag = (
            f'<iframe src="{page_url}" title="{obj.heading}" '
            f'style="width:100%;border:0;height:900px" scrolling="no"></iframe>'
        )
        box = ("width:100%;font:12px ui-monospace,SFMono-Regular,Menlo,monospace;"
               "padding:8px;border:1px solid #b8b3ac;border-radius:4px;"
               # Both colours pinned: inheriting either one leaves the snippet
               # unreadable under the admin's dark theme.
               "background:#fbfaf8;color:#1a1a1a")
        return format_html(
            '<p style="margin:0 0 4px"><strong>Script tag</strong> — preferred; resizes itself.</p>'
            '<textarea readonly rows="2" style="{}" onclick="this.select()">{}</textarea>'
            '<p style="margin:12px 0 4px"><strong>Plain iframe</strong> — for a CMS that strips '
            '&lt;script&gt;. Fixed height, so it may scroll internally.</p>'
            '<textarea readonly rows="3" style="{}" onclick="this.select()">{}</textarea>'
            '<p style="margin:12px 0 0"><a href="{}" target="_blank" rel="noopener">Preview this embed →</a></p>',
            box, script_tag, box, iframe_tag, page_url,
        )

    def get_form(self, request, obj=None, **kwargs):
        self._request = request        # so snippets can carry an absolute URL
        return super().get_form(request, obj, **kwargs)


@admin.register(SummaryPrompt)
class SummaryPromptAdmin(admin.ModelAdmin):
    """What the AI summariser is told, tunable without a deploy.

    Keep the old rows. Tuning a prompt means going backwards as often as
    forwards, and the cheapest way to answer "was it better before rule 4?" is to
    still have the version that came before rule 4.
    """

    list_display = ("name", "active_flag", "model_used", "template_used", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "system_prompt", "user_template", "notes")
    readonly_fields = ("created_at", "updated_at")
    actions = ["make_active"]
    fieldsets = (
        (None, {"fields": ("name", "is_active")}),
        ("Instructions", {
            "fields": ("system_prompt",),
            "description": "Sent as the system message. The two rules about using only "
                           "the source text are what keep the model quoting the inspector "
                           "rather than explaining what a violation usually means — these "
                           "summaries run under an inspector's byline, so treat those two "
                           "as load-bearing.",
        }),
        ("The message itself", {
            "fields": ("user_template", "model"),
            "description": "Leave the template blank to send the inspections on their own, "
                           "which is what the shipped prompt does. Fill it in when you want "
                           "to wrap them in something — an example summary to work from is "
                           "the usual reason. {html} is where the inspections go, and it has "
                           "to appear somewhere.",
        }),
        ("Internal", {"fields": ("notes", "created_at", "updated_at")}),
    )
    # A prompt is code-shaped text; proportional font at 40 columns makes it
    # unreadable exactly where careful reading matters.
    formfield_overrides = {
        django_models.TextField: {
            "widget": Textarea(attrs={
                "rows": 24, "cols": 100,
                "style": "font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;"
                         "max-width:100%",
            })
        }
    }

    @admin.display(description="Live", boolean=True, ordering="is_active")
    def active_flag(self, obj):
        return obj.is_active

    @admin.display(description="Model")
    def model_used(self, obj):
        return obj.model or format_html('<span style="color:#888">{}</span>', "OPENAI_MODEL")

    @admin.display(description="Wraps the export", boolean=True)
    def template_used(self, obj):
        return bool(obj.user_template.strip())

    @admin.action(description="Make this the active prompt")
    def make_active(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(
                request, "Pick exactly one prompt to activate.", level="error"
            )
            return
        prompt = queryset.get()
        prompt.is_active = True
        # Through save(), so the others are deactivated in the same transaction.
        prompt.save()
        self.message_user(request, f"'{prompt.name}' is now the active prompt.")

    def has_delete_permission(self, request, obj=None):
        """The live prompt can't be deleted out from under the button."""
        if obj is not None and obj.is_active:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(Dashboard)
class DashboardAdmin(admin.ModelAdmin):
    """One newspaper's map-and-table view over its readership area.

    A paper names its counties; you fill this in and send them the snippet. No
    file, no template, no deploy — the API reads this row at request time.
    """

    list_display = ("slug", "publication", "county_list", "rows_per_page", "is_active")
    list_filter = ("is_active", "publication")
    search_fields = ("slug", "publication", "notes")
    readonly_fields = ("embed_snippet", "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("slug", "publication", "is_active")}),
        ("What it covers", {
            "fields": ("counties",),
            "description": "Every county in this paper's readership area. The map and "
                           "table both stay inside this list — a reader cannot filter "
                           "their way out of it.",
        }),
        ("Presentation", {"fields": ("title_override", "rows_per_page")}),
        ("Give this to the newsroom", {"fields": ("embed_snippet",)}),
        ("Internal", {"fields": ("notes", "created_at", "updated_at")}),
    )

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "counties":
            # A checkbox grid beats typing a comma-separated list of 75 counties.
            from django import forms

            from .counties import COUNTY_CHOICES

            return forms.MultipleChoiceField(
                choices=[(c, c) for c, _ in COUNTY_CHOICES if c != "UNKNOWN"],
                widget=forms.CheckboxSelectMultiple,
                label="Counties",
                help_text=db_field.help_text,
            )
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    @admin.display(description="Counties")
    def county_list(self, obj):
        counties = obj.counties or []
        if len(counties) <= 4:
            return ", ".join(counties)
        return f"{', '.join(counties[:4])} +{len(counties) - 4} more"

    @admin.display(description="Embed code")
    def embed_snippet(self, obj):
        if not obj.pk:
            return "Save the dashboard first, then the snippet appears here."

        request = getattr(self, "_request", None)
        page_url = obj.get_absolute_url()
        loader_url = reverse("dashboard-loader", args=[obj.slug])
        if request is not None:
            page_url = request.build_absolute_uri(page_url)
            loader_url = request.build_absolute_uri(loader_url)

        snippet = (
            f'<div data-arhi-dashboard></div>\n'
            f'<script src="{loader_url}" async></script>'
        )
        iframe = (
            f'<iframe src="{page_url}" title="{obj.heading}" '
            f'style="width:100%;border:0;height:1200px"></iframe>'
        )
        box = ("width:100%;font:12px ui-monospace,SFMono-Regular,Menlo,monospace;"
               "padding:8px;border:1px solid #b8b3ac;border-radius:4px;"
               # Both colours pinned: inheriting either leaves the snippet
               # unreadable under the admin's dark theme.
               "background:#fbfaf8;color:#1a1a1a")
        return format_html(
            '<p style="margin:0 0 4px"><strong>Component</strong> — preferred. Mounts into '
            'the page, so deep links and the back button work and the map does not fight '
            'a phone\'s scroll.</p>'
            '<textarea readonly rows="3" style="{}" onclick="this.select()">{}</textarea>'
            '<p style="margin:12px 0 4px"><strong>Iframe</strong> — fallback for a site we '
            'do not control. Fixed height.</p>'
            '<textarea readonly rows="3" style="{}" onclick="this.select()">{}</textarea>'
            '<p style="margin:12px 0 0"><a href="{}" target="_blank" rel="noopener">'
            'Open this dashboard \u2192</a></p>',
            box, snippet, box, iframe, page_url,
        )

    def get_form(self, request, obj=None, **kwargs):
        self._request = request        # so the snippet can carry an absolute URL
        return super().get_form(request, obj, **kwargs)
