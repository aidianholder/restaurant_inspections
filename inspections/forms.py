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


class FacilityFilterForm(forms.Form):
    """Filters for the browse view. Every field is optional.

    The date range targets each facility's *most recent* inspection, so it answers
    "who was last inspected in this window" rather than "who has any inspection in
    it". Either bound may be given on its own.
    """

    q = forms.CharField(
        required=False,
        label="Search",
        widget=forms.TextInput(attrs={"placeholder": "Name or city"}),
    )
    # A plain CharField with a Select widget, deliberately: restricting valid
    # values to counties currently in the database would make a bookmarked
    # ?county=X invalid once X's data is gone, and an invalid field would drop
    # the *other* filters too. An unknown county should return nothing, not
    # everything.
    county = forms.CharField(required=False, widget=forms.Select(choices=[]))
    date_from = forms.DateField(
        required=False, label="Last inspected from", widget=forms.DateInput(attrs={"type": "date"})
    )
    date_to = forms.DateField(
        required=False, label="to", widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args, counties=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["county"].widget.choices = [("", "All counties")] + [(c, c) for c in counties]

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if start and end and start > end:
            raise forms.ValidationError("The start date must come before the end date.")
        return cleaned

    @property
    def is_filtered(self):
        """True when the user actually narrowed anything."""
        if not self.is_bound:
            return False
        cleaned = getattr(self, "cleaned_data", {})
        return any(cleaned.get(name) for name in self.fields)
