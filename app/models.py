import enum
import json
import uuid
from datetime import datetime
from flask_login import UserMixin
from .extensions import db, login_manager


# ── Enums ──────────────────────────────────────────────────────────────────────

class UserRole(str, enum.Enum):
    INITIATOR = "INITIATOR"
    BUSINESS_HEAD = "BUSINESS_HEAD"
    HR_MANAGER = "HR_MANAGER"
    HEAD_HR = "HEAD_HR"
    DR_BHOON = "DR_BHOON"
    SUPER_ADMIN = "SUPER_ADMIN"


class RequestStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PENDING_BH = "PENDING_BH"
    PENDING_DR_BHOON = "PENDING_DR_BHOON"
    PENDING_HR_MANAGER = "PENDING_HR_MANAGER"
    PENDING_HEAD_HR = "PENDING_HEAD_HR"
    ACTIVE = "ACTIVE"
    REJECTED_BH = "REJECTED_BH"
    REJECTED_DR_BHOON = "REJECTED_DR_BHOON"
    REJECTED_HRM = "REJECTED_HRM"
    REJECTED_HEAD_HR = "REJECTED_HEAD_HR"


class ApprovalActionType(str, enum.Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FLAGGED_SPECIAL = "FLAGGED_SPECIAL"


class NotificationType(str, enum.Enum):
    EMAIL = "EMAIL"
    IN_APP = "IN_APP"


class FieldType(str, enum.Enum):
    TEXT = "text"
    EMAIL = "email"
    TEL = "tel"
    NUMBER = "number"
    DATE = "date"
    DROPDOWN = "dropdown"
    RADIO = "radio"
    FILE = "file"
    TEXTAREA = "textarea"


class OptionsSource(str, enum.Enum):
    INLINE = "inline"           # options from FormFieldOption table
    DESIGNATION = "designation" # options from Designation table
    PLANT_LOCATION = "plant_location"  # options from PlantLocation table


class AuditCategory(str, enum.Enum):
    AUTH         = "AUTH"          # Login, logout, password reset
    USER_MGMT    = "USER_MGMT"     # Admin creates/edits/disables users
    REQUEST      = "REQUEST"       # Onboarding request lifecycle
    ADMIN_PLANT  = "ADMIN_PLANT"   # Plant location CRUD
    ADMIN_DESIG  = "ADMIN_DESIG"   # Designation CRUD
    ADMIN_FIELD  = "ADMIN_FIELD"   # Form field CRUD + reorder
    PROFILE      = "PROFILE"       # Self-service profile / password changes
    EXPORT       = "EXPORT"        # Excel report downloads
    NOTIFICATION = "NOTIFICATION"  # Notification read events
    ADMIN_STAFFING_NORM   = "ADMIN_STAFFING_NORM"    # NormRoleCategory/Tier/Requirement CRUD
    ADMIN_PLANT_MAPPING   = "ADMIN_PLANT_MAPPING"    # PlantDvtMapping CRUD + auto-match runs
    ADMIN_CLUSTER_MAPPING = "ADMIN_CLUSTER_MAPPING"  # ClusterNameMapping CRUD + auto-match runs
    ADMIN_ZINGHR_DEPT     = "ADMIN_ZINGHR_DEPT"      # ZingHrDepartmentClassification CRUD


class NormScope(str, enum.Enum):
    PLANT = "PLANT"
    CLUSTER = "CLUSTER"


class NormSheet(str, enum.Enum):
    SHEET1 = "SHEET1"
    SHEET2 = "SHEET2"


class NormRequirementType(str, enum.Enum):
    FIXED = "FIXED"
    RATE_PER_VOLUME = "RATE_PER_VOLUME"
    PER_BUSINESS_HEAD = "PER_BUSINESS_HEAD"
    NONE = "NONE"


class MatchConfidence(str, enum.Enum):
    AUTO_EXACT = "auto_exact"
    AUTO_FUZZY = "auto_fuzzy"
    MANUAL = "manual"
    UNMATCHED = "unmatched"


class GateResult(str, enum.Enum):
    ALLOWED = "ALLOWED"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


# The three companies this app serves (added 2026-09-15, multi-company
# support). Canonical list — plain strings, not a Python enum, to match the
# pre-existing OnboardingRequest.company_code/PlantLocation.company string
# columns exactly. Order here also drives display order (RDC first — the
# original, most heavily-used company). Keep in sync with seed.py's
# company_code FormField options and the plant-company admin one-time script.
COMPANY_CHOICES = ["RDC", "Ultrafine", "ROBO"]

# ── Admin-managed reference tables ─────────────────────────────────────────────

class PlantLocation(db.Model):
    __tablename__ = "plant_locations"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    # Which company this plant belongs to (added 2026-09-15, multi-company
    # support). Plain string, not a Python enum, matching
    # OnboardingRequest.company_code's own convention — values are exactly
    # "RDC" / "Ultrafine" / "ROBO" (see seed.py's company_code FormField).
    # Existing rows are all RDC concrete plants (this table's only other use
    # is as a candidate-name source for the RDC-specific DVT auto-matcher in
    # matching.py) so the default backfills every pre-existing row correctly.
    company = db.Column(db.String(20), nullable=False, default="RDC")
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Designation(db.Model):
    __tablename__ = "designations"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    notice_period_days = db.Column(db.Integer, default=30, nullable=False)
    # If True, employees with this designation get userAppAttendance=1 in Truein
    truein_app_attendance = db.Column(db.Boolean, default=False, nullable=False)
    # Which RDC staffing-norm role bucket this designation counts toward (nullable —
    # designations not covered by the norms table, e.g. corporate/HR roles, stay None).
    norm_category_id = db.Column(db.Integer, db.ForeignKey("norm_role_categories.id"), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    norm_category = db.relationship("NormRoleCategory")


# ── Dynamic form schema ────────────────────────────────────────────────────────

class FormField(db.Model):
    __tablename__ = "form_fields"

    id = db.Column(db.Integer, primary_key=True)
    field_key = db.Column(db.String(100), unique=True, nullable=False)
    field_label = db.Column(db.String(200), nullable=False)
    field_type = db.Column(db.Enum(FieldType), nullable=False, default=FieldType.TEXT)
    step = db.Column(db.Integer, nullable=False, default=1)  # 1, 2, or 3
    is_required = db.Column(db.Boolean, default=True)
    is_active = db.Column(db.Boolean, default=True)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    is_readonly = db.Column(db.Boolean, default=False)  # e.g. notice_period
    sort_order = db.Column(db.Integer, default=0)
    placeholder = db.Column(db.String(200))
    help_text = db.Column(db.String(500))
    options_source = db.Column(db.Enum(OptionsSource), default=OptionsSource.INLINE)
    allow_other = db.Column(db.Boolean, default=False)  # show free-text when "Other" selected
    # Input validation constraints (applies to text / textarea fields)
    # Values: None | 'alpha' | 'alphanumeric' | 'numeric'
    input_pattern = db.Column(db.String(20), nullable=True)
    min_length = db.Column(db.Integer, nullable=True)
    max_length = db.Column(db.Integer, nullable=True)

    options = db.relationship(
        "FormFieldOption",
        back_populates="field",
        order_by="FormFieldOption.sort_order",
        cascade="all, delete-orphan",
    )

    @property
    def active_options(self):
        return [o for o in self.options if o.is_active]


class FormFieldOption(db.Model):
    __tablename__ = "form_field_options"

    id = db.Column(db.Integer, primary_key=True)
    field_id = db.Column(db.Integer, db.ForeignKey("form_fields.id"), nullable=False)
    option_value = db.Column(db.String(300), nullable=False)
    option_label = db.Column(db.String(300), nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)

    field = db.relationship("FormField", back_populates="options")


# ── Users ──────────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    __tablename__ = "users"
    __table_args__ = (
        db.Index("idx_user_bh_id",       "business_head_id"),
        db.Index("idx_user_role_active",  "role", "is_active"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False)
    # Optional short username for login (lowercase, alphanumeric + underscore)
    username = db.Column(db.String(60), unique=True, nullable=True)
    employee_code = db.Column(db.String(50), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.Enum(UserRole), nullable=False, default=UserRole.INITIATOR)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    profile_pic = db.Column(db.String(500), nullable=True)
    # Legacy — direct Initiator -> Business Head assignment, superseded
    # 2026-09-04 by region-based routing (see InitiatorRegion /
    # app/utils.py::bh_ids_for_initiator()). No longer set or read by any
    # reachable code path; left in place, unused, to avoid a destructive
    # column drop on live data (same convention as
    # StaffingGateCheck.overridden_* — see CLAUDE.md).
    business_head_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    # Brute-force / lockout tracking
    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Email notification preferences (Profile -> Email Notifications).
    # email_mode gates real-time email only — in-app notifications are never
    # silenced. daily_digest_enabled is an independent add-on, not a third
    # mode: a user can have real-time HIRING_ONLY mail AND the daily digest.
    # See notify_users() in app/utils.py and app/services/digest_email.py.
    email_mode = db.Column(db.Enum("ALL", "HIRING_ONLY", name="email_mode"), default="ALL", nullable=False)
    daily_digest_enabled = db.Column(db.Boolean, default=False, nullable=False)
    last_digest_sent_at = db.Column(db.DateTime, nullable=True)

    requests = db.relationship("OnboardingRequest", back_populates="initiator",
                               foreign_keys="OnboardingRequest.initiated_by")
    actions = db.relationship("ApprovalAction", back_populates="actor")
    notifications = db.relationship("Notification", back_populates="recipient")
    # Legacy — see business_head_id comment above. Self-referential
    # many-to-one: foreign_keys tells SQLAlchemy which column is the FK;
    # remote_side tells it which column is the PK on the "parent" side.
    # Do NOT add primaryjoin — SQLAlchemy infers it and having both causes warnings.
    business_head = db.relationship(
        "User",
        foreign_keys="[User.business_head_id]",
        remote_side="[User.id]",
        uselist=False,
    )

    def get_id(self):
        return str(self.id)

    @property
    def role_label(self):
        labels = {
            UserRole.INITIATOR: "Initiator",
            UserRole.BUSINESS_HEAD: "Business Head",
            UserRole.HR_MANAGER: "HR Manager",
            UserRole.HEAD_HR: "Head HR",
            UserRole.DR_BHOON: "Dr. Bhoon",
            UserRole.SUPER_ADMIN: "Admin",
        }
        return labels.get(self.role, self.role.value)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ── Onboarding Request ─────────────────────────────────────────────────────────

class OnboardingRequest(db.Model):
    __tablename__ = "onboarding_requests"
    __table_args__ = (
        db.Index("idx_req_status_deleted",  "status", "is_deleted"),
        db.Index("idx_req_initiated_by",    "initiated_by"),
        db.Index("idx_req_public_token",    "public_token"),
        db.Index("idx_req_updated_at",      "updated_at"),
        # company_code became a first-class WHERE clause 2026-09-21 for
        # BH/HR Manager dashboard scoping (app/main/routes.py) and the admin
        # requests-list company filter — previously unindexed.
        db.Index("idx_req_company_code",    "company_code"),
        # candidate_email/candidate_govt_id (2026-09-22): denormalized +
        # indexed the same way candidate_name/company_code/etc. already are,
        # so _check_email_registered()/_check_govt_id_registered() (fired
        # on every onboarding-form field blur) can do an indexed lookup
        # instead of loading and JSON-parsing every non-deleted/non-rejected
        # request into Python on every keystroke-blur — negligible at
        # today's live row count but a genuine linear-scan risk that only
        # gets worse, and safest to fix now while the data is small enough
        # to backfill trivially (see backfill_candidate_lookup_columns.py).
        db.Index("idx_req_candidate_email",   "candidate_email"),
        db.Index("idx_req_candidate_govt_id", "candidate_govt_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    # Non-guessable public identifier used in URLs
    public_token = db.Column(db.String(32), unique=True, nullable=True,
                             default=lambda: uuid.uuid4().hex)
    status = db.Column(db.Enum(RequestStatus), nullable=False, default=RequestStatus.DRAFT)
    initiated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    # Quick-access columns (populated from form_data on save) for display in tables
    candidate_name = db.Column(db.String(200))   # = form_data['associate_name']
    company_code = db.Column(db.String(50))       # = form_data['company_code']
    plant_location = db.Column(db.String(200))    # = form_data['plant_location']
    # Lowercased/digits-only mirrors of form_data['email_id']/['aadhar_no'],
    # kept in sync purely so the duplicate-check queries below can filter at
    # the DB level — see idx_req_candidate_email/idx_req_candidate_govt_id above.
    candidate_email = db.Column(db.String(200))    # = form_data['email_id'].strip().lower()
    candidate_govt_id = db.Column(db.String(20))   # = digits-only form_data['aadhar_no']
    # Digits-only mirror of form_data['mobile_number'], same convention and
    # reason as candidate_govt_id above — added 2026-09-24 so
    # _check_mobile_registered() can filter at the DB level instead of a
    # full scan (see idx_req_candidate_mobile).
    candidate_mobile = db.Column(db.String(15))
    designation = db.Column(db.String(200))       # = form_data['designation']
    # The exact email address OTP-verified for THIS request (2026-09-23 fix)
    # — was previously tracked only in the Flask session (session['_email_otp_verified']),
    # so any session loss (10-min inactivity auto-logout, a different device,
    # closing the browser) forced re-verification of an email already proven
    # once for this same draft. NULL = never verified. Compared against the
    # live email_id value, never blindly trusted — changing the email after
    # verification correctly requires re-verifying the new address.
    candidate_email_verified = db.Column(db.String(200))

    # All form responses stored as JSON {field_key: value}
    _form_data = db.Column("form_data", db.Text, default="{}")

    # File uploads: list of {type, name, filename, url}
    _documents = db.Column("documents", db.Text, default="[]")

    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    retry_count = db.Column(db.Integer, default=0, nullable=False)
    # Set True when the initiator explicitly acknowledges an RDC staffing-gate
    # block (via the form popup or the Submit-time fallback page) and chooses
    # to proceed anyway. Routes approval through the over-norm chain
    # (Business Head -> Head HR -> Dr. Bhoon, skipping HR Manager) instead of
    # the standard chain. Reset to False whenever plant_location/designation
    # changes (forces re-acknowledgment) or when a later gate re-check finds
    # capacity has opened up. See app/utils.py::get_new_status().
    is_special_case = db.Column(db.Boolean, default=False, nullable=False)
    # Truein push tracking
    truein_pushed_at      = db.Column(db.DateTime, nullable=True)  # set on successful push
    truein_push_error     = db.Column(db.Text,     nullable=True)  # last error message
    truein_retry_count    = db.Column(db.Integer,  default=0, nullable=False)  # total attempts made
    truein_last_attempt_at = db.Column(db.DateTime, nullable=True)  # timestamp of last attempt
    truein_retry_stopped  = db.Column(db.Boolean,  default=False, nullable=False)  # admin manually stopped
    truein_dropped_fields = db.Column(db.Text,     nullable=True)  # CSV of fields dropped to make the push succeed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    initiator = db.relationship("User", back_populates="requests", foreign_keys=[initiated_by])
    actions = db.relationship("ApprovalAction", back_populates="request",
                              order_by="ApprovalAction.acted_at")
    notifications = db.relationship("Notification", back_populates="request")

    # ── form_data helpers ─────────────────────────────────────────────────────

    @property
    def form_data(self):
        try:
            return json.loads(self._form_data or "{}")
        except Exception:
            return {}

    @form_data.setter
    def form_data(self, value):
        self._form_data = json.dumps(value)

    def get_field(self, key, default=""):
        return self.form_data.get(key, default) or default

    # ── documents helpers ─────────────────────────────────────────────────────

    @property
    def documents(self):
        try:
            return json.loads(self._documents or "[]")
        except Exception:
            return []

    @documents.setter
    def documents(self, value):
        self._documents = json.dumps(value)

    # ── display helpers ───────────────────────────────────────────────────────

    @property
    def status_label(self):
        labels = {
            RequestStatus.DRAFT: "Draft",
            RequestStatus.PENDING_BH: "Pending Business Head",
            RequestStatus.PENDING_DR_BHOON: "Pending Dr. Bhoon",
            RequestStatus.PENDING_HR_MANAGER: "Pending HR Manager",
            RequestStatus.PENDING_HEAD_HR: "Pending Head HR",
            RequestStatus.ACTIVE: "Approved",
            RequestStatus.REJECTED_BH: "Rejected by Business Head",
            RequestStatus.REJECTED_DR_BHOON: "Rejected by Dr. Bhoon",
            RequestStatus.REJECTED_HRM: "Rejected by HR Manager",
            RequestStatus.REJECTED_HEAD_HR: "Rejected by Head HR",
        }
        return labels.get(self.status, self.status.value)

    @property
    def status_color(self):
        if self.status == RequestStatus.ACTIVE:
            return "green"
        if self.status == RequestStatus.DRAFT:
            return "gray"
        if self.status.value.startswith("REJECTED"):
            return "red"
        return "blue"

    @property
    def last_action(self):
        return self.actions[-1] if self.actions else None

    @property
    def truein_dropped_list(self):
        """Return the list of fields dropped during Truein push (human labels)."""
        if not self.truein_dropped_fields:
            return []
        labels = {
            "manager_emp_id":         "Reporting Manager (manager emp id not found in Truein)",
            "sitePoint":              "Plant Location / Site Point (not configured in Truein)",
            "mobile":                 "Mobile Number (invalid format — must be a 10-digit number starting with 6, 7, 8, or 9)",
            "mobile_truein_rejected": "Mobile Number (rejected by Truein — often means it's already registered to another employee in Truein)",
        }
        return [labels.get(f.strip(), f.strip())
                for f in self.truein_dropped_fields.split(",") if f.strip()]


# ── Approval & Notification ────────────────────────────────────────────────────

class ApprovalAction(db.Model):
    __tablename__ = "approval_actions"

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("onboarding_requests.id"), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    action = db.Column(db.Enum(ApprovalActionType), nullable=False)
    remark = db.Column(db.Text)
    acted_at = db.Column(db.DateTime, default=datetime.utcnow)

    request = db.relationship("OnboardingRequest", back_populates="actions")
    actor = db.relationship("User", back_populates="actions")


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("onboarding_requests.id"), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    type = db.Column(db.Enum(NotificationType), nullable=False, default=NotificationType.IN_APP)
    subject = db.Column(db.String(500))
    body = db.Column(db.Text)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_read = db.Column(db.Boolean, default=False)

    request = db.relationship("OnboardingRequest", back_populates="notifications")
    recipient = db.relationship("User", back_populates="notifications")


# ── Truein Push Log ────────────────────────────────────────────────────────────

class TrueinPushLog(db.Model):
    """
    Full audit trail of every Truein addEmployeeDtls attempt.
    One row per attempt — records payload sent, raw response, HTTP status,
    success/failure, error message, and whether it was auto or manual.
    Never updated after creation.
    """
    __tablename__ = "truein_push_logs"
    __table_args__ = (
        db.Index("idx_tpl_request_id",  "request_id"),
        db.Index("idx_tpl_attempted_at", "attempted_at"),
    )

    id               = db.Column(db.Integer,  primary_key=True)
    request_id       = db.Column(db.Integer,  db.ForeignKey("onboarding_requests.id"), nullable=False)
    attempt_number   = db.Column(db.Integer,  nullable=False)           # 1-based counter per request
    triggered_by     = db.Column(db.String(20), nullable=False, default="auto")  # "auto" | "manual"
    emp_id           = db.Column(db.String(50), nullable=True)           # empId sent to Truein
    payload_sent     = db.Column(db.Text,     nullable=True)            # full JSON body sent
    http_status      = db.Column(db.Integer,  nullable=True)            # HTTP response code
    response_received = db.Column(db.Text,    nullable=True)            # full JSON response body
    success          = db.Column(db.Boolean,  nullable=False, default=False)
    error_message    = db.Column(db.Text,     nullable=True)            # human-readable error
    attempted_at     = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    request = db.relationship("OnboardingRequest", backref=db.backref("truein_logs", order_by="TrueinPushLog.attempted_at"))


# ── System Configuration ──────────────────────────────────────────────────────

class SystemConfig(db.Model):
    """Key-value store for admin-configurable system settings (e.g. SMTP credentials)."""
    __tablename__ = "system_config"

    id         = db.Column(db.Integer, primary_key=True)
    key        = db.Column(db.String(100), unique=True, nullable=False)
    value      = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── Audit Log ──────────────────────────────────────────────────────────────────

class AuditLog(db.Model):
    """
    Immutable record of every user-triggered action across the system.
    Written via utils.log_audit() — always committed atomically with the action.
    Never updated or deleted after creation.
    """
    __tablename__ = "audit_logs"
    __table_args__ = (
        db.Index("idx_audit_actor",    "actor_id"),
        db.Index("idx_audit_category", "action_category"),
        db.Index("idx_audit_created",  "created_at"),
        db.Index("idx_audit_resource", "resource_type", "resource_id"),
    )

    id              = db.Column(db.Integer, primary_key=True)
    # NULL for actions before authentication (e.g. login attempts for unknown users)
    actor_id        = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    action_category = db.Column(db.Enum(AuditCategory), nullable=False)
    # Short machine-readable code, e.g. "LOGIN_SUCCESS", "USER_DISABLED"
    action_type     = db.Column(db.String(60), nullable=False)
    # What kind of object was acted on: "User", "OnboardingRequest", "PlantLocation", …
    resource_type   = db.Column(db.String(60),  nullable=True)
    # Primary-key of that object (for deep linking from the audit log)
    resource_id     = db.Column(db.Integer,     nullable=True)
    # Human-readable label: "John Smith", "Request #42 — Jane Doe", "Plant: Mumbai"
    resource_label  = db.Column(db.String(300), nullable=True)
    # JSON blob with before/after values, remark, filter params, etc.
    detail          = db.Column(db.Text,        nullable=True)
    ip_address      = db.Column(db.String(45),  nullable=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    actor = db.relationship("User", foreign_keys=[actor_id], lazy="joined")


# ── RDC Staffing Norms ──────────────────────────────────────────────────────────
# Admin-managed reference data for the RDC hiring gate: how many people of each
# role are allowed at a plant/cluster, given its recent production volume /
# plant count. See app/services/staffing_norms.py for the evaluation logic.

class NormRoleCategory(db.Model):
    """
    One of the staffing-norm role buckets — at plant scope: Plant Manager,
    Technical (absorbs the old "FTs/LT/TO" formula), Batchers/Production
    Officer, Materials, Assistant, Operations (visibility-only, ungated); at
    cluster scope: Accounts, Credit Control, .... Designation.norm_category_id
    maps our internal designations onto these for new hires; existing
    employees are bucketed by raw Department value instead (see
    app/services/headcount.py::_classify_by_department()).
    """
    __tablename__ = "norm_role_categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    scope = db.Column(db.Enum(NormScope), nullable=False)
    sheet = db.Column(db.Enum(NormSheet), nullable=False, default=NormSheet.SHEET1)
    # False for roles that aren't actually tied to concrete production (e.g.
    # Operations) — hiring for these is never blocked by plant volume /
    # cluster plant-count, regardless of what the NormRequirement table for
    # their tier says. True (the default) for roles that do scale with
    # output — Plant Manager, Technical, Batchers/Production Officer,
    # Materials, Assistant.
    is_capacity_gated = db.Column(db.Boolean, default=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class NormTier(db.Model):
    """
    A volume bracket (plant scope, e.g. "<3000 m³") or plant-count bracket
    (cluster scope, e.g. "1-2 Plants") that NormRequirement rows attach to.
    """
    __tablename__ = "norm_tiers"

    id = db.Column(db.Integer, primary_key=True)
    sheet = db.Column(db.Enum(NormSheet), nullable=False, default=NormSheet.SHEET1)
    scope = db.Column(db.Enum(NormScope), nullable=False)
    # Machine-stable key, e.g. 'LT_3000', '3000_5000', 'GT_5000', '1_2_PLANTS'
    tier_key = db.Column(db.String(50), nullable=False)
    tier_label = db.Column(db.String(100), nullable=False)  # e.g. "< 3000 m³"
    min_value = db.Column(db.Float, nullable=True)  # inclusive lower bound (volume or plant count)
    max_value = db.Column(db.Float, nullable=True)  # exclusive upper bound; NULL = no upper bound
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True, nullable=False)


class NormRequirement(db.Model):
    """
    The actual per-(tier, role) headcount rule — one row per spreadsheet cell.
    Exactly one of fixed_count / rate_per_unit+unit_volume is populated,
    depending on requirement_type.
    """
    __tablename__ = "norm_requirements"

    id = db.Column(db.Integer, primary_key=True)
    tier_id = db.Column(db.Integer, db.ForeignKey("norm_tiers.id"), nullable=False)
    norm_role_category_id = db.Column(db.Integer, db.ForeignKey("norm_role_categories.id"), nullable=False)
    requirement_type = db.Column(db.Enum(NormRequirementType), nullable=False)
    fixed_count = db.Column(db.Integer, nullable=True)     # used when requirement_type == FIXED
    rate_per_unit = db.Column(db.Float, nullable=True)     # e.g. 1  (used when RATE_PER_VOLUME)
    unit_volume = db.Column(db.Float, nullable=True)       # e.g. 900, or 15000 for the cluster Accounts row
    notes = db.Column(db.String(300), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tier = db.relationship("NormTier", backref=db.backref("requirements"))
    norm_role_category = db.relationship("NormRoleCategory", backref=db.backref("requirements"))


# ── Cross-system name reconciliation ────────────────────────────────────────────
# Plant/cluster names differ across every system we integrate with (our own
# form's live-from-Truein plant list, the Daily Volume Tracker's plant_code/
# region, ZingHR's City, Truein's category). These tables hold the admin-
# reviewed mapping between them, auto-populated by app/services/matching.py
# and correctable via the admin screens.

class ClusterNameMapping(db.Model):
    """Three-way city/cluster reconciliation: DVT region <-> ZingHR City <-> Truein category."""
    __tablename__ = "cluster_name_mappings"

    id = db.Column(db.Integer, primary_key=True)
    canonical_cluster_name = db.Column(db.String(200), nullable=False, unique=True)
    dvt_region = db.Column(db.String(200), nullable=True)
    zinghr_city = db.Column(db.String(200), nullable=True)
    truein_category = db.Column(db.String(200), nullable=True)
    match_confidence = db.Column(db.Enum(MatchConfidence), nullable=False, default=MatchConfidence.UNMATCHED)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BusinessHeadRegion(db.Model):
    """Which region(s) (clusters) a Business Head is scoped to for the RDC staffing dashboard."""
    __tablename__ = "business_head_regions"
    __table_args__ = (db.UniqueConstraint("business_head_id", "cluster_id"),)

    id = db.Column(db.Integer, primary_key=True)
    business_head_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    cluster_id = db.Column(db.Integer, db.ForeignKey("cluster_name_mappings.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    business_head = db.relationship("User", backref=db.backref("region_links", cascade="all, delete-orphan"))
    cluster = db.relationship("ClusterNameMapping", backref=db.backref("bh_links", cascade="all, delete-orphan"))


class InitiatorRegion(db.Model):
    """
    Which region(s) (clusters) an Initiator belongs to — the mirror image of
    BusinessHeadRegion. Routing (which Business Head sees/can act on a
    request) is a many-to-many overlap: any active Business Head who shares
    at least one region with the initiator. See
    app/utils.py::bh_ids_for_initiator(). Superseded the old single
    User.business_head_id direct assignment 2026-09-04.
    """
    __tablename__ = "initiator_regions"
    __table_args__ = (db.UniqueConstraint("initiator_id", "cluster_id"),)

    id = db.Column(db.Integer, primary_key=True)
    initiator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    cluster_id = db.Column(db.Integer, db.ForeignKey("cluster_name_mappings.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    initiator = db.relationship("User", backref=db.backref("initiator_region_links", cascade="all, delete-orphan"))
    cluster = db.relationship("ClusterNameMapping", backref=db.backref("initiator_links", cascade="all, delete-orphan"))


class UserCompanyScope(db.Model):
    """
    Companies (RDC/Ultrafine/ROBO) a user is ticked for — shared across
    INITIATOR, BUSINESS_HEAD, HR_MANAGER (2026-09-21, the only 3 roles that
    get company scoping; HEAD_HR/DR_BHOON stay unscoped, confirmed with the
    stakeholder). One shared table rather than role-specific tables (unlike
    BusinessHeadRegion/InitiatorRegion, which are genuinely directional)
    since "companies this account is ticked for" is identical regardless of
    role. Fail-closed by design, unlike the region tables above: zero rows
    means scoped to nothing, not "unscoped/sees everything" — see
    backfill_company_scope.py, which ticks "RDC" for every pre-existing
    user of these 3 roles so this doesn't strand live accounts at ship
    time. See app/utils.py::bh_ids_for_initiator()/company_scope_ids()/
    hr_manager_ids_for_company() for how this gates approval routing, and
    the "RDC region scope only matters when RDC is ticked here" rule in
    app/admin/routes.py::new_user()/edit_user().
    """
    __tablename__ = "user_company_scopes"
    __table_args__ = (db.UniqueConstraint("user_id", "company"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    company = db.Column(db.String(20), nullable=False)  # one of COMPANY_CHOICES
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("company_scope_links", cascade="all, delete-orphan"))


class PlantDvtMapping(db.Model):
    """
    Maps the free-text plant string actually submitted on onboarding requests
    (our form's Plant dropdown is populated live from Truein at request time —
    PlantLocation is only a DB fallback, so this maps strings, not
    PlantLocation.id) to the Daily Volume Tracker's plant_code.
    """
    __tablename__ = "plant_dvt_mappings"

    id = db.Column(db.Integer, primary_key=True)
    plant_location_name = db.Column(db.String(200), nullable=False, unique=True)
    dvt_plant_code = db.Column(db.String(50), nullable=True)
    dvt_daily_tracker_name = db.Column(db.String(200), nullable=True)
    dvt_erp_name = db.Column(db.String(300), nullable=True)
    # Truein's per-employee "sub_site" field is the plant-level location for that
    # source (distinct from "category", which is cluster-level — see
    # app/services/headcount.py). Employees never resolve to a PLANT-scope
    # bucket without this, since Truein has no other plant-granularity field.
    truein_sub_site = db.Column(db.String(200), nullable=True)
    cluster_id = db.Column(db.Integer, db.ForeignKey("cluster_name_mappings.id"), nullable=True)
    match_confidence = db.Column(db.Enum(MatchConfidence), nullable=False, default=MatchConfidence.UNMATCHED)
    match_score = db.Column(db.Float, nullable=True)
    # Which DVT field this row's match came from — 'erp_name' or
    # 'tracker_name' (DVT's daily_tracker_name). NULL for MANUAL rows and
    # UNMATCHED rows. auto_match_plants() only ever accepts an exact
    # (normalized) name match against one of these two fields — see its
    # docstring for why fuzzy matching was deliberately dropped.
    matched_on = db.Column(db.String(20), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    cluster = db.relationship("ClusterNameMapping", backref=db.backref("plants"))
    updated_by = db.relationship("User", foreign_keys=[updated_by_id])

    @property
    def display_name(self):
        """
        The proper "ERP-Tracker" name to show users everywhere a plant name
        is displayed — matches the combined format the Daily Volume Tracker
        API itself uses for its own `plant_name` field: "{tracker_name}
        ({erp_name})", e.g. "NCR-Faridabad (Faridabad-Mathura Road)".
        plant_location_name is only the internal stable join key (e.g.
        "AP-Vizag") and should not be shown to end users on its own.
        Falls back gracefully for the ~40% of plants not yet DVT-matched, or
        matched with only one of the two names, so nothing renders blank.
        """
        tracker, erp = self.dvt_daily_tracker_name, self.dvt_erp_name
        if tracker and erp and tracker != erp:
            return f"{tracker} ({erp})"
        return tracker or erp or self.plant_location_name


class PlantNameAlias(db.Model):
    """
    Alternate ZingHR Location / Truein sub_site strings that refer to the
    same real plant as a PlantNameAlias.plant_dvt_mapping row — e.g.
    "KAR-Mangalore" and "Mangalore" both being Mangalore 1 RMC Plant
    (DVT code MG1). Without this, matching.py's auto-matcher creates one
    PlantDvtMapping row per distinct name variant it sees, and
    headcount.py's plant resolution (keyed on the literal
    plant_location_name/truein_sub_site string) silently splits one
    plant's real headcount across multiple rows instead of summing it —
    undercounting the norm gate's "current headcount" for every affected
    plant. Merging duplicates moves the losing row's name(s) here rather
    than just soft-deleting the row outright, so those ZingHR/Truein
    records keep resolving to the surviving canonical plant instead of
    going unresolved.
    """
    __tablename__ = "plant_name_aliases"

    id = db.Column(db.Integer, primary_key=True)
    alias_name = db.Column(db.String(200), nullable=False, unique=True)
    plant_dvt_mapping_id = db.Column(db.Integer, db.ForeignKey("plant_dvt_mappings.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    plant = db.relationship("PlantDvtMapping", backref=db.backref("aliases"))


class ExternalDesignationSource(str, enum.Enum):
    ZINGHR = "ZINGHR"
    TRUEIN = "TRUEIN"


# ── RDC Staffing Snapshot ────────────────────────────────────────────────────────
# Cached headcount computation, refreshed every 2 hours by a background thread
# (app/services/snapshot_refresh.py) rather than live per-request — ZingHR/
# Truein/DVT are never called synchronously during a submission. Rows are
# append-only history; readers always want the latest row per
# (scope, location_key, norm_role_category_id).

class StaffingSnapshot(db.Model):
    __tablename__ = "staffing_snapshots"
    __table_args__ = (
        db.Index("idx_snapshot_lookup", "scope", "location_key", "norm_role_category_id", "computed_at"),
        # computed_at alone: every read helper resolves "the latest run" via
        # MAX(computed_at) first. It's the LAST column in idx_snapshot_lookup
        # above, so that composite index can't answer a bare MAX() — without
        # this dedicated index MySQL does a full table scan (confirmed:
        # ~293k rows, ~0.12s per call) every time any snapshot is read.
        db.Index("idx_snapshot_computed_at", "computed_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    scope = db.Column(db.Enum(NormScope), nullable=False)
    location_key = db.Column(db.String(200), nullable=False)  # plant_location_name or canonical_cluster_name
    norm_role_category_id = db.Column(db.Integer, db.ForeignKey("norm_role_categories.id"), nullable=False)
    current_headcount = db.Column(db.Integer, nullable=False, default=0)
    zinghr_count = db.Column(db.Integer, nullable=False, default=0)
    truein_count = db.Column(db.Integer, nullable=False, default=0)
    deduped_count = db.Column(db.Integer, nullable=False, default=0)  # employees matched in both, counted once
    unclassified_count = db.Column(db.Integer, nullable=False, default=0)
    # The actual norm limit, pre-computed at snapshot time (one bulk DVT call
    # per run, not a live lookup per row) so the dashboard can show a real
    # "can hire / at capacity" answer instead of a placeholder. NULL means
    # "unresolvable this run" (plant not DVT-mapped, no matching tier, role
    # not yet configured, etc.) — shown as "Unknown", never guessed as yes/no.
    allowed_headcount = db.Column(db.Integer, nullable=True)
    tier_label = db.Column(db.String(100), nullable=True)
    # The basis value actually used to compute allowed_headcount for THIS
    # role's requirement — real DVT m^3 for RATE_PER_VOLUME roles, but the
    # cluster's plant_count for FIXED/NONE/PER_BUSINESS_HEAD roles (a
    # cluster's tier is plant-count-based, not volume-based, so most cluster
    # roles have no volume basis at all). Do NOT render this as "m^3" —
    # use production_volume for that.
    volume_used = db.Column(db.Float, nullable=True)
    # The location's actual last-month DVT production volume in m^3, always
    # real regardless of this row's role/requirement type — identical across
    # every row for the same (scope, location_key). This is what the
    # dashboard's "Last Month's Production" callouts should read.
    production_volume = db.Column(db.Float, nullable=True)
    can_hire = db.Column(db.Boolean, nullable=True)  # current_headcount < allowed_headcount; NULL if allowed unknown
    computed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    source_warnings = db.Column(db.Text, nullable=True)  # JSON list of strings

    norm_role_category = db.relationship("NormRoleCategory")


class StaffingGateCheck(db.Model):
    """
    One row per RDC staffing-gate evaluation at submit_request()/
    resubmit_request() time. Source for the "Hiring Not Possible" page and
    an audit trail. overridden_*/override_remark are legacy columns from a
    since-removed HR Manager override queue (superseded by
    OnboardingRequest.is_special_case + the over-norm approval chain) — left
    in place, unused, to avoid a destructive column drop on live data.
    """
    __tablename__ = "staffing_gate_checks"
    __table_args__ = (
        db.Index("idx_gate_check_request", "request_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("onboarding_requests.id"), nullable=False)
    checked_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    plant_name = db.Column(db.String(200), nullable=True)
    cluster_name = db.Column(db.String(200), nullable=True)
    norm_role_category_id = db.Column(db.Integer, db.ForeignKey("norm_role_categories.id"), nullable=True)
    tier_label = db.Column(db.String(100), nullable=True)
    volume_used = db.Column(db.Float, nullable=True)
    current_headcount = db.Column(db.Integer, nullable=True)
    allowed_headcount = db.Column(db.Integer, nullable=True)
    snapshot_computed_at = db.Column(db.DateTime, nullable=True)
    result = db.Column(db.Enum(GateResult), nullable=False)
    detail = db.Column(db.Text, nullable=True)  # JSON: source breakdown, dedup info, warnings

    # Legacy — unused (see docstring)
    overridden_at = db.Column(db.DateTime, nullable=True)
    overridden_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    override_remark = db.Column(db.Text, nullable=True)

    request = db.relationship("OnboardingRequest", backref=db.backref("staffing_gate_checks", order_by="StaffingGateCheck.checked_at"))
    norm_role_category = db.relationship("NormRoleCategory")
    overridden_by = db.relationship("User", foreign_keys=[overridden_by_id])


class EmployeeLocationSnapshot(db.Model):
    """
    One row per active employee per 2-hourly refresh run — backs the
    dashboard's drill-down ("cluster -> plants in it -> people at a plant").
    Carries enough detail (designation, department, joining date, source
    system) to be useful on its own, not just name+designation. Append-only,
    same "latest computed_at wins" read pattern as the other snapshots.
    """
    __tablename__ = "employee_location_snapshots"
    __table_args__ = (
        db.Index("idx_emp_snapshot_plant", "plant_location_key", "computed_at"),
        db.Index("idx_emp_snapshot_cluster", "cluster_location_key", "computed_at"),
        # See idx_snapshot_computed_at on StaffingSnapshot — same reasoning,
        # same fix: computed_at is the trailing column in both composite
        # indexes above, so a bare MAX(computed_at) still forces a full
        # table scan (confirmed: ~573k rows, ~0.29s per call) without this.
        db.Index("idx_emp_snapshot_computed_at", "computed_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.Enum(ExternalDesignationSource), nullable=False)
    employee_code = db.Column(db.String(50), nullable=True)
    employee_name = db.Column(db.String(200), nullable=True)
    designation = db.Column(db.String(300), nullable=True)   # raw string as it appears externally
    department = db.Column(db.String(200), nullable=True)    # raw ZingHR/Truein department string
    date_of_joining = db.Column(db.String(50), nullable=True)  # kept as the source system's own string format
    norm_role_category_id = db.Column(db.Integer, db.ForeignKey("norm_role_categories.id"), nullable=True)
    plant_location_key = db.Column(db.String(200), nullable=True)
    cluster_location_key = db.Column(db.String(200), nullable=True)
    # Added 2026-09-15 (multi-company support). Nullable: pre-existing rows
    # are implicitly RDC (this table's whole prior history is the RDC-only
    # pipeline) and are never read by the new company-scoped queries, so no
    # backfill is needed. New RDC rows still leave this null too — only the
    # new Ultrafine/ROBO snapshot path (_compute_and_store_other_company_snapshot()
    # in headcount.py) sets it, since that's the only place company scoping
    # is actually needed (RDC's own queries key off plant/cluster as before).
    company = db.Column(db.String(20), nullable=True)
    computed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    norm_role_category = db.relationship("NormRoleCategory")
