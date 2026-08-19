"""Worker entry points. Kept thin so the real logic stays testable in ingest.py."""

from .ingest import run_scrape


def scrape_run_task(run_id):
    return run_scrape(run_id)
