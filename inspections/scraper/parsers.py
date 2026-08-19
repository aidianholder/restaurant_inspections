"""Turn ADH inspection-search HTML into plain dicts.

Everything here is pure: HTML in, data out, no network. That keeps the parsing
rules testable against saved fixtures as the state's markup drifts.
"""

import html
import re
from datetime import datetime

from bs4 import BeautifulSoup

from ..places import AR_PLACES

# Longest-first so "North Little Rock" is matched before "Little Rock".
_PLACES_BY_LENGTH = sorted(AR_PLACES, key=lambda p: -len(p))

ADDRESS_TAIL_RE = re.compile(
    r"^(?P<left>.*?),\s*(?P<state>[A-Z]{2})\.?\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$"
)
OBSERVATION_COUNT_RE = re.compile(r"Observation\(s\)\s*(\d+)", re.I)
POSTBACK_TARGET_RE = re.compile(r"WebForm_PostBackOptions\(\s*&quot;|WebForm_PostBackOptions\(\s*\"([^\"]+)\"")
EXTERNAL_FILE_RE = re.compile(r"(/Web/Common/ExternalFileViewer\.aspx\?[^'\"\s]+)")


def clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def parse_date(value):
    value = clean(value)
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def split_address(blob):
    """Split "10815 Colonel Glenn Rd STE 450 Little Rock, AR 72204".

    The state runs street and city together with inconsistent whitespace, so we
    anchor on the trailing ", ST ZIP" and then match the longest known Arkansas
    place name at the end of what remains. Returns a dict; `needs_review` is True
    when the city could not be identified, so a human can check it in the admin.
    """
    blob = clean(blob)
    out = {
        "raw": blob,
        "street": blob,
        "city": "",
        "state": "AR",
        "zip_code": "",
        "needs_review": True,
    }
    if not blob:
        return out

    m = ADDRESS_TAIL_RE.match(blob)
    if not m:
        return out

    left = clean(m.group("left"))
    out["state"] = m.group("state").upper()
    out["zip_code"] = m.group("zip")
    out["street"] = left
    out["needs_review"] = False

    upper = left.upper()
    for place in _PLACES_BY_LENGTH:
        p = place.upper()
        if upper == p:
            out["street"], out["city"] = "", left
            return out
        if upper.endswith(" " + p):
            cut = len(left) - len(p)
            out["street"] = clean(left[:cut])
            out["city"] = clean(left[cut:])
            return out

    # Tail didn't match a known place: keep the whole thing as street and flag it.
    out["needs_review"] = True
    return out


def _postback_target(el):
    """Pull the ASP.NET control name a link or image button posts back as."""
    if el is None:
        return ""
    if el.name == "input" and el.get("name"):
        return el["name"]
    blob = (el.get("href") or "") + " " + (el.get("onclick") or "")
    m = re.search(r"WebForm_PostBackOptions\(\s*[\"']([^\"']+)[\"']", html.unescape(blob))
    if m:
        return m.group(1)
    m = re.search(r"__doPostBack\(\s*[\"']([^\"']+)[\"']", html.unescape(blob))
    return m.group(1) if m else ""


def parse_form_state(soup):
    """Every hidden input on the page — the ViewState bundle we must echo back."""
    state = {}
    for inp in soup.select("input[type=hidden]"):
        name = inp.get("name")
        if name:
            state[name] = inp.get("value", "")
    return state


def _parse_inspection_row(row):
    """One inspection line, from either the main grid or a nested history grid.

    The two grids have different leading columns (the main grid starts with a
    spacer and the name/address cell), so rather than hardcode indexes we find
    the cell that parses as a date and read the rest relative to it.
    """
    cells = row.find_all("td", recursive=False)
    date_idx = date = None
    for i, cell in enumerate(cells):
        parsed = parse_date(cell.get_text())
        if parsed is not None:
            date_idx, date = i, parsed
            break
    if date is None:
        return None

    def cell_at(offset):
        idx = date_idx + offset
        return cells[idx] if 0 <= idx < len(cells) else None

    type_cell = cell_at(1)
    violations_cell = cell_at(2)
    report_cell = cell_at(3)

    violations_link = violations_cell.find("a", id=re.compile(r"lnkViolations")) if violations_cell else None
    report_btn = report_cell.find("input", id=re.compile(r"btnInspectionReport")) if report_cell else None

    count = 0
    if violations_link:
        m = OBSERVATION_COUNT_RE.search(clean(violations_link.get_text()))
        if m:
            count = int(m.group(1))

    return {
        "date": date,
        "date_idx": date_idx,
        "inspection_type": clean(type_cell.get_text()) if type_cell else "",
        "observation_count": count,
        "violations_target": _postback_target(violations_link) if count else "",
        "report_target": _postback_target(report_btn),
    }


def parse_results_page(html_text):
    """Parse one page of the results grid.

    Returns {"facilities": [...], "page": n, "total_pages": n, "coordinates": [...]}.
    Each facility carries its full inspection list, because the site ships every
    past inspection inline in a nested table — no extra request needed.
    """
    soup = BeautifulSoup(html_text, "lxml")
    grid = soup.find("table", id="MainContent_gvInspections")
    result = {"facilities": [], "page": 1, "total_pages": 1, "coordinates": parse_map_points(html_text)}
    if grid is None:
        return result

    body = grid.find("tbody") or grid
    for row in body.find_all("tr", recursive=False):
        css = " ".join(row.get("class") or [])
        if "GridPager" in css:
            nums = [int(a.get_text()) for a in row.find_all("a") if a.get_text().strip().isdigit()]
            current = [int(s.get_text()) for s in row.find_all("span") if s.get_text().strip().isdigit()]
            result["page"] = current[0] if current else 1
            result["total_pages"] = max(nums + current + [1])
            continue
        if "GridItem" not in css and "GridAltItem" not in css:
            continue

        cells = row.find_all("td", recursive=False)
        if not cells:
            continue

        latest = _parse_inspection_row(row)
        if latest is None:
            continue

        # Name/address cell sits immediately left of the date column: the bare
        # name as a text node, then a div of address, then a div of phone.
        name_cell = cells[latest["date_idx"] - 1] if latest["date_idx"] >= 1 else cells[0]
        name = next(
            (clean(t) for t in name_cell.find_all(string=True, recursive=False) if clean(t)), ""
        )
        divs = name_cell.find_all("div")
        address_blob = clean(divs[0].get_text()) if divs else ""
        phone = clean(divs[1].get_text()) if len(divs) > 1 else ""

        past_link = row.find("a", id=re.compile(r"lnkPastInspections"))
        facility = {
            "name": name,
            "phone": phone,
            "address": split_address(address_blob),
            "source_key": (past_link.get("key") if past_link else "") or "",
            "source_sequence_key": (past_link.get("inspectionsequencekey") if past_link else "") or "",
            "inspections": [],
        }

        facility["inspections"].append(latest)

        nested = row.find("table", id=re.compile(r"gvPastInspections"))
        if nested is None:
            # The nested history grid renders in the following sibling row.
            sib = row.find_next_sibling("tr")
            if sib is not None:
                nested = sib.find("table", id=re.compile(r"gvPastInspections"))
        if nested is not None:
            nbody = nested.find("tbody") or nested
            for nrow in nbody.find_all("tr", recursive=False):
                ncss = " ".join(nrow.get("class") or [])
                if "GridItem" not in ncss and "GridAltItem" not in ncss:
                    continue
                parsed = _parse_inspection_row(nrow)
                if parsed:
                    facility["inspections"].append(parsed)

        # Same date+type can legitimately appear twice; number them for the natural key.
        seen = {}
        for insp in facility["inspections"]:
            insp.pop("date_idx", None)
            key = (insp["date"], insp["inspection_type"])
            insp["sequence_within_day"] = seen.get(key, 0)
            seen[key] = insp["sequence_within_day"] + 1

        result["facilities"].append(facility)

    return result


def parse_map_points(html_text):
    """Coordinates the server emits when "Display Map" is checked.

    Bonus data only: this array covers a partial subset of the result set and is
    not aligned with grid order, so callers match on name + address, never index.
    """
    pts = re.search(r"var points = \[(.*?)\];", html_text, re.S)
    names = re.search(r"var names = \[(.*?)\];", html_text, re.S)
    if not pts or not names:
        return []
    coords = re.findall(r"LatLng\(\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*\)", pts.group(1))
    labels = [html.unescape(x) for x in re.findall(r"'((?:[^'\\]|\\.)*)'", names.group(1))]
    out = []
    for i, label in enumerate(labels):
        if i < len(coords):
            out.append({"name": clean(label), "latitude": coords[i][0], "longitude": coords[i][1]})
    return out


def parse_violations(html_text):
    """The observations overlay: code, code explanation, inspector comments."""
    soup = BeautifulSoup(html_text, "lxml")
    out = []
    codes = soup.select('span[id*="rptViolations_lblRegulatorCodeType_"]')
    for i, code_el in enumerate(codes):
        suffix = code_el.get("id", "").rsplit("_", 1)[-1]
        # The lbl* element is only the header text; the body lives in the panel.
        expl = soup.find(id=re.compile(rf"rptViolations_pnlCodeExplanation_{suffix}$"))
        comments = soup.find(id=re.compile(rf"rptViolations_pnlComments_{suffix}$"))

        expl_text = clean(expl.get_text()) if expl else ""
        expl_text = re.sub(r"^Code Explanation\s*", "", expl_text)
        comment_text = clean(comments.get_text()) if comments else ""
        comment_text = re.sub(r"^Inspector Comments\s*", "", comment_text)

        out.append(
            {
                "ordinal": i,
                "code": clean(code_el.get_text()),
                "code_explanation": expl_text,
                "inspector_comments": comment_text,
            }
        )
    return out


def find_report_url(html_text):
    """The PDF viewer URL the report postback injects via window.open()."""
    m = EXTERNAL_FILE_RE.search(html.unescape(html_text))
    return m.group(1) if m else ""
