from pathlib import Path

from environs import Env

env = Env()
BASE_DIR = Path(__file__).resolve().parent.parent.parent
env.read_env(BASE_DIR / ".env", recurse=False)

SECRET_KEY = env.str("SECRET_KEY", "dev-insecure-key")
DEBUG = env.bool("DEBUG", False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", ["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "simple_history",
    "django_htmx",
    "apps.core",
    "apps.catalog",
    "apps.ingest",
    "apps.recon",
    "apps.dashboard",
    "apps.api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.dashboard.context_processors.last_book_date",
            ],
        },
    },
]

DATABASES = {"default": env.dj_db_url("DATABASE_URL", "sqlite:///" + str(BASE_DIR / "db.sqlite3"))}
DATABASES["default"].setdefault("CONN_MAX_AGE", 60)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "id"
TIME_ZONE = "Asia/Jakarta"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "apps" / "dashboard" / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "day"
LOGOUT_REDIRECT_URL = "login"

# --- Domain settings ---
# Toleransi selisih nominal saat mencocokkan (rupiah). 0 = harus persis.
MATCH_AMOUNT_TOLERANCE = 0
# Ambang skor rapidfuzz (0-100) untuk kandidat AUTO_FUZZY.
MATCH_FUZZY_THRESHOLD = 82
# Umur (hari) sebuah discrepancy OPEN sebelum jadi alarm merah.
DISCREPANCY_ALARM_DAYS = 2
# Tanggal mulai siklus "bulan bisnis" (1-31). 1 = kalender biasa. Contoh: 29 -> siklus
# tgl 29 bulan lalu s/d 28 bulan ini. Dipakai utk expand alarm SLA per periode berjalan.
BUSINESS_MONTH_START_DAY = env.int("BUSINESS_MONTH_START_DAY", 1)

# Kunci API untuk proteksi endpoint REST API /api/*
API_KEY = env.str("API_KEY", "")

