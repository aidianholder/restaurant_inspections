"""Data model for Arkansas restaurant health inspections.

Shape: Facility 1--N Inspection 1--N Violation. Facilities are identified by the
ADH's own establishment key where the site exposes one; where it doesn't (the
key only appears on rows that have past inspections), we fall back to a
normalized name+address fingerprint and adopt the source key later if it shows up.
"""

import hashlib
import re

from django.contrib.gis.db import models
from django.urls import reverse
from django.utils.text import slugify

from .counties import COUNTY_CHOICES


class ViolationSource(models.TextChoices):
    PDF = "pdf", "Inspection report PDF"
    WEB = "web", "Website observations overlay"


class PriorityLevel(models.TextChoices):
    PRIORITY = "P", "Priority"
    PRIORITY_FOUNDATION = "PF", "Priority Foundation"
    CORE = "C", "Core"


class GeocodeSource(models.TextChoices):
    ARKANSAS_GIS = "arkansas_gis", "Arkansas GIS composite locator"
    CENSUS = "census", "Census geocoder"
    MANUAL = "manual", "Entered by hand"
    # Retained so historical rows stay readable. No longer produced: the ADH map
    # coordinates proved unreliable when checked against the state locator.
    ADH_MAP = "adh_map", "ADH map payload (retired)"


def fingerprint(name, street, zip_code):
    """Stable identity for facilities the site gives us no source key for."""
    parts = [re.sub(r"[^a-z0-9]+", "", (p or "").lower()) for p in (name, street, zip_code)]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


class Facility(models.Model):
    # ADH establishment key, from the `key` attribute on the results row. Absent
    # for establishments with no inspection history yet, hence null=True.
    source_key = models.CharField(max_length=32, unique=True, null=True, blank=True, db_index=True)
    fingerprint = models.CharField(max_length=40, unique=True, db_index=True)
    # The inspectionSequenceKey from the "Past Inspection(s)" link. Groups a
    # facility's inspection history upstream; not unique per inspection.
    source_sequence_key = models.CharField(max_length=32, blank=True, db_index=True)

    name = models.CharField(max_length=255)
    street = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120, blank=True)
    state = models.CharField(max_length=2, default="AR")
    zip_code = models.CharField(max_length=10, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    # The site renders street and city run together with no reliable delimiter.
    # Keep the original blob so a parsing miss never loses data.
    raw_address = models.CharField(max_length=400, blank=True)
    address_needs_review = models.BooleanField(default=False)
    county = models.CharField(max_length=64, blank=True, db_index=True, choices=COUNTY_CHOICES)

    # WGS84 point. geography=True so distance queries come back in meters, which
    # is what "restaurants near me" wants.
    location = models.PointField(geography=True, srid=4326, null=True, blank=True)
    geocode_source = models.CharField(
        max_length=16, blank=True, db_index=True, choices=GeocodeSource.choices
    )
    # The address the geocoder actually matched, which is not always the address
    # we sent. Worth keeping so a bad match is visible rather than silent.
    geocode_matched_address = models.CharField(max_length=400, blank=True)
    geocoded_at = models.DateTimeField(null=True, blank=True)
    # Some addresses simply aren't in the Census address ranges. Count attempts
    # so a permanent miss isn't re-queried on every subsequent scrape.
    geocode_attempts = models.PositiveSmallIntegerField(default=0)
    geocode_last_attempt = models.DateTimeField(null=True, blank=True)

    slug = models.SlugField(max_length=280, unique=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "facilities"
        ordering = ["name"]
        indexes = [models.Index(fields=["county", "name"])]

    def __str__(self):
        return f"{self.name} ({self.city})"

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(f"{self.name}-{self.city}")[:250] or "facility"
            candidate, n = base, 1
            while Facility.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                n += 1
                candidate = f"{base}-{n}"[:280]
            self.slug = candidate
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("facility-detail", args=[self.slug])

    @property
    def latitude(self):
        return self.location.y if self.location else None

    @property
    def longitude(self):
        return self.location.x if self.location else None

    @property
    def has_location(self):
        return self.location is not None

    @property
    def address_display(self):
        line2 = ", ".join(p for p in [self.city, f"{self.state} {self.zip_code}".strip()] if p)
        return ", ".join(p for p in [self.street, line2] if p)


class Inspection(models.Model):
    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name="inspections")

    date = models.DateField(db_index=True)
    inspection_type = models.CharField(max_length=120, blank=True)

    # The site exposes no per-inspection ID, so identity is
    # (facility, date, type, nth-of-that-kind-that-day). The last term is a
    # tiebreaker for the rare case of two same-type inspections on one date.
    sequence_within_day = models.PositiveSmallIntegerField(default=0)

    # Count shown in the results grid, e.g. "Observation(s) 3". Compare against
    # violations.count() to detect a detail scrape that silently came back short.
    observation_count = models.PositiveIntegerField(default=0)

    report_pdf = models.FileField(upload_to="reports/%Y/%m/", null=True, blank=True)
    report_source_url = models.URLField(max_length=500, blank=True)
    report_sha256 = models.CharField(max_length=64, blank=True)

    # The state's own per-inspection ID. It appears only inside the report PDF —
    # the results grid never exposes it.
    source_inspection_id = models.CharField(max_length=32, blank=True, db_index=True)
    # Where the current violation set came from. The PDF is a strict superset of
    # the website overlay, so it wins whenever a report is available.
    violations_source = models.CharField(
        max_length=4, blank=True, choices=ViolationSource.choices
    )
    report_parsed_at = models.DateTimeField(null=True, blank=True)

    # Null until the observations overlay has been scraped for this inspection.
    details_scraped_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]
        indexes = [models.Index(fields=["-date", "facility"])]
        constraints = [
            models.UniqueConstraint(
                fields=["facility", "date", "inspection_type", "sequence_within_day"],
                name="unique_inspection_natural_key",
            )
        ]

    def __str__(self):
        return f"{self.facility.name} — {self.date} ({self.inspection_type})"

    @property
    def details_complete(self):
        return self.details_scraped_at is not None and self.violations.count() >= self.observation_count

    @property
    def violation_count(self):
        """Real count, which for a parsed report exceeds the website's number."""
        return self.violations.count()

    @property
    def website_undercounted(self):
        """True when the site's observation count understates the report."""
        return self.violations_source == ViolationSource.PDF and (
            self.violation_count > self.observation_count
        )


class Violation(models.Model):
    inspection = models.ForeignKey(Inspection, on_delete=models.CASCADE, related_name="violations")
    ordinal = models.PositiveSmallIntegerField(default=0)

    code = models.CharField(max_length=120, blank=True)          # e.g. "20 CAR 192-501 (f)"
    code_explanation = models.TextField(blank=True)              # short label, web overlay only
    inspector_comments = models.TextField(blank=True)            # the substantive narrative

    # Only the report PDF carries these three.
    item_number = models.CharField(max_length=8, blank=True)
    priority_level = models.CharField(
        max_length=2, blank=True, db_index=True, choices=PriorityLevel.choices
    )
    correct_by = models.DateField(null=True, blank=True)

    source = models.CharField(
        max_length=4, choices=ViolationSource.choices, default=ViolationSource.WEB, db_index=True
    )

    class Meta:
        ordering = ["ordinal"]
        unique_together = [("inspection", "ordinal")]

    def __str__(self):
        return f"{self.code} ({self.inspection_id})"

    @property
    def is_priority(self):
        return self.priority_level in (PriorityLevel.PRIORITY, PriorityLevel.PRIORITY_FOUNDATION)


class ScrapeRun(models.Model):
    """One user-triggered retrieval: a county plus a date range."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Complete"
        FAILED = "failed", "Failed"

    county = models.CharField(max_length=64, choices=COUNTY_CHOICES)
    date_from = models.DateField()
    date_to = models.DateField()

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    task_id = models.CharField(max_length=64, blank=True)

    pages_fetched = models.PositiveIntegerField(default=0)
    facilities_created = models.PositiveIntegerField(default=0)
    inspections_created = models.PositiveIntegerField(default=0)
    violations_created = models.PositiveIntegerField(default=0)
    reports_downloaded = models.PositiveIntegerField(default=0)
    facilities_located = models.PositiveIntegerField(default=0)
    violations_from_pdf = models.PositiveIntegerField(default=0)

    progress_note = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.county} {self.date_from}–{self.date_to} [{self.status}]"

    @property
    def is_active(self):
        return self.status in (self.Status.QUEUED, self.Status.RUNNING)
