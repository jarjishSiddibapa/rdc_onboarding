import os
import re
import uuid
from flask import render_template, redirect, url_for, flash, request, current_app, jsonify
from flask_login import login_required, current_user
from ..extensions import db, bcrypt
from ..models import User
from ..utils import validate_password, validate_mime, log_audit
from . import profile_bp

ALLOWED_IMG = {"png", "jpg", "jpeg", "gif", "webp"}


def _allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[-1].lower() in ALLOWED_IMG


def _save_profile_pic(file_obj):
    ext = file_obj.filename.rsplit(".", 1)[-1].lower()
    stored = f"profile_{uuid.uuid4().hex}.{ext}"
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored)
    file_obj.save(path)
    return stored


# ── Availability check (AJAX) ─────────────────────────────────────────────────

@profile_bp.route("/api/check-username")
@login_required
def check_username():
    """Return {available: bool} — excludes the current user from the uniqueness check."""
    username = request.args.get("username", "").strip().lower()
    if not username:
        return jsonify({"available": True})
    q = User.query.filter(User.username == username, User.id != current_user.id)
    return jsonify({"available": q.first() is None})


# ── My Profile ─────────────────────────────────────────────────────────────────

@profile_bp.route("/", methods=["GET", "POST"])
@login_required
def my_profile():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "update_profile":
            name = request.form.get("name", "").strip()
            new_username = request.form.get("username", "").strip().lower() or None
            if not name:
                flash("Name cannot be empty.", "danger")
                return redirect(url_for("profile.my_profile"))
            # Username format check
            if new_username:
                if not re.match(r'^[a-z0-9_]+$', new_username):
                    flash("Username may only contain lowercase letters, numbers, and underscores.", "danger")
                    return redirect(url_for("profile.my_profile"))
                conflict = User.query.filter_by(username=new_username).first()
                if conflict and conflict.id != current_user.id:
                    flash("Username already taken.", "danger")
                    return redirect(url_for("profile.my_profile"))
            _old_name     = current_user.name
            _old_username = current_user.username
            current_user.name = name
            current_user.username = new_username
            # Handle profile pic upload
            pic = request.files.get("profile_pic")
            _pic_uploaded = False
            if pic and pic.filename:
                _img_exts = {"png", "jpg", "jpeg", "gif", "webp"}
                if _allowed_image(pic.filename) and validate_mime(pic, _img_exts):
                    if current_user.profile_pic:
                        old_path = os.path.join(
                            current_app.config["UPLOAD_FOLDER"], current_user.profile_pic
                        )
                        try:
                            if os.path.exists(old_path):
                                os.remove(old_path)
                        except OSError:
                            pass
                    current_user.profile_pic = _save_profile_pic(pic)
                    _pic_uploaded = True
                else:
                    flash("Only PNG, JPG, GIF, or WEBP images are allowed.", "warning")
            # Log granular changes
            if _old_name != name:
                log_audit("PROFILE", "PROFILE_NAME_CHANGED",
                          resource_type="User", resource_id=current_user.id,
                          resource_label=current_user.name,
                          detail={"from": _old_name, "to": name})
            if (_old_username or "") != (new_username or ""):
                log_audit("PROFILE", "PROFILE_USERNAME_CHANGED",
                          resource_type="User", resource_id=current_user.id,
                          resource_label=current_user.name,
                          detail={"from": _old_username, "to": new_username})
            if _pic_uploaded:
                log_audit("PROFILE", "PROFILE_PICTURE_UPLOADED",
                          resource_type="User", resource_id=current_user.id,
                          resource_label=current_user.name,
                          detail={"original_filename": pic.filename})
            db.session.commit()
            flash("Profile updated.", "success")

        elif action == "remove_pic":
            if current_user.profile_pic:
                old_path = os.path.join(
                    current_app.config["UPLOAD_FOLDER"], current_user.profile_pic
                )
                try:
                    if os.path.exists(old_path):
                        os.remove(old_path)
                except OSError:
                    pass
                current_user.profile_pic = None
                log_audit("PROFILE", "PROFILE_PICTURE_REMOVED",
                          resource_type="User", resource_id=current_user.id,
                          resource_label=current_user.name)
                db.session.commit()
                flash("Profile picture removed.", "info")

        elif action == "change_password":
            current_pwd = request.form.get("current_password", "")
            new_pwd = request.form.get("new_password", "").strip()
            confirm_pwd = request.form.get("confirm_password", "").strip()
            if not bcrypt.check_password_hash(current_user.password_hash, current_pwd):
                flash("Current password is incorrect.", "danger")
            else:
                pwd_errors = validate_password(new_pwd)
                if new_pwd != confirm_pwd:
                    pwd_errors.append("Passwords do not match.")
                if pwd_errors:
                    for e in pwd_errors:
                        flash(e, "danger")
                else:
                    current_user.password_hash = bcrypt.generate_password_hash(new_pwd).decode("utf-8")
                    log_audit("PROFILE", "PASSWORD_CHANGED_BY_SELF",
                              resource_type="User", resource_id=current_user.id,
                              resource_label=current_user.name)
                    db.session.commit()
                    flash("Password changed successfully.", "success")

        elif action == "update_notification_prefs":
            new_mode = request.form.get("email_mode", "ALL")
            if new_mode not in ("ALL", "HIRING_ONLY"):
                new_mode = "ALL"
            new_digest = request.form.get("daily_digest_enabled") == "on"
            _old = {"email_mode": current_user.email_mode, "daily_digest_enabled": current_user.daily_digest_enabled}
            current_user.email_mode = new_mode
            current_user.daily_digest_enabled = new_digest
            log_audit("PROFILE", "NOTIFICATION_PREFS_UPDATED",
                      resource_type="User", resource_id=current_user.id,
                      resource_label=current_user.name,
                      detail={"from": _old, "to": {"email_mode": new_mode, "daily_digest_enabled": new_digest}})
            db.session.commit()
            flash("Notification preferences updated.", "success")

        return redirect(url_for("profile.my_profile"))

    return render_template("profile/profile.html")
