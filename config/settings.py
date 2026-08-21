import sys
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["*"]),
    MAX_VIDEO_SECONDS=(int, 300),
    STORAGE_BACKEND=(str, "local"),
    EMBEDDING_MODEL=(str, "sentence-transformers/all-MiniLM-L6-v2"),
    EMBEDDING_DIM=(int, 384),
)
environ.Env.read_env(BASE_DIR / ".env")

DEBUG = env("DEBUG")

# the insecure fallback exists for local dev only — refuse to boot with it in production
SECRET_KEY = env("SECRET_KEY", default="dev-insecure-key-change-me" if DEBUG else "")
if not SECRET_KEY:
    raise ImproperlyConfigured("SECRET_KEY must be set when DEBUG=False")

ALLOWED_HOSTS = env("ALLOWED_HOSTS")
if not DEBUG and ALLOWED_HOSTS == ["*"]:
    raise ImproperlyConfigured("Set ALLOWED_HOSTS to real hostnames when DEBUG=False")

CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "sslserver",
    "drf_spectacular",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # must precede CommonMiddleware so preflights get their headers even on a redirect
    "corsheaders.middleware.CorsMiddleware",
    # serves the admin's static files without a separate web server
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "config.middleware.DisableCSRFMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# Only the admin renders HTML; the API is JSON-only.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
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

# --- CORS: the Next.js client is a separate origin ---
CORS_ALLOW_ALL_ORIGINS = env.bool("CORS_ALLOW_ALL_ORIGINS", default=True)
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = ["ngrok-skip-browser-warning", "content-type", "Authorization"]

# session cookie has to survive a cross-site XHR from the client origin
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "None"
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=True)

_LOCAL_DB = "postgres://postgres:postgres@localhost:5433/context_ai"
DATABASES = {"default": env.db("DATABASE_URL", default=_LOCAL_DB)}

# `manage.py test` CREATEs and DROPs a whole database, which has no business happening on
# managed infrastructure — and against a connection pooler the DROP fails anyway, because
# the pooler keeps a session open ("database is being accessed by other users").
if "test" in sys.argv:
    DATABASES = {"default": env.db("TEST_DATABASE_URL", default=_LOCAL_DB)}

# A managed database (Supabase et al) is a TLS round-trip away, so reopening a connection
# per request costs far more than it does against localhost.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

# Supabase's transaction pooler (port 6543) multiplexes connections, so server-side
# prepared statements and cursors leak across sessions and error out. Its session pooler
# (5432) has no such problem — this only kicks in for the transaction one.
if ":6543/" in env("DATABASE_URL", default=""):
    DATABASES["default"].setdefault("OPTIONS", {})["prepare_threshold"] = None
    DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = True

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# uploads stream to a temp file instead of being buffered in memory
DATA_UPLOAD_MAX_MEMORY_SIZE = env.int("DATA_UPLOAD_MAX_MEMORY_SIZE", default=5 * 1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = DATA_UPLOAD_MAX_MEMORY_SIZE

# --- production hardening (all no-ops while DEBUG=True) ---
if not DEBUG:
    # gunicorn sits behind a proxy/tunnel that terminates TLS
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
    SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"

# W003: CsrfViewMiddleware is intentionally absent — DisableCSRFMiddleware replaces it so a
# cross-origin SPA can use the session cookie (the certifier_reloaded_backend pattern).
# Residual risk, accepted knowingly: the session cookie is SameSite=None, and multipart or
# form-encoded POSTs are "simple" requests that a third-party page can send without a
# preflight. Close it by requiring a non-simple header on writes, or by restoring CSRF.
SILENCED_SYSTEM_CHECKS = ["security.W003"]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {"django.request": {"level": "ERROR", "handlers": ["console"], "propagate": False}},
}

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_AUTHENTICATION_CLASSES": ["core.permissions.SessionAuthentication"],
    # authenticated by default; a view opts out with `authentication = False`
    "DEFAULT_PERMISSION_CLASSES": ["core.permissions.APIAuthenticationPermission"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Context-Aware Ad Recommender",
    "DESCRIPTION": (
        "Ingests a video, understands each scene (visuals, speech, tone) and recommends "
        "which ad to place at which timestamp, with a rationale and a brand-safety flag."
    ),
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,  # the schema endpoint itself is not an API operation
    "SCHEMA_PATH_PREFIX": "",
    "ENUM_NAME_OVERRIDES": {"VideoStatusEnum": "core.models.Video.Status"},
}

# --- storage: local filesystem by default, Cloudflare R2 when STORAGE_BACKEND=r2 ---
STORAGE_BACKEND = env("STORAGE_BACKEND")
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))
MEDIA_URL = "/media/"

if STORAGE_BACKEND == "r2":
    _default_storage = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": env("R2_BUCKET"),
            "access_key": env("R2_ACCESS_KEY_ID"),
            "secret_key": env("R2_SECRET_ACCESS_KEY"),
            "endpoint_url": env("R2_ENDPOINT_URL"),
            "region_name": "auto",
            "default_acl": None,  # R2 has no ACLs
            "querystring_auth": True,  # keyframe URLs are presigned
            "querystring_expire": env.int("R2_URL_EXPIRE", default=3600),
        },
    }
else:
    _default_storage = {"BACKEND": "django.core.files.storage.FileSystemStorage"}

STORAGES = {
    "default": _default_storage,
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# --- celery ---
REDIS_URL = env("REDIS_URL", default="redis://localhost:6380/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_TRACK_STARTED = True
# macOS aborts (SIGABRT) when a forked child loads torch — the Objective-C and OpenMP
# runtimes are not fork-safe. Threads also mean one shared embedding model instead of
# one copy per worker process. Linux keeps the default prefork pool.
CELERY_WORKER_POOL = env("CELERY_WORKER_POOL", default="threads" if sys.platform == "darwin" else "prefork")
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# --- sample video: Meridian, Netflix Open Content, CC BY 4.0 (~850 MB, 4K HDR) ---
SAMPLE_VIDEO_URL = env(
    "SAMPLE_VIDEO_URL",
    default="http://download.opencontent.netflix.com.s3.amazonaws.com/Meridian/Meridian_UHD4k5994_HDR_P3PQ.mp4",
)
SAMPLE_DIR = env("SAMPLE_DIR", default=str(BASE_DIR / ".samples"))

# --- gemini + matching ---
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
# flash-lite: cheapest of the Flash family and the one with a usable free-tier quota.
# `gemini-flash-latest` is better at vision but its free tier is 20 requests/day.
GEMINI_MODEL = env("GEMINI_MODEL", default="gemini-flash-lite-latest")
# Two Gemini calls per scene, against a free-tier cap of 15 requests/minute. 4/m = 8
# first-attempt requests, leaving headroom for retries — which spend the same budget,
# so a tighter rate cascades into 429s. Raise it on a paid key; it is the main brake
# on a long video.
SCENE_ANALYSIS_RATE = env("SCENE_ANALYSIS_RATE", default="4/m")
GEMINI_RETRIES = env.int("GEMINI_RETRIES", default=5)
# a scene that still 429s after the SDK's own backoff is re-queued this many times
SCENE_RETRIES = env.int("SCENE_RETRIES", default=3)
TOP_K_ADS = env.int("TOP_K_ADS", default=3)
AD_CANDIDATES = env.int("AD_CANDIDATES", default=50)  # pulled by vector distance, then re-ranked
IAB_BOOST = env.float("IAB_BOOST", default=0.15)  # added per shared IAB category

# --- pipeline knobs (used from Phase 3 on) ---
MAX_VIDEO_SECONDS = env("MAX_VIDEO_SECONDS")
# cached video pulls older than this are swept at the start of each run
TMP_CACHE_HOURS = env.float("TMP_CACHE_HOURS", default=6.0)
WHISPER_MODEL = env("WHISPER_MODEL", default="base")
# ContentDetector sensitivity: lower cuts more. Worth tuning per source — 4K HDR
# grades and film grain both shift what counts as a cut.
SCENE_THRESHOLD = env.float("SCENE_THRESHOLD", default=27.0)
EMBEDDING_MODEL = env("EMBEDDING_MODEL")
EMBEDDING_DEVICE = env("EMBEDDING_DEVICE", default="cpu")
EMBEDDING_DIM = env("EMBEDDING_DIM")
