from pathlib import Path

import environ

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

SECRET_KEY = env("SECRET_KEY", default="dev-insecure-key-change-me")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "core",
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

DATABASES = {"default": env.db("DATABASE_URL", default="postgres://postgres:postgres@localhost:5433/context_ai")}

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

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
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
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# --- celery ---
REDIS_URL = env("REDIS_URL", default="redis://localhost:6380/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_TRACK_STARTED = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# --- sample video: Meridian, Netflix Open Content, CC BY 4.0 (~850 MB, 4K HDR) ---
SAMPLE_VIDEO_URL = env(
    "SAMPLE_VIDEO_URL",
    default="http://download.opencontent.netflix.com.s3.amazonaws.com/Meridian/Meridian_UHD4k5994_HDR_P3PQ.mp4",
)
SAMPLE_DIR = env("SAMPLE_DIR", default=str(BASE_DIR / ".samples"))

# --- pipeline knobs (used from Phase 3 on) ---
MAX_VIDEO_SECONDS = env("MAX_VIDEO_SECONDS")
EMBEDDING_MODEL = env("EMBEDDING_MODEL")
EMBEDDING_DIM = env("EMBEDDING_DIM")
