"""Application configuration.

The app is database-agnostic via SQLAlchemy. By default it uses a local SQLite
file so the project runs with zero configuration. To use PostgreSQL (the
"real-world" deployment target), set the ``DATABASE_URL`` environment variable,
e.g.::

    DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/swiftroute

See ``.env.example`` and ``docker-compose.yml`` for a ready-to-use Postgres setup.
"""
import os
from datetime import timedelta

from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(__file__))

# Load environment variables from the PROJECT-LOCAL .env only (no upward search),
# so the project stays self-contained and reproducible regardless of any global
# .env files elsewhere on the machine. Every entry point (run.py, seed.py, flask
# CLI, pytest) therefore sees identical configuration.
load_dotenv(os.path.join(basedir, ".env"))


def _normalize_db_url(url: str) -> str:
    """Normalise a Postgres URL to the psycopg3 SQLAlchemy dialect.

    Managed providers (Neon, Heroku, Render, …) hand out ``postgres://`` or
    ``postgresql://`` URLs. SQLAlchemy defaults those to psycopg2; we pin them to
    psycopg3 (``postgresql+psycopg://``) which has first-class Python 3.14 wheels.
    """
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _env_bool(name: str, default: str = "0") -> bool:
    """Parse a boolean environment variable tolerantly.

    Accepts ``1``, ``true``, ``yes`` and ``on`` (any case) as true. Deployment
    dashboards (Railway, Render, …) make people type ``true`` naturally, so this
    avoids the trap where ``AUTO_INIT_DB=true`` silently evaluates to false and
    the database tables are never created.
    """
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me-in-production")

    # Database -------------------------------------------------------------
    _raw_db_url = os.environ.get("DATABASE_URL")
    if _raw_db_url:
        SQLALCHEMY_DATABASE_URI = _normalize_db_url(_raw_db_url)
    else:
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.path.join(basedir, "instance", "swiftroute.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Sessions / security --------------------------------------------------
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    REMEMBER_COOKIE_DURATION = timedelta(days=7)
    WTF_CSRF_TIME_LIMIT = None
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # File uploads (proof of delivery) ------------------------------------
    UPLOAD_FOLDER = os.path.join(basedir, "app", "static", "uploads")
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8 MB
    ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}

    # Routing engine -------------------------------------------------------
    # Center of the service area (Maadi, Cairo) used to download the street graph.
    SERVICE_CENTER_LAT = float(os.environ.get("SERVICE_CENTER_LAT", "29.9602"))
    SERVICE_CENTER_LON = float(os.environ.get("SERVICE_CENTER_LON", "31.2569"))
    SERVICE_RADIUS_M = int(os.environ.get("SERVICE_RADIUS_M", "5000"))
    # When false, the optimizer skips the heavy OSMnx street-graph download and
    # uses the fast pure-Python fallback (straight-line geometry).
    ENABLE_STREET_ROUTING = _env_bool("ENABLE_STREET_ROUTING")

    # Street-following geometry + traffic ---------------------------------
    # Provider for drawing routes that follow real roads:
    #   "osrm"     -> call a public OSRM server (default; no API key, cached).
    #   "osmnx"    -> use a locally downloaded street graph (heavy, optional).
    #   "straight" -> straight lines only (offline-safe fallback).
    # The optimizer always degrades gracefully to straight lines if the chosen
    # provider is unreachable, so the system never fails.
    ROUTING_PROVIDER = os.environ.get("ROUTING_PROVIDER", "osrm")
    OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
    ROUTING_TIMEOUT = float(os.environ.get("ROUTING_TIMEOUT", "6"))

    # Deployment bootstrap -------------------------------------------------
    # On platforms like Render/Railway there is no shell step to create tables.
    # Set AUTO_INIT_DB=1 to create any missing tables on startup, and SEED_DEMO=1
    # to load demo data the first time (only when the database is empty).
    AUTO_INIT_DB = _env_bool("AUTO_INIT_DB")
    SEED_DEMO = _env_bool("SEED_DEMO")


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    # Never hit the network during tests; draw straight lines.
    ROUTING_PROVIDER = "straight"


config_by_name = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}
