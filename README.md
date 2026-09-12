# Arkansas Restaurant Health Inspections

Retrieves restaurant inspection results from the Arkansas Department of Health's
[public inspection search](https://foodserviceprod.adh.arkansas.gov/Web/inspection/publicinspectionsearch.aspx),
stores them, and presents them in a readable form.

## How the source works

The ADH site is ASP.NET WebForms — every interaction is a form POST that echoes
back the page's hidden ViewState. **No headless browser is required**; the
scraper is `requests` + BeautifulSoup. What that means in practice:

- **Search** posts county + date range. Results are a GridView, 15 rows per page.
- **Inspection history is free.** Each result row already contains a nested table
  with that facility's entire past-inspection list. The "Past Inspection(s)" link
  is a client-side toggle, not a request.
- **Observations cost one request each.** Clicking "Observation(s) N" is a full
  postback (~500KB) returning the code, code explanation, and inspector comments —
  but it is an incomplete list; see below.
- **Reports are PDFs, not images.** The report postback emits a
  `window.open('/Web/Common/ExternalFileViewer.aspx?ID=…&Key=…')`; that URL
  returns `application/pdf` (~400KB each).
- **Facility IDs come from the source.** Rows expose the ADH establishment key,
  so facilities don't need fuzzy name matching — except for establishments with
  no inspection history, which carry no key and fall back to a name+address
  fingerprint.
- **Checking "Display Map" makes the server emit pre-geocoded coordinates.** The
  scraper always requests them, but the array covers only part of the result set
  and isn't ordered like the grid, so coordinates are matched by name and treated
  as a bonus, never as a guarantee.

Known quirks of the source data: establishment names are truncated to 30
characters by the state, and street/city are run together with inconsistent
whitespace (parsed by matching against a bundled list of Arkansas place names;
anything unmatched is flagged `address_needs_review` for review in the admin).

## Embeds

Newspapers embed a live table with one line. Everything about what it shows lives
in an `Embed` row in the admin, so a change propagates to every paper without
anyone touching their CMS.

```
<script src="https://ourdomain/embed/pulaski-monthly.js" async></script>
```

That injects an iframe pointing at `/embed/<slug>/`, a server-rendered,
self-contained, cached HTML document, and keeps its height in step via
`postMessage`. A plain `<iframe>` snippet is generated too, for a CMS that strips
`<script>`. Both appear on the embed's admin page alongside a preview link.

Nothing exists per embed except a database row — one view, one template, the same
way `facility_detail` serves every facility. See
[`iframe_display.md`](iframe_display.md) for the full design and the reasoning.

### How it behaves

Under 600px each inspection is a card (name and address, violation count, then
date and type with a caret); at 600px and up it becomes a conventional table. A row
expands in place to show every violation — short description, priority, the
inspector's notes — ending in a link to the source PDF. A clean inspection still
expands, so the report is always reachable.

Paging, search and sort all happen **in the reader's browser**. A Pulaski
county-month is 167 inspections and 302 violations: 144 KB of HTML, 20 KB gzipped,
including every detail panel. Shipping it all keeps the embed a single cacheable
document, where server-side paging would make the cache key page × sort × search
and send a long tail of misses to Django.

### Two things that are easy to get wrong

- **`X-Frame-Options`.** Django sends `DENY` by default, which silently breaks
  every embed. The embed view is exempt; the loader verifies `event.origin` so only
  its own iframe can drive the height.
- **Measure the content, not the document.** `documentElement.scrollHeight` is
  forced up by the iframe's own height, so an embed measured that way grows but
  never shrinks when a reader collapses a row. The page measures its content
  wrapper instead, and posts immediately rather than only inside
  `requestAnimationFrame`, which never runs while a tab is in the background.

## Output data

`/output/` turns stored inspections into an HTML fragment a desk can paste
straight into a story: pick a county and a date range, press Output, copy the
box. It is a fragment, not a document — paragraphs, headings and lists, no
wrapper, no styling, no classes — because it is going into someone's CMS.

The shape is fixed: the four explanatory paragraphs about what priority,
priority foundation and core mean, then each day as a heading, then each
establishment cited that day in alphabetical order with its address, inspection
type, one group per category it was cited under, the inspector's own wording for
each observation, and a link to the report.

Everything is read straight from the database. The observations are quotes from
a public record, so they are reproduced verbatim; nothing is paraphrased on the
way out.

### Only cited establishments, only categorised violations

A clean inspection has nothing to list and does not appear. Neither does a
violation with no priority level — the three categories come from the report
PDF, and a violation scraped from the website overlay alone has no category to
file it under. Those are counted and reported under the box rather than dropped
silently, because "this restaurant wasn't cited" and "we haven't parsed its
report yet" are very different claims to make in print.

### Summarize with AI

With `OPEN_AI_TOKEN` set, a second button sends the output to OpenAI and shows
the shortened version in its own box, with its own copy button. One summary per
category per establishment; everything else — the boilerplate, the dates, the
names, the addresses, the report links — comes back unchanged.

The prompt tells the model to use nothing but the text it was handed. That is
the whole point: these are an inspector's words about a named business, and a
model that helpfully explains what a violation *means*, or what usually causes
one, would be putting words in that inspector's mouth. Read the summary against
the box above it before publishing.

The call goes through `/output/summarize/` on this server, never from the
reader's browser, so the token stays server-side. That endpoint takes a county
and a date range and rebuilds the export itself rather than accepting HTML
posted to it — otherwise it would be an open pipe to the newsroom's OpenAI
account for whatever text someone cared to POST at it.

`OPENAI_MODEL` picks the model (anything that speaks the chat completions API).
No `temperature` or token cap is sent: which of the two the API accepts changes
between model generations, and sending one the chosen model rejects fails the
whole call. Without a token the button renders disabled and says why; the rest
of the page works as normal.

### Tuning the prompt, in the admin

The wording lives in `SummaryPrompt` rows, edited under **Summary prompts**. An
edit takes effect on the next press of the button — no deploy, and nothing is
cached, because a stale prompt would mean "I changed it and nothing happened",
which is the failure this exists to prevent.

Keep the old rows. Tuning a prompt means going backwards as often as forwards,
and the cheapest answer to "was it better before that rule?" is to still have the
version from before that rule. Copy a prompt to a new row, change the copy, tick
it active. The previous one is one click away. Django's admin history will not do
this for you — `LogEntry` records who changed what and when, but not the old
values.

Exactly one row is active, enforced twice: `save()` clears the others in the same
transaction, and a partial unique index on the table catches anything that
bypasses `save()`. Constraint validation is skipped at form level, because a
ModelForm checks constraints *before* saving and would otherwise reject the very
edit that switches prompts. The live row cannot be deleted.

`user_template` is optional and wraps the export — an example summary to work
from is the usual reason to want one. `{html}` is where the inspections go, and a
template that never inserts them, or that uses a placeholder that doesn't exist,
is refused on save rather than raising `KeyError` in the middle of someone's
deadline. `model` overrides `OPENAI_MODEL` for that prompt alone, which is what
you want when comparing two prompts on the same model, or one prompt on two.

The output page reports which prompt and which model produced what you are
looking at. Without that, comparing two summaries means guessing which made
either.

`summarize.SYSTEM_PROMPT` stays in the code as the shipped wording and the
fallback; migration `0012` seeds it as the first active row and never overwrites
an existing one. An install that has never run the seed, or a table someone has
emptied, summarises with that rather than failing — the same deal
`ViolationItem.plain_description` gets from `0008`.

**The editorial constraint is in the prompt, and only in the prompt.** Rules 1
and 2 are what keep the model quoting the inspector instead of explaining what a
violation usually means. Anyone with admin access can now remove them with one
save, and the result publishes under an inspector's byline. The admin says so on
the form. If more than one person has access and that isn't comfortable, the
stricter arrangement is a fixed provenance preamble in code concatenated with the
editable rules from the row.

## Scheduled scrapes

Each county can carry a standing instruction to re-scrape itself — cadence, the day
and time it runs, and how far back it looks. These are `ScrapeSchedule` rows,
managed in the admin, so adding a county never needs shell access and the schedule
is visible to anyone who can see the site.

There is no cron entry and no second scheduler. `django-q2` already ships one, and
the `qcluster` worker already running is what fires it. A single recurring
dispatcher wakes every few minutes, asks which counties are due, and queues those
scrapes:

```bash
.venv/bin/python manage.py install_scheduler --every 10   # once, at deploy
.venv/bin/python manage.py run_due_scrapes --dry-run      # what would fire now
.venv/bin/python manage.py run_due_scrapes                # fire it by hand
```

The dispatcher interval sets worst-case lateness: at ten minutes, a job set for
2:00am starts by 2:10.

### Lookback must overlap the interval

The setting that matters most, and the one whose failure is invisible. **The state
backdates.** An inspection can appear online days or weeks after it happened, so a
weekly job that searches only the last seven days will silently drop records and
nothing will look wrong.

The model enforces a minimum per cadence — 7 days for daily, 14 weekly, 21
biweekly, 45 monthly — and refuses a shorter window with an explanation. Overlap
costs almost nothing: inspections upsert on their natural key, facilities on the
ADH establishment key, PDFs are content-hashed, and `details_scraped_at` stops
detail being re-fetched. A re-scraped window writes nothing it already has.

### Not scraping the same county twice at once

A Pulaski month is 147 facilities and hundreds of PDF fetches. If a run is still
going when the next falls due, the dispatcher skips that county rather than
doubling the load on the state's server — and advances the schedule anyway, so a
stuck county can't re-trigger on every tick or build a backlog.

`catch_up` is `False` in `Q_CLUSTER` for the same reason: after downtime you get
the next scheduled run, not every run you missed firing at once.

### Operational notes

- **Stagger the counties.** If this grows to all 75, don't put them all on the 1st.
  Two or three a night, overnight, is polite and keeps any single night short.
- **Watch for failures.** Once embeds are live, a silently failing scheduled scrape
  means newspapers publish stale data under their own banner. `ScrapeRun` records
  status and error for every run and the schedule's last result shows in the admin
  list, but nobody is watching at 2am — alerting on a failed scheduled run is worth
  adding before the embeds go out.
- **Supervise the worker.** Nothing fires if `qcluster` is not running. Use systemd
  with `Restart=always`, and see the migration warning above.

## Violations come from the PDF, not the website

**The website's "Observations" overlay under-reports violations.** Measured across
177 reports, the overlay listed 173 violations where the PDFs contained 332 —
nearly half missing. The omissions skew toward Core-priority items (128 of 159)
but are not limited to them: 19 Priority Foundation and 3 Priority violations were
also absent. Most consequentially, **32 inspections that showed zero observations
online had violations in the report**.

No violation ever appeared online but not in the PDF, so the report is a strict
superset and is treated as authoritative wherever one exists. When a report is
parsed its violations replace the overlay's; the short `code_explanation` label,
which only ever appears online, is carried across onto matching codes.

The reports are text PDFs with a properly ruled observations table, so
[`report_pdf.py`](inspections/scraper/report_pdf.py) reads real table cells via
pdfplumber rather than guessing at text layout. No OCR is involved.

The PDF also yields four things the search results never expose:

| Field | Notes |
|---|---|
| `Violation.item_number` | The numbered item on the inspection form (3, 5, 39…) |
| `Violation.priority_level` | `P` Priority, `PF` Priority Foundation, `C` Core |
| `Violation.correct_by` | Deadline the inspector set |
| `Inspection.source_inspection_id` | **The state's own per-inspection ID.** The results grid has no such ID — identity there has to be inferred from facility + date + type — so this is the real natural key, available once a report is parsed. |

`Violation.source` and `Inspection.violations_source` record where the current set
came from, so PDF-derived and web-derived rows are never confused.

Backfill reports collected before this existed, or after a parser fix:

```bash
.venv/bin/python manage.py parse_reports             # unparsed reports only
.venv/bin/python manage.py parse_reports --force     # re-parse everything
.venv/bin/python manage.py parse_reports --county Pulaski
```

Because the PDF supersedes it, the overlay request is now only a safety net for
inspections with no report, plus the source of `code_explanation`. Setting
`SCRAPE_WEB_OBSERVATIONS=False` skips it and halves the per-inspection load on the
state's server.

Not currently extracted, but present in the reports and straightforward to add:
the temperature-observations table, inspector name, and time in/out.

## The 57 form items

Every violation cites a numbered item (1–57) from the state's inspection form, and
each number has a short description of what it covers — "Proper cold holding
temperatures", "Adequate handwashing facilities supplied & accessible". That's the
text a reader sees before opening the inspector's notes.

The list is **not typed by hand**. The checklist is printed on page 1 of every
report, so it was extracted from 188 of them and cross-checked, requiring each
number to yield identical text across all. Only item 26 disagreed — 11 reports bled
a stray glyph into the cell — and the majority text wins. The result lives in
[`violation_items.py`](inspections/violation_items.py) and is seeded into the
`ViolationItem` table by a migration.

It's a table rather than a dict for one reason that matters: `plain_description`.
The state's wording is bureaucratic, and a table lets you write reader-facing text
in the admin without a deploy, surviving any re-seed. `official_description` keeps
the state's own words verbatim alongside it.

`Violation.item` is a nullable FK, resolved at parse time. The raw `item_number`
string is kept as printed, so an unrecognised number degrades to "no short
description" rather than failing an import. Item 57 is the catch-all — its official
wording is an instruction to inspectors, so it ships with "Other violations" as its
reader-facing text. It accounts for about 3% of violations.

Because violations are now keyed to a stable number, they aggregate: "every
cold-holding violation in Pulaski this year" is a real query, which free-text
comments could never answer.

```bash
.venv/bin/python manage.py seed_violation_items --relink   # after a form revision
```

## Locations

Every facility gets a point (PostGIS `PointField`, WGS84, `geography=True` so
distance queries return meters) the first time it is identified.

**Primary: the [Arkansas GIS Office statewide composite locator](https://gis.arkansas.gov/arcgis/rest/services/Locator/ASDI_Composite_Locator/GeocodeServer).**
It returns real address points rather than interpolated street ranges, is already
WGS84, and resolves Arkansas addresses the Census data has never heard of. On a
sample of 169 facilities it located 153; Census managed 3 more.

**Fallback: the Census Bureau geocoder** — free, public, no API key.

Matches are accepted only at address precision. The Arkansas locator answers
nearly *every* query, so the `Addr_type` filter is what makes it safe: a
`Locality` match is the containing city's centroid and a `StreetName` match is a
whole street, and accepting either would scatter pins across town centres.
Accepted types and the minimum `Score` are in settings. Whatever matched — type
and score included — is stored in `geocode_matched_address`, so a questionable
match is visible rather than silent.

### Why the state's own map coordinates aren't used

The ADH search page emits pre-geocoded coordinates when "Display Map" is ticked,
and the obvious assumption is that the state's own numbers are authoritative.
They are not. Checked against the Arkansas GIS locator, a third of them were more
than 300m from the address, and BNG PIZZA — 226 W Main St, Hampton, in Calhoun
County — was placed 129km north, near Little Rock. Two distinct Fordyce
facilities also shared one identical point.

The scraper therefore no longer requests the map payload (`include_map=False`),
though the parsing is retained so the payload can still be pulled for comparison.
Rows geocoded before this change keep `geocode_source="adh_map"` and should be
re-run with `--force`.

### Addresses that can't be geocoded

Some facilities are unlocatable because the state's own address is wrong or
stale, not because the geocoders failed — a dozen Simmons Bank Arena concession
stands are still filed under `ONE VERIZON ARENA WAY`. Those are counted in
`geocode_attempts` and abandoned after `MAX_GEOCODE_ATTEMPTS` so later scrapes
don't re-query them forever; fix them by dropping a pin on the map widget in the
admin, which records the point as `manual`.

```bash
.venv/bin/python manage.py geocode_facilities            # only the unlocated
.venv/bin/python manage.py geocode_facilities --county Pulaski --limit 50
.venv/bin/python manage.py geocode_facilities --force    # re-geocode everything
```

The Arkansas locator also exposes a batch endpoint (`geocodeAddresses`, up to
1,000 per call), worth adopting if backfills ever run to thousands of rows.

## What each run stores

Every inspection the site lists is stored, because the history comes for free.
The expensive per-inspection detail — observations and the report PDF — is fetched
**only for inspections inside the requested date range**. Older inspections land
as list-only rows and can be backfilled by re-running a scrape over an earlier
window.

## Setup

Requires Python 3.14, PostgreSQL with PostGIS (Postgres.app ships it), and the
GEOS/GDAL libraries GeoDjango links against (`brew install gdal geos`, or
`libgdal-dev libgeos-dev` on Debian/Ubuntu). If Django can't find them — common
with Homebrew on Apple Silicon — set `GEOS_LIBRARY_PATH` and `GDAL_LIBRARY_PATH`
in `.env`.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then edit DB credentials
createdb health_inspections
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
```

> **Restart the worker after every migration.** `qcluster` is a long-lived process
> that holds model definitions in memory. If a migration changes the schema while
> it is running, the worker keeps issuing queries against the old columns and every
> scrape queued from the web UI fails with something like
> `column inspections_facility.latitude does not exist` — while `manage.py
> scrape_county` keeps working, because it starts a fresh process each time. That
> asymmetry is the tell.

Run the web server and the background worker in two terminals:

```bash
.venv/bin/python manage.py runserver
```

```bash
.venv/bin/python manage.py qcluster
```

Then open <http://localhost:8000/retrieve/> to request a county and date range.

## Command line

Scrapes can also run without the web trigger or a worker:

```bash
.venv/bin/python manage.py scrape_county --county Pulaski --from 2026-07-01 --to 2026-08-17
```

## Tests

```bash
.venv/bin/python manage.py test inspections
```

Parser tests run against real HTML and a real report PDF captured from the ADH
site (`inspections/tests/fixtures/`), so they fail loudly if the state changes its
markup or report layout rather than silently importing empty rows.

## Layout

| Path | Purpose |
|---|---|
| `inspections/scraper/client.py` | HTTP/ViewState session against ADH |
| `inspections/scraper/parsers.py` | Pure HTML → dicts; no network, fully testable |
| `inspections/scraper/report_pdf.py` | Report PDF → violations, via pdfplumber |
| `inspections/ingest.py` | Drives the scraper, writes to the database |
| `inspections/scheduling.py` | Works out when each county is next due, and dispatches |
| `inspections/embed_views.py` | Public embed page and loader script |
| `inspections/output.py` | Stored inspections → pasteable HTML for a story |
| `inspections/summarize.py` | Sends an export to OpenAI to be shortened, using the active `SummaryPrompt` |
| `inspections/geocoding.py` | Arkansas-GIS-then-Census location lookup |
| `inspections/models.py` | Facility → Inspection → Violation, plus ScrapeRun |
| `inspections/places.py` | Arkansas place names (2023 Census Gazetteer) for address splitting |
| `inspections/violation_items.py` | The 57 numbered form items, extracted from the reports |
| `inspections/counties.py` | County name → ADH form ID |

## Deployment notes

- `django-q2` uses the Postgres database as its broker, so production needs only
  Postgres — no Redis. Run `manage.py qcluster` under systemd alongside the app.
- **Deploys must restart the worker after `migrate`,** not just the web process.
  A worker left running across a schema change queries the old columns and fails
  every job. Make the restart part of the deploy script rather than a step someone
  has to remember.
- Report PDFs are written to `MEDIA_ROOT`. Swap in `django-storages` (S3/R2) for
  production without a model change; they are deliberately *not* database blobs.
- `SCRAPER_DELAY_SECONDS` throttles requests to the state's server. Don't set it
  to zero.
- PostGIS is required; the migration installs the extension itself, so the
  database role needs permission to `CREATE EXTENSION`.
- `ARKANSAS_GIS_DELAY` and `CENSUS_GEOCODER_DELAY` throttle the geocoders the
  same way `SCRAPER_DELAY_SECONDS` throttles ADH. Set `GEOCODING_ENABLED=False`
  to turn location lookup off entirely.
- The summariser's wording is a database row, not a setting — edit it under
  **Summary prompts** in the admin, not in `summarize.py`. The constant there is
  only the seed and the fallback.
- `OPEN_AI_TOKEN` enables the summarise button on `/output/`. It is optional and
  read only on the server. Summarising happens in the request the button makes,
  not on the worker, so the web process needs an outbound route to the API and a
  proxy timeout above `OPENAI_TIMEOUT` (120s by default) — a wide date range is a
  long document and gunicorn's own 30s default will cut it off.
