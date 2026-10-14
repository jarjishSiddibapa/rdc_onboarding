"""
One-off: fills the new request_events ledger (2026-10-14) for every request that already
existed, so response-time analysis covers the whole history and not just what happens after
the feature shipped.

Rebuilt from data that was already recorded with an exact time:
  - onboarding_requests.created_at        -> DRAFT_CREATED
  - audit_logs REQUEST_SUBMITTED / RESUBMITTED / DELETED / REQUEST_BLANK_DRAFTS_CLEARED
  - approval_actions (acted_at, actor, remark) -> APPROVED / REJECTED (FLAGGED_SPECIAL for the one
    historical request that used the removed "flag for Dr. Bhoon" button)
  - audit_logs REQUEST_APPROVED/ACTIVATED/REJECTED detail -> from_status / to_status of each decision
  - truein_push_logs (one row per attempt)  -> TRUEIN_PUSHED / TRUEIN_PUSH_FAILED
waiting_since / duration_seconds / cycle_seconds are then worked out exactly like the live code
(utils.record_request_event) does. Every row is tagged detail={"backfilled": true}.
Not recoverable and therefore not created: SPECIAL_CASE_ACKNOWLEDGED (it was never logged).
Actor name/role are the account's CURRENT values (the originals were not stored).

Safe to rerun: an event is skipped if the same request already has the same event type within
5 seconds of it. Additive only - nothing is updated or deleted. Uses a plain database connection
(never create_app(), see CLAUDE.md gotcha #2). Run it BEFORE restarting the app on the new code:

    venv\\Scripts\\python backfill_request_events.py --dry-run     # shows what it would add
    venv\\Scripts\\python backfill_request_events.py               # does it
"""
import json
import os
import sys
from collections import defaultdict
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from sqlalchemy import create_engine, inspect, text   # noqa: E402

from app.models import RequestEvent   # noqa: E402

_STAGE_MARKERS = ("DRAFT_CREATED", "SUBMITTED", "RESUBMITTED", "APPROVED", "REJECTED", "FLAGGED_SPECIAL")
_PENDING_BY_ROLE = {"BUSINESS_HEAD": "PENDING_BH", "HR_MANAGER": "PENDING_HR_MANAGER",
                    "HEAD_HR": "PENDING_HEAD_HR", "DR_BHOON": "PENDING_DR_BHOON"}
_PRIORITY = {"DRAFT_CREATED": 0, "SUBMITTED": 1, "RESUBMITTED": 1, "APPROVED": 2, "REJECTED": 2,
             "FLAGGED_SPECIAL": 2, "TRUEIN_PUSHED": 3, "TRUEIN_PUSH_FAILED": 3, "DRAFT_DELETED": 4}


def _j(raw):
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def build_events(conn):
    users = {r.id: r for r in conn.execute(text("SELECT id, name, role FROM users"))}

    def who(uid):
        u = users.get(uid)
        return (u.id, u.name, u.role) if u else (uid, None, None)

    reqs = list(conn.execute(text(
        "SELECT id, initiated_by, created_at, company_code, is_special_case, status FROM onboarding_requests ORDER BY id")))
    actions = defaultdict(list)
    for r in conn.execute(text("SELECT id, request_id, actor_id, action, remark, acted_at "
                               "FROM approval_actions ORDER BY acted_at, id")):
        actions[r.request_id].append(r)
    audits = defaultdict(list)
    for r in conn.execute(text(
            "SELECT resource_id, action_type, actor_id, detail, created_at FROM audit_logs "
            "WHERE resource_type = 'OnboardingRequest' AND resource_id IS NOT NULL "
            "AND action_type IN ('REQUEST_SUBMITTED','REQUEST_RESUBMITTED','REQUEST_DELETED',"
            "'REQUEST_APPROVED','REQUEST_ACTIVATED','REQUEST_REJECTED') ORDER BY created_at, id")):
        audits[r.resource_id].append(r)
    cleared = defaultdict(list)   # request id -> audit rows of REQUEST_BLANK_DRAFTS_CLEARED that listed it
    for r in conn.execute(text("SELECT detail, created_at FROM audit_logs "
                               "WHERE action_type = 'REQUEST_BLANK_DRAFTS_CLEARED' ORDER BY created_at")):
        for rid in _j(r.detail).get("request_ids", []):
            cleared[rid].append(r)
    pushes = defaultdict(list)
    for r in conn.execute(text("SELECT request_id, attempt_number, triggered_by, emp_id, http_status, success, "
                               "error_message, attempted_at FROM truein_push_logs ORDER BY attempted_at, id")):
        pushes[r.request_id].append(r)

    out = []
    for rq in reqs:
        ev = []   # dicts: type, at, actor(id,name,role)|None, from_status, to_status, remark, via, detail, action_id

        def add(etype, at, actor=None, frm=None, to=None, remark=None, via="web", detail=None, action_id=None):
            d = {"type": etype, "at": at, "actor": actor, "from": frm, "to": to, "remark": remark,
                 "via": via, "detail": dict(detail or {}), "action_id": action_id}
            d["detail"]["backfilled"] = True
            ev.append(d)

        add("DRAFT_CREATED", rq.created_at, who(rq.initiated_by), to="DRAFT")

        for a in audits[rq.id]:
            det = _j(a.detail)
            if a.action_type == "REQUEST_SUBMITTED":
                add("SUBMITTED", a.created_at, who(a.actor_id or rq.initiated_by), frm="DRAFT", to="PENDING_BH",
                    detail={"designation": det.get("designation"), "plant": det.get("plant")})
            elif a.action_type == "REQUEST_RESUBMITTED":
                add("RESUBMITTED", a.created_at, who(a.actor_id or rq.initiated_by),
                    frm=det.get("previous_rejection_status"), to="PENDING_BH",
                    detail={"retry_number": det.get("retry_number")})
            elif a.action_type == "REQUEST_DELETED":
                add("DRAFT_DELETED", a.created_at, who(a.actor_id or rq.initiated_by), frm="DRAFT", to="DRAFT")
        for a in cleared.get(rq.id, []):
            add("DRAFT_DELETED", a.created_at, None, frm="DRAFT", to="DRAFT", via="system",
                detail={"reason": "blank draft cleared"})

        decisions = [a for a in audits[rq.id]
                     if a.action_type in ("REQUEST_APPROVED", "REQUEST_ACTIVATED", "REQUEST_REJECTED")]
        acts = actions[rq.id]
        paired = len(decisions) == len(acts)
        for i, a in enumerate(acts):
            dd = _j(decisions[i].detail) if paired else {}
            frm = dd.get("from_status") or _PENDING_BY_ROLE.get(users[a.actor_id].role if a.actor_id in users else "")
            if a.action == "REJECTED":
                etype = "REJECTED"
            elif a.action == "FLAGGED_SPECIAL":
                etype = "FLAGGED_SPECIAL"
            else:
                etype = "APPROVED"
            add(etype, a.acted_at, who(a.actor_id), frm=frm, to=dd.get("to_status"), remark=a.remark,
                via=dd.get("via", "web"), action_id=a.id)

        for p in pushes[rq.id]:
            ok = bool(p.success)
            add("TRUEIN_PUSHED" if ok else "TRUEIN_PUSH_FAILED", p.attempted_at, None,
                remark=None if ok else p.error_message, via="manual" if p.triggered_by == "manual" else "system",
                detail={"attempt": p.attempt_number, "triggered_by": p.triggered_by, "empId": p.emp_id,
                        "http_status": p.http_status})

        ev.sort(key=lambda d: (d["at"], _PRIORITY[d["type"]]))

        # durations, worked out the same way as the live recorder
        last_marker = last_any = last_submit = None
        attempt = 1
        for d in ev:
            at = d["at"]
            d["waiting_since"] = d["duration"] = d["cycle"] = None
            if d["type"] != "DRAFT_CREATED":
                prev = last_marker if d["type"] in ("SUBMITTED", "RESUBMITTED", "APPROVED", "REJECTED", "FLAGGED_SPECIAL") else last_any
                d["waiting_since"] = prev if prev is not None else rq.created_at
                d["duration"] = max(int((at - d["waiting_since"]).total_seconds()), 0)
            if d["type"] in ("APPROVED", "REJECTED", "FLAGGED_SPECIAL") and last_submit is not None:
                d["cycle"] = max(int((at - last_submit).total_seconds()), 0)
            if d["type"] == "RESUBMITTED":
                attempt += 1
            d["attempt"] = attempt
            last_any = at
            if d["type"] in _STAGE_MARKERS:
                last_marker = at
            if d["type"] in ("SUBMITTED", "RESUBMITTED"):
                last_submit = at

        for d in ev:
            actor = d["actor"]
            out.append(dict(
                request_id=rq.id, event_type=d["type"], occurred_at=d["at"],
                actor_id=actor[0] if actor else None, actor_name=actor[1] if actor else None,
                actor_role=actor[2] if actor else None,
                from_status=d["from"], to_status=d["to"], attempt_no=d["attempt"],
                waiting_since=d["waiting_since"], duration_seconds=d["duration"], cycle_seconds=d["cycle"],
                approval_action_id=d["action_id"], remark=d["remark"], via=d["via"],
                company_code=rq.company_code, is_special_case=bool(rq.is_special_case),
                detail=json.dumps(d["detail"], default=str), ip_address=None))
    return out


def main():
    dry = "--dry-run" in sys.argv
    engine = create_engine(os.environ["DATABASE_URL"])
    table_exists = inspect(engine).has_table("request_events")
    if not table_exists and not dry:
        RequestEvent.__table__.create(engine)
        table_exists = True
    with engine.begin() as conn:
        rows = build_events(conn)
        existing = defaultdict(list)
        if table_exists:
            for r in conn.execute(text("SELECT request_id, event_type, occurred_at FROM request_events")):
                existing[(r.request_id, r.event_type)].append(r.occurred_at)
        new_rows = []
        for r in rows:
            near = existing.get((r["request_id"], r["event_type"]), [])
            if any(abs((r["occurred_at"] - t).total_seconds()) <= 5 for t in near):
                continue
            new_rows.append(r)
        by_type = defaultdict(int)
        for r in new_rows:
            by_type[r["event_type"]] += 1
        print(f"{len(rows)} events reconstructed, {len(rows) - len(new_rows)} already present, {len(new_rows)} to add")
        for k in sorted(by_type):
            print(f"  {k:22s} {by_type[k]}")
        if dry:
            print("dry run - nothing written")
            return
        if new_rows:
            conn.execute(RequestEvent.__table__.insert(), new_rows)
        print("done")


if __name__ == "__main__":
    main()
