"""Drive the scraper and persist what it returns.

Scope rule (chosen deliberately): every inspection the site lists is stored,
because the results grid ships each facility's whole history inline for free.
The expensive per-inspection detail — the observations overlay and the report
PDF, one request each — is fetched only for inspections inside the requested
date range. Older inspections land as list-only rows that can be backfilled
later by re-running a scrape over an earlier window.
"""

import hashlib
import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import latest_inspection
from .geocoding import locate_facility
from .models import (
    Facility, Inspection, ScrapeRun, Violation, ViolationItem, ViolationSource, fingerprint,
)
from .scraper import ADHClient
from .scraper.report_pdf import parse_report

logger = logging.getLogger(__name__)


def upsert_facility(data, county):
    """Find or create a facility, preferring the ADH's own establishment key.

    Rows for establishments with no inspection history carry no key, so those
    fall back to a name+address fingerprint. If the key later appears for a
    facility we first saw without one, we adopt it rather than duplicating.
    """
    addr = data["address"]
    fp = fingerprint(data["name"], addr["street"], addr["zip_code"])
    source_key = data.get("source_key") or None

    facility = None
    if source_key:
        facility = Facility.objects.filter(source_key=source_key).first()
    if facility is None:
        facility = Facility.objects.filter(fingerprint=fp).first()

    created = False
    if facility is None:
        facility = Facility(fingerprint=fp)
        created = True

    if source_key and not facility.source_key:
        facility.source_key = source_key

    facility.name = data["name"] or facility.name
    facility.street = addr["street"]
    facility.city = addr["city"]
    facility.state = addr["state"] or "AR"
    facility.zip_code = addr["zip_code"]
    facility.raw_address = addr["raw"]
    facility.address_needs_review = addr["needs_review"]
    facility.phone = data.get("phone", "")
    facility.county = county
    if data.get("source_sequence_key"):
        facility.source_sequence_key = data["source_sequence_key"]
    if not created:
        facility.fingerprint = fp

    try:
        # Savepoint, so a rejected row leaves the surrounding transaction usable.
        # Without one the recovery query below is the statement that raises, with
        # TransactionManagementError burying whatever the database actually
        # objected to — the original error then survives only in the log.
        with transaction.atomic():
            facility.save()
    except IntegrityError:
        # Only a fingerprint collision is recoverable: another row already
        # represents this address, so reuse it rather than failing the run. Any
        # other IntegrityError is a real fault — a worker still holding
        # pre-migration models and omitting NOT NULL columns, say — and must not
        # be disguised as a collision, which is how one such bug spent a day
        # reported as a transaction error.
        existing = Facility.objects.filter(fingerprint=fp).first()
        if existing is None:
            raise
        logger.warning("Facility save collided for %r; reusing existing row", data["name"])
        facility = existing
        created = False

    return facility, created


def upsert_inspection(facility, data):
    inspection, created = Inspection.objects.get_or_create(
        facility=facility,
        date=data["date"],
        inspection_type=data["inspection_type"],
        sequence_within_day=data.get("sequence_within_day", 0),
        defaults={"observation_count": data["observation_count"]},
    )
    if not created and inspection.observation_count != data["observation_count"]:
        inspection.observation_count = data["observation_count"]
        inspection.save(update_fields=["observation_count"])
    return inspection, created


def run_scrape(run_id):
    """Execute one ScrapeRun. Called by the worker; safe to call synchronously."""
    run = ScrapeRun.objects.get(pk=run_id)
    run.status = ScrapeRun.Status.RUNNING
    run.started_at = timezone.now()
    run.error = ""
    run.save(update_fields=["status", "started_at", "error"])

    client = ADHClient(
        delay_seconds=settings.SCRAPER_DELAY_SECONDS,
        user_agent=settings.SCRAPER_USER_AGENT,
    )

    # Facilities this run touched, refreshed once at the end rather than on
    # every write. See inspections/latest_inspection.py.
    touched = set()

    try:
        page, form_state = client.search(run.county, run.date_from, run.date_to)
        total_pages = page["total_pages"]
        page_number = 1

        while True:
            run.progress_note = f"Page {page_number} of {total_pages}"
            run.pages_fetched = page_number
            run.save(update_fields=["progress_note", "pages_fetched"])

            for fdata in page["facilities"]:
                with transaction.atomic():
                    facility, fcreated = upsert_facility(fdata, run.county)
                    touched.add(facility.pk)
                    if fcreated:
                        run.facilities_created += 1

                    for idata in fdata["inspections"]:
                        inspection, icreated = upsert_inspection(facility, idata)
                        if icreated:
                            run.inspections_created += 1
                        idata["_model"] = inspection

                # Locate the facility as soon as it's identified. Outside the
                # transaction so a slow HTTP call never holds a DB lock.
                if locate_facility(facility):
                    run.facilities_located += 1

                # Detail fetches are the expensive part, so they happen only for
                # inspections inside the requested window, and outside the
                # transaction so a slow HTTP call never holds a DB lock.
                for idata in fdata["inspections"]:
                    inspection = idata["_model"]
                    if not (run.date_from <= inspection.date <= run.date_to):
                        continue
                    run.violations_created += _fetch_details(client, form_state, inspection, idata)
                    if _fetch_report(client, form_state, inspection, idata):
                        run.reports_downloaded += 1
                        # The report is a superset of the overlay, so its
                        # violations replace what the overlay produced.
                        from_pdf = parse_report_pdf(inspection)
                        if from_pdf is not None:
                            run.violations_from_pdf += from_pdf

            run.save()

            if page_number >= total_pages:
                break
            page_number += 1
            page, form_state = client.goto_page(form_state, page_number)

        run.status = ScrapeRun.Status.SUCCESS
        run.progress_note = "Complete"
    except Exception as exc:
        logger.exception("Scrape run %s failed", run_id)
        run.status = ScrapeRun.Status.FAILED
        run.error = f"{type(exc).__name__}: {exc}"
        run.progress_note = "Failed"
    finally:
        # Even a run that died partway through leaves facilities whose newest
        # inspection changed, so this belongs in `finally`. Its own failure must
        # not replace whatever error actually stopped the run.
        try:
            changed = latest_inspection.refresh(touched)
            if changed:
                logger.info("Refreshed latest-inspection columns for %s facilities", changed)
        except Exception:
            logger.exception("Could not refresh latest-inspection columns after run %s", run_id)

        run.finished_at = timezone.now()
        run.save()

    return run.status


def _fetch_details(client, form_state, inspection, idata):
    """Scrape the observations overlay for one inspection. Returns rows created."""
    if inspection.details_scraped_at is not None:
        return 0
    if not settings.SCRAPE_WEB_OBSERVATIONS:
        return 0
    if not idata.get("observation_count"):
        inspection.details_scraped_at = timezone.now()
        inspection.save(update_fields=["details_scraped_at"])
        return 0

    violations = client.fetch_violations(form_state, idata["violations_target"])
    if len(violations) < idata["observation_count"]:
        logger.warning(
            "Inspection %s: grid promised %d observations, overlay returned %d",
            inspection.pk, idata["observation_count"], len(violations),
        )

    created = 0
    with transaction.atomic():
        inspection.violations.all().delete()
        for v in violations:
            Violation.objects.create(inspection=inspection, source=ViolationSource.WEB, **v)
            created += 1
        inspection.details_scraped_at = timezone.now()
        inspection.violations_source = ViolationSource.WEB
        inspection.save(update_fields=["details_scraped_at", "violations_source"])
    return created


def _resolve_item(item_number, known_items):
    """Map the number printed on the report to a form item, or None if unknown."""
    if item_number.isdigit() and int(item_number) in known_items:
        return int(item_number)
    if item_number:
        logger.info("Unrecognised violation item number %r", item_number)
    return None


def parse_report_pdf(inspection):
    """Read violations out of a stored report PDF and make them authoritative.

    The website overlay is an incomplete view of the same inspection, so once a
    report is parsed its violations replace whatever the overlay gave us. The
    short `code_explanation` label only ever appears online, so it's carried
    across onto matching codes rather than lost.

    Returns the number of violation rows written, or None if nothing was parsed.
    """
    if not inspection.report_pdf:
        return None

    try:
        report = parse_report(inspection.report_pdf.path)
    except Exception as exc:
        logger.warning("Could not parse report for inspection %s: %s", inspection.pk, exc)
        return None

    explanations = {
        v.code: v.code_explanation
        for v in inspection.violations.all()
        if v.code and v.code_explanation
    }
    # One query for the 57 reference rows, rather than one per violation.
    known_items = set(ViolationItem.objects.values_list("number", flat=True))

    with transaction.atomic():
        inspection.violations.all().delete()
        for ordinal, row in enumerate(report["violations"]):
            Violation.objects.create(
                inspection=inspection,
                ordinal=ordinal,
                code=row["code"],
                code_explanation=explanations.get(row["code"], ""),
                inspector_comments=row["comment"],
                item_number=row["item_number"],
                item_id=_resolve_item(row["item_number"], known_items),
                priority_level=row["priority_level"],
                correct_by=row["correct_by"],
                source=ViolationSource.PDF,
            )

        if report["inspection_id"]:
            inspection.source_inspection_id = report["inspection_id"]
        inspection.violations_source = ViolationSource.PDF
        inspection.report_parsed_at = timezone.now()
        inspection.save(
            update_fields=["source_inspection_id", "violations_source", "report_parsed_at"]
        )

    return len(report["violations"])


def _fetch_report(client, form_state, inspection, idata):
    """Download the inspection report PDF if we don't already have it."""
    if inspection.report_pdf or not idata.get("report_target"):
        return False

    content, url = client.fetch_report_pdf(form_state, idata["report_target"])
    if not content:
        return False

    digest = hashlib.sha256(content).hexdigest()
    name = f"{inspection.facility_id}-{inspection.date:%Y%m%d}-{digest[:12]}.pdf"
    inspection.report_pdf.save(name, ContentFile(content), save=False)
    inspection.report_source_url = url
    inspection.report_sha256 = digest
    inspection.save(update_fields=["report_pdf", "report_source_url", "report_sha256"])
    return True
