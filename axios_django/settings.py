from pathlib import Path
import os


BASE_DIR = Path(__file__).resolve().parent.parent


def load_env_file(path: Path):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env_key = key.strip()
        env_value = value.strip().strip('"').strip("'")
        if not os.environ.get(env_key):
            os.environ[env_key] = env_value


load_env_file(BASE_DIR / ".env")
load_env_file(BASE_DIR.parent / ".env")

SECRET_KEY = "axios-django-api-local-secret"
DEBUG = True
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "axios_django.urls"
WSGI_APPLICATION = "axios_django.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "api",
]

MIDDLEWARE = [
    "api.middleware.CorsAndOriginMiddleware",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "api.sqlite3",
        "OPTIONS": {"timeout": 20},
    }
}

USE_TZ = True
TIME_ZONE = "UTC"
