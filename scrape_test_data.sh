for c in Conway Pulaski Faulkner Saline Lonoke White Garland;
do .venv/bin/python manage.py scrape_county --county "$c" --from 2025-09-01 --to 2026-09-01; done