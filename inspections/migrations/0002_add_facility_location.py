"""Store facility locations as a PostGIS point.

Hand-ordered: install the extension, add the point column and carry any existing
decimal coordinates into it, and only then drop the old columns, so no data is
lost on an existing database.
"""

import django.contrib.gis.db.models.fields
from django.contrib.postgres.operations import CreateExtension
from django.db import migrations, models
from django.utils import timezone


def copy_coordinates_to_point(apps, schema_editor):
    from django.contrib.gis.geos import Point

    Facility = apps.get_model("inspections", "Facility")
    moved = 0
    for facility in Facility.objects.exclude(latitude=None).exclude(longitude=None):
        facility.location = Point(float(facility.longitude), float(facility.latitude), srid=4326)
        # These predate provenance tracking; they came from the ADH map payload.
        facility.geocode_source = "adh_map"
        facility.geocoded_at = timezone.now()
        facility.save(update_fields=["location", "geocode_source", "geocoded_at"])
        moved += 1
    if moved:
        print(f"  carried {moved} coordinate pair(s) into location")


def point_back_to_coordinates(apps, schema_editor):
    Facility = apps.get_model("inspections", "Facility")
    for facility in Facility.objects.exclude(location=None):
        facility.latitude = facility.location.y
        facility.longitude = facility.location.x
        facility.save(update_fields=["latitude", "longitude"])


class Migration(migrations.Migration):

    dependencies = [
        ("inspections", "0001_initial"),
    ]

    operations = [
        CreateExtension("postgis"),
        migrations.AddField(
            model_name="facility",
            name="location",
            field=django.contrib.gis.db.models.fields.PointField(
                blank=True, geography=True, null=True, srid=4326
            ),
        ),
        migrations.AddField(
            model_name="facility",
            name="geocode_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("adh_map", "ADH map payload"),
                    ("census", "Census geocoder"),
                    ("manual", "Entered by hand"),
                ],
                db_index=True,
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="facility",
            name="geocode_matched_address",
            field=models.CharField(blank=True, max_length=400),
        ),
        migrations.AddField(
            model_name="facility",
            name="geocoded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(copy_coordinates_to_point, point_back_to_coordinates),
        migrations.RemoveField(model_name="facility", name="latitude"),
        migrations.RemoveField(model_name="facility", name="longitude"),
        migrations.AddField(
            model_name="scraperun",
            name="facilities_located",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
