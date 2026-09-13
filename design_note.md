# Design note: the per-newspaper inspections dashboard

> **Built 2026-09-13.** What follows is the reasoning that shaped it; where
> the build taught us something the note did not anticipate, it says so.

Written 2026-09-12, before any code; revised as decisions were made. A
per-publication public view over a readership area — 8–9 counties at the largest,
under 10,000 facilities — with a MapLibre map above a searchable, sortable,
paginated table of inspections, the two kept in sync.

Doable, and mostly with pieces already here. But it doesn't fit the current
embed, and the reason is worth being precise about, because it decides the rest.

The existing embed is one server-rendered, self-contained, cached HTML document.
Everything interactive happens in the reader's browser over data already
shipped. `Embed.row_limit`'s help text says where that ends: *"Paging and
sorting happen in the reader's browser, which stops being viable past ~1000."*
That architecture can't stretch to this. Not because of the map — because of the
row count.

## The number that decides everything

**Settled.** The largest readership area is 8–9 counties; under 10,000 facilities
is the ceiling, 5,000 or fewer is typical. Geocoding coverage is 98–99%.

Measured payloads for the map, gzipped, carrying id, position, name, address and
violation count:

| Facilities | GeoJSON | Compact arrays |
|---|---|---|
| 5,000 (typical) | **122 KB** | 106 KB |
| 8,000 | 194 KB | 169 KB |
| 10,000 (ceiling) | 242 KB | 211 KB |

So the whole matching set ships to the browser in one request, once, cached hard
against the data fingerprint. **Use GeoJSON, not the compact encoding** — it goes
straight into MapLibre with no client-side transform, and the 13% saving is not
worth a decode step.

Client-side `setFilter` then handles county, date and type filtering with no
network at all. This is what makes the map and table sync instant.

The table is still server-side paginated. That is not about payload — it is about
not shipping inspection detail for 10,000 facilities, and about sorting and
searching in the database rather than in JavaScript.

### If it ever outgrows that

Recorded for later, not for now. Around 12,000+ the single payload gets
uncomfortable, and the fix is **not** bbox queries. At low zoom the bbox is the
whole region, so it reduces nothing exactly where the pressure is; at high zoom
it reduces a payload that was never a problem. What makes low zoom work is
*aggregation*, and zoom-dependent aggregation plus bbox plus caching is vector
tiles, which PostGIS already offers via `ST_AsMVT`.

The shape would be a tiled base layer for everything, plus a small GeoJSON layer
holding just the current matches — filtering reduces the set, so the match layer
stays cheap. The API decides: under ~10,000 matches, here is GeoJSON; above that,
use the tiles.

bbox does earn its place in two narrower cases: fetching labels for the current
viewport at high zoom if names are ever dropped from the main payload, and
"search this area" as a deliberate product feature — which changes what the table
means, and should be decided on its own merits rather than arrived at sideways
through a performance problem.

## Delivery: an API underneath, one component, two mount points

An earlier draft of this note framed the choice as "iframe the app, or send
readers to a page." That was a false pair, because it ran together two things
that are independent:

1. **Where the querying happens** — the server. Settled above, non-negotiable at
   5,000 facilities.
2. **Where the HTML is produced** — a Django template, or JavaScript running on
   the newspaper's own page.

Server-side pagination is *exactly what an API does*: the client sends
`?county=Faulkner&sort=name&page=3`, the server returns 25 rows and a total. None
of that requires Django to emit the markup. Paging is not an argument for
server-rendering.

### Build the API first, whatever the presentation

Even the fully Django-rendered version needs JSON endpoints. Otherwise sorting a
column is a full page reload, the map re-initialises on every interaction, and
the whole thing feels like 2009. The moment the map and table have to stay in
step without reloading, JSON is being fetched — so the API exists either way.

The API is therefore not an alternative to the page. It is the foundation under
every option, and once it exists the presentation choice is cheap and reversible.

Three read-only endpoints:

```
GET /api/v1/dashboard/<slug>/rows?q=&county=&from=&to=&type=&sort=&page=
    -> {rows: [...25...], total, page, pages}

GET /api/v1/dashboard/<slug>/map?q=&county=&from=&to=&type=
    -> compact array of every match: [id, lon, lat, count, name, county]

GET /api/v1/dashboard/<slug>/facility/<id>
    -> one row, for the "selected" strip when a pin is clicked
```

No DRF. Three read-only GETs returning `JsonResponse` is what `scrape_status` and
`output_render` already do, and a framework for it would cut against the
dependency-light line held elsewhere (Postgres as the broker, no Redis).

CORS is the one new piece. The data is public record, so `*` is defensible, but
an allowlist per publication gives a kill switch and keeps the bandwidth ours.

### Write the front end once, mount it twice

Build it as a mountable component. Host it at a canonical URL — that is the
full-screen page and the iframe target — and ship the same bundle as a loader
script a paper can drop into its own template. Same code, same API, two mount
points. Supporting both costs almost nothing after the first.

### What actually decides iframe vs. widget

Not paging. These:

- **CSP.** A paper running a Content Security Policy needs the script domain in
  `script-src` *and* `connect-src`, and the tile bucket in `connect-src` and
  `img-src`. An iframe needs only `frame-src`. This is the most common way widget
  integrations die, and it is a conversation with each paper's web team rather
  than something we can fix from here.
- **CSS collisions.** Their stylesheet reaches into our table and ours leaks out.
  Shadow DOM solves it properly, though MapLibre inside a shadow root needs
  testing — it reaches for `document` in a few places. Prefixed classes are the
  boring fallback.
- **Their runtime.** An ad script, an old jQuery, another map library at a
  different version. An iframe is insulated from all of it. This is the honest
  argument for iframes.
- **Page weight.** MapLibre is ~200KB gzipped. On our page that is our budget; on
  their article page it is theirs, and papers watch Core Web Vitals closely. Lazy
  load the map only when it scrolls into view.

Against those: no height-sync dance, no mobile touch fight between a pan/zoom map
and the article's scroll, deep links and the back button working through the host
page's own URL, and the paper's analytics seeing the interactions.

**Decided: the component is the default, the iframe is the fallback.**

The two risks that argued for iframes are both absent here — WEHCO controls the
newspapers' CSS, and CSP is not a constraint. That removes the case against
mounting directly on the host page, and everything in the "against" column
(no height sync, no mobile touch fight, working deep links and back button,
their analytics seeing the interactions) applies.

The iframe stays supported for any publication outside that control. Same
bundle, same API, so it costs nothing to keep.

One risk that does remain: **some WEHCO weather features already run MapLibre on
the same pages, mounted on `#map`.** Two consequences, both handled in the
interface spec below — the component must never use that id (or any id), and
MapLibre must be imported as a module rather than loaded as a UMD script tag, so
there is no `window.maplibregl` global for the two features to fight over.

### What stays server-rendered

The per-facility history page. That is content — linkable, quotable, worth
indexing.

**Built as a second view, not a reuse of `facility_detail`.** The staff page
carries coordinates, the geocoder used, the "state site shows N" badge, and
inspections whose details have never been fetched — none of which belongs in
front of a reader, and all of which the staff need. One template with
`{% if request.user.is_staff %}` around the sensitive parts is how that leaks;
a separate template cannot render what it does not contain. It also has no site
navigation, because the staff pages are going behind a VPN or a login.

The dashboard itself is a tool, not an article. Nobody needs Google to index a
filter state.

## Who serves the basemap

The thing most often missed: MapLibre GL JS is the renderer, not the data. Tiles
are a separate decision, and on a newspaper embed that spikes the day a story
runs, a metered tile bill arrives exactly when you least want it. Any API key in
an embed is public, so it has to be domain-restricted.

**Self-hosted PMTiles** — a single `.pmtiles` file for the area, served as a
static object. No key, no per-view cost, no rate limit, no third party that can
throttle you mid-story. For a fixed geographic area that never changes, a very
good fit. MapTiler or Stadia are the hosted alternatives.

**Settled, 2026-09-12, and in use by the dashboard since 2026-09-13.**
`wehco.pmtiles` on DigitalOcean Spaces, with `protostyle3.json`. Using it costs
two more vendored files — `pmtiles.js` and `fflate.js` — plus an `addProtocol`
call before the map is built. The archive measured well — clustered, 2.7KB root directory,
834 leaf dirs averaging 7.7KB, z0–15 — and the CDN served cold random ranges at a
95ms median, three times faster than the Spaces origin. Sixty parallel cold
ranges completed in 242ms, so a viewport's worth of tiles is not a concern.

The slowness that prompted the investigation was in the *style*, not the
archive: glyphs were being fetched from `protomaps.github.io` (GitHub Pages,
76–130KB per range, blocking every label), three of four sprite sheets were
unreachable dead weight on a second domain, and a declared-but-unused
`raster-dem` source cost a TileJSON round trip to a third party. `protostyle3`
fixes all three and self-hosts the glyphs, so everything now comes from one
domain — one DNS lookup, one TLS handshake, HTTP/2 throughout.

One caveat worth remembering: sustained bursts against that bucket *can* draw
Cloudflare throttling. It showed up during testing as 60-second stalls after
roughly a hundred rapid requests. No normal reader will approach that, but bulk
or scripted tile access should be paced.

This is also where the "no external CSS, JS, fonts or images" rule in the embed
documentation formally breaks — MapLibre is ~200KB gzipped plus CSS. Worth
updating that principle deliberately rather than by accident.

## Keeping the map and table in sync

Very doable, and the load concern mostly dissolves once three things usually
lumped together are separated:

**Sorting must not touch the map at all.** Sorting reorders rows; the *set* of
matching facilities is identical. The map has nothing to redraw. Rapid sorting is
therefore free by construction.

**Paging must not touch the map either.** If the map only showed the current
page's 25 pins it would be useless. The map reflects the *filter* state, not the
page.

So the map only reacts to search and filter changes — a much rarer event, and one
naturally debounced by typing.

Then the payload. **The map needs far less per facility than the table does**:
id, lat, lon, name, violation count, county. That's ~50–80 bytes in a compact
format — around 100KB gzipped for 5,000 facilities. Fetch once per publication,
cache hard (the `_data_fingerprint` trick in `embed_views` already does this kind
of invalidation), then filter client-side with MapLibre's `setFilter` against
feature properties. Zero network on most interactions, instant sync.

| | Table | Map |
|---|---|---|
| Payload | 25–50 rows, full detail | all matching, 5 fields each |
| Refetches on | search, filter, sort, page | search/filter only — often not even then |

For the row dropdown: include the latest inspection's violations in the table
response. 25 rows × ~5 violations is nothing, and the dropdown opens instantly
with no second request.

At 5,000 points, MapLibre's built-in clustering is needed when zoomed out. That's
free, but it interacts with "click a pin to select" — readers have to zoom in to
break a cluster apart.

## Interface specification

Vertical order: **map, then filter and search controls, then the table.**

Everything is scoped under a single root element, `.arhi-dashboard`. Every class
is prefixed `arhi-`. **No ids are used anywhere** — the map container is found by
class within the root and passed to MapLibre as an element, not an id string.
That sidesteps the existing `#map` collision by construction rather than by
choosing a different name. (`arhi-` is one find-and-replace away from anything
else.)

### Sizing

Designed for a content column between **480px and 1280px**. The component is sized
by its container, not the viewport, so breakpoints are container queries with
media-query fallbacks.

| Container width | Map height |
|---|---|
| ≤ 640px | 360px |
| 641–1023px | 460px |
| ≥ 1024px | 560px |

560 rather than 600 at the top end for a specific reason: the reader needs the map
*and* at least the first row of filter controls visible together to understand that
one drives the other. On a 900px-tall browser window, below the paper's own header,
600px pushes the controls under the fold. 600 is fine if the component sits high on
the page — worth checking against a real article template before fixing it.

On a 480px phone, 360px is already 45% of the screen; anything taller means
scrolling past the map to reach the table on every interaction.

### Table columns by width

The row dropdown carries the full inspection detail regardless, so narrow layouts
drop columns rather than truncating them.

| Container width | Columns |
|---|---|
| ≥ 1024px | Name · Address · County · Last inspected · Type · P · PF · C |
| 768–1023px | Name · City · Last inspected · P · PF · C |
| < 768px | Name · Last inspected · total violations |

### Selection

Selection is explicit and bidirectional: clicking a table row selects the map
point, clicking a map point selects the table row.

- The selected row is **highlighted and pinned** in a distinct "Selected" strip
  directly above the table header, so a pin click never disturbs the reader's
  page or filters. If that facility also appears in the current page of results,
  that row carries the highlight too.
- The pinned strip has an explicit clear control.
- On the map, the selected point is drawn larger with an accent stroke.

### Map labels

Two symbol layers over the facility points, both using the self-hosted Noto Sans
glyphs.

1. **Zoom labels** — `minzoom` 16, every facility, collision enabled so MapLibre
   drops labels rather than stacking them. This is the "high zoom, won't clutter"
   case.
2. **Selection label** — no minzoom, filtered to the selected facility, with
   `text-allow-overlap` and `text-ignore-placement` so it is always drawn
   whatever the zoom.

Both render the same two-line block, name above address, name larger:

```js
["format",
  ["get", "name"], {"text-font": ["literal", ["Noto Sans Medium"]]},
  "\n", {},
  ["get", "address"], {"text-font": ["literal", ["Noto Sans Regular"]], "font-scale": 0.82}
]
```

with a white halo for legibility over the basemap. Name and address therefore have
to be in the GeoJSON — which the payload numbers above already account for.

## What will actually bite

**The latest-inspection columns — done, and for a different reason than first
argued.** An earlier draft of this note called the correlated-subquery pattern
"the expensive query". Measured at 10,000 facilities it is not: a page sorted by
date takes 26ms, which is fine.

What it could not do is sort by violation count. Those counts were attached
*after* paging, and you cannot order by a value you compute after you have
paged. Reaching it from the old shape needs a subquery whose `OuterRef` points at
another annotation, which is the fragile construct
`views._attach_latest_violation_counts` existed to avoid.

So `Facility` now carries `latest_inspection`, `latest_inspection_date`,
`latest_inspection_type`, `latest_violation_total`, the P/PF/C breakdown, and
`inspection_count`. Sorting by violation count went from 35.5ms to 0.7ms, which
also buys headroom for a public page when a story spikes traffic.

**The counts describe the latest inspection alone, never the history.** One real
example: 25 inspections, 32 violations lifetime, clean at the most recent visit,
so the stored total is 0. The lifetime figure would libel a clean restaurant.

They are recomputed rather than incrementally maintained — scrape runs refresh
what they touched, `InspectionAdmin` refreshes on hand edits, and
`manage.py rebuild_latest_inspection` covers everything else.

For "search across all fields", `icontains` will creak. `pg_trgm` with a GIN
index is a small addition to a database already running PostGIS.

## Two wrinkles to decide early

**Not every facility is on the map** — but at **98–99% coverage this is a
footnote, not a design driver.** A filter returning 200 results might show 197
pins. The dashboard should still say so ("3 of these could not be mapped"),
because a reader who notices the discrepancy will otherwise assume the map is
broken. It does not change the layout.

**Map → row, when the row is on page 7.** Clicking a pin has to surface that
facility in a server-paginated table. Jumping the reader to page 7 is
disorienting. Better to fetch just that facility and pin it in a "selected" strip
above the results, leaving pagination and filters untouched.

## What the build actually taught us

Three things the reasoning above did not anticipate:

**Vendoring MapLibre means three files, not one.** `maplibre-gl.mjs` imports
`maplibre-gl-shared.mjs`, and separately resolves `maplibre-gl-worker.mjs`
relative to `import.meta.url`. Miss the worker and the map never finishes
loading — no console error, no failed layer, just a style that stays unloaded
forever. That cost more debugging than anything else in the build.

**MapLibre's ESM build has no default export.** `import maplibregl from …` fails
with a clear error; named imports are required. Which is arguably better — it
makes what the component uses explicit.

**Self-hosted glyphs constrain how font stacks may be written.** MapLibre joins
a multi-font stack into one comma-separated glyph URL. A hosted service resolves
that server-side; a static bucket has no such directory, so every stack must name
exactly one font. And `text-font` has to be set at the layer level as well as
inside a `format` expression, or MapLibre additionally resolves its own composite
default. Both failures are silent — the labels simply do not draw.

**There is no public getter for a GeoJSON source's data.** Reaching into
`source._data` to find a feature by id works right up until MapLibre renames a
private field. The component keeps its own reference to what it last set
instead.

## Open questions

Three, in the order they block work.

All three are settled.

**Build step:** none. Plain ES modules served as static files, MapLibre vendored
beside them.

**Denormalisation:** done — see above.

**Real data:** 1,919 facilities across seven counties, which the whole build was
verified against.

Defaults taken, all still open to correction: map pins are **a single neutral
colour**, because grading them by violation count would be an editorial claim
about a named business rather than a design choice; the date filter targets the
latest inspection date; and the sortable columns are name, city, date and
violation count.

Still worth a decision before this goes in front of readers: whether the API
should be rate-limited or cached at the edge. It is public and unauthenticated by
design, like the rest of the reader-facing site, and `api/map` is the expensive
one.
