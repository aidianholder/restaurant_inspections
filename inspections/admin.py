from django.contrib.gis import admin
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    Facility, GeocodeSource, Inspection, ScrapeRun, Violation, ViolationItem,
)


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


@admin.register(ScrapeRun)
class ScrapeRunAdmin(admin.ModelAdmin):
    list_display = (
        "county", "date_from", "date_to", "status", "pages_fetched",
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
