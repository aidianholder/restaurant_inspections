"""Static files storage.

Hashed filenames are what make a deploy invalidate its own caches: `/static/` is
served with `expires 30d`, so without them a corrected `dashboard.js` sits in the
CDN and in readers' browsers for a month and the only remedy is a manual purge.
"""

from django.contrib.staticfiles.storage import ManifestStaticFilesStorage


class ForgivingManifestStaticFilesStorage(ManifestStaticFilesStorage):
    """Hashed names, but a missing manifest entry is recomputed, not fatal.

    Strict mode turns one absent manifest entry into a 500 on every page that
    references the asset. Without it, Django falls back to hashing the file on
    disk, so a deploy whose manifest is stale or missing keeps serving correct,
    cache-busted URLs instead of failing outright — worth having in a project
    that has already shipped without running `collectstatic`.

    This does not rescue a checkout where the files were never collected at all:
    there is nothing on disk to hash, and `static()` still raises. Local
    development should set `DJANGO_DEBUG=True`, which selects plain
    `StaticFilesStorage` — see Setup in the README.
    """

    manifest_strict = False
