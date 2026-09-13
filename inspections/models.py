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
from django.db import transaction
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
    ARKANSAS_NG911 = "arkansas_ng911", "Arkansas GIS NG911 address lookup"
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


class Embed(models.Model):
    """A configured table for a newspaper to embed.

    One row per embed. The newsroom pastes a one-line snippet; everything about
    what the table shows lives here, so it can be changed centrally without anyone
    touching their CMS.
    """

    class Sort(models.TextChoices):
        DATE_DESC = "date_desc", "Date, newest first"
        DATE_ASC = "date_asc", "Date, oldest first"
        NAME_ASC = "name_asc", "Facility name, A–Z"
        VIOLATIONS_DESC = "violations_desc", "Most violations first"

    slug = models.SlugField(max_length=80, unique=True, help_text="Appears in the embed URL.")
    publication = models.CharField(
        max_length=120, blank=True, help_text="Which paper this is for. Internal only."
    )
    county = models.CharField(max_length=64, choices=COUNTY_CHOICES)

    # Rolling window by default: a fixed range goes stale the day after it's set
    # and nobody notices for months.
    window_days = models.PositiveSmallIntegerField(
        null=True, blank=True, default=30,
        help_text="Rolling window, e.g. 30 for 'the last 30 days'. Leave blank to use fixed dates.",
    )
    date_from = models.DateField(null=True, blank=True, help_text="Fixed range, for a specific story.")
    date_to = models.DateField(null=True, blank=True)

    title_override = models.CharField(
        max_length=200, blank=True,
        help_text="Replaces the automatic '<County> County Health Inspections' heading.",
    )
    rows_per_page = models.PositiveSmallIntegerField(
        default=20, help_text="0 shows every row without paging."
    )
    row_limit = models.PositiveSmallIntegerField(
        default=500,
        help_text="Hard cap on rows sent to the browser. Paging and sorting happen "
                  "in the reader's browser, which stops being viable past ~1000.",
    )
    default_sort = models.CharField(max_length=20, choices=Sort.choices, default=Sort.DATE_DESC)
    search_enabled = models.BooleanField(
        default=True, help_text="Searches facility name and address/city."
    )
    show_report_links = models.BooleanField(
        default=True, help_text="Link each inspection to its source PDF."
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Switching this off blanks the embed everywhere it appears, "
                  "without anyone editing their CMS.",
    )
    notes = models.TextField(blank=True, help_text="Internal only.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["county", "slug"]

    def __str__(self):
        return f"{self.slug} ({self.county})"

    def clean(self):
        has_fixed = bool(self.date_from or self.date_to)
        if self.window_days and has_fixed:
            raise ValidationError(
                "Use either a rolling window or a fixed date range, not both."
            )
        if not self.window_days and not (self.date_from and self.date_to):
            raise ValidationError(
                "Set a rolling window, or both a start and end date."
            )
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValidationError({"date_to": "The end date must fall after the start date."})

    def resolved_window(self, today=None):
        """The dates this embed covers right now."""
        if self.window_days:
            today = today or datetime.date.today()
            return today - datetime.timedelta(days=self.window_days), today
        return self.date_from, self.date_to

    @property
    def heading(self):
        return self.title_override or f"{self.county} County Health Inspections"

    def date_range_label(self, today=None):
        start, end = self.resolved_window(today)
        if start.year == end.year:
            if start.month == end.month:
                return f"{start:%B %-d}–{end:%-d, %Y}"
            return f"{start:%B %-d} – {end:%B %-d, %Y}"
        return f"{start:%B %-d, %Y} – {end:%B %-d, %Y}"

    def get_absolute_url(self):
        return reverse("embed-page", args=[self.slug])


class SummaryPrompt(models.Model):
    """What the AI summariser is told to do, editable without a deploy.

    Several rows, exactly one active. Rows rather than one edit-in-place record
    because tuning a prompt means going backwards as often as forwards — "that
    rule fixed the skipping but now it's too wordy, put it back" — and a
    singleton destroys the previous wording on every save. Django's admin
    history does not rescue you there: `LogEntry` stores who changed what and
    when, but not the old field values.

    The code keeps `summarize.SYSTEM_PROMPT` as the fallback and the first
    migration seeds it as row one, the same arrangement `ViolationItem` has with
    `violation_items.py`: a fresh install works untouched, and emptying the table
    degrades to the shipped wording rather than breaking the button.
    """

    # The only substitution offered in `user_template`. Anything else is a typo,
    # and a typo here is a KeyError in the middle of somebody's deadline, so
    # clean() rejects it at save time instead.
    USER_PLACEHOLDERS = {"html"}

    name = models.CharField(
        max_length=120, unique=True,
        help_text="What you'll pick between later, e.g. 'Terser, September 2026'.",
    )
    system_prompt = models.TextField(
        help_text="The instructions. Rules 1 and 2 are load-bearing: they are what "
                  "keeps the model quoting the inspector instead of explaining what a "
                  "violation usually means. Change them only deliberately.",
    )
    user_template = models.TextField(
        blank=True,
        help_text="Optional wrapper for the export, e.g. an example of a good summary "
                  "to work from. Use {html} where the inspections should go. Leave "
                  "blank to send the export on its own, which is the default.",
    )
    model = models.CharField(
        max_length=80, blank=True,
        help_text="Overrides OPENAI_MODEL for this prompt only. Leave blank to use "
                  "whatever the environment is set to.",
    )

    is_active = models.BooleanField(
        default=False,
        help_text="Exactly one prompt is active. Ticking this unticks the others.",
    )
    notes = models.TextField(blank=True, help_text="Internal only. What you changed and why.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_active", "name"]
        constraints = [
            # Belt and braces with the save() below. "Which prompt is actually
            # live" is not a question anyone should be guessing at on deadline.
            models.UniqueConstraint(
                fields=["is_active"],
                condition=models.Q(is_active=True),
                name="only_one_active_summary_prompt",
            )
        ]

    def __str__(self):
        return f"{self.name}{' (active)' if self.is_active else ''}"

    def clean(self):
        if not self.system_prompt.strip():
            raise ValidationError({"system_prompt": "The instructions can't be empty."})

        if self.user_template.strip():
            # A sentinel rather than an empty string: "{{html}}" is an escaped
            # literal brace, not a substitution, and comparing against the
            # original text would wave it through.
            sentinel = "\x00inspections\x00"
            try:
                rendered = self.user_template.format(
                    **{key: sentinel for key in self.USER_PLACEHOLDERS}
                )
            except (KeyError, IndexError) as exc:
                raise ValidationError({
                    "user_template": f"{exc} isn't a placeholder this template can use. "
                                     f"The only one available is {{html}}; everything else "
                                     f"has to be written out in full."
                }) from exc
            except ValueError as exc:
                raise ValidationError({
                    "user_template": f"That isn't a valid template ({exc}). A literal curly "
                                     f"brace has to be doubled: {{{{ and }}}}."
                }) from exc
            if sentinel not in rendered:
                raise ValidationError({
                    "user_template": "This template never inserts the inspections. Put "
                                     "{html} where they should go, or leave the field "
                                     "blank to send them on their own.",
                })

    def validate_constraints(self, exclude=None):
        """Skip the one-active check at form level; `save()` is what enforces it.

        A ModelForm validates constraints *before* calling save(), so the admin
        would see the currently-active row alongside this one and reject the edit
        — and ticking "active" on a second prompt is precisely how you switch
        prompts. save() clears the others in the same transaction, so by the time
        anything is written the index is satisfied. The constraint stays on the
        table to catch what bypasses save(): a bulk update(), a migration, a hand
        at the dbshell.
        """
        exclude = set(exclude or ()) | {"is_active"}
        return super().validate_constraints(exclude=exclude)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.is_active:
                # Before the write, so the partial unique index never sees two.
                SummaryPrompt.objects.exclude(pk=self.pk).filter(is_active=True).update(
                    is_active=False
                )
            super().save(*args, **kwargs)

    def render_user_message(self, html):
        """The user message to send for this export."""
        return self.user_template.format(html=html) if self.user_template.strip() else html

    @classmethod
    def active(cls):
        """The live prompt, or None to fall back to the wording shipped in code."""
        return cls.objects.filter(is_active=True).first()


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
