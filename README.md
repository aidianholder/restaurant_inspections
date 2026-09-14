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

`/output/` turns stored inspections into **Markdown** a desk can read, edit and
publish: pick a county and a date range, press Output, and copy it either as
HTML for the web or as text for InDesign.

Markdown is the canonical artefact here, not an intermediate. A person edits the
summary before it goes out, and once they do, their text is the authority — so
there is no structured representation running alongside it that could drift.
HTML is rendered from the Markdown on demand; the text an InDesign operator
places *is* the Markdown, and any transform they want is a GREP find/change on
their end.

The shape is fixed:

```markdown
# Faulkner County health inspections 9/04/26 - 9/11/26
```

then the four explanatory paragraphs about what priority, priority foundation
and core mean, then every cited establishment in one alphabetical run — name,
address, inspection type, one group per category it was cited under, and the
inspector's own wording for each observation.

Address and inspection type are separate paragraphs. They shared one with a
`<br>` until this became Markdown, where a mid-paragraph line break is two
trailing spaces — invisible whitespace that the first person to edit the
document would destroy without noticing.

The range in that heading is the *requested* range, not the span of days that
happened to produce a citation: it tells the reader what period was searched.

Inspections are not grouped by date and carry no date of their own, so the
heading is the only date on the page. An establishment inspected twice in the
range appears twice, consecutively, in the order the inspections happened — the
inspection type is what tells them apart.

Everything is read straight from the database. The observations are quotes from
a public record, so they are reproduced verbatim; nothing is paraphrased on the
way out.

### Escaping

Inspector prose is full of characters CommonMark reads as markup, and one of
them matters a great deal: `<41F` is house style for a cold-holding violation
and raw it is an HTML tag. `output.py` escapes the seven that change meaning
inline — and only those, since backslashing every `.` and `-` would produce a
document nobody can read, which would defeat the point of using Markdown. It
also escapes block openers at the start of a paragraph, because addresses really
do begin `#5 Highway 65`.

Raw HTML passthrough is off in the renderer. By the time Markdown reaches it a
person has been typing in a textarea, and a stray `<script>` pasted in from
somewhere should land in the story as visible characters.

### Only cited establishments, only categorised violations

A clean inspection has nothing to list and does not appear. Neither does a
violation with no priority level — the three categories come from the report
PDF, and a violation scraped from the website overlay alone has no category to
file it under. Those are counted and reported under the box rather than dropped
silently, because "this restaurant wasn't cited" and "we haven't parsed its
report yet" are very different claims to make in print.

### Summarize with AI

With `OPEN_AI_TOKEN` set, a second button shortens each establishment's
observations — one summary per category — and drops the result into an
**editable** box with a live preview beside it. Fix a sentence, cut an
establishment, add one the model missed, then copy. Nothing is stored: the
canonical version is whatever gets published.

The prompt tells the model to use nothing but the text it was handed. That is
the whole point: these are an inspector's words about a named business, and a
model that helpfully explains what a violation *means*, or what usually causes
one, would be putting words in that inspector's mouth. Read the summary against
the box above it before publishing.

The call goes through `/output/summarize/` on this server, never from the
reader's browser, so the token stays server-side. That endpoint takes a county
and a date range and rebuilds the export itself rather than accepting Markdown
posted to it — otherwise it would be an open pipe to the newsroom's OpenAI
account for whatever text someone cared to POST at it.

### Three things keep the document intact

The model used to be handed the whole document and asked nicely to preserve it.
Rules 3 and 4 in the shipped prompt are scar tissue from that. Now:

* **The preamble is never sent.** The heading and the four explanatory
  paragraphs are ours and fixed, so they are held back and re-attached
  afterwards. A model cannot reword what it was never shown.
* **Establishments go in batches** of `OPENAI_BATCH_SIZE`, up to
  `OPENAI_MAX_PARALLEL` at a time. We generated the document, so we know where
  every block begins and can cut on seams we wrote rather than asking a model to
  respect them. A month of a large county becomes several small calls instead of
  one that times out. A batch that fails fails the whole run — half a roundup is
  worse than none, because it looks finished to whoever pastes it.
* **What comes back is reconciled against what went in.** Every establishment
  sent is expected back, by name. Any that are missing — or any the model
  invented — are named in red above the editor. Dropping an establishment is the
  failure this has actually hit, and a quietly short roundup is the worst way to
  find out.

`OPENAI_MODEL` picks the model (anything that speaks the chat completions API).
No `temperature` or token cap is sent: which of the two the API accepts changes
between model generations, and sending one the chosen model rejects fails the
whole call. Without a token the button renders disabled and says why; the rest
of the page works as normal.

### Preview and Copy HTML share one code path

`/output/render/` takes Markdown and returns HTML, and it backs both the live
preview and the Copy HTML button. That is deliberate: a preview that can drift
from what gets copied is worse than no preview at all.

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

## Public and staff surfaces

Two audiences, split by URL so the whole staff side can go behind a VPN or a
login in one stroke.

| Public | Staff |
|---|---|
| `/establishment/<slug>/` | `/` (browse) |
| `/dashboard/<slug>/…` | `/facility/<slug>/` |
| `/embed/<slug>/…` | `/retrieve/…`, `/output/…`, `/admin/` |

**Gate by allowlist, not denylist.** Default-deny with the public prefixes
allowed means a new staff view is private the day it is written, rather than the
day somebody remembers to add it.

Two things must stay reachable alongside the public prefixes:

- **`/media/`** — the reader-facing page links to inspection report PDFs. Gate it
  and every "Read the full inspection report" link breaks for readers, silently.
- **`/static/`** — the dashboard's JavaScript, CSS and vendored MapLibre.

Note that `/` is the *staff* browse page. Once the staff side is gated there is
no public landing page at the bare domain; readers arrive via a paper's embed or
a direct establishment link.

### The reader's establishment page

`/establishment/<slug>/` is a separate view and template from the staff
`facility_detail`, not the same template with `{% if request.user.is_staff %}`
around the sensitive parts. Conditionals are how internal detail leaks: a
separate template *cannot* accidentally render the geocoder used, because that
markup does not exist in it.

It carries no site navigation — the staff links are going behind a gate, and
showing a reader links they cannot follow is worse than showing none. It also
leaves out coordinates and the geocoding source, and the "state site shows N"
badge, which is a discrepancy for us to reconcile rather than something to
explain to a reader.

**Inspections whose details were never retrieved are left out entirely.**
Publishing "1 violation" under a named business when the state recorded five and
we simply have not fetched them yet is worse than saying nothing. An inspection
the state recorded as having *no* observations is kept, because `observation_count`
of 0 comes from the state's own results grid — it means nothing was cited, not
that we failed to look.

`Facility.get_absolute_url()` is this page, since it is the record's canonical
public address; the admin's "View on site" and the dashboard both land here.
Staff templates link to `facility-detail` explicitly.

## Dashboards

A `Dashboard` is one newspaper's public map-and-table view over its readership
area — several counties, a MapLibre map above a searchable, sortable, paginated
table, the two kept in sync. Distinct from `Embed`, which is a single cached
table for one county.

Configure it in the admin (counties, rows per page, title) and hand the newsroom
the snippet. Two ways to mount it, both from the same bundle and the same API:

```html
<!-- preferred: mounts into the paper's own page -->
<div data-arhi-dashboard></div>
<script src="https://…/dashboard/<slug>/embed.js" async></script>

<!-- fallback for a site we don't control -->
<iframe src="https://…/dashboard/<slug>/" style="width:100%;border:0;height:1200px"></iframe>
```

The component is the default because WEHCO controls the papers' CSS and CSP,
which removes both arguments for an iframe — and mounting directly means deep
links and the back button work, and a pan-zoom map does not fight a phone's
scroll. `design_note.md` has the full reasoning.

### Why it is an API and not a rendered table

Server-side paging is *what an API does*; it was never an argument for
server-rendering. Three read-only `JsonResponse` endpoints:

| Endpoint | Returns |
|---|---|
| `api/rows` | One page of the table, with each row's violations for the dropdown |
| `api/map` | Every facility matching the filters, as GeoJSON |
| `api/facility/<id>` | One row, for a pin click whose row is on another page |

**The map reflects the filter state, never the sort or the page.** Sorting
reorders rows without changing which facilities match, so a column click costs
one request and the map is untouched; paging shows a different slice of the same
matches, and a map showing only page one would be useless. That is what makes
rapid sorting free rather than expensive.

Measured on 1,919 facilities across seven counties, the map payload is 446KB raw
and **85KB gzipped** — `map` and `rows` are gzipped by decorator rather than by
global middleware, because compressing every response, including admin pages with
a CSRF token beside reflected search terms, is the setup BREACH needs.

### Front end

No build step. Plain ES modules in `inspections/static/inspections/dashboard/`,
with MapLibre vendored beside them.

- Every class is prefixed `arhi-` and **no ids are used anywhere** — WEHCO's
  weather features already own `#map`, and the surest way not to collide is to
  have nothing to collide with.
- MapLibre is imported as an ES module, so no `window.maplibregl` is ever
  created for two features to fight over. Vendoring it means **three** files:
  `maplibre-gl.mjs`, `maplibre-gl-shared.mjs`, and `maplibre-gl-worker.mjs`. The
  worker is resolved relative to `import.meta.url`; without it the map silently
  never finishes loading, with no console error.
- Sized by container query, not viewport: the component's width comes from
  whatever column it is dropped into. Map height 360/460/560px; table columns
  drop rather than truncate, since the dropdown carries everything anyway.
- The basemap is **our own PMTiles archive**, `protostyle3.json` on DigitalOcean
  Spaces. Self-hosted on purpose: a newspaper embed spikes the day a story runs,
  which is exactly when a metered or donation-funded tile service is worst placed
  to absorb it, and there is no key to leak in a public page. `DASHBOARD_MAP_STYLE`
  changes it.
- That means two more vendored files, `pmtiles.js` and `fflate.js`, and one
  patched line: pmtiles' ESM build has a bare `import … from "fflate"`, which a
  browser cannot resolve without an import map or a bundler, so the vendored copy
  points at `./fflate.js`. MapLibre is taught the `pmtiles://` scheme with
  `addProtocol` before any map is built.

#### Fonts are a property of the style, not the component

`DASHBOARD_MAP_FONTS` names the glyphs the style's font source actually carries.
Ours has **Regular, Medium and Italic**; OpenFreeMap has **Regular, Bold and
Italic** — so switching `DASHBOARD_MAP_STYLE` means switching these too.

Two traps, both of which produce labels that silently do not draw:

- **Single font names only.** MapLibre joins a multi-font stack into *one*
  comma-separated glyph URL (`Noto Sans Medium,Open Sans Semibold/0-255.pbf`).
  A hosted service resolves that server-side; a static bucket has no such
  directory and 404s.
- **Set `text-font` at the layer level, not only inside a `format` expression.**
  Otherwise MapLibre additionally resolves its own default stack, "Open Sans
  Regular,Arial Unicode MS Regular", which is both composite and absent.

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

**Supplement: the [NG911/USPS address lookup](https://gis.arkansas.gov/arcgis/rest/services/Locator/NG911_USPS_Address_Lookup/GeocodeServer)**,
built on the address points counties maintain for emergency dispatch. Same Esri
API, same fields, same WGS84, so it costs almost nothing to call.

It is a supplement and not a replacement, which is worth being precise about
because it looks like an upgrade. Measured against the composite locator on 25
real failures it rescued exactly one — a duplicated address string
(`8350 WARDEN ROAD 8350 WARDEN ROAD 8350 WARD`) that its parser recovered from
and the composite's did not. On clean addresses the two agree exactly, type and
score. On messy ones NG911 more often returns nothing at all where the composite
at least returns a `Locality`. And it is readier to invent a confident match: it
turned `ROUTE 2, BOX 8` into a street called "PO BOX" at score 82. Hence a
minimum score of 90 rather than 80, plus an explicit refusal of any match to a
PO box — a mail destination is not a place.

**Last resort: the Census Bureau geocoder** — free, public, no API key.

Matches are accepted only at address precision. The Arkansas locator answers
nearly *every* query, so the `Addr_type` filter is what makes it safe: a
`Locality` match is the containing city's centroid and a `StreetName` match is a
whole street, and accepting either would scatter pins across town centres.
Accepted types and the minimum `Score` are in settings. Whatever matched — type
and score included — is stored in `geocode_matched_address`, so a questionable
match is visible rather than silent.

### The admin's basemap

Django's GIS widget defaults to OpenStreetMap's public tile server, which blocks
sustained use — as its tile usage policy says it will. `inspections/widgets.py`
swaps the basemap for [OpenFreeMap](https://openfreemap.org/) vector tiles: no
API key, no usage limits, and a self-contained style whose glyphs and sprites
resolve from the same host, so there is nothing extra to host or keep alive.

Only the basemap changes. Placing, dragging and clearing the point, and the
GeoJSON serialisation behind it, are still Django's — `MapWidget.layerBuilder` is
the documented extension point for exactly this, so none of that had to be
reimplemented to change a tile source. The OpenLayers version is unpacked from
Django's own widget media rather than pinned, so it tracks whatever Django ships.

Two details worth knowing if it ever looks wrong. The style's `background` layer
belongs to no source, so applying the style per-source skips it — the land colour
comes from CSS on `.dj_map` instead, and must match the style if you change it.
And `ol-mapbox-style` is pinned at 12.2.1, the last release whose peer range still
covers the OpenLayers 7.2.2 that Django bundles.

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

**Almost every remaining failure is a bad address, not a geocoder that needs
replacing.** On a real sample of 25, three clusters accounted for 20 of them:

- **12 — `ONE VERIZON ARENA WAY`.** The arena was renamed; `1 Simmons Bank Arena
  Way` resolves at PointAddress 98 in *both* locators. Eleven of those twelve are
  concession stands in that one building. No geocoder resolves a street name that
  no longer exists.
- **6 — `N BUSINESS 9`, Morrilton.** Both locators reach `StreetName` and stop.
  `1621 N Highway 9 B` gives StreetAddress 98, but the match comes back as
  `1621 HIGHWAY 9` with the B dropped, which may be a different road — worth
  checking against a map before rewriting six addresses on the strength of it.
- **2 — repeated or concatenated strings.** `raw_address` shows these arrived
  from ADH that way, truncated around 43 characters at the source. Not our
  parser.

The rest are genuinely unaddressable: a rural-route box, a road intersection with
no house number, a mangled cove name.

Retrying never fixes any of this, so when `geocode_attempts` reaches
`MAX_GEOCODE_ATTEMPTS` the facility is flagged `address_needs_review` and
abandoned. `FacilityAdmin` sorts flagged rows to the top, which puts them in front
of someone who can correct the address once — fix by dropping a pin on the map
widget, which records the point as `manual`. Nothing ever clears the flag
automatically: a person unticks it when the address is right.

```bash
.venv/bin/python manage.py geocode_facilities            # only the unlocated
.venv/bin/python manage.py geocode_facilities --county Pulaski --limit 50
.venv/bin/python manage.py geocode_facilities --force    # re-geocode everything
```

The Arkansas locator also exposes a batch endpoint (`geocodeAddresses`, up to
1,000 per call), worth adopting if backfills ever run to thousands of rows.

## Latest-inspection columns

`Facility` carries a denormalised summary of its own newest inspection:
`latest_inspection`, `latest_inspection_date`, `latest_inspection_type`,
`latest_violation_total`, `latest_priority`, `latest_priority_foundation`,
`latest_core`, and `inspection_count`.

**The violation counts describe the latest inspection alone, never the facility's
history.** MI PUEBLITO has 25 inspections on record and 32 violations across all
of them, but it was clean on 24 August 2026, so `latest_violation_total` is 0.
Storing the lifetime figure would brand a currently-clean restaurant with 32
violations. `inspection_count` is the one field here that covers everything — and
it counts inspections, not violations.

### Why they exist

Not speed, mostly. A correlated subquery over 10,000 facilities runs in about
26ms, which is fine. What the columns buy is reach: violation counts used to be
attached *after* paging, so you could not order by them, and the dashboard needs
that column sortable. Working around it means a subquery whose `OuterRef` points
at another annotation — the fragile shape the old
`views._attach_latest_violation_counts` existed to avoid. Measured at 10,000
facilities, sorting by violation count went from 35.5ms to 0.7ms, which also
buys headroom for a public page when a story spikes traffic.

### How they stay true

Recomputed, never incrementally maintained. Four places change the answer — a new
inspection, the website overlay writing violations, a report PDF replacing them,
someone editing violations in the admin — and hooking each one is how derived
columns go quietly wrong.

- Every scrape run refreshes the facilities it touched, in a `finally` so a run
  that dies partway still leaves them correct.
- `InspectionAdmin` refreshes on inline edits and deletions, the one path a
  person can reach without a run.
- Everything else is `manage.py rebuild_latest_inspection`.

```bash
.venv/bin/python manage.py rebuild_latest_inspection --check     # audit, writes nothing
.venv/bin/python manage.py rebuild_latest_inspection             # rebuild all
.venv/bin/python manage.py rebuild_latest_inspection --county Pulaski
```

The refresh only writes rows whose values actually differ, so `--check` can run
it inside a rolled-back transaction and report honestly how many are stale.

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
                              # and set DJANGO_DEBUG=True for local work
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
| `inspections/dashboard_views.py` | Per-newspaper dashboard: JSON API, page, and mount script |
| `inspections/output.py` | Stored inspections → Markdown for a story |
| `inspections/markdown_render.py` | Markdown → HTML, for the web copy and the editor's preview |
| `inspections/summarize.py` | Batches an export to OpenAI to be shortened, and reconciles what comes back |
| `inspections/geocoding.py` | Arkansas-GIS-then-NG911-then-Census location lookup |
| `inspections/latest_inspection.py` | Rebuilds Facility's denormalised latest-inspection columns |
| `inspections/widgets.py` | The admin's map widget, on OpenFreeMap vector tiles |
| `inspections/models.py` | Facility → Inspection → Violation, plus ScrapeRun |
| `inspections/places.py` | Arkansas place names (2023 Census Gazetteer) for address splitting |
| `inspections/violation_items.py` | The 57 numbered form items, extracted from the reports |
| `inspections/counties.py` | County name → ADH form ID |

## Deployment notes

### Deploying a change

```bash
git pull
.venv/bin/pip install -r requirements.txt        # only if requirements changed
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
systemctl restart inspections.service            # web
systemctl restart inspections-qcluster.service   # worker
```

Both restarts and the `collectstatic` are load-bearing, and each one fails
quietly rather than loudly — the site keeps serving, so nothing draws attention
to the step that was missed:

- **Skipping `collectstatic`** leaves `STATIC_ROOT` without the dashboard bundle.
  The embed snippet still loads, because Django serves that, so the failure looks
  like it comes from nowhere: `import()` of `dashboard.js` 404s and the browser
  reports it as a CORS error rather than a missing file. Front-end assets live in
  `inspections/static/` and are not served from there in production.
- **Skipping the worker restart** is the failure the box under [Setup](#setup)
  describes. Its shape depends on the migration: a *removed* column fails every
  job immediately, while an *added* `NOT NULL` column fails only the jobs that
  insert a new row — so a scrape of a county with no new establishments still
  succeeds, and the breakage looks intermittent rather than total.

### nginx

Two requirements the dashboard embed depends on. Neither affects this site's own
pages, so both look fine until a newspaper embeds the component:

- **`.mjs` needs a JavaScript MIME type.** nginx ships the mapping from 1.21.1;
  on anything older (Ubuntu 22.04 pins 1.18) `maplibre-gl.mjs` is served as
  `application/octet-stream` and the browser refuses to execute it as a module.
  Add `text/javascript mjs;` to `/etc/nginx/mime.types`, then **restart** nginx —
  a reload does not pick that file up.
- **`/static/` needs `Access-Control-Allow-Origin`.** The loader mounts the
  component into the paper's own page, so `import()` of `dashboard.js` is a
  cross-origin module fetch — and ES module imports are CORS-checked even though a
  plain `<script src>` is not. Set `add_header Access-Control-Allow-Origin "*"
  always;` in the `location /static/` block. The JSON API sets the same header
  itself, in `dashboard_views.cross_origin`, so only the static files need nginx's
  help.

`/static/` is served with `expires 30d`, so a deploy has to invalidate its own
caches or a corrected asset sits in the CDN and in readers' browsers for a month.
That is what `config.storage.ForgivingManifestStaticFilesStorage` is for: from
`collectstatic` on, `dashboard.js` is published as `dashboard.<hash>.js` and a
changed file is simply a new URL. It is selected only when `DEBUG` is off, which
is why local work wants `DJANGO_DEBUG=True` — `runserver` serves assets straight
from the app directories, with no manifest to read.

One gap to know about: Django rewrites hashed names inside CSS `url()`, but
**not** inside ES module `import` statements. `dashboard.js` pulls MapLibre and
pmtiles in by relative path, so those vendor files keep their plain names and
stay cached for the full 30 days. It costs nothing day to day — vendor bundles
change rarely — but bumping MapLibre does still need a manual CDN purge.

### Notes

- **All configuration reaches Django through `.env`.** The systemd units pass only
  `DJANGO_SETTINGS_MODULE`, and nothing inherits an interactive shell's
  environment — so a variable exported in `~/.bashrc` is visible to `manage.py`
  over ssh and invisible to gunicorn and the worker. A setting can look correct
  from the shell while the live site reads an empty string.
- `django-q2` uses the Postgres database as its broker, so production needs only
  Postgres — no Redis. Run `manage.py qcluster` under systemd alongside the app.
- **Deploys must restart the worker after `migrate`,** not just the web process —
  it is the last line of [Deploying a change](#deploying-a-change) for a reason. A
  worker left running across a schema change keeps building queries from the model
  definitions it loaded at start-up.
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
  gunicorn `--timeout` comfortably above `OPENAI_TIMEOUT` (120s by default) —
  gunicorn's own 30s default will cut it off. Batching keeps each call short, but
  the request holds a worker for as long as the slowest batch takes.
- `/output/` and its two POST endpoints are **unauthenticated**, like the rest of
  the reader-facing site. `/output/summarize/` cannot be made to summarise
  arbitrary text — it rebuilds the export from the database — but anyone who can
  reach it can spend the OpenAI account, and one press is now several calls. If
  the site is on the public internet, put that endpoint behind staff login.
