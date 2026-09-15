from datetime import datetime, timedelta
from flask import render_template, redirect, url_for, flash, request, current_app, session
from flask_login import login_user, logout_user, login_required, current_user
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from ..extensions import db, bcrypt, mail, limiter
from ..models import User
from ..utils import send_email, validate_password, log_audit
from . import auth_bp


def _make_reset_token(email):
    s = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    return s.dumps(email, salt=current_app.config["PASSWORD_RESET_SALT"])


def _verify_reset_token(token):
    s = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    try:
        email = s.loads(
            token,
            salt=current_app.config["PASSWORD_RESET_SALT"],
            max_age=current_app.config["PASSWORD_RESET_MAX_AGE"],
        )
    except (SignatureExpired, BadSignature):
        return None
    return email


# ── Login ──────────────────────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        login_id = request.form.get("login_id", "").strip()
        password  = request.form.get("password", "")
        remember  = bool(request.form.get("remember"))
        login_lower = login_id.lower()

        # Try email first; fall back to username
        user = (
            User.query.filter_by(email=login_lower).first()
            or User.query.filter_by(username=login_lower).first()
        )

        if user and user.is_active and bcrypt.check_password_hash(user.password_hash, password):
            # ── Successful login — reset counters ──
            user.failed_login_attempts = 0
            user.locked_until = None
            log_audit("AUTH", "LOGIN_SUCCESS",
                      resource_type="User", resource_id=user.id,
                      resource_label=user.name, actor_id=user.id,
                      detail={"login_id": login_id, "remember_me": remember})
            db.session.commit()
            login_user(user, remember=remember)
            session["_last_active"] = datetime.utcnow().timestamp()
            next_page = request.args.get("next")
            # Guard against open-redirect: only allow relative paths
            if next_page and (next_page.startswith("http") or "//" in next_page):
                next_page = None
            return redirect(next_page or url_for("main.dashboard"))

        # ── Failed login ──
        if user and not user.is_active:
            log_audit("AUTH", "LOGIN_FAILED",
                      resource_type="User", resource_id=user.id,
                      resource_label=user.name, actor_id=user.id,
                      detail={"login_id": login_id, "reason": "account_deactivated"})
            db.session.commit()
            flash("Your account has been deactivated. Contact an administrator.", "danger")
        elif user:
            # No lockout, no attempt limit — failed logins are logged only,
            # never counted toward locking the account or resetting the password.
            log_audit("AUTH", "LOGIN_FAILED",
                      resource_type="User", resource_id=user.id,
                      resource_label=user.name, actor_id=user.id,
                      detail={"login_id": login_id})
            db.session.commit()
            flash("Invalid credentials. Please check your email/username and password.", "danger")
        else:
            # Unknown user — still show generic message (no enumeration)
            log_audit("AUTH", "LOGIN_FAILED",
                      resource_label=login_id,
                      detail={"login_id": login_id, "reason": "user_not_found"})
            db.session.commit()
            flash("Invalid credentials. Please check your email/username and password.", "danger")

    return render_template("auth/login.html")


# ── Logout ─────────────────────────────────────────────────────────────────────

@auth_bp.route("/logout")
@login_required
def logout():
    # Log BEFORE logout_user() clears current_user
    log_audit("AUTH", "LOGOUT",
              resource_type="User", resource_id=current_user.id,
              resource_label=current_user.name)
    db.session.commit()
    logout_user()
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


# ── Forgot Password ────────────────────────────────────────────────────────────

@auth_bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()

        # Always show success even if email not found (prevents user enumeration)
        if user and user.is_active:
            token = _make_reset_token(user.email)
            reset_url = url_for("auth.reset_password", token=token, _external=True)
            body = (
                f"Hello {user.name},\n\n"
                f"You requested a password reset for your RDC Teamlease Employee Onboarding Portal account.\n\n"
                f"Click the link below to reset your password (valid for 30 minutes):\n"
                f"{reset_url}\n\n"
                f"If you did not request this, you can safely ignore this email.\n\n"
                f"— RDC Teamlease HR Portal"
            )
            send_email(
                subject="Password Reset — RDC HR Onboarding Portal",
                recipients=[user.email],
                body=body,
            )
            log_audit("AUTH", "PASSWORD_RESET_REQUESTED",
                      resource_type="User", resource_id=user.id,
                      resource_label=user.name, actor_id=user.id,
                      detail={"email": email})
            db.session.commit()

        flash(
            "If that email is registered, a password reset link has been sent. "
            "Check your inbox (and spam folder).",
            "info",
        )
        return redirect(url_for("auth.forgot_password"))

    return render_template("auth/forgot_password.html")


# ── Reset Password ─────────────────────────────────────────────────────────────

@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    email = _verify_reset_token(token)
    if not email:
        flash("This password reset link is invalid or has expired.", "danger")
        return redirect(url_for("auth.forgot_password"))

    user = User.query.filter_by(email=email).first()
    if not user or not user.is_active:
        flash("Account not found or deactivated.", "danger")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        password  = request.form.get("password", "")
        confirm   = request.form.get("confirm_password", "")

        errors = validate_password(password)
        if password != confirm:
            errors.append("Passwords do not match.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("auth/reset_password.html", token=token)

        user.password_hash = bcrypt.generate_password_hash(password).decode("utf-8")
        user.failed_login_attempts = 0
        user.locked_until = None
        log_audit("AUTH", "PASSWORD_RESET_COMPLETED",
                  resource_type="User", resource_id=user.id,
                  resource_label=user.name, actor_id=user.id,
                  detail={"email": user.email})
        db.session.commit()

        flash("Password updated successfully. Please sign in with your new password.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", token=token)
