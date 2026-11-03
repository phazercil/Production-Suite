"""Production Suite settings.

Everything plant-specific comes from environment variables or a `.env` file next to manage.py
(see `.env.example`). With no `.env`, the suite runs on a local SQLite file with demo data.
"""
from . import version as _version
import os
from pathlib import Path

# MariaDB driver: mysqlclient when installed, otherwise PyMySQL (pure Python; what the Windows installer ships).
try:
    import MySQLdb  # noqa: F401
except ImportError:
    try:
        import pymysql
        pymysql.install_as_MySQLdb()
    except ImportError:
        pass

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env(path):
    """Minimal .env reader: KEY=value lines, # comments, optional quotes. Real env vars win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():  # -sig: Windows editors may add a BOM
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        os.environ.setdefault(key, val)


_load_env(BASE_DIR / ".env")


def env(key, default=""):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    v = os.environ.get(key)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-change-me-in-.env")
# Debug pages are on only for a demo with no .env at all; any real install (with a .env) defaults to off.
DEBUG = env_bool("DJANGO_DEBUG", not (BASE_DIR / ".env").exists())
if not DEBUG and SECRET_KEY.startswith("dev-only"):
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY in .env before running with DJANGO_DEBUG=0.")
ALLOWED_HOSTS = [h.strip() for h in env("ALLOWED_HOSTS", "*").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [h.strip() for h in env("CSRF_TRUSTED_ORIGINS", "").split(",") if h.strip()]
# Linux install: the suite sits behind Apache on this server. Apache tells it the visitor's address
# (X-Forwarded-For, see plant.middleware.forwarded_client) and whether the page was opened with HTTPS.
BEHIND_PROXY = env("BEHIND_PROXY", "0") == "1"
if BEHIND_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "plant",
    "hmi",
    "scheduler",
    "datakiosk",
    "labor",
    "reports",
    "suite",   # display helpers for the server-built screens (suite/templatetags)
]

MIDDLEWARE = [
    "plant.middleware.PrivateNetworkOnlyMiddleware",   # first: anything not from the private network stops here
    "django.middleware.security.SecurityMiddleware",
    "plant.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "plant.middleware.StationKeyMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
X_FRAME_OPTIONS = "SAMEORIGIN"
# Browsers ignore this header on plain-http plant pages and log a warning on every page; leave it off.
SECURE_CROSS_ORIGIN_OPENER_POLICY = None
try:  # serve /static/ straight from Django under gunicorn/waitress when whitenoise is installed
    import whitenoise  # noqa: F401
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
except ImportError:
    pass

ROOT_URLCONF = "suite.urls"

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
                "plant.context.suite",
            ],
            "builtins": ["suite.templatetags.suite"],
        },
    },
]

WSGI_APPLICATION = "suite.wsgi.application"

# ---------------------------------------------------------------------------
# Databases
#   default: the suite's own records (MariaDB in production, SQLite for a demo)
#   assets:  optional read-only connection to the asset database that holds
#            changeover instructions (queried with saved SQL from Admin › Changeover data)
#   s2k:     optional read-only connection to the S2K database (open MOs, Admin › Connections › S2K)
#   machines: optional read-only connection to the database the machines log their readings to
# ---------------------------------------------------------------------------
if env("DB_ENGINE", "sqlite").lower() in ("mariadb", "mysql"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "HOST": env("DB_HOST", "127.0.0.1"),
            "PORT": env("DB_PORT", "3306"),
            "NAME": env("DB_NAME", "production_suite"),
            "USER": env("DB_USER", "suite"),
            "PASSWORD": env("DB_PASSWORD", ""),
            "OPTIONS": {"charset": "utf8mb4", "init_command": "SET sql_mode='STRICT_TRANS_TABLES'"},
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / env("SQLITE_PATH", "suite.sqlite3"),
            # IMMEDIATE: each transaction takes the write lock up front, so the minute ticker and a tap on a
            # kiosk can never overwrite each other's changes to the same shift (MariaDB uses row locks instead).
            "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
        }
    }

# Read-only outside databases (see plant/dbconf.py): never written to, never migrated.
from plant.dbconf import read_only_mysql  # noqa: E402

for _alias, _prefix in (("assets", "ASSET_DB"), ("s2k", "S2K_DB"), ("machines", "MACHINE_DB")):
    _db = read_only_mysql(env, _prefix)
    if _db:
        DATABASES[_alias] = _db
DATABASE_ROUTERS = ["plant.dbconf.SuiteRouter"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Kept in the database so every server process shares it (used for the wrong-PIN limit).
CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "suite_cache"}}

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("PLANT_TZ", "America/New_York")
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = Path(env("STATIC_ROOT", str(BASE_DIR / "staticfiles")))  # Linux: /var/www/production-suite/static, served by Apache

# Kiosk sessions: an operator stays logged in on a screen until they log out;
# shift lead (admin) mode times out after the idle minutes set in Admin › Rules.
SESSION_COOKIE_AGE = 60 * 60 * 24 * 30
SESSION_SAVE_EVERY_REQUEST = False

# ---------------------------------------------------------------------------
# Plant integrations
# ---------------------------------------------------------------------------
SUITE = {
    "STATION_KEY": env("STATION_KEY"),            # if set, every screen must present it once (?key=...)
    "FEED_KEY": env("FEED_KEY"),                  # header X-Feed-Key for machines pushing readings
    "PAYCOM_SID": env("PAYCOM_SID"),
    "PAYCOM_TOKEN": env("PAYCOM_TOKEN"),
    "PAYCOM_URL": env("PAYCOM_URL", "https://api.paycomonline.net/v4/rest/index.php/api/v1/employeedirectory"),
    "PAYCOM_FIELD_DEPT": env("PAYCOM_FIELD_DEPT"),
    "PAYCOM_FIELD_POSITION": env("PAYCOM_FIELD_POSITION"),
    "PAYCOM_FIELD_BADGE": env("PAYCOM_FIELD_BADGE"),
    # Downtime alerts (Admin > Alerts): mail server, and Twilio for text messages
    **{k: env(k) for k in ("ALERT_SMTP_HOST", "ALERT_SMTP_PORT", "ALERT_SMTP_SECURITY", "ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD",
                           "ALERT_FROM", "ALERT_SMTP_INTERNET", "TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "GRAFANA_TOKEN",
                           "INFLUX_URL", "INFLUX_VERSION", "INFLUX_ORG", "INFLUX_TOKEN", "INFLUX_USER", "INFLUX_DB", "INFLUX_WRITE_BUCKET")},
    # Run the per-minute machine ticker inside `runserver` (handy for demos). In production
    # run `python manage.py runplant` as its own service and set this to 0.
    "TICKER_IN_PROCESS": env_bool("TICKER_IN_PROCESS", True),
    # Changing the schedule needs a shift lead PIN (same people and PINs as the Operator Kiosk). Set 0 to let anyone edit.
    "SCHEDULER_REQUIRE_LEAD": env_bool("SCHEDULER_REQUIRE_LEAD", True),
    "VERSION": _version.VERSION,
    "REVISION": _version.REVISION,
    "BEHIND_PROXY": BEHIND_PROXY,
}

# Logs go to the console; an installed server (LOG_DIR set by serve.py) also keeps rotating files:
# logs/web.log and logs/ticker.log, 5 files of 5 MB each, so the disk never fills.
_handlers = {}
if not env("LOG_DIR") or env_bool("LOG_CONSOLE", False):
    _handlers["console"] = {"class": "logging.StreamHandler", "formatter": "plain"}
if env("LOG_DIR"):
    Path(env("LOG_DIR")).mkdir(parents=True, exist_ok=True)
    _handlers["file"] = {"class": "logging.handlers.RotatingFileHandler", "formatter": "plain", "encoding": "utf-8",
                         "filename": str(Path(env("LOG_DIR")) / f"{env('SUITE_LOG_NAME', 'suite')}.log"),
                         "maxBytes": 5 * 1024 * 1024, "backupCount": 5}
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": _handlers,
    "loggers": {
        "plant": {"handlers": list(_handlers), "level": env("LOG_LEVEL", "INFO"), "propagate": False},
        "django.request": {"handlers": list(_handlers), "level": "ERROR", "propagate": False},
        "waitress": {"handlers": list(_handlers), "level": "INFO", "propagate": False},
    },
}
