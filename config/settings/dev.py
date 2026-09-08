from .base import *  # noqa: F401,F403
from .base import env

DEBUG = env.bool("DEBUG", True)
INTERNAL_IPS = ["127.0.0.1"]
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Dev: jangan pakai manifest storage (butuh collectstatic + hash).
STORAGES["staticfiles"]["BACKEND"] = "django.contrib.staticfiles.storage.StaticFilesStorage"  # noqa: F405
