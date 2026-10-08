import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "pipeline",
    "clinical",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"timeout": 30},
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

FHIR_BASE_URL = os.environ.get("FHIR_BASE_URL", "https://hapi.fhir.org/baseR4")
EXTRACT_STRATEGY = "export"
EXPORT_RESOURCE_TYPES = ["Patient", "Observation"]
EXPORT_WORKERS = int(os.environ.get("EXPORT_WORKERS", "3"))
EXPORT_POLL_MAX_WAIT = int(os.environ.get("EXPORT_POLL_MAX_WAIT", "10"))
HTTP_TIMEOUT_SECONDS = 20
RETRY_MAX_ATTEMPTS = 5
RETRY_MAX_BACKOFF_SECONDS = 30
CIRCUIT_BREAKER_THRESHOLD = 10
FILE_MAX_RUNS = 3
TRANSFORM_BATCH_SIZE = 1000
MAPPING_VERSION = "2"

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "config.logging.JsonFormatter"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "json"},
        "file": {
            "class": "logging.FileHandler",
            "filename": LOG_DIR / "migration.log",
            "formatter": "json",
        },
    },
    "loggers": {
        "pipeline": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
    },
}

if "test" in sys.argv[1:2]:
    LOGGING["loggers"]["pipeline"] = {"handlers": [], "level": "CRITICAL", "propagate": False}
