"""
Shared pytest fixtures for the RDC Onboarding Portal test suite.

Uses an in-memory SQLite database so tests never touch MySQL.
CSRF is disabled because we test route logic, not form tokenisation.

IMPORTANT: DATABASE_URL is forced to sqlite in-memory via os.environ BEFORE
`app.config` / `create_app` is ever imported. create_app() itself runs
db.create_all()/_auto_migrate() against whatever DATABASE_URL is active at
import time — if that were still the real MySQL URL from .env, and only
overridden afterward via app.config.update(), Flask-SQLAlchemy would have
already cached an engine bound to the real database, and the drop_all() at
session teardown below would drop every table in the real MySQL database
instead of the intended in-memory one. (This happened once — see incident
notes in project memory. Do not remove this env-var override.)
"""
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest
import sqlalchemy as sa
from app import create_app
from app.extensions import db as _db
from app.extensions import bcrypt
from app.extensions import limiter as _limiter
from app.models import User, UserRole


# ── App / DB fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def app():
    """Create a test application instance (session-scoped — created once)."""
    a = create_app()
    a.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        # Config.SQLALCHEMY_ENGINE_OPTIONS hardcodes MySQL QueuePool-only
        # kwargs (pool_size, max_overflow, pool_timeout) which SQLite's
        # StaticPool rejects outright — must be cleared for tests.
        SQLALCHEMY_ENGINE_OPTIONS={},
        WTF_CSRF_ENABLED=False,
        # Disable email so tests don't hit SMTP
        MAIL_USERNAME=None,
        # Disable rate limiting in tests
        RATELIMIT_ENABLED=False,
        SECRET_KEY="test-secret-key",
        PASSWORD_RESET_SALT="test-reset-salt",
        SERVER_NAME="localhost",
    )
    # RATELIMIT_ENABLED=False above has NO effect on its own: create_app()
    # already called limiter.init_app(app), and Flask-Limiter's Limiter
    # reads config.setdefault(ConfigVars.ENABLED, ...) exactly once during
    # init, caching the result on self.enabled — it never re-reads config
    # on later requests. Setting app.config after the fact is a silent
    # no-op. This alone caused a whole test session's worth of login-heavy
    # test files (run together) to start 429-ing on /auth/login's
    # "20 per minute" limit partway through, which cascaded into unrelated
    # later tests failing with 302s (login blocked -> never authenticated
    # -> @login_required redirects). Flip the already-initialized
    # extension's own flag directly instead.
    _limiter.enabled = False
    # Hard safety net: refuse to run destructive create_all/drop_all against
    # anything that isn't the in-memory test database.
    assert a.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:", (
        "Refusing to run tests: SQLALCHEMY_DATABASE_URI is not sqlite in-memory. "
        "This fixture calls db.drop_all() at teardown — running it against a "
        "real database would destroy all data."
    )
    with a.app_context():
        _db.create_all()
        yield a
        _db.drop_all()


@pytest.fixture(scope="function")
def db(app):
    """
    Provide a clean DB transaction that rolls back after each test.

    A plain `_db.session.begin_nested()` + `rollback()` is NOT enough:
    application code (login, approvals, log_audit, ...) calls
    `db.session.commit()` internally, which commits the SAVEPOINT for real
    and leaves nothing for the final rollback() to undo. That silently
    leaked fixture rows (e.g. alice@test.com) across tests, causing
    order-dependent UNIQUE-constraint failures whenever the full suite ran
    together (individual test files passed in isolation, masking this).

    Fix: bind a fresh session to its own connection/transaction that this
    fixture owns directly, and reopen a SAVEPOINT after every app-level
    commit (SQLAlchemy's documented "join a session into an external
    transaction" pattern) so nothing survives past the outer
    transaction.rollback() below, no matter how many times application
    code commits during the test.
    """
    with app.app_context():
        connection = _db.engine.connect()
        transaction = connection.begin()

        session_factory = sa.orm.sessionmaker(bind=connection)
        test_session = sa.orm.scoped_session(session_factory)
        old_session = _db.session
        _db.session = test_session

        nested = connection.begin_nested()

        @sa.event.listens_for(test_session, "after_transaction_end")
        def _restart_savepoint(sess, trans):
            nonlocal nested
            if not nested.is_active:
                nested = connection.begin_nested()

        yield _db

        test_session.remove()
        _db.session = old_session
        transaction.rollback()
        connection.close()


@pytest.fixture(scope="function")
def client(app):
    return app.test_client()


# ── User factory helpers ───────────────────────────────────────────────────────

def _make_user(name, email, role, db_session, password="Test1234", companies=None):
    """
    companies: iterable of company strings (e.g. ["RDC"]) to tick via
    UserCompanyScope — company scope is fail-closed (2026-09-21, see
    app/models.py::UserCompanyScope), so any test giving this user an
    approval/submit role must tick at least one company or it can act on/
    submit nothing. The initiator/business_head/hr_manager fixtures below
    default to ["RDC"] to match the real-world backfill
    (backfill_company_scope.py) and keep existing RDC-focused tests
    unaffected by this change.
    """
    pw_hash = bcrypt.generate_password_hash(password).decode("utf-8")
    user = User(name=name, email=email, password_hash=pw_hash, role=role)
    db_session.session.add(user)
    db_session.session.flush()
    if companies:
        from app.models import UserCompanyScope
        for c in companies:
            db_session.session.add(UserCompanyScope(user_id=user.id, company=c))
        db_session.session.flush()
    return user


@pytest.fixture(scope="function")
def initiator(db):
    return _make_user("Alice Initiator", "alice@test.com", UserRole.INITIATOR, db, companies=["RDC"])


@pytest.fixture(scope="function")
def business_head(db):
    return _make_user("Bob BH", "bob@test.com", UserRole.BUSINESS_HEAD, db, companies=["RDC"])


@pytest.fixture(scope="function")
def hr_manager(db):
    return _make_user("Carol HRM", "carol@test.com", UserRole.HR_MANAGER, db, companies=["RDC"])


@pytest.fixture(scope="function")
def head_hr(db):
    return _make_user("Dave HHR", "dave@test.com", UserRole.HEAD_HR, db)


@pytest.fixture(scope="function")
def dr_bhoon(db):
    return _make_user("Dr. Bhoon", "drbhoon@test.com", UserRole.DR_BHOON, db)


@pytest.fixture(scope="function")
def super_admin(db):
    return _make_user("Eve Admin", "eve@test.com", UserRole.SUPER_ADMIN, db)


# ── Login helper ───────────────────────────────────────────────────────────────

def login(client, email, password="Test1234"):
    """POST to the login route and return the response."""
    return client.post("/auth/login", data={
        "login_id": email,
        "password": password,
    }, follow_redirects=True)


def logout(client):
    return client.get("/auth/logout", follow_redirects=True)
