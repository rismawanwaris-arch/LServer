from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import SECRET_KEY, env

# SECRET_KEY jatuh ke "dev-insecure-key" (base.py) kalau env var kosong, dan
# .env.example menaruh placeholder di bawah ini secara terbuka di repo publik --
# kalau operator lupa menggantinya, server jalan pakai key yang siapa pun bisa lihat
# di GitHub, cukup untuk forge session/CSRF cookie. Tolak start daripada diam-diam
# menjalankan aplikasi finansial dengan key yang sudah bocor duluan.
#
# Dua lapis, karena satu saja tidak cukup:
# 1. Blocklist eksak -- menangkap placeholder resmi (.env.example, default base.py)
#    walau kebetulan panjang (placeholder Indonesia di .env.example 45 karakter,
#    lolos cek panjang di bawah kalau berdiri sendiri).
# 2. Ambang PANJANG minimum -- menangkap placeholder custom yang orang ketik sendiri
#    (mis. "dev-only-change-me", "changeme") yang mustahil ditebak semua lewat
#    blocklist, tapi hampir selalu jauh lebih pendek dari key acak asli (Django
#    `get_random_secret_key()` = 50 karakter).
_KNOWN_PLACEHOLDER_SECRET_KEYS = {"", "dev-insecure-key", "ganti-dengan-string-acak-panjang-rahasia-anda"}
_MIN_SECRET_KEY_LENGTH = 32
if SECRET_KEY in _KNOWN_PLACEHOLDER_SECRET_KEYS or len(SECRET_KEY) < _MIN_SECRET_KEY_LENGTH:
    raise ImproperlyConfigured(
        f"SECRET_KEY belum diisi dengan nilai rahasia asli (masih placeholder, atau panjangnya "
        f"cuma {len(SECRET_KEY)} karakter -- minimal {_MIN_SECRET_KEY_LENGTH}). Set SECRET_KEY di "
        ".env dengan string acak panjang, misalnya hasil dari: "
        'python -c "import secrets; print(secrets.token_urlsafe(50))"'
    )

DEBUG = False
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", ["*"])
CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    ["http://localhost:3300", "http://127.0.0.1:3300", "http://localhost:8000", "http://127.0.0.1:8000"],
)

SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", False)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
