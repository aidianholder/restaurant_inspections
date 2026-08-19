import datetime as dt

from django import forms

from .counties import COUNTY_IDS
from .models import ScrapeRun

# Widest window the site will accept in one run before it becomes unreasonable
# to ask of the state's server in a single request.
MAX_RANGE_DAYS = 366


class ScrapeRequestForm(forms.ModelForm):
    county = forms.ChoiceField(
        choices=[("", "Select a county…")] + [(c, c) for c in sorted(COUNTY_IDS) if c != "UNKNOWN"],
    )
    date_from = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        initial=lambda: dt.date.today() - dt.timedelta(days=30),
    )
    date_to = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        initial=dt.date.today,
    )

    class Meta:
        model = ScrapeRun
        fields = ["county", "date_from", "date_to"]

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if start and end:
            if start > end:
                raise forms.ValidationError("The start date must come before the end date.")
            if (end - start).days > MAX_RANGE_DAYS:
                raise forms.ValidationError(
                    f"Please request {MAX_RANGE_DAYS} days or fewer at a time."
                )
            if start > dt.date.today():
                raise forms.ValidationError("The start date is in the future.")
        return cleaned
