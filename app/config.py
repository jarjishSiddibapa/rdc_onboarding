import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── Secret key ───────────────────────────────────────────────────────────────
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-CHANGE-IN-PROD")

    # ── Database ────────────────────────────────────────────────────────────────
    _db_url = os.environ.get("DATABASE_URL")
    if not _db_url:
        raise ValueError(
            "DATABASE_URL environment variable is not set. "
            "Set it in your .env file before starting the application."
        )
    SQLALCHEMY_DATABASE_URI = _db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # pool_size/max_overflow/pool_timeout are QueuePool-only kwargs — SQLite
    # (used by the test suite, sqlite:///:memory:) uses StaticPool instead
    # and create_engine() rejects them outright. Only apply the full MySQL
    # pool tuning when actually pointed at MySQL.
    if _db_url.startswith("mysql"):
        SQLALCHEMY_ENGINE_OPTIONS = {
            "pool_pre_ping":   True,     # test connection before use (prevents stale-connection crashes)
            "pool_recycle":    1800,     # recycle connections after 30 min (avoids MySQL wait_timeout)
            "pool_size":       10,
            "max_overflow":    20,
            "pool_timeout":    30,
        }
    else:
        SQLALCHEMY_ENGINE_OPTIONS = {}
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max upload

    # ── Session & cookie security ────────────────────────────────────────────────
    # Sessions expire after 10 minutes of inactivity
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=10)
    SESSION_COOKIE_HTTPONLY = True        # JS cannot read the session cookie
    SESSION_COOKIE_SAMESITE = "Lax"      # CSRF mitigation for cross-site requests
    SESSION_COOKIE_SECURE = False         # Set True when served over HTTPS
    SESSION_COOKIE_NAME = "rdc_session"  # Non-default name (obscures framework)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_DURATION = timedelta(days=7)

    # ── CSRF (Flask-WTF) ─────────────────────────────────────────────────────────
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 3600           # Token valid for 1 hour

    # ── Rate limiting (Flask-Limiter) ────────────────────────────────────────────
    RATELIMIT_DEFAULT = "5000 per day;1000 per hour"
    RATELIMIT_STORAGE_URI = "memory://"

    # ── Password reset ───────────────────────────────────────────────────────────
    PASSWORD_RESET_SALT = os.environ.get("PASSWORD_RESET_SALT", "rdc-password-reset-salt")
    PASSWORD_RESET_MAX_AGE = 1800        # 30 minutes

    # ── Mail ────────────────────────────────────────────────────────────────────
    MAIL_SERVER = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
    MAIL_PORT = int(os.environ.get("EMAIL_PORT", 587))
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.environ.get("EMAIL_USER")
    MAIL_PASSWORD = os.environ.get("EMAIL_PASS")
    MAIL_DEFAULT_SENDER = os.environ.get("EMAIL_FROM")

    # ── Uploads ─────────────────────────────────────────────────────────────────
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")
    ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "jpg", "jpeg", "png"}

    # ── ZingHR integration ──────────────────────────────────────────────────────
    # Unused by any reachable code path — app/integrations/zinghr.py reads
    # these same env vars directly via os.environ, not through this Config
    # class. Left here only as documentation of what's configurable; not
    # dead-code-removed to avoid touching something else might depend on.
    ZINGHR_CLIENT_ID = os.environ.get("ZINGHR_CLIENT_ID", "")
    ZINGHR_CLIENT_SECRET = os.environ.get("ZINGHR_CLIENT_SECRET", "")

    # ── Daily Volume Tracker integration ────────────────────────────────────────
    # Same unused-duplicate note as ZINGHR_* above. DVT_BASE_URL's fallback
    # (an internal network address) was removed 2026-09-15 as part of
    # setting this project up as a git repo — real value lives in .env only.
    DVT_BASE_URL = os.environ.get("DVT_BASE_URL", "")
    DVT_USERNAME = os.environ.get("DVT_USERNAME", "")
    DVT_PASSWORD = os.environ.get("DVT_PASSWORD", "")


class DevelopmentConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = False


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True          # Requires HTTPS in production
    WTF_CSRF_SSL_STRICT = True


def get_config():
    env = os.environ.get("FLASK_ENV", "development").lower()
    if env == "production":
        return ProductionConfig
    return DevelopmentConfig
