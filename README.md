# RDC Associates Onboarding

A role-based web application that moves a new hire from **"we want to hire this person"** to **"registered in the attendance system"** through a configurable, multi-step approval workflow. An initiator fills a three-step form, the request travels through Business Head, HR and final approvers, and on final approval the employee is pushed automatically to the attendance platform (Truein). A headcount dashboard and a staffing-norms gate stop hiring above the allowed headcount unless the exception is explicitly justified.

> **About the screenshots:** they were taken from a local demo database filled by [`seed_demo.py`](seed_demo.py) with fictional people. No real candidate or employee data is in this repository.

![Approver dashboard](docs/screenshots/04-approver-dashboard.png)

## Features

- **Configurable 3-step onboarding form** (personal info, employment, bank and documents). Admins add, reorder (drag and drop), edit and hide fields, and manage dropdown options, designations and plants, with no code change.
- **Approval workflow as a state machine** with three paths:
  - *RDC standard:* Initiator → Business Head → HR Manager → Head HR → Active.
  - *RDC over-norm exception:* when the staffing gate blocks a hire and the initiator proceeds anyway, the request goes Business Head → Head HR → final approver, and each approver must write a longer justification.
  - *Ultrafine / ROBO:* one fixed five-step chain that always ends with the final approver.
  - Any approver can reject; the initiator edits and resubmits.
- **Region and company routing.** Initiators and Business Heads are linked by region, and each user is ticked for the companies they may act on. Access is fail-closed: with no company ticked, a user can act on nothing.
- **Attendance-system push (Truein).** On final approval the employee is registered through the Truein API. The push validates mobile numbers, derives site, category and department from plant mappings, retries in the background on transient failures, and shows exactly which fields were dropped and why. A pre-flight check warns approvers *before* the final approval, and an admin dry-run writes the payload to a file without sending it.
- **Live duplicate checks** on the form: e-mail (with one-time-password verification), Aadhaar number and mobile number are checked against existing requests and the attendance system as you type.
- **Staffing-norms hiring gate.** Current headcount, reconciled from ZingHR and Truein, is compared with the headcount allowed by plant production volume (pulled from the Daily Volume Tracker). The result is a read-only dashboard by region and plant, and a gate that blocks over-norm hiring requests.
- **Notifications.** In-app notifications for every step, asynchronous e-mail with per-user preferences (all, or hiring-only) and an optional daily digest.
- **Documents.** Uploads are validated by extension and magic bytes, stored under random names, and served only to authorised users, with single-file and download-all (.zip) options.
- **Audit log and Excel reports.** An append-only audit trail of authentication, request, admin and export events, plus a filterable Excel export with preview.
- **Security by default.** bcrypt passwords, CSRF protection, rate limiting, session timeouts, strict security headers (a Content-Security-Policy that allows no external hosts), and public tokens instead of sequential IDs in URLs.

| New request form | Request detail with workflow status |
|---|---|
| ![New request form](docs/screenshots/03-new-request-form.png) | ![Request detail](docs/screenshots/05-request-detail.png) |

| Initiator dashboard | Admin: users |
|---|---|
| ![Initiator dashboard](docs/screenshots/02-initiator-dashboard.png) | ![Admin users](docs/screenshots/06-admin-users.png) |

| Audit log | Login |
|---|---|
| ![Audit log](docs/screenshots/07-admin-audit-log.png) | ![Login](docs/screenshots/01-login.png) |

## Tech stack

Python, Flask (application factory and blueprints), SQLAlchemy, MySQL in production and SQLite for tests, Flask-Login, Flask-WTF, Flask-Limiter, Flask-Mail, Flask-Bcrypt, openpyxl, RapidFuzz (plant-name matching), pytest. All front-end assets, including fonts and SortableJS, are vendored under `app/static/` rather than loaded from a CDN.

## Project structure

```
app/
  __init__.py       application factory, security headers, auto-migration of new columns
  models.py         users, requests, approvals, audit log, staffing norms, mappings
  utils.py          workflow state machine (TRANSITIONS), permissions, audit and notification helpers
  auth/ main/ requests_bp/ admin/ exports/ profile/   blueprints
  integrations/     truein.py (attendance push), zinghr.py (headcount), dvt.py (production volume)
  services/         headcount reconciliation, staffing norms, plant matching, digest e-mail
  templates/ static/
tests/              362 pytest tests (in-memory SQLite, no MySQL needed)
seed.py             creates tables, the admin user, plants, designations and form fields
seed_demo.py        fictional users and requests for demos and screenshots
*.py (root)         one-off migration and backfill scripts
```

## Getting started

Requires Python 3.10 or newer and MySQL 8 (SQLite also works for a local trial).

```bash
git clone https://github.com/jarjishSiddibapa/rdc_onboarding.git
cd rdc_onboarding
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

1. Copy `.env.example` to `.env` and fill it in (see the table below).
2. Create the schema and base data. **This drops every table in the configured database**, so it asks for `--yes`:

   ```bash
   python seed.py --yes
   ```

   It creates a `SUPER_ADMIN` user with the e-mail from `ADMIN_SEED_EMAIL` and the password from `ADMIN_SEED_PASSWORD`. Change that password after the first login.
3. *(Optional)* Add fictional demo users and requests (all share the password defined in the script), only on a throwaway database:

   ```bash
   python seed_demo.py
   ```
4. Run the app and open <http://localhost:5000>:

   ```bash
   python run.py
   ```

To try it without MySQL, set `DATABASE_URL=sqlite:///demo.db`.

### Configuration

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Required. For example `mysql+mysqlconnector://user:pass@host/dbname` |
| `SECRET_KEY` | Flask session secret |
| `ADMIN_SEED_PASSWORD`, `ADMIN_SEED_EMAIL` | First admin account created by `seed.py` |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USER`, `EMAIL_PASS`, `EMAIL_FROM` | Optional. Without them, e-mail is skipped silently |
| `TRUEIN_ACCESS_KEY`, `TRUEIN_SECRET_KEY`, `TRUEIN_SUBSCRIPTION_KEY` | Required for any Truein call (push, dry-run, manager lookup) |
| `ZINGHR_CLIENT_ID`, `ZINGHR_CLIENT_SECRET` | Required for the headcount snapshot refresh |
| `DVT_BASE_URL`, `DVT_USERNAME`, `DVT_PASSWORD` | Required for the production-volume pull used by the staffing norms |

Integration credentials have no defaults. Without them the app runs, but Truein, ZingHR and volume calls fail with authentication errors, and the headcount refresh logs a failure at start-up.

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

## Notes

- The workflow rules live in one place: the `TRANSITIONS` table and `can_act_on()` in `app/utils.py`.
- New database columns are added to existing databases automatically at start-up by `_auto_migrate()` in `app/__init__.py`.
- Soft-delete is used for plants, designations, form fields and requests, so queries must filter on `is_deleted`.
- The integrations are written against the live Truein and ZingHR APIs, so they are tested with mocks, not against the real services.

## Author

Built by [Jarjish Siddibapa](https://github.com/jarjishSiddibapa) at RDC Concrete. See also the [portfolio](https://jarjishsiddibapa.github.io/jarjish-portfolio-website/).
