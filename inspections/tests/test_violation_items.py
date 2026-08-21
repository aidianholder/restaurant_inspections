"""The form-item lookup: seeding, resolution, and reader-facing text."""

from django.test import TestCase

from inspections.models import Violation, ViolationItem
from inspections.violation_items import CATCH_ALL_NUMBER, VIOLATION_ITEMS


class SeedTests(TestCase):
    """The seed runs as a migration, so it is already applied in the test DB."""

    def test_all_fifty_seven_items_exist(self):
        self.assertEqual(ViolationItem.objects.count(), 57)
        numbers = set(ViolationItem.objects.values_list("number", flat=True))
        self.assertEqual(numbers, set(range(1, 58)))

    def test_sections_split_at_twenty_nine(self):
        """1–29 are risk factors; 30–57 are good retail practices."""
        self.assertEqual(
            ViolationItem.objects.get(number=29).section, ViolationItem.Section.RISK_FACTORS
        )
        self.assertEqual(
            ViolationItem.objects.get(number=30).section,
            ViolationItem.Section.GOOD_RETAIL_PRACTICES,
        )

    def test_every_item_has_official_text_and_a_subsection(self):
        for item in ViolationItem.objects.all():
            self.assertTrue(item.official_description, f"item {item.number} has no description")
            self.assertTrue(item.subsection, f"item {item.number} has no subsection")

    def test_stray_glyph_stripped_from_item_26(self):
        """11 of 188 reports bled an 'H' into this cell; the majority text wins."""
        item = ViolationItem.objects.get(number=26)
        self.assertEqual(
            item.official_description, "Pasteurized foods used; prohibited foods not offered"
        )

    def test_catch_all_has_reader_friendly_wording(self):
        item = ViolationItem.objects.get(number=CATCH_ALL_NUMBER)
        self.assertIn("Code Number must be noted", item.official_description)
        self.assertEqual(item.display_description, "Other violations")

    def test_module_and_table_agree(self):
        self.assertEqual(len(VIOLATION_ITEMS), 57)


class DisplayDescriptionTests(TestCase):
    def test_official_text_used_when_no_plain_wording(self):
        item = ViolationItem.objects.get(number=1)
        self.assertEqual(item.display_description, item.official_description)

    def test_plain_wording_overrides_official(self):
        item = ViolationItem.objects.get(number=22)
        item.plain_description = "Cold food kept too warm"
        item.save()
        self.assertEqual(item.display_description, "Cold food kept too warm")


class ResolutionTests(TestCase):
    def setUp(self):
        from inspections.models import Facility, Inspection, fingerprint
        import datetime as dt

        facility = Facility.objects.create(name="X", fingerprint=fingerprint("X", "", ""))
        self.inspection = Inspection.objects.create(
            facility=facility, date=dt.date(2026, 1, 1), inspection_type="Routine"
        )

    def make(self, item_number, item_id=None):
        return Violation.objects.create(
            inspection=self.inspection, ordinal=Violation.objects.count(),
            item_number=item_number, item_id=item_id,
        )

    def test_short_description_comes_from_the_linked_item(self):
        v = self.make("22", item_id=22)
        self.assertEqual(v.short_description, "Proper cold holding temperatures")

    def test_unlinked_violation_has_no_short_description(self):
        """An unrecognised number must degrade quietly, not raise."""
        v = self.make("999")
        self.assertEqual(v.short_description, "")

    def test_raw_item_number_is_preserved_alongside_the_link(self):
        v = self.make("22", item_id=22)
        self.assertEqual(v.item_number, "22")
        self.assertEqual(v.item.number, 22)


class IngestResolutionTests(TestCase):
    def test_resolver_maps_known_numbers_and_rejects_others(self):
        from inspections.ingest import _resolve_item

        known = set(range(1, 58))
        self.assertEqual(_resolve_item("39", known), 39)
        self.assertIsNone(_resolve_item("99", known))
        self.assertIsNone(_resolve_item("H24", known))
        self.assertIsNone(_resolve_item("", known))
