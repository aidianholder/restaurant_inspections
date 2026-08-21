"""Worker entry points. Kept thin so the real logic stays testable in ingest.py."""

from .ingest import run_scrape
from .scheduling import dispatch_due_scrapes


def scrape_run_task(run_id):
    return run_scrape(run_id)


def dispatch_due_scrapes_task():
    """Entry point for the recurring dispatcher registered with django-q."""
    result = dispatch_due_scrapes()
    return f"queued={result['queued']} skipped={result['skipped']}"
