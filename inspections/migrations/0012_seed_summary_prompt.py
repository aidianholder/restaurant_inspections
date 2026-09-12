"""Seed the shipped summariser wording as the first, active prompt.

Written to be re-runnable and to never overwrite an edit: if the row already
exists, it is left exactly as the newsroom has tuned it. That is the same deal
`ViolationItem.plain_description` gets from 0008 — the whole reason this is a
table rather than a constant is that somebody's edits have to survive a deploy.
"""

from django.db import migrations

from inspections.summarize import SHIPPED_PROMPT_NAME, SYSTEM_PROMPT


def seed(apps, schema_editor):
    SummaryPrompt = apps.get_model("inspections", "SummaryPrompt")
    if SummaryPrompt.objects.exists():
        return

    SummaryPrompt.objects.create(
        name=SHIPPED_PROMPT_NAME,
        system_prompt=SYSTEM_PROMPT,
        is_active=True,
        notes="The wording this feature shipped with. Copy it to a new row before "
              "experimenting, so there is always something known-good to go back to.",
    )


def unseed(apps, schema_editor):
    apps.get_model("inspections", "SummaryPrompt").objects.filter(
        name=SHIPPED_PROMPT_NAME
    ).delete()


class Migration(migrations.Migration):

    dependencies = [("inspections", "0011_summaryprompt")]

    operations = [migrations.RunPython(seed, unseed)]
