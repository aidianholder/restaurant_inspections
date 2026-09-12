import datetime as dt
import json
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from inspections.models import Inspection, PriorityLevel, SummaryPrompt
from inspections.summarize import SHIPPED_PROMPT_NAME, SYSTEM_PROMPT, summarize
from inspections.tests.test_output import cite, make_facility
from inspections.tests.test_summarize import SETTINGS, block, completion, fake_export


def make_prompt(name="Terser", **kwargs):
    kwargs.setdefault("system_prompt", "Summarise each group. Invent nothing.")
    return SummaryPrompt.objects.create(name=name, **kwargs)


class SeedTests(TestCase):
    def test_the_shipped_wording_is_seeded_and_active(self):
        prompt = SummaryPrompt.objects.get(name=SHIPPED_PROMPT_NAME)
        self.assertTrue(prompt.is_active)
        self.assertEqual(prompt.system_prompt, SYSTEM_PROMPT)

    def test_only_the_seeded_row_exists(self):
        self.assertEqual(SummaryPrompt.objects.count(), 1)


class RetuneMigrationTests(TestCase):
    """0013 retunes the shipped wording for the new output shape.

    The behaviour that matters is what it does *not* touch: a newsroom's edits
    are the reason this lives in a table at all, and a migration that overwrote
    them would take that back.
    """

    def retune(self):
        from django.apps import apps

        from inspections.migrations import _latest_prompt as migration

        migration.retune(apps, None)

    def test_an_untouched_prompt_is_retuned(self):
        from inspections.migrations import _latest_prompt as migration

        SummaryPrompt.objects.filter(name=SHIPPED_PROMPT_NAME).update(
            system_prompt=migration.PREVIOUS
        )
        self.retune()

        prompt = SummaryPrompt.objects.get(name=SHIPPED_PROMPT_NAME)
        self.assertEqual(prompt.system_prompt, migration.REPLACEMENT)
        self.assertIn("Return Markdown", prompt.system_prompt)
        self.assertNotIn("HTML fragment", prompt.system_prompt)

    def test_an_edited_prompt_is_left_alone(self):
        from inspections.migrations import _latest_prompt as migration

        edited = migration.PREVIOUS + "\n8. Keep it under 20 words."
        SummaryPrompt.objects.filter(name=SHIPPED_PROMPT_NAME).update(system_prompt=edited)
        self.retune()

        self.assertEqual(
            SummaryPrompt.objects.get(name=SHIPPED_PROMPT_NAME).system_prompt, edited
        )

    def test_the_replacement_is_what_the_code_ships(self):
        """Change SYSTEM_PROMPT without a migration and existing installs keep
        the old wording — this is the reminder."""
        from inspections.migrations import _latest_prompt as migration

        self.assertEqual(migration.REPLACEMENT, SYSTEM_PROMPT)


class ActivationTests(TestCase):
    def test_activating_one_deactivates_the_rest(self):
        seeded = SummaryPrompt.objects.get(name=SHIPPED_PROMPT_NAME)
        challenger = make_prompt(is_active=True)

        seeded.refresh_from_db()
        self.assertFalse(seeded.is_active)
        self.assertTrue(challenger.is_active)
        self.assertEqual(SummaryPrompt.active(), challenger)

    def test_resaving_the_active_prompt_leaves_it_active(self):
        prompt = SummaryPrompt.objects.get(name=SHIPPED_PROMPT_NAME)
        prompt.notes = "unchanged wording, new note"
        prompt.save()
        prompt.refresh_from_db()
        self.assertTrue(prompt.is_active)

    def test_two_active_rows_are_refused_by_the_database(self):
        # save() clears the others, so reach past it to prove the constraint holds.
        make_prompt(is_active=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SummaryPrompt.objects.filter(name=SHIPPED_PROMPT_NAME).update(is_active=True)

    def test_deactivating_everything_leaves_no_active_prompt(self):
        SummaryPrompt.objects.update(is_active=False)
        self.assertIsNone(SummaryPrompt.active())


class ValidationTests(TestCase):
    def test_empty_instructions_are_refused(self):
        with self.assertRaises(ValidationError) as caught:
            SummaryPrompt(name="Blank", system_prompt="   ").full_clean()
        self.assertIn("system_prompt", caught.exception.error_dict)

    def test_unknown_placeholder_is_refused_at_save_time(self):
        prompt = SummaryPrompt(
            name="Bad", system_prompt="Do the thing.",
            user_template="Roundup for {county}:\n{html}",
        )
        with self.assertRaises(ValidationError) as caught:
            prompt.full_clean()
        self.assertIn("county", str(caught.exception))

    def test_template_without_the_placeholder_is_refused(self):
        prompt = SummaryPrompt(
            name="Bad", system_prompt="Do the thing.",
            user_template="Summarise today's inspections, please.",
        )
        with self.assertRaisesMessage(ValidationError, "never inserts the inspections"):
            prompt.full_clean()

    def test_an_escaped_brace_does_not_count_as_the_placeholder(self):
        # "{{html}}" is a literal "{html}", not a substitution. A naive check
        # that compares the rendered text to the original waves this through.
        prompt = SummaryPrompt(
            name="Bad", system_prompt="Do the thing.",
            user_template="Put the inspections where {{html}} is.",
        )
        with self.assertRaisesMessage(ValidationError, "never inserts the inspections"):
            prompt.full_clean()

    def test_a_stray_brace_is_explained_rather_than_crashing(self):
        prompt = SummaryPrompt(
            name="Bad", system_prompt="Do the thing.", user_template="{html} and { a stray",
        )
        with self.assertRaisesMessage(ValidationError, "doubled"):
            prompt.full_clean()

    def test_a_valid_template_passes(self):
        SummaryPrompt(
            name="Good", system_prompt="Do the thing.",
            user_template="Here is today's roundup:\n\n{html}",
        ).full_clean()

    def test_a_blank_template_passes(self):
        SummaryPrompt(name="Good", system_prompt="Do the thing.").full_clean()


class RenderTests(TestCase):
    def test_a_blank_template_sends_the_export_alone(self):
        prompt = make_prompt()
        self.assertEqual(prompt.render_user_message("<p>x</p>"), "<p>x</p>")

    def test_a_template_wraps_the_export(self):
        prompt = make_prompt(user_template="Roundup:\n\n{html}\n\nThanks.")
        self.assertEqual(
            prompt.render_user_message("<p>x</p>"), "Roundup:\n\n<p>x</p>\n\nThanks."
        )


@override_settings(**SETTINGS)
class SummarizeUsesTheActivePromptTests(TestCase):
    def sent(self, post):
        return post.call_args.kwargs["json"]

    def test_the_active_rows_wording_is_sent(self):
        make_prompt(system_prompt="Be terse. Invent nothing.", is_active=True)
        with patch("inspections.summarize.requests.post", return_value=completion(block("TEST DINER"))) as post:
            summary = summarize(fake_export())

        self.assertEqual(self.sent(post)["messages"][0]["content"], "Be terse. Invent nothing.")
        self.assertEqual(summary.prompt, "Terser")

    def test_the_active_rows_template_wraps_the_export(self):
        make_prompt(user_template="Roundup:\n{html}", is_active=True)
        with patch("inspections.summarize.requests.post", return_value=completion(block("TEST DINER"))) as post:
            summarize(fake_export())

        self.assertTrue(self.sent(post)["messages"][1]["content"].startswith("Roundup:\n### TEST DINER"))

    def test_a_prompts_model_override_wins_over_the_setting(self):
        make_prompt(model="some-other-model", is_active=True)
        with patch("inspections.summarize.requests.post", return_value=completion(block("TEST DINER"))) as post:
            summarize(fake_export())

        self.assertEqual(self.sent(post)["model"], "some-other-model")

    def test_a_blank_model_override_falls_back_to_the_setting(self):
        make_prompt(is_active=True)
        with patch("inspections.summarize.requests.post", return_value=completion(block("TEST DINER"))) as post:
            summarize(fake_export())

        self.assertEqual(self.sent(post)["model"], "test-model")

    def test_an_edit_takes_effect_on_the_next_call_with_no_restart(self):
        prompt = make_prompt(system_prompt="First wording.", is_active=True)
        with patch("inspections.summarize.requests.post", return_value=completion(block("TEST DINER"))) as post:
            summarize(fake_export())
        self.assertEqual(self.sent(post)["messages"][0]["content"], "First wording.")

        prompt.system_prompt = "Second wording."
        prompt.save()

        with patch("inspections.summarize.requests.post", return_value=completion(block("TEST DINER"))) as post:
            summarize(fake_export())
        self.assertEqual(self.sent(post)["messages"][0]["content"], "Second wording.")

    def test_an_emptied_table_falls_back_to_the_shipped_wording(self):
        SummaryPrompt.objects.all().delete()
        with patch("inspections.summarize.requests.post", return_value=completion(block("TEST DINER"))) as post:
            summary = summarize(fake_export())

        self.assertEqual(self.sent(post)["messages"][0]["content"], SYSTEM_PROMPT)
        self.assertEqual(summary.prompt, SHIPPED_PROMPT_NAME)

    def test_no_active_row_falls_back_to_the_shipped_wording(self):
        SummaryPrompt.objects.update(is_active=False)
        with patch("inspections.summarize.requests.post", return_value=completion(block("TEST DINER"))) as post:
            summarize(fake_export())

        self.assertEqual(self.sent(post)["messages"][0]["content"], SYSTEM_PROMPT)

    def test_an_explicit_prompt_beats_the_active_row(self):
        make_prompt(system_prompt="The active one.", is_active=True)
        other = SummaryPrompt(name="Ad hoc", system_prompt="This one instead.")
        with patch("inspections.summarize.requests.post", return_value=completion(block("TEST DINER"))) as post:
            summarize(fake_export(), prompt=other)

        self.assertEqual(self.sent(post)["messages"][0]["content"], "This one instead.")


@override_settings(**SETTINGS)
class SummaryViewReportsThePromptTests(TestCase):
    def setUp(self):
        facility = make_facility("TEST DINER")
        inspection = Inspection.objects.create(
            facility=facility, date=dt.date(2026, 8, 3), inspection_type="Routine"
        )
        cite(inspection, PriorityLevel.PRIORITY, "Milk held above 41 degrees.")

    def test_the_response_names_the_prompt_that_ran(self):
        make_prompt(is_active=True)
        with patch("inspections.summarize.requests.post", return_value=completion(block("TEST DINER"))):
            r = self.client.post(reverse("output-summary"), {
                "county": "Pulaski", "date_from": "2026-08-01", "date_to": "2026-08-31",
            })
        self.assertEqual(json.loads(r.content)["prompt"], "Terser")


class AdminTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User

        self.client.force_login(
            User.objects.create_superuser("editor", "e@example.com", "pw")
        )

    def test_the_prompt_is_listed_and_editable(self):
        prompt = SummaryPrompt.objects.get(name=SHIPPED_PROMPT_NAME)
        listing = self.client.get(reverse("admin:inspections_summaryprompt_changelist"))
        self.assertContains(listing, SHIPPED_PROMPT_NAME)

        form = self.client.get(
            reverse("admin:inspections_summaryprompt_change", args=[prompt.pk])
        )
        self.assertContains(form, "load-bearing")

    def test_the_live_prompt_cannot_be_deleted(self):
        prompt = SummaryPrompt.objects.get(name=SHIPPED_PROMPT_NAME)
        r = self.client.post(
            reverse("admin:inspections_summaryprompt_delete", args=[prompt.pk]),
            {"post": "yes"},
        )
        self.assertEqual(r.status_code, 403)
        self.assertTrue(SummaryPrompt.objects.filter(pk=prompt.pk).exists())

    def test_a_retired_prompt_can_be_deleted(self):
        retired = make_prompt(name="Retired")
        r = self.client.post(
            reverse("admin:inspections_summaryprompt_delete", args=[retired.pk]),
            {"post": "yes"},
        )
        self.assertEqual(r.status_code, 302)
        self.assertFalse(SummaryPrompt.objects.filter(pk=retired.pk).exists())

    def test_the_make_active_action_switches_the_live_prompt(self):
        challenger = make_prompt(name="Challenger")
        self.client.post(
            reverse("admin:inspections_summaryprompt_changelist"),
            {"action": "make_active", "_selected_action": [str(challenger.pk)]},
            follow=True,
        )
        self.assertEqual(SummaryPrompt.active(), challenger)

    def test_adding_an_active_prompt_switches_over_rather_than_erroring(self):
        """A ModelForm validates constraints before save(), so this is the path
        that the one-active partial index would otherwise block."""
        r = self.client.post(
            reverse("admin:inspections_summaryprompt_add"),
            {
                "name": "Terser", "system_prompt": "Be terse. Invent nothing.",
                "user_template": "", "model": "", "notes": "", "is_active": "on",
            },
        )
        self.assertEqual(r.status_code, 302, "the admin refused to save the new prompt")
        self.assertEqual(SummaryPrompt.active().name, "Terser")
        self.assertEqual(SummaryPrompt.objects.filter(is_active=True).count(), 1)

    def test_ticking_active_on_an_existing_prompt_switches_over(self):
        challenger = make_prompt(name="Challenger")
        r = self.client.post(
            reverse("admin:inspections_summaryprompt_change", args=[challenger.pk]),
            {
                "name": "Challenger", "system_prompt": challenger.system_prompt,
                "user_template": "", "model": "", "notes": "", "is_active": "on",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(SummaryPrompt.active(), challenger)

    def test_the_make_active_action_refuses_an_ambiguous_selection(self):
        a, b = make_prompt(name="A"), make_prompt(name="B")
        r = self.client.post(
            reverse("admin:inspections_summaryprompt_changelist"),
            {"action": "make_active", "_selected_action": [str(a.pk), str(b.pk)]},
            follow=True,
        )
        self.assertContains(r, "exactly one")
        self.assertEqual(SummaryPrompt.active().name, SHIPPED_PROMPT_NAME)
