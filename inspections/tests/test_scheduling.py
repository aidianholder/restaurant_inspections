"""Scheduling: when the next run lands, and what the dispatcher does about it."""

import datetime as dt
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from inspections.models import ScrapeRun, ScrapeSchedule
from inspections.scheduling import compute_next_run, dispatch_due_scrapes, due_schedules

CHICAGO = timezone.get_current_timezone()


def local(year, month, day, hour=0, minute=0):
    return dt.datetime(year, month, day, hour, minute, tzinfo=CHICAGO)


def make(cadence=ScrapeSchedule.Cadence.WEEKLY, **kwargs):
    defaults = {
        "county": "Pulaski",
        "cadence": cadence,
        "day_of_week": 0,          # Monday
        "run_at": dt.time(2, 0),
        "lookback_days": 30,
    }
    defaults.update(kwargs)
    return ScrapeSchedule(**defaults)


class NextRunTests(TestCase):
    def test_weekly_lands_on_the_chosen_weekday_and_time(self):
        # Wednesday 21 Aug 2026 → the next Monday is 24 Aug.
        schedule = make()
        nxt = compute_next_run(schedule, after=local(2026, 8, 19, 10, 0))
        self.assertEqual(timezone.localtime(nxt).weekday(), 0)
        self.assertEqual(timezone.localtime(nxt).date(), dt.date(2026, 8, 24))
        self.assertEqual(timezone.localtime(nxt).hour, 2)

    def test_weekly_skips_to_next_week_when_todays_slot_has_passed(self):
        """It is already Monday 3am; the 2am slot is gone."""
        schedule = make()
        nxt = compute_next_run(schedule, after=local(2026, 8, 24, 3, 0))
        self.assertEqual(timezone.localtime(nxt).date(), dt.date(2026, 8, 31))

    def test_weekly_takes_todays_slot_when_still_ahead(self):
        schedule = make()
        nxt = compute_next_run(schedule, after=local(2026, 8, 24, 1, 0))
        self.assertEqual(timezone.localtime(nxt).date(), dt.date(2026, 8, 24))

    def test_daily_rolls_to_tomorrow(self):
        schedule = make(cadence=ScrapeSchedule.Cadence.DAILY, day_of_week=None, lookback_days=7)
        nxt = compute_next_run(schedule, after=local(2026, 8, 19, 6, 0))
        self.assertEqual(timezone.localtime(nxt).date(), dt.date(2026, 8, 20))
        self.assertEqual(timezone.localtime(nxt).hour, 2)

    def test_monthly_uses_the_chosen_date(self):
        schedule = make(
            cadence=ScrapeSchedule.Cadence.MONTHLY, day_of_week=None,
            day_of_month=5, lookback_days=45,
        )
        nxt = compute_next_run(schedule, after=local(2026, 8, 19))
        self.assertEqual(timezone.localtime(nxt).date(), dt.date(2026, 9, 5))

    def test_monthly_rolls_across_the_year_boundary(self):
        schedule = make(
            cadence=ScrapeSchedule.Cadence.MONTHLY, day_of_week=None,
            day_of_month=5, lookback_days=45,
        )
        nxt = compute_next_run(schedule, after=local(2026, 12, 20))
        self.assertEqual(timezone.localtime(nxt).date(), dt.date(2027, 1, 5))

    def test_biweekly_keeps_a_fortnight_between_runs(self):
        schedule = make(cadence=ScrapeSchedule.Cadence.BIWEEKLY, lookback_days=21)
        schedule.last_queued_at = local(2026, 8, 24, 2, 0)     # ran this Monday
        nxt = compute_next_run(schedule, after=local(2026, 8, 24, 3, 0))
        # The following Monday is only 7 days out, so it must skip to the next.
        self.assertEqual(timezone.localtime(nxt).date(), dt.date(2026, 9, 7))

    def test_run_time_survives_a_daylight_saving_change(self):
        """2am local before and after the November change, not 1am or 3am."""
        schedule = make(cadence=ScrapeSchedule.Cadence.DAILY, day_of_week=None, lookback_days=7)
        for after in (local(2026, 10, 15, 12, 0), local(2026, 11, 15, 12, 0)):
            nxt = compute_next_run(schedule, after=after)
            self.assertEqual(timezone.localtime(nxt).hour, 2)


class ValidationTests(TestCase):
    def test_weekly_requires_a_weekday(self):
        with self.assertRaises(ValidationError) as ctx:
            make(day_of_week=None).full_clean()
        self.assertIn("day_of_week", ctx.exception.error_dict)

    def test_monthly_requires_a_day_of_month(self):
        schedule = make(cadence=ScrapeSchedule.Cadence.MONTHLY, day_of_week=None, lookback_days=45)
        with self.assertRaises(ValidationError) as ctx:
            schedule.full_clean()
        self.assertIn("day_of_month", ctx.exception.error_dict)

    def test_lookback_shorter_than_the_interval_is_rejected(self):
        """The failure this prevents is silent, so it's worth blocking at entry."""
        with self.assertRaises(ValidationError) as ctx:
            make(lookback_days=7).full_clean()
        self.assertIn("lookback_days", ctx.exception.error_dict)

    def test_generous_lookback_is_accepted(self):
        make(lookback_days=45).full_clean()      # must not raise


class DispatcherTests(TestCase):
    def setUp(self):
        self.schedule = make(lookback_days=30)
        self.schedule.save()

    def test_saving_computes_the_first_run(self):
        self.assertIsNotNone(self.schedule.next_run_at)

    def test_nothing_fires_before_it_is_due(self):
        self.assertEqual(list(due_schedules(timezone.now())), [])

    def test_due_schedule_is_queued_with_the_right_window(self):
        now = timezone.now()
        ScrapeSchedule.objects.update(next_run_at=now - dt.timedelta(minutes=1))
        with patch("django_q.tasks.async_task", return_value="task-1") as mock:
            result = dispatch_due_scrapes(now)
        self.assertEqual(result["queued"], ["Pulaski"])
        mock.assert_called_once()

        run = ScrapeRun.objects.get()
        self.assertEqual(run.county, "Pulaski")
        self.assertEqual(run.schedule_id, self.schedule.pk)
        self.assertEqual((run.date_to - run.date_from).days, 30)

    def test_next_run_advances_so_it_cannot_fire_repeatedly(self):
        now = timezone.now()
        ScrapeSchedule.objects.update(next_run_at=now - dt.timedelta(minutes=1))
        with patch("django_q.tasks.async_task", return_value="t"):
            dispatch_due_scrapes(now)
            second = dispatch_due_scrapes(now)
        self.assertEqual(second["queued"], [])
        self.assertEqual(ScrapeRun.objects.count(), 1)
        self.schedule.refresh_from_db()
        self.assertGreater(self.schedule.next_run_at, now)

    def test_county_already_being_scraped_is_skipped_not_doubled(self):
        ScrapeRun.objects.create(
            county="Pulaski", date_from=dt.date(2026, 8, 1), date_to=dt.date(2026, 8, 20),
            status=ScrapeRun.Status.RUNNING,
        )
        now = timezone.now()
        ScrapeSchedule.objects.update(next_run_at=now - dt.timedelta(minutes=1))
        with patch("django_q.tasks.async_task") as mock:
            result = dispatch_due_scrapes(now)
        self.assertEqual(result["skipped"], ["Pulaski"])
        mock.assert_not_called()
        self.assertEqual(ScrapeRun.objects.count(), 1)

    def test_skipped_schedule_still_advances(self):
        """A stuck county must not re-trigger on every dispatcher tick."""
        ScrapeRun.objects.create(
            county="Pulaski", date_from=dt.date(2026, 8, 1), date_to=dt.date(2026, 8, 20),
            status=ScrapeRun.Status.RUNNING,
        )
        now = timezone.now()
        ScrapeSchedule.objects.update(next_run_at=now - dt.timedelta(minutes=1))
        dispatch_due_scrapes(now)
        self.schedule.refresh_from_db()
        self.assertGreater(self.schedule.next_run_at, now)

    def test_inactive_schedules_are_ignored(self):
        now = timezone.now()
        ScrapeSchedule.objects.update(next_run_at=now - dt.timedelta(minutes=1), is_active=False)
        with patch("django_q.tasks.async_task") as mock:
            result = dispatch_due_scrapes(now)
        self.assertEqual(result["queued"], [])
        mock.assert_not_called()

    def test_one_schedule_per_county(self):
        from django.db.utils import IntegrityError

        with self.assertRaises(IntegrityError):
            ScrapeSchedule.objects.create(
                county="Pulaski", cadence=ScrapeSchedule.Cadence.DAILY, run_at=dt.time(3, 0)
            )
