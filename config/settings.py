"""Django settings for the Arkansas health inspections project."""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes", "on")


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "insecure-dev-key")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h for h in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.gis",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_q",
    "inspections",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.contrib.gis.db.backends.postgis",
        "NAME": os.getenv("DB_NAME", "health_inspections"),
        "USER": os.getenv("DB_USER", ""),
        "PASSWORD": os.getenv("DB_PASSWORD", ""),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": os.getenv("DB_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Chicago"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# Overridable because the checkout lives under a home directory nginx cannot
# traverse in production; collected assets go somewhere www-data can read.
STATIC_ROOT = Path(os.getenv("DJANGO_STATIC_ROOT") or BASE_DIR / "staticfiles")

# Inspection report PDFs are ingested content, not project assets, so they live
# under MEDIA_ROOT. Swap in django-storages (S3/R2) later without a model change.
MEDIA_URL = "media/"
MEDIA_ROOT = Path(os.getenv("DJANGO_MEDIA_ROOT") or BASE_DIR / "media")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Rendered embeds are cached, and the cache must be shared across processes:
# the default LocMemCache gives every worker its own copy, so embeds would serve
# inconsistently and could not be invalidated. Postgres is already here and the
# volume is tiny (one row per embed), so use the database rather than adding Redis.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache",
        "TIMEOUT": 900,
        "OPTIONS": {"MAX_ENTRIES": 5000},
    }
}

# django-q2 using the Postgres database as its broker: no Redis, no extra service.
Q_CLUSTER = {
    "name": "health_inspections",
    "workers": 2,
    "timeout": 60 * 55,
    "retry": 60 * 60,
    "queue_limit": 50,
    "bulk": 1,
    "orm": "default",
    "save_limit": 250,
    "catch_up": False,
}

# GeoDjango needs the GEOS/GDAL shared libraries. Django finds them on its own on
# most Linux installs; Homebrew on Apple Silicon puts them somewhere ctypes does
# not search, so point at them when they're there. Override via env in production.
for _var, _default in (
    ("GEOS_LIBRARY_PATH", "/opt/homebrew/lib/libgeos_c.dylib"),
    ("GDAL_LIBRARY_PATH", "/opt/homebrew/lib/libgdal.dylib"),
):
    _path = os.getenv(_var) or _default
    if Path(_path).exists():
        globals()[_var] = _path

# Geocoding. The Arkansas GIS Office's statewide composite locator is the primary
# source: it returns actual address points, covers addresses the Census TIGER
# ranges lack, and is already WGS84. Census is the fallback.
#
# The ADH search page can emit its own coordinates, but they were measured
# against the state locator and found unreliable — a third were >300m off and one
# was 129km from its address — so they are no longer used. See README.
GEOCODING_ENABLED = env_bool("GEOCODING_ENABLED", True)

ARKANSAS_GIS_GEOCODER_URL = (
    "https://gis.arkansas.gov/arcgis/rest/services/Locator/"
    "ASDI_Composite_Locator/GeocodeServer/findAddressCandidates"
)
ARKANSAS_GIS_TIMEOUT = float(os.getenv("ARKANSAS_GIS_TIMEOUT", "30"))
ARKANSAS_GIS_DELAY = float(os.getenv("ARKANSAS_GIS_DELAY", "0.25"))
# Esri match types precise enough to map. Anything else — Locality (a city
# centroid), StreetName (a whole street), Postal — is rejected: a pin in the
# wrong place is worse than no pin.
ARKANSAS_GIS_ACCEPTED_TYPES = {
    "PointAddress", "Subaddress", "BuildingName", "StreetAddress",
    "StreetAddressExt", "POI",
}
ARKANSAS_GIS_MIN_SCORE = float(os.getenv("ARKANSAS_GIS_MIN_SCORE", "80"))
CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
CENSUS_GEOCODER_BENCHMARK = os.getenv("CENSUS_GEOCODER_BENCHMARK", "Public_AR_Current")
CENSUS_GEOCODER_TIMEOUT = float(os.getenv("CENSUS_GEOCODER_TIMEOUT", "30"))
CENSUS_GEOCODER_DELAY = float(os.getenv("CENSUS_GEOCODER_DELAY", "0.5"))
# Give up after this many failed lookups; those addresses need a human.
MAX_GEOCODE_ATTEMPTS = int(os.getenv("MAX_GEOCODE_ATTEMPTS", "3"))

# The report PDF is a strict superset of the website's observations overlay, so
# the overlay is now only a safety net for inspections with no report, plus the
# source of the short code_explanation label. Turning this off halves the
# requests made to the state's server per inspection.
SCRAPE_WEB_OBSERVATIONS = env_bool("SCRAPE_WEB_OBSERVATIONS", True)

# Optional AI summarisation of an export, on the Output data screen. The token
# stays server-side: the browser posts to our own endpoint, which rebuilds the
# export and calls OpenAI, so the key is never shipped to a reader's browser.
# The model is configurable because the sensible choice moves faster than this
# codebase does; anything that speaks the chat completions API will work.
OPENAI_TOKEN = os.getenv("OPEN_AI_TOKEN", "")
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
# Per request. Batching keeps each one small, so this is a ceiling rather than
# something a normal run approaches.
OPENAI_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "120"))
# Establishments per request. The document is cut on block boundaries we wrote,
# so batching costs nothing structurally — it just keeps each call short enough
# to finish and small enough not to be truncated.
OPENAI_BATCH_SIZE = int(os.getenv("OPENAI_BATCH_SIZE", "10"))
# Batches in flight at once. Low on purpose: the gain over serial is most of the
# way there by four, and beyond that a wide range starts tripping rate limits.
OPENAI_MAX_PARALLEL = int(os.getenv("OPENAI_MAX_PARALLEL", "4"))

# Scraper behaviour.
SCRAPER_DELAY_SECONDS = float(os.getenv("SCRAPER_DELAY_SECONDS", "1.5"))
SCRAPER_USER_AGENT = os.getenv(
    "SCRAPER_USER_AGENT", "health-inspections-bot/0.1 (public records research)"
)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "{levelname} {asctime} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "loggers": {
        "inspections": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}


# Production hardening. nginx terminates TLS and proxies over a unix socket, so
# Django only learns the original scheme from X-Forwarded-Proto — without this
# is_secure() is always False and the redirect below would loop forever.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # nginx already 301s :80 to :443, so SECURE_SSL_REDIRECT would only duplicate
    # that — and it turns every test-client request into a 301.
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    SECURE_HSTS_PRELOAD = False
    CSRF_TRUSTED_ORIGINS = [f"https://{h}" for h in ALLOWED_HOSTS if not h.startswith(".")]
