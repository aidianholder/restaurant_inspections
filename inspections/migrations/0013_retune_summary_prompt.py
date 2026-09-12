"""Retune the shipped prompt for the new output shape — but only if untouched.

The export no longer groups by date and no longer carries report links, so a
prompt telling the model to preserve "every date heading" and "every report
link" is describing a document that does not arrive any more, and says nothing
about the <h1> that does.

Only a row still holding the previous wording verbatim is updated. Anything
edited is left alone — the point of putting this in a table was that somebody's
tuning survives a deploy, and a migration that overwrites it would take that
back. An install whose prompt has been edited keeps it, and the two stale
clauses are worth a look next time someone opens the admin.

Both texts are written out in full rather than imported from `summarize`, so
this migration keeps meaning the same thing after the code moves on again.
"""

from django.db import migrations

PREVIOUS = (
    "You are preparing a newspaper's health inspection roundup for publication. The user message is an HTML fragment listing food service establishments and the violations each one was cited for, grouped under \"Priority\", \"Priority Foundation\" and \"Core\" headings.\n"
    "\n"
    "For each establishment, summarise each group of violations separately. Replace the list items under a group heading with a single list item containing one shortened summary of all the observations in that group.\n"
    "\n"
    "Rules:\n"
    "\n"
    "1. Use only the information in the text you were given. Do not infer, assume or add anything else you know about these violations, these establishments, food safety, or the health code. If a detail is not in the source text, it does not go in the summary.\n"
    "2. Summarise each group on its own. A \"Priority\" summary covers only that establishment's priority observations, and likewise for \"Priority Foundation\" and \"Core\". Never merge observations across groups or across establishments.\n"
    "3. Do not combine violations -- each violation should be considered separately. You can combine multiple violations into a single sentence, but it should always be clear that they are separate violations.\n"
    "4. Do not skip violations - each violation should be included in the summary.\n"
    "5. Keep everything else exactly as it appears: the explanatory paragraphs at the top, every date heading, every establishment name, address and inspection type, every group heading, and every report link.\n"
    "6. Return valid HTML using the same tags and structure as the input, changing only the contents of the lists.\n"
    "7. Return the HTML and nothing else \u2014 no Markdown code fences, no preamble, no closing remarks."
)

REPLACEMENT = (
    "You are preparing a newspaper's health inspection roundup for publication. The user message is an HTML fragment: a heading, a few explanatory paragraphs, then every food service establishment inspected in that period in alphabetical order, with the violations each one was cited for grouped under \"Priority\", \"Priority Foundation\" and \"Core\" headings.\n"
    "\n"
    "For each establishment, summarise each group of violations separately. Replace the list items under a group heading with a single list item containing one shortened summary of all the observations in that group.\n"
    "\n"
    "Rules:\n"
    "\n"
    "1. Use only the information in the text you were given. Do not infer, assume or add anything else you know about these violations, these establishments, food safety, or the health code. If a detail is not in the source text, it does not go in the summary.\n"
    "2. Summarise each group on its own. A \"Priority\" summary covers only that establishment's priority observations, and likewise for \"Priority Foundation\" and \"Core\". Never merge observations across groups or across establishments.\n"
    "3. Do not combine violations -- each violation should be considered separately. You can combine multiple violations into a single sentence, but it should always be clear that they are separate violations.\n"
    "4. Do not skip violations - each violation should be included in the summary.\n"
    "5. Keep everything else exactly as it appears: the heading at the top, the explanatory paragraphs below it, every establishment name, address and inspection type, and every group heading. Keep the establishments in the order they are given.\n"
    "6. Return valid HTML using the same tags and structure as the input, changing only the contents of the lists.\n"
    "7. Return the HTML and nothing else \u2014 no Markdown code fences, no preamble, no closing remarks."
)


def retune(apps, schema_editor):
    SummaryPrompt = apps.get_model("inspections", "SummaryPrompt")
    SummaryPrompt.objects.filter(system_prompt=PREVIOUS).update(system_prompt=REPLACEMENT)


def revert(apps, schema_editor):
    SummaryPrompt = apps.get_model("inspections", "SummaryPrompt")
    SummaryPrompt.objects.filter(system_prompt=REPLACEMENT).update(system_prompt=PREVIOUS)


class Migration(migrations.Migration):

    dependencies = [("inspections", "0012_seed_summary_prompt")]

    operations = [migrations.RunPython(retune, revert)]
