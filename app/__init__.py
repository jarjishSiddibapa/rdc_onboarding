import os
import uuid
from datetime import datetime, timedelta
from flask import Flask, session, redirect, url_for, request
from flask_login import current_user, logout_user
from werkzeug.middleware.proxy_fix import ProxyFix
from .config import get_config
from .extensions import db, login_manager, bcrypt, mail, csrf, limiter

# Columns that must exist but may be missing on older installs
# Format: (table_name, column_name, column_definition)
_REQUIRED_COLUMNS = [
    ("plant_locations",     "is_deleted",              "TINYINT(1) NOT NULL DEFAULT 0"),
    ("designations",        "is_deleted",              "TINYINT(1) NOT NULL DEFAULT 0"),
    ("form_fields",         "is_deleted",              "TINYINT(1) NOT NULL DEFAULT 0"),
    ("onboarding_requests", "is_deleted",              "TINYINT(1) NOT NULL DEFAULT 0"),
    ("users",               "profile_pic",             "VARCHAR(500) NULL"),
    ("users",               "business_head_id",        "INT NULL"),
    ("users",               "username",                "VARCHAR(60) NULL"),
    ("form_fields",         "input_pattern",           "VARCHAR(20) NULL"),
    ("form_fields",         "min_length",              "INT NULL"),
    ("form_fields",         "max_length",              "INT NULL"),
    ("users",               "failed_login_attempts",   "INT NOT NULL DEFAULT 0"),
    ("users",               "locked_until",            "DATETIME NULL"),
    ("onboarding_requests", "public_token",            "VARCHAR(32) NULL"),
    ("onboarding_requests", "truein_pushed_at",         "DATETIME NULL"),
    ("onboarding_requests", "truein_push_error",        "TEXT NULL"),
    ("onboarding_requests", "truein_retry_count",       "INT NOT NULL DEFAULT 0"),
    ("onboarding_requests", "truein_last_attempt_at",   "DATETIME NULL"),
    ("onboarding_requests", "truein_retry_stopped",     "TINYINT(1) NOT NULL DEFAULT 0"),
    ("onboarding_requests", "truein_dropped_fields",    "TEXT NULL"),
    ("designations",        "truein_app_attendance",    "TINYINT(1) NOT NULL DEFAULT 0"),
    ("users",               "employee_code",            "VARCHAR(50) NULL"),
    ("designations",        "norm_category_id",         "INT NULL"),
    ("staffing_snapshots",  "allowed_headcount",         "INT NULL"),
    ("staffing_snapshots",  "tier_label",                "VARCHAR(100) NULL"),
    ("staffing_snapshots",  "volume_used",               "FLOAT NULL"),
    ("staffing_snapshots",  "production_volume",         "FLOAT NULL"),
    ("staffing_snapshots",  "can_hire",                  "TINYINT(1) NULL"),
    ("plant_dvt_mappings",  "truein_sub_site",           "VARCHAR(200) NULL"),
    ("norm_role_categories", "is_capacity_gated",         "TINYINT(1) NOT NULL DEFAULT 1"),
    ("plant_dvt_mappings",  "matched_on",                "VARCHAR(20) NULL"),
    ("onboarding_requests", "is_special_case",           "TINYINT(1) NOT NULL DEFAULT 0"),
    ("users",               "region_id",                 "INT NULL"),
    ("users",               "email_mode",                "ENUM('ALL','HIRING_ONLY') NOT NULL DEFAULT 'ALL'"),
    ("users",               "daily_digest_enabled",      "TINYINT(1) NOT NULL DEFAULT 0"),
    ("users",               "last_digest_sent_at",       "DATETIME NULL"),
    ("plant_locations",     "company",                   "VARCHAR(20) NOT NULL DEFAULT 'RDC'"),
    ("employee_location_snapshots", "company",           "VARCHAR(20) NULL"),
    ("onboarding_requests", "candidate_email",           "VARCHAR(200) NULL"),
    ("onboarding_requests", "candidate_govt_id",         "VARCHAR(20) NULL"),
    ("onboarding_requests", "candidate_email_verified",  "VARCHAR(200) NULL"),
]


def _auto_migrate(engine):
    """Add any missing columns to existing tables (safe to call every startup)."""
    if engine.dialect.name != "mysql":
        # This whole function is MySQL-dialect raw SQL (information_schema,
        # DATABASE()) for patching an already-deployed database. On any other
        # backend (e.g. sqlite:///:memory: in tests) db.create_all() already
        # creates every table with every current column — there's nothing to
        # migrate, and running MySQL-only SQL against it would just error.
        return
    with engine.connect() as conn:
        for table, col, definition in _REQUIRED_COLUMNS:
            result = conn.execute(
                db.text(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() "
                    "AND table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": col},
            )
            if result.scalar() == 0:
                conn.execute(db.text(
                    f"ALTER TABLE `{table}` ADD COLUMN `{col}` {definition}"
                ))
                conn.commit()

        # Ensure indexes exist on existing DBs.
        # NOTE (found & fixed 2026-09-21): `CREATE INDEX IF NOT EXISTS` is
        # NOT valid MySQL syntax at all (confirmed live, MySQL 9.5.0 — this
        # was never version-specific) — every attempt in this loop has
        # always thrown a syntax error and been silently swallowed by the
        # try/except below. Any index in this list added to a table that
        # already existed live (i.e. not present when that table's
        # db.create_all() first ran) was NEVER actually created — this
        # mechanism has been dead code for that case since it was written.
        # Fixed the same way _REQUIRED_COLUMNS above checks for an existing
        # column: query information_schema first, only CREATE INDEX (no
        # IF NOT EXISTS) when it's actually missing.
        _INDEXES = [
            ("idx_req_status_deleted", "onboarding_requests", "status, is_deleted"),
            ("idx_req_initiated_by",   "onboarding_requests", "initiated_by"),
            ("idx_req_public_token",   "onboarding_requests", "public_token"),
            ("idx_req_updated_at",     "onboarding_requests", "updated_at"),
            ("idx_user_bh_id",         "users",               "business_head_id"),
            ("idx_user_role_active",   "users",               "role, is_active"),
            ("idx_req_company_code",          "onboarding_requests",        "company_code"),
            ("idx_snapshot_computed_at",       "staffing_snapshots",         "computed_at"),
            ("idx_emp_snapshot_computed_at",   "employee_location_snapshots", "computed_at"),
            ("idx_req_candidate_email",        "onboarding_requests",        "candidate_email"),
            ("idx_req_candidate_govt_id",      "onboarding_requests",        "candidate_govt_id"),
        ]
        for idx_name, tbl, cols in _INDEXES:
            result = conn.execute(
                db.text(
                    "SELECT COUNT(*) FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() "
                    "AND table_name = :t AND index_name = :i"
                ),
                {"t": tbl, "i": idx_name},
            )
            if result.scalar() == 0:
                try:
                    conn.execute(db.text(
                        f"CREATE INDEX `{idx_name}` ON `{tbl}` ({cols})"
                    ))
                    conn.commit()
                except Exception:
                    pass  # best-effort — never block app startup on an index failure

        # Backfill public_token for existing requests that have NULL
        rows = conn.execute(db.text(
            "SELECT id FROM onboarding_requests WHERE public_token IS NULL"
        )).fetchall()
        for row in rows:
            conn.execute(db.text(
                "UPDATE onboarding_requests SET public_token = :tok WHERE id = :rid"
            ), {"tok": uuid.uuid4().hex, "rid": row[0]})
        if rows:
            conn.commit()

        # ── Form-field data migrations (idempotent; skips empty test DBs) ─────
        _ff_count = conn.execute(db.text("SELECT COUNT(*) FROM form_fields")).scalar() or 0
        if _ff_count > 0:
            # 1. Bank details label
            conn.execute(db.text(
                "UPDATE form_fields "
                "SET field_label = 'Bank Details (Statement/ Passbook Front/ Cancelled Cheque)' "
                "WHERE field_key = 'bank_details'"
            ))
            # 2. Designation + notice_period → step 1 (so UAN conditional works same page)
            conn.execute(db.text(
                "UPDATE form_fields SET step = 1 "
                "WHERE field_key IN ('designation', 'notice_period') AND step = 2"
            ))
            # 3. PAN Number field (step 1, after notice_period sort_order=18 → 50)
            if not conn.execute(db.text(
                "SELECT COUNT(*) FROM form_fields WHERE field_key = 'pan_number'"
            )).scalar():
                conn.execute(db.text(
                    "INSERT INTO form_fields "
                    "(field_key, field_label, field_type, step, is_required, is_active, "
                    "is_deleted, is_readonly, allow_other, sort_order, options_source) "
                    "VALUES ('pan_number', 'PAN Number', 'text', 1, 1, 1, 0, 0, 0, 50, 'inline')"
                ))
            # 3b. Deactivate legacy 'pan_no' now that 'pan_number' exists
            conn.execute(db.text(
                "UPDATE form_fields SET is_active = 0 "
                "WHERE field_key = 'pan_no' "
                "AND EXISTS (SELECT 1 FROM (SELECT id FROM form_fields WHERE field_key='pan_number') t)"
            ))
            # 4. Designation before UAN — only run if currently out of order
            _desig_so = conn.execute(db.text(
                "SELECT sort_order FROM form_fields "
                "WHERE field_key='designation' AND step=1 AND is_deleted=0"
            )).scalar()
            _uan_so = conn.execute(db.text(
                "SELECT sort_order FROM form_fields "
                "WHERE field_key='uan_number' AND step=1 AND is_deleted=0"
            )).scalar()
            if _desig_so is not None and _uan_so is not None and int(_desig_so) > int(_uan_so):
                conn.execute(db.text(
                    "UPDATE form_fields SET sort_order = CASE field_key "
                    "  WHEN 'designation'    THEN 13 "
                    "  WHEN 'notice_period'  THEN 14 "
                    "  WHEN 'uan_number'     THEN 16 "
                    "  WHEN 'marital_status' THEN 17 "
                    "  WHEN 'blood_group'    THEN 18 "
                    "END "
                    "WHERE field_key IN "
                    "  ('designation','notice_period','uan_number','marital_status','blood_group') "
                    "  AND step=1"
                ))

            # 5. Replacement employee code (step 2, right after replacement_employee)
            if not conn.execute(db.text(
                "SELECT COUNT(*) FROM form_fields WHERE field_key = 'replacement_employee_code'"
            )).scalar():
                _rs = conn.execute(db.text(
                    "SELECT sort_order FROM form_fields WHERE field_key = 'replacement_employee'"
                )).scalar()
                if _rs:
                    conn.execute(db.text(
                        "UPDATE form_fields SET sort_order = sort_order + 1 "
                        "WHERE step = 2 AND sort_order > :s"
                    ), {"s": _rs})
                conn.execute(db.text(
                    "INSERT INTO form_fields "
                    "(field_key, field_label, field_type, step, is_required, is_active, "
                    "is_deleted, is_readonly, allow_other, sort_order, options_source, help_text) "
                    "VALUES ('replacement_employee_code', 'Replacement Employee Code', 'text', "
                    "2, 0, 1, 0, 0, 0, :so, 'inline', "
                    "'Employee code of the person being replaced. Required if Hiring is Replacement.')"
                ), {"so": (_rs + 1) if _rs else 999})
            conn.commit()


# All datetimes are stored naive-UTC (datetime.utcnow()) throughout this
# app. This app is India-only, so every timestamp shown to a user should
# display in IST — converted at render time only, never at storage time.
_IST_OFFSET = timedelta(hours=5, minutes=30)


def _format_ist(dt, fmt="%d %b %Y, %H:%M"):
    """Jinja filter: naive-UTC datetime -> IST-formatted string. None-safe."""
    if dt is None:
        return "—"
    return (dt + _IST_OFFSET).strftime(fmt)


def _plant_display_name(plant_location_name):
    """
    Jinja filter: raw plant_location_name (the internal join key, e.g.
    "AP-Vizag") -> the proper "ERP-Tracker" display name (see
    PlantDvtMapping.display_name), used consistently everywhere a plant
    name is shown to a user instead of the raw internal key. Falls back to
    the raw value for blank/unmapped names. Cached per-request via flask.g
    so a list page with repeated plant names doesn't re-query per row.
    """
    if not plant_location_name:
        return plant_location_name
    from flask import g
    cache = getattr(g, "_plant_display_name_cache", None)
    if cache is None:
        cache = g._plant_display_name_cache = {}
    if plant_location_name in cache:
        return cache[plant_location_name]
    from .models import PlantDvtMapping
    row = PlantDvtMapping.query.filter_by(plant_location_name=plant_location_name, is_deleted=False).first()
    result = row.display_name if row else plant_location_name
    cache[plant_location_name] = result
    return result


def create_app():
    app = Flask(__name__)
    app.config.from_object(get_config())

    # ── ProxyFix: trust 1 level of reverse-proxy headers so that
    #    request.remote_addr, request.scheme, etc. reflect the real client ───────
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # ── Warn at startup if running with the insecure default secret key ─────────
    _sk = app.config.get("SECRET_KEY", "")
    if _sk in ("dev-secret-key-CHANGE-IN-PROD", "", "change-me"):
        import warnings
        warnings.warn(
            "[SECURITY] SECRET_KEY is using the insecure default value. "
            "Set the SECRET_KEY environment variable before deploying to production.",
            stacklevel=2,
        )

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    bcrypt.init_app(app)
    mail.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    app.jinja_env.filters["ist"] = _format_ist
    app.jinja_env.filters["plant_name"] = _plant_display_name

    # ── Session inactivity timeout ────────────────────────────────────────────
    @app.before_request
    def enforce_session_timeout():
        """Force re-login if user has been idle for PERMANENT_SESSION_LIFETIME."""
        if not current_user.is_authenticated:
            return
        # Exempt the logout route itself to avoid redirect loop
        if request.endpoint in ("auth.logout", "auth.login", "static"):
            return
        last_active = session.get("_last_active")
        now = datetime.utcnow().timestamp()
        lifetime = app.config["PERMANENT_SESSION_LIFETIME"].total_seconds()
        if last_active and (now - last_active) > lifetime:
            logout_user()
            session.clear()
            from flask import flash
            flash("Your session expired due to inactivity. Please sign in again.", "warning")
            return redirect(url_for("auth.login"))
        session["_last_active"] = now
        session.permanent = True

    # ── Security response headers ────────────────────────────────────────────
    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=()"
        )
        # HSTS: browsers remember to use HTTPS for 1 year (only meaningful over HTTPS)
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        # Allow inline styles/scripts needed by templates. No external hosts
        # anywhere — every asset the app loads (Inter font, SortableJS) is
        # vendored under app/static/ and served from 'self' (see CLAUDE.md,
        # "no CDN dependency" — 2026-09-11). fonts.googleapis.com/gstatic.com
        # were removed from here the same day the Inter font was self-hosted;
        # don't add an external host back without vendoring the asset first.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self';"
        )
        return response

    from .auth import auth_bp
    from .main import main_bp
    from .requests_bp import requests_bp
    from .admin import admin_bp
    from .exports import exports_bp
    from .profile import profile_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(requests_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(exports_bp)
    app.register_blueprint(profile_bp)

    # ── Custom error pages ───────────────────────────────────────────────────
    from flask import render_template as _render

    @app.errorhandler(403)
    def forbidden(e):
        return _render("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        return _render("errors/404.html"), 404

    @app.errorhandler(429)
    def rate_limited(e):
        return _render("errors/429.html"), 429

    @app.errorhandler(500)
    def server_error(e):
        return _render("errors/500.html"), 500

    with app.app_context():
        db.create_all()          # create any brand-new tables
        _auto_migrate(db.engine) # add missing columns to existing tables

    # ── Resume Truein retry threads for any ACTIVE requests not yet pushed ────
    from .integrations.truein import resume_pending_retries
    resume_pending_retries(app)

    # ── Start the 2-hourly RDC staffing headcount snapshot refresh ───────────
    from .services.snapshot_refresh import start_snapshot_refresh_thread
    start_snapshot_refresh_thread(app)

    # ── Start the daily digest-email thread (opt-in, see User.daily_digest_enabled) ──
    from .services.digest_email import start_digest_thread
    start_digest_thread(app)

    return app
