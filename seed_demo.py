"""
Demo data for the onboarding workflow.

Run AFTER `python seed.py --yes`. Adds one user per role (all RDC-scoped), a region that links the
initiator to the business head, and a handful of fictional onboarding requests spread across the
approval chain so the dashboards and request pages have something to show.

Every name, number and e-mail address below is invented. All demo users share DEMO_PASSWORD.
Never run this against a production database.
"""
import json
from datetime import datetime, timedelta

from app import create_app
from app.extensions import bcrypt, db
from app.models import (
    ApprovalAction, ApprovalActionType, BusinessHeadRegion, ClusterNameMapping, InitiatorRegion,
    OnboardingRequest, RequestStatus, User, UserCompanyScope, UserRole,
)

DEMO_PASSWORD = "Demo@12345"

USERS = [
    ("Isha Initiator", "initiator@example.com", UserRole.INITIATOR),
    ("Bharat Business Head", "bh@example.com", UserRole.BUSINESS_HEAD),
    ("Hema HR Manager", "hrm@example.com", UserRole.HR_MANAGER),
    ("Harsh Head HR", "headhr@example.com", UserRole.HEAD_HR),
    ("Dev Director", "director@example.com", UserRole.DR_BHOON),
]

# candidate, designation, plant, status, days ago
REQUESTS = [
    ("Aarav Kulkarni", "Batching Plant Operator", "MUM-Deonar", RequestStatus.PENDING_BH, 1),
    ("Meera Nair", "Executive Accounts", "BG-Yelhanka", RequestStatus.PENDING_BH, 2),
    ("Rohan Deshmukh", "TM Driver", "MUM-Kalyan", RequestStatus.PENDING_HR_MANAGER, 3),
    ("Sana Sheikh", "Lab Technician", "HYD-Nacharam", RequestStatus.PENDING_HEAD_HR, 4),
    ("Vikram Rao", "Field Technician", "CHE-Ambattur", RequestStatus.ACTIVE, 9),
    ("Priya Menon", "Assistant", "BG-Whitefield", RequestStatus.ACTIVE, 12),
    ("Karan Malhotra", "Pump Operator", "MUM-Turbhe", RequestStatus.REJECTED_BH, 6),
    ("Neha Joshi", "Officer - Safety", "KOL-Howrah", RequestStatus.DRAFT, 0),
]

CHAIN = [
    (RequestStatus.PENDING_BH, "bh@example.com", "Approved. Position is within the plant plan."),
    (RequestStatus.PENDING_HR_MANAGER, "hrm@example.com", "Documents verified. Approved."),
    (RequestStatus.PENDING_HEAD_HR, "headhr@example.com", "Approved for onboarding."),
]


def main():
    app = create_app()
    with app.app_context():
        if User.query.filter(User.email == "initiator@example.com").first():
            print("Demo data already present.")
            return
        pw = bcrypt.generate_password_hash(DEMO_PASSWORD).decode("utf-8")
        users = {}
        for name, email, role in USERS:
            user = User(name=name, email=email, password_hash=pw, role=role)
            db.session.add(user)
            users[email] = user
        db.session.flush()
        for email in ("initiator@example.com", "bh@example.com", "hrm@example.com"):
            db.session.add(UserCompanyScope(user_id=users[email].id, company="RDC"))

        region = ClusterNameMapping(canonical_cluster_name="Demo Region")
        db.session.add(region)
        db.session.flush()
        db.session.add(InitiatorRegion(initiator_id=users["initiator@example.com"].id, cluster_id=region.id))
        db.session.add(BusinessHeadRegion(business_head_id=users["bh@example.com"].id, cluster_id=region.id))

        initiator = users["initiator@example.com"]
        now = datetime.utcnow()
        for i, (candidate, designation, plant, status, days_ago) in enumerate(REQUESTS, start=1):
            email = f"{candidate.split()[0].lower()}.demo@example.com"
            form = {
                "company_code": "RDC", "associate_name": candidate, "designation": designation,
                "plant_location": plant, "email_id": email, "mobile_number": f"90000000{i:02d}",
            }
            req = OnboardingRequest(
                initiated_by=initiator.id, status=status, company_code="RDC",
                candidate_name=candidate, designation=designation, plant_location=plant,
                candidate_email=email, candidate_mobile=form["mobile_number"],
                created_at=now - timedelta(days=days_ago), updated_at=now - timedelta(days=max(days_ago - 1, 0)),
            )
            req._form_data = json.dumps(form)
            db.session.add(req)
            db.session.flush()

            # Record the approvals that already happened on the way to this status.
            for step, (stage, approver, remark) in enumerate(CHAIN):
                reached = [s for s, _, _ in CHAIN]
                if status == RequestStatus.ACTIVE or (stage in reached and reached.index(status) > step
                                                     if status in reached else False):
                    db.session.add(ApprovalAction(
                        request_id=req.id, actor_id=users[approver].id, action=ApprovalActionType.APPROVED,
                        remark=remark, acted_at=now - timedelta(days=max(days_ago - step - 1, 0))))
            if status == RequestStatus.REJECTED_BH:
                db.session.add(ApprovalAction(
                    request_id=req.id, actor_id=users["bh@example.com"].id, action=ApprovalActionType.REJECTED,
                    remark="Headcount for this plant is already full.", acted_at=now - timedelta(days=5)))
        db.session.commit()
        print(f"Created {len(USERS)} demo users and {len(REQUESTS)} requests.")


if __name__ == "__main__":
    main()
