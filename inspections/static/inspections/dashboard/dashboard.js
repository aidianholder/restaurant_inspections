/*
 * Per-newspaper inspections dashboard: map on top, filters, then the table.
 *
 * Mounted either on our own canonical page or straight into a newspaper's
 * template — same module, same API, two mount points. MapLibre is imported as an
 * ES module rather than loaded from a script tag, so no `window.maplibregl` is
 * ever created: WEHCO's weather features already own that global, and two
 * copies fighting over it is a bug nobody would enjoy debugging.
 *
 * The rule that makes the map and table stay in step cheaply: the map reflects
 * the FILTER state, never the sort or the page. Sorting reorders rows without
 * changing which facilities match, so it costs no request at all; paging shows
 * a different slice of the same matches, and a map showing only page one would
 * be useless.
 */

// Named imports: MapLibre's ESM build has no default export, and pulling in
// only what is used keeps the intent visible.
import {
  Map as MapLibreMap, NavigationControl, LngLatBounds, addProtocol,
} from "../vendor/maplibre-gl.mjs";
import { Protocol } from "../vendor/pmtiles.js";

// The basemap is a single .pmtiles archive on our own storage, so MapLibre has
// to be taught the `pmtiles://` scheme before any map is built. Self-hosted on
// purpose: a newspaper embed spikes the day a story runs, which is exactly when
// a metered or donation-funded tile service is worst placed to absorb it.
// Registered once per page, however many dashboards are mounted.
if (!window.__arhiPmtilesRegistered) {
  addProtocol("pmtiles", new Protocol().tile);
  window.__arhiPmtilesRegistered = true;
}

const SEARCH_DEBOUNCE_MS = 300;
const LABEL_MIN_ZOOM = 16;      // where names stop cluttering the view
const SELECT_ZOOM = 16;

const LEVELS = [
  ["P", "Priority"],
  ["PF", "Priority Foundation"],
  ["C", "Core"],
];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function option(value, label) {
  const o = document.createElement("option");
  o.value = value;
  o.textContent = label;
  return o;
}

function formatDate(iso) {
  if (!iso) return "—";
  const [y, m, d] = iso.split("-").map(Number);
  return `${m}/${String(d).padStart(2, "0")}/${String(y).slice(2)}`;
}

export function mountDashboard(root, options) {
  const api = options.apiBase.replace(/\/$/, "");
  // Which glyphs exist is a property of the style's font source, not of this
  // component: our own bucket carries Regular/Medium/Italic, OpenFreeMap carries
  // Regular/Bold/Italic. Single names only — MapLibre joins a multi-font stack
  // with commas into one glyph URL, which a static bucket has no directory for.
  const fonts = Object.assign(
    { regular: "Noto Sans Regular", emphasis: "Noto Sans Medium" }, options.fonts || {});
  const state = {
    q: "", county: "", from: "", to: "", type: "", cited: false,
    sort: "-date", page: 1, selected: null,
  };
  let mapCache = new Map();      // filter key -> GeoJSON, so revisiting a filter is free
  let mapFeatures = [];          // what is currently on the map, for lookups by id
  let searchTimer = null;
  let rowsToken = 0;

  root.classList.add("arhi-dashboard");
  root.innerHTML = "";

  // ---------------------------------------------------------------- layout
  if (options.heading) root.appendChild(el("h2", "arhi-heading", options.heading));

  const mapWrap = el("div", "arhi-map");
  const mapNote = el("div", "arhi-map-note");
  mapNote.hidden = true;
  mapWrap.appendChild(mapNote);
  root.appendChild(mapWrap);

  const controls = el("div", "arhi-controls");
  root.appendChild(controls);

  const search = buildField(controls, "Search", "search");
  const countySel = buildSelect(controls, "County", ["", ...(options.counties || [])],
    (c) => c || "All counties");
  const typeSel = buildSelect(controls, "Inspection type", ["", ...(options.types || [])],
    (t) => t || "All types");
  const fromInput = buildField(controls, "Inspected from", "date");
  const toInput = buildField(controls, "to", "date");

  const citedField = el("div", "arhi-field");
  const citedLabel = el("label", "arhi-toggle");
  const citedBox = document.createElement("input");
  citedBox.type = "checkbox";
  citedLabel.append(citedBox, document.createTextNode("Cited only"));
  citedField.append(el("label", null, " "), citedLabel);
  controls.appendChild(citedField);

  const status = el("div", "arhi-status");
  const statusText = el("span");
  const reset = el("button", "arhi-reset", "Reset filters");
  reset.type = "button";
  status.append(statusText, reset);
  root.appendChild(status);

  const selected = el("div", "arhi-selected");
  selected.hidden = true;
  root.appendChild(selected);

  const table = el("table", "arhi-table");
  const thead = el("thead");
  const tbody = el("tbody");
  table.append(thead, tbody);
  root.appendChild(table);
  buildHead();

  const pager = el("div", "arhi-pager");
  const prev = el("button", null, "Previous");
  const next = el("button", null, "Next");
  prev.type = next.type = "button";
  const pagerCount = el("span", "arhi-pager-count");
  pager.append(prev, next, pagerCount);
  if (options.fullscreenUrl) {
    const link = el("a", "arhi-fullscreen", "Open full screen →");
    link.href = options.fullscreenUrl;
    link.target = "_blank";
    link.rel = "noopener";
    pager.appendChild(link);
  }
  root.appendChild(pager);

  function buildField(parent, label, type) {
    const wrap = el("div", "arhi-field");
    const input = document.createElement("input");
    input.type = type;
    const lab = el("label", null, label);
    wrap.append(lab, input);
    parent.appendChild(wrap);
    return input;
  }

  function buildSelect(parent, label, values, labeller) {
    const wrap = el("div", "arhi-field");
    const select = document.createElement("select");
    values.forEach((v) => select.appendChild(option(v, labeller(v))));
    wrap.append(el("label", null, label), select);
    parent.appendChild(wrap);
    return select;
  }

  function buildHead() {
    const tr = el("tr");
    const columns = [
      ["Establishment", "name", null],
      ["City", "city", "arhi-col-city"],
      ["County", null, "arhi-col-county"],
      ["Last inspected", "date", null],
      ["Type", null, "arhi-col-type"],
      ["Violations (P / PF / C)", "violations", "arhi-col-levels"],
    ];
    columns.forEach(([label, sortKey, cls]) => {
      const th = el("th", cls);
      if (sortKey) {
        const button = el("button", null, label);
        button.type = "button";
        button.dataset.sort = sortKey;
        th.appendChild(button);
      } else {
        th.textContent = label;
      }
      tr.appendChild(th);
    });
    thead.appendChild(tr);
  }

  // ------------------------------------------------------------------ map
  const map = new MapLibreMap({
    container: mapWrap,
    style: options.mapStyle,
    center: options.center || [-92.4, 34.8],
    zoom: options.zoom || 7,
    attributionControl: { compact: true },
  });
  map.addControl(new NavigationControl({ showCompass: false }), "top-left");
  let mapReady = false;

  map.on("load", () => {
    map.addSource("facilities", {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] },
      promoteId: "id",
      // Thousands of pins at low zoom is an unreadable smear; MapLibre's own
      // clustering is free and looks like what readers expect.
      cluster: true,
      clusterRadius: 48,
      clusterMaxZoom: 13,
    });

    map.addLayer({
      id: "arhi-clusters", type: "circle", source: "facilities",
      filter: ["has", "point_count"],
      paint: {
        "circle-color": "#8c1d1d",
        "circle-opacity": 0.85,
        "circle-radius": ["step", ["get", "point_count"], 15, 25, 20, 100, 26, 500, 33],
        "circle-stroke-width": 2, "circle-stroke-color": "#fff",
      },
    });
    map.addLayer({
      id: "arhi-cluster-count", type: "symbol", source: "facilities",
      filter: ["has", "point_count"],
      layout: {
        "text-field": ["get", "point_count_abbreviated"],
        "text-font": [fonts.emphasis],
        "text-size": 12,
      },
      paint: { "text-color": "#fff" },
    });
    map.addLayer({
      id: "arhi-points", type: "circle", source: "facilities",
      filter: ["!", ["has", "point_count"]],
      paint: {
        // One neutral colour. Grading pins by violation count would be an
        // editorial claim about an establishment, not a design choice.
        "circle-color": "#2b6cb0",
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 4, 14, 7, 17, 9],
        "circle-stroke-width": 1.5, "circle-stroke-color": "#fff",
      },
    });
    map.addLayer({
      id: "arhi-selected-point", type: "circle", source: "facilities",
      filter: ["==", ["get", "id"], -1],
      paint: {
        "circle-color": "#8c1d1d",
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 7, 14, 11, 17, 14],
        "circle-stroke-width": 3, "circle-stroke-color": "#fff",
      },
    });

    // Two label layers. One appears only when zoomed in far enough that names
    // will not clutter the view, and lets MapLibre drop colliding labels; the
    // other follows the selection and is always drawn, whatever the zoom.
    const labelLayout = (collide) => ({
      // Set at the layer level as well as inside the format sections. Without it
      // MapLibre still resolves its own default stack — "Open Sans Regular,
      // Arial Unicode MS Regular" — as one comma-joined glyph URL, which a
      // self-hosted font directory has no entry for.
      "text-font": [fonts.regular],
      "text-field": [
        "format",
        ["get", "name"], { "text-font": ["literal", [fonts.emphasis]] },
        "\n", {},
        ["get", "address"], { "text-font": ["literal", [fonts.regular]], "font-scale": 0.82 },
      ],
      "text-size": 12,
      "text-offset": [0, 1.1],
      "text-anchor": "top",
      "text-allow-overlap": !collide,
      "text-ignore-placement": !collide,
      "text-max-width": 14,
    });
    const labelPaint = {
      "text-color": "#1a1a1a", "text-halo-color": "#fff", "text-halo-width": 1.6,
    };
    map.addLayer({
      id: "arhi-labels", type: "symbol", source: "facilities",
      filter: ["!", ["has", "point_count"]], minzoom: LABEL_MIN_ZOOM,
      layout: labelLayout(true), paint: labelPaint,
    });
    map.addLayer({
      id: "arhi-selected-label", type: "symbol", source: "facilities",
      filter: ["==", ["get", "id"], -1],
      layout: labelLayout(false), paint: { ...labelPaint, "text-halo-width": 2 },
    });

    map.on("click", "arhi-points", (event) => {
      const id = event.features[0].properties.id;
      selectFacility(id, { fromMap: true });
    });

    // Clicking the map away from a pin clears the selection, the same as the
    // Clear link. Only the point layers count as "a facility": a cluster is a
    // navigation target, so clicking one zooms in *and* drops the selection,
    // which is what the reader means by clicking somewhere else.
    //
    // MapLibre fires this alongside the layer handlers above, so it has to
    // hit-test rather than assume — otherwise selecting a pin would immediately
    // deselect it again.
    map.on("click", (event) => {
      if (state.selected === null) return;
      const onFacility = map.queryRenderedFeatures(event.point, {
        layers: ["arhi-points", "arhi-selected-point"].filter((id) => map.getLayer(id)),
      });
      if (!onFacility.length) selectFacility(null);
    });
    map.on("click", "arhi-clusters", (event) => {
      const feature = event.features[0];
      map.getSource("facilities")
        .getClusterExpansionZoom(feature.properties.cluster_id)
        .then((zoom) => map.easeTo({ center: feature.geometry.coordinates, zoom }))
        .catch(() => {});
    });
    ["arhi-points", "arhi-clusters"].forEach((layer) => {
      map.on("mouseenter", layer, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; });
    });

    mapReady = true;
    loadMap();
  });

  map.on("error", (event) => {
    // A basemap that fails to load leaves a usable table; say so rather than
    // showing an empty grey box with no explanation.
    if (event && event.error) console.warn("Dashboard map:", event.error.message);
  });

  // -------------------------------------------------------------- fetching
  function filterParams() {
    const p = new URLSearchParams();
    if (state.q) p.set("q", state.q);
    if (state.county) p.set("county", state.county);
    if (state.from) p.set("from", state.from);
    if (state.to) p.set("to", state.to);
    if (state.type) p.set("type", state.type);
    if (state.cited) p.set("cited", "1");
    return p;
  }

  function loadMap() {
    if (!mapReady) return;
    const key = filterParams().toString();
    const cached = mapCache.get(key);
    if (cached) { applyMap(cached); return; }

    fetch(`${api}/map?${key}`, { credentials: "same-origin" })
      .then((r) => r.json())
      .then((geojson) => {
        // Cache by filter state: a reader flipping between two counties, or
        // undoing a search, then costs nothing.
        if (mapCache.size > 12) mapCache = new Map();
        mapCache.set(key, geojson);
        applyMap(geojson);
      })
      .catch((error) => console.warn("Dashboard map data:", error));
  }

  function applyMap(geojson) {
    const source = map.getSource("facilities");
    if (!source) return;
    source.setData(geojson);
    // Kept alongside, rather than read back out of the source: MapLibre exposes
    // no public getter for a GeoJSON source's data, and reaching into `_data`
    // breaks silently whenever they rename a private field.
    mapFeatures = geojson.features;
    if (geojson.features.length) fitTo(geojson);
  }

  let fitted = false;
  function fitTo(geojson) {
    // Fit once on first load, and again whenever a filter narrows things down,
    // but never while a facility is selected — that would yank the reader away
    // from what they just clicked.
    if (state.selected != null) return;
    const bounds = new LngLatBounds();
    geojson.features.forEach((f) => bounds.extend(f.geometry.coordinates));
    if (!bounds.isEmpty()) {
      map.fitBounds(bounds, { padding: 48, maxZoom: 14, duration: fitted ? 600 : 0 });
      fitted = true;
    }
  }

  function loadRows() {
    const token = ++rowsToken;
    const params = filterParams();
    params.set("sort", state.sort);
    params.set("page", String(state.page));
    table.classList.add("arhi-loading");

    fetch(`${api}/rows?${params}`, { credentials: "same-origin" })
      .then((r) => r.json())
      .then((data) => {
        if (token !== rowsToken) return;   // a later request already answered
        renderRows(data);
      })
      .catch((error) => {
        if (token !== rowsToken) return;
        tbody.innerHTML = "";
        const tr = el("tr");
        tr.appendChild(el("td", "arhi-empty", "Could not load inspections. Try again."));
        tr.firstChild.colSpan = 6;
        tbody.appendChild(tr);
        console.warn("Dashboard rows:", error);
      })
      .finally(() => { if (token === rowsToken) table.classList.remove("arhi-loading"); });
  }

  // ------------------------------------------------------------- rendering
  function levels(row) {
    const wrap = document.createDocumentFragment();
    if (!row.total) {
      wrap.appendChild(el("span", "arhi-clean", "None"));
      return wrap;
    }
    [["arhi-p", row.p], ["arhi-pf", row.pf], ["", row.c]].forEach(([cls, n]) => {
      const span = el("span", `arhi-lvl ${cls} ${n ? "" : "arhi-zero"}`.trim(), String(n));
      wrap.appendChild(span);
      wrap.appendChild(document.createTextNode(" "));
    });
    return wrap;
  }

  function detailFor(row) {
    const box = el("div", "arhi-detail");
    const grouped = LEVELS.map(([code, label]) => [
      label, (row.violations || []).filter((v) => v.level === code),
    ]).filter(([, list]) => list.length);

    const uncategorised = (row.violations || []).filter(
      (v) => !LEVELS.some(([code]) => code === v.level));
    if (uncategorised.length) grouped.push(["Other observations", uncategorised]);

    if (!grouped.length) {
      box.appendChild(el("p", "arhi-sub",
        row.total ? "Observation details have not been retrieved for this inspection yet."
                  : "No violations were noted at this inspection."));
    }
    grouped.forEach(([label, list]) => {
      const group = el("div", "arhi-group");
      group.appendChild(el("div", "arhi-group-label", label));
      const ul = document.createElement("ul");
      list.forEach((v) => ul.appendChild(el("li", null, v.text)));
      group.appendChild(ul);
      box.appendChild(group);
    });

    const link = el("a", null, "All inspections for this establishment →");
    link.href = row.url;
    link.target = "_blank";
    link.rel = "noopener";
    const p = el("p");
    p.style.margin = "12px 0 0";
    p.appendChild(link);
    box.appendChild(p);
    return box;
  }

  function rowElement(row) {
    const tr = el("tr", "arhi-row");
    tr.dataset.id = String(row.id);

    const name = el("td");
    name.appendChild(el("div", "arhi-name", row.name));
    name.appendChild(el("div", "arhi-sub", row.street || ""));
    tr.appendChild(name);
    tr.appendChild(el("td", "arhi-col-city", row.city || ""));
    tr.appendChild(el("td", "arhi-col-county", row.county || ""));
    tr.appendChild(el("td", null, formatDate(row.date)));
    tr.appendChild(el("td", "arhi-col-type", row.type || ""));
    const lvl = el("td", "arhi-col-levels");
    lvl.appendChild(levels(row));
    tr.appendChild(lvl);

    const detailRow = el("tr", "arhi-detail-row");
    const cell = el("td", "arhi-detail-cell");
    cell.colSpan = 6;
    cell.appendChild(detailFor(row));
    detailRow.appendChild(cell);
    detailRow.hidden = true;

    tr.addEventListener("click", () => {
      const opening = detailRow.hidden;
      detailRow.hidden = !opening;
      if (opening) selectFacility(row.id, { row });
    });
    return [tr, detailRow];
  }

  function renderRows(data) {
    tbody.innerHTML = "";
    if (!data.rows.length) {
      const tr = el("tr");
      const td = el("td", "arhi-empty", "No establishments match these filters.");
      td.colSpan = 6;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    data.rows.forEach((row) => {
      const [tr, detail] = rowElement(row);
      if (state.selected === row.id) tr.classList.add("arhi-is-selected");
      tbody.append(tr, detail);
    });

    const shown = data.total ? `${data.total.toLocaleString()} establishment${data.total === 1 ? "" : "s"}` : "No establishments";
    statusText.textContent = shown;
    pagerCount.textContent = data.total ? `Page ${data.page} of ${data.pages}` : "";
    prev.disabled = data.page <= 1;
    next.disabled = data.page >= data.pages;
    state.page = data.page;

    // Geocoding misses a percent or two. A reader who spots the map showing
    // fewer pins than the count says will assume the map is broken.
    if (data.unmapped) {
      mapNote.hidden = false;
      mapNote.textContent = `${data.unmapped} of these could not be mapped`;
    } else {
      mapNote.hidden = true;
    }

    thead.querySelectorAll("button[data-sort]").forEach((button) => {
      const key = button.dataset.sort;
      const active = state.sort === key || state.sort === `-${key}`;
      const descending = state.sort === `-${key}`;
      button.textContent = button.textContent.replace(/ [↑↓]$/, "");
      if (active) button.textContent += descending ? " ↓" : " ↑";
    });
  }

  // ------------------------------------------------------------- selection
  function renderSelected(row) {
    selected.innerHTML = "";
    const head = el("div", "arhi-selected-head");
    head.appendChild(el("span", null, "Selected"));
    const clear = el("button", "arhi-clear", "Clear");
    clear.type = "button";
    clear.addEventListener("click", () => selectFacility(null));
    head.appendChild(clear);
    selected.appendChild(head);

    const line = el("div");
    line.appendChild(el("strong", null, row.name));
    selected.appendChild(line);
    selected.appendChild(el("div", "arhi-sub", `${row.address || ""}${row.type ? " · " + row.type : ""} · ${formatDate(row.date)}`));
    const lvl = el("div");
    lvl.style.marginTop = "6px";
    lvl.appendChild(levels(row));
    selected.appendChild(lvl);
    selected.appendChild(detailFor(row));
    selected.hidden = false;
  }

  function selectFacility(id, opts = {}) {
    state.selected = id;

    map.getLayer("arhi-selected-point") &&
      map.setFilter("arhi-selected-point", ["==", ["get", "id"], id == null ? -1 : id]);
    map.getLayer("arhi-selected-label") &&
      map.setFilter("arhi-selected-label", ["==", ["get", "id"], id == null ? -1 : id]);

    tbody.querySelectorAll(".arhi-row").forEach((tr) => {
      tr.classList.toggle("arhi-is-selected", tr.dataset.id === String(id));
    });

    if (id == null) { selected.hidden = true; return; }

    // Pinned above the table rather than jumping the reader to whichever page
    // the row lives on — a pin click should never move their pagination.
    const known = opts.row;
    if (known) renderSelected(known);
    else {
      fetch(`${api}/facility/${id}`, { credentials: "same-origin" })
        .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
        .then(renderSelected)
        .catch(() => { selected.hidden = true; });
    }

    if (opts.fromMap) {
      selected.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } else {
      const feature = mapFeatures.find((f) => f.properties.id === id);
      if (feature) {
        map.easeTo({ center: feature.geometry.coordinates, zoom: Math.max(map.getZoom(), SELECT_ZOOM), duration: 700 });
      }
    }
  }

  // ----------------------------------------------------------------- wiring
  function filtersChanged() {
    state.page = 1;
    loadRows();
    loadMap();     // only the filters move the map; sort and page never do
  }

  search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.q = search.value.trim(); filtersChanged(); },
                             SEARCH_DEBOUNCE_MS);
  });
  countySel.addEventListener("change", () => { state.county = countySel.value; filtersChanged(); });
  typeSel.addEventListener("change", () => { state.type = typeSel.value; filtersChanged(); });
  fromInput.addEventListener("change", () => { state.from = fromInput.value; filtersChanged(); });
  toInput.addEventListener("change", () => { state.to = toInput.value; filtersChanged(); });
  citedBox.addEventListener("change", () => { state.cited = citedBox.checked; filtersChanged(); });

  reset.addEventListener("click", () => {
    search.value = ""; countySel.value = ""; typeSel.value = "";
    fromInput.value = ""; toInput.value = ""; citedBox.checked = false;
    Object.assign(state, { q: "", county: "", from: "", to: "", type: "", cited: false });
    selectFacility(null);
    fitted = false;
    filtersChanged();
  });

  thead.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-sort]");
    if (!button) return;
    const key = button.dataset.sort;
    // Sorting changes the order, not the matching set — so the map is left
    // alone and this costs exactly one request.
    state.sort = state.sort === `-${key}` ? key : `-${key}`;
    state.page = 1;
    loadRows();
  });

  prev.addEventListener("click", () => { if (state.page > 1) { state.page -= 1; loadRows(); } });
  next.addEventListener("click", () => { state.page += 1; loadRows(); });

  loadRows();
  return { map, state };
}
