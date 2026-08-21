"""Work out when each county's next scrape is due, and fire the ones that are.

A single dispatcher runs every few minutes, rather than one system-level timer per
county. That keeps the schedule as data — visible and editable in the admin,
auditable after the fact — instead of as configuration on one machine, and it means
adding a county never needs shell access.

All arithmetic is done in the project's local timezone, because "every Monday at
2am" means local 2am on both sides of a daylight-saving change.
"""

import datetime
import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def _aware(naive):
    return timezone.make_aware(naive, timezone.get_current_timezone())


def _at_time(day, run_at):
    return _aware(datetime.datetime.combine(day, run_at))


def _add_months(day, months):
    """Advance a date by whole months, holding the day-of-month steady.

    Safe because day_of_month is capped at 28, so every month has the date.
    """
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return datetime.date(year, month, day.day)


def compute_next_run(schedule, after=None):
    """The first occurrence of this schedule strictly after `after`."""
    after = after or timezone.now()
    local_after = timezone.localtime(after)
    today = local_after.date()
    cadence = schedule.cadence
    Cadence = type(schedule).Cadence

    if cadence == Cadence.DAILY:
        candidate = _at_time(today, schedule.run_at)
        if candidate <= after:
            candidate = _at_time(today + datetime.timedelta(days=1), schedule.run_at)
        return candidate

    if cadence in (Cadence.WEEKLY, Cadence.BIWEEKLY):
        target = schedule.day_of_week if schedule.day_of_week is not None else 0
        days_ahead = (target - today.weekday()) % 7
        candidate = _at_time(today + datetime.timedelta(days=days_ahead), schedule.run_at)
        if candidate <= after:
            candidate = _at_time(
                today + datetime.timedelta(days=days_ahead + 7), schedule.run_at
            )
        if cadence == Cadence.BIWEEKLY and schedule.last_queued_at:
            # Biweekly means the same weekday, but a fortnight apart. If the next
            # matching weekday is less than 14 days from the last run, skip a week.
            last = timezone.localtime(schedule.last_queued_at).date()
            while (candidate.date() - last).days < 14:
                candidate = _at_time(candidate.date() + datetime.timedelta(days=7), schedule.run_at)
        return candidate

    # Monthly.
    day_of_month = schedule.day_of_month or 1
    try:
        this_month = today.replace(day=day_of_month)
    except ValueError:                       # defensive; the field is capped at 28
        this_month = today.replace(day=28)
    candidate = _at_time(this_month, schedule.run_at)
    if candidate <= after:
        candidate = _at_time(_add_months(this_month, 1), schedule.run_at)
    return candidate


def due_schedules(now=None):
    from .models import ScrapeSchedule

    now = now or timezone.now()
    return ScrapeSchedule.objects.filter(
        is_active=True, next_run_at__isnull=False, next_run_at__lte=now
    ).order_by("next_run_at")


def dispatch_due_scrapes(now=None, queue=True):
    """Queue a scrape for every county that is due. Returns a summary dict.

    Safe to call as often as you like: a county already being scraped is skipped
    rather than queued twice, and every schedule advances to its next occurrence
    whether or not it fired, so a backlog can never build up.
    """
    from django_q.tasks import async_task

    from .models import ScrapeRun, ScrapeSchedule

    now = now or timezone.now()
    queued, skipped = [], []

    for schedule in due_schedules(now):
        in_flight = ScrapeRun.objects.filter(
            county=schedule.county,
            status__in=(ScrapeRun.Status.QUEUED, ScrapeRun.Status.RUNNING),
        ).exists()

        if in_flight:
            # The previous run is still going. Skip this turn rather than doubling
            # the load on the state's server, and try again next occurrence.
            logger.warning("Skipping %s: a scrape is already in flight", schedule.county)
            skipped.append(schedule.county)
        else:
            date_from, date_to = schedule.window_for(timezone.localtime(now).date())
            run = ScrapeRun.objects.create(
                county=schedule.county, date_from=date_from, date_to=date_to, schedule=schedule
            )
            if queue:
                run.task_id = async_task(
                    "inspections.tasks.scrape_run_task", run.pk, task_name=f"scrape-{run.pk}"
                )
                run.save(update_fields=["task_id"])
            schedule.last_queued_at = now
            schedule.last_run = run
            queued.append(schedule.county)
            logger.info(
                "Queued scheduled scrape for %s (%s to %s)", schedule.county, date_from, date_to
            )

        schedule.next_run_at = compute_next_run(schedule, after=now)
        schedule.save(update_fields=["next_run_at", "last_queued_at", "last_run"])

    return {"queued": queued, "skipped": skipped, "checked_at": now}
