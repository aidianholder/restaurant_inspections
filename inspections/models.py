"""Data model for Arkansas restaurant health inspections.

Shape: Facility 1--N Inspection 1--N Violation. Facilities are identified by the
ADH's own establishment key where the site exposes one; where it doesn't (the
key only appears on rows that have past inspections), we fall back to a
normalized name+address fingerprint and adopt the source key later if it shows up.
"""

import datetime
import hashlib
import re

from django.contrib.gis.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
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


class ViolationItem(models.Model):
    """One of the 57 numbered items on the state's inspection form.

    Reference data, seeded from `violation_items.py`. The number is the primary
    key because it *is* the identifier — the reports cite it directly.

    `official_description` is the state's own wording, kept verbatim.
    `plain_description` is ours, for readers, and is never overwritten by a
    re-seed — that's the whole reason this is a table rather than a dict.
    """

    class Section(models.TextChoices):
        RISK_FACTORS = "RISK_FACTORS", "Foodborne illness risk factors"
        GOOD_RETAIL_PRACTICES = "GOOD_RETAIL_PRACTICES", "Good retail practices"

    number = models.PositiveSmallIntegerField(primary_key=True)
    section = models.CharField(max_length=24, choices=Section.choices, db_index=True)
    subsection = models.CharField(max_length=120, blank=True)
    official_description = models.TextField()
    plain_description = models.TextField(
        blank=True, help_text="Reader-facing wording. Overrides the official text on the site."
    )

    class Meta:
        ordering = ["number"]

    def __str__(self):
        return f"{self.number}. {self.display_description}"

    @property
    def display_description(self):
        return self.plain_description or self.official_description


class Violation(models.Model):
    inspection = models.ForeignKey(Inspection, on_delete=models.CASCADE, related_name="violations")
    ordinal = models.PositiveSmallIntegerField(default=0)

    code = models.CharField(max_length=120, blank=True)          # e.g. "20 CAR 192-501 (f)"
    code_explanation = models.TextField(blank=True)              # short label, web overlay only
    inspector_comments = models.TextField(blank=True)            # the substantive narrative

    # Only the report PDF carries these three.
    # The raw string stays as printed; `item` is the resolved lookup, left null
    # when the number is unrecognised so an odd value never fails an import.
    item_number = models.CharField(max_length=8, blank=True)
    item = models.ForeignKey(
        ViolationItem, on_delete=models.PROTECT, null=True, blank=True, related_name="violations"
    )
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

    @property
    def short_description(self):
        """What a reader sees before opening the inspector's notes."""
        return self.item.display_description if self.item_id else ""


class ScrapeSchedule(models.Model):
    """A standing instruction to re-scrape one county on a cadence.

    One row per county. The dispatcher wakes periodically, fires anything whose
    `next_run_at` has passed, and computes the following occurrence.
    """

    class Cadence(models.TextChoices):
        DAILY = "daily", "Every day"
        WEEKLY = "weekly", "Every week"
        BIWEEKLY = "biweekly", "Every two weeks"
        MONTHLY = "monthly", "Every month"

    # Minimum lookback per cadence. The state backdates: an inspection can appear
    # online days or weeks after it happened, so a window equal to the interval
    # silently drops records. Overlap is nearly free because ingest is idempotent.
    MINIMUM_LOOKBACK = {
        Cadence.DAILY: 7,
        Cadence.WEEKLY: 14,
        Cadence.BIWEEKLY: 21,
        Cadence.MONTHLY: 45,
    }

    DAYS_OF_WEEK = [
        (0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"),
        (4, "Friday"), (5, "Saturday"), (6, "Sunday"),
    ]

    county = models.CharField(max_length=64, unique=True, choices=COUNTY_CHOICES)
    cadence = models.CharField(max_length=10, choices=Cadence.choices, default=Cadence.WEEKLY)

    # Which day the run lands on: weekday for weekly/biweekly, date for monthly,
    # ignored for daily.
    day_of_week = models.PositiveSmallIntegerField(
        choices=DAYS_OF_WEEK, null=True, blank=True,
        help_text="For weekly and biweekly schedules.",
    )
    day_of_month = models.PositiveSmallIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(28)],
        help_text="For monthly schedules. Capped at 28 so every month has the date.",
    )
    run_at = models.TimeField(
        default=datetime.time(2, 0),
        help_text="Local time of day to start. Overnight is kindest to the state's server.",
    )

    lookback_days = models.PositiveSmallIntegerField(
        default=30,
        help_text="How far back each run searches. Must overlap the interval — "
                  "inspections often appear online well after the date they happened.",
    )

    is_active = models.BooleanField(default=True)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_queued_at = models.DateTimeField(null=True, blank=True)
    last_run = models.ForeignKey(
        "ScrapeRun", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["county"]

    def __str__(self):
        return f"{self.county}: {self.get_cadence_display().lower()} ({self.schedule_description})"

    @property
    def schedule_description(self):
        time_text = self.run_at.strftime("%-I:%M %p").lower()
        if self.cadence == self.Cadence.DAILY:
            return f"daily at {time_text}"
        if self.cadence in (self.Cadence.WEEKLY, self.Cadence.BIWEEKLY):
            day = dict(self.DAYS_OF_WEEK).get(self.day_of_week, "?")
            every = "every" if self.cadence == self.Cadence.WEEKLY else "every other"
            return f"{every} {day} at {time_text}"
        return f"day {self.day_of_month} of each month at {time_text}"

    @property
    def minimum_lookback(self):
        return self.MINIMUM_LOOKBACK.get(self.cadence, 14)

    def clean(self):
        errors = {}
        if self.cadence in (self.Cadence.WEEKLY, self.Cadence.BIWEEKLY) and self.day_of_week is None:
            errors["day_of_week"] = "Choose a day of the week for this cadence."
        if self.cadence == self.Cadence.MONTHLY and self.day_of_month is None:
            errors["day_of_month"] = "Choose a day of the month for this cadence."
        if self.lookback_days and self.lookback_days < self.minimum_lookback:
            errors["lookback_days"] = (
                f"Use at least {self.minimum_lookback} days for this cadence. The state "
                f"publishes inspections days or weeks after they happen, so a window that "
                f"only just covers the interval will silently miss records. Re-scraping an "
                f"overlapping window is cheap — nothing is stored twice."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.next_run_at is None:
            from .scheduling import compute_next_run

            self.next_run_at = compute_next_run(self)
        super().save(*args, **kwargs)

    def reschedule(self, after=None):
        """Recompute the next occurrence, e.g. after the timing fields change."""
        from .scheduling import compute_next_run

        self.next_run_at = compute_next_run(self, after=after)
        return self.next_run_at

    def window_for(self, run_date):
        """The date range a run started on `run_date` should request."""
        return run_date - datetime.timedelta(days=self.lookback_days), run_date


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

    # Set when a run was queued by a schedule rather than by hand.
    schedule = models.ForeignKey(
        ScrapeSchedule, on_delete=models.SET_NULL, null=True, blank=True, related_name="runs"
    )

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
