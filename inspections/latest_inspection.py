"""Keep `Facility`'s denormalised latest-inspection columns true.

Those columns exist so the dashboard can filter, sort and page against one
indexed table. The value they buy is mostly not speed — a correlated subquery
over 10,000 facilities runs in about 26ms — but reach: violation counts used to
be attached *after* paging, which makes it impossible to order by them, and
working around that means a subquery whose OuterRef points at another
annotation, the fragile shape `views._attach_latest_violation_counts` was
written to avoid.

**Recomputed, not incrementally maintained.** Four different places change the
answer — a new inspection arriving, the website overlay writing violations, a
report PDF replacing them, someone editing a violation inline in the admin — and
a fifth will be added one day by someone who does not know this file exists.
Hooking each one is how these columns go quietly wrong. One idempotent pass at
the end of a scrape run cannot drift, and can be re-run over everything at any
time to prove it.

The counts describe the latest inspection alone. `inspection_count` is the one
field that covers the whole history.
"""

import logging

from django.db import connection

logger = logging.getLogger(__name__)

# `DISTINCT ON` picks one inspection per facility; the ORDER BY decides which.
# It matches the tiebreak used everywhere else — date, then sequence within the
# day, then pk — so "latest" means the same thing here as it does in the views.
_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (i.facility_id)
           i.facility_id, i.id AS inspection_id, i.date, i.inspection_type
    FROM inspections_inspection i
    {facility_filter}
    ORDER BY i.facility_id, i.date DESC, i.sequence_within_day DESC, i.id DESC
),
counted AS (
    SELECT l.facility_id, l.inspection_id, l.date, l.inspection_type,
           COUNT(v.id) AS total,
           COUNT(v.id) FILTER (WHERE v.priority_level = 'P')  AS priority,
           COUNT(v.id) FILTER (WHERE v.priority_level = 'PF') AS priority_foundation,
           COUNT(v.id) FILTER (WHERE v.priority_level = 'C')  AS core
    FROM latest l
    LEFT JOIN inspections_violation v ON v.inspection_id = l.inspection_id
    GROUP BY l.facility_id, l.inspection_id, l.date, l.inspection_type
),
history AS (
    SELECT facility_id, COUNT(*) AS n
    FROM inspections_inspection
    {facility_filter_history}
    GROUP BY facility_id
)
UPDATE inspections_facility f
SET latest_inspection_id        = c.inspection_id,
    latest_inspection_date      = c.date,
    latest_inspection_type      = COALESCE(c.inspection_type, ''),
    latest_violation_total      = COALESCE(c.total, 0),
    latest_priority             = COALESCE(c.priority, 0),
    latest_priority_foundation  = COALESCE(c.priority_foundation, 0),
    latest_core                 = COALESCE(c.core, 0),
    inspection_count            = COALESCE(h.n, 0)
-- Self-join so the LEFT JOINs apply: a facility whose inspections were all
-- deleted has to be reset to nulls and zeros, not merely left alone.
FROM inspections_facility target
LEFT JOIN counted c ON c.facility_id = target.id
LEFT JOIN history h ON h.facility_id = target.id
WHERE f.id = target.id
  {target_filter}
  -- Skip rows already correct: an unchanged facility costs no write, and a
  -- re-run over the whole table reports honestly how much actually moved.
  AND (f.latest_inspection_id       IS DISTINCT FROM c.inspection_id
    OR f.latest_inspection_date     IS DISTINCT FROM c.date
    OR f.latest_inspection_type     IS DISTINCT FROM COALESCE(c.inspection_type, '')
    OR f.latest_violation_total     IS DISTINCT FROM COALESCE(c.total, 0)
    OR f.latest_priority            IS DISTINCT FROM COALESCE(c.priority, 0)
    OR f.latest_priority_foundation IS DISTINCT FROM COALESCE(c.priority_foundation, 0)
    OR f.latest_core                IS DISTINCT FROM COALESCE(c.core, 0)
    OR f.inspection_count           IS DISTINCT FROM COALESCE(h.n, 0))
"""


def refresh(facility_ids=None):
    """Recompute the denormalised columns. Returns the number of rows changed.

    `facility_ids` limits the work to those facilities — what a scrape run
    passes. Omitted, every facility is rebuilt.
    """
    if facility_ids is not None:
        facility_ids = list(facility_ids)
        if not facility_ids:
            return 0
        sql = _SQL.format(
            facility_filter="WHERE i.facility_id = ANY(%s)",
            facility_filter_history="WHERE facility_id = ANY(%s)",
            target_filter="AND target.id = ANY(%s)",
        )
        params = [facility_ids, facility_ids, facility_ids]
    else:
        sql = _SQL.format(facility_filter="", facility_filter_history="", target_filter="")
        params = []

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount
