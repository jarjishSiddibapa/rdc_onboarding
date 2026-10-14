# Running RDC Associates Hiring in Docker

The app and its MySQL database run as two containers started by one command. Everything that has to
survive (database, candidate documents, backups) lives in Docker volumes, so the containers themselves
can be rebuilt or thrown away at any time.

| File | Purpose |
|---|---|
| `Dockerfile` | Builds the app image (Python 3.14 + gunicorn, runs as an unprivileged user) |
| `docker-compose.yml` | The app + MySQL 9.5 + the volumes |
| `.env.docker.example` | Template for your settings. Copy to `.env.docker` (git-ignored) |
| `wsgi.py` | Production entry point (`run.py` stays for local development) |
| `.dockerignore` | Keeps secrets, backups and uploads out of the image |
| `docker/export.sh`, `docker/import.sh` | Pack everything into one file / set up a machine from it (section 4) |

Requires Docker Engine 24+ with the Compose plugin (`docker compose version`), or Docker Desktop.

## 1. First start

```bash
cp .env.docker.example .env.docker      # Windows PowerShell: Copy-Item .env.docker.example .env.docker
```

Edit `.env.docker`: set `MYSQL_ROOT_PASSWORD` (and the same password inside `DATABASE_URL`), `SECRET_KEY`,
`APP_BASE_URL`, and copy the e-mail / Truein / ZingHR / DVT values from your current `.env`.

```bash
docker compose up -d --build
docker compose ps                        # both should become "healthy"
docker compose logs -f app               # Ctrl+C leaves it running
```

The app is then at `http://<this-machine>:5000`. On the first start it creates every table itself.

**Brand-new, empty database only:** create the first SUPER_ADMIN with

```bash
docker compose exec app python seed.py --yes
```

`seed.py` DROPS and recreates all tables - never run it on a database that has real data.

## 2. Bring over the data from the current (non-Docker) install

Use this instead of `seed.py`. Stop the old app first so nothing changes while you copy.

1. In the old app: Admin -> Database Backup -> *Back up now*, then download the `.sql` (or take the newest
   file in the `backups` folder).
2. Create `.env.docker` (section 1), then in the project folder run (Git Bash on Windows, or any Linux/macOS shell):
   ```bash
   ./docker/import.sh --sql rdc_onboarding-XXXX.sql --uploads app/static/uploads
   ```
   That starts the database, restores the backup, starts the app and copies in the candidate documents
   (without them the Open / Download links on old requests fail). About 1-2 minutes.

Only run the old and the new copy at the same time for testing with the integration keys blanked: both
would otherwise push approved hires to Truein, send the daily e-mails and run the 02:00 sync.

## 3. Day-to-day

```bash
docker compose up -d --build     # after pulling new code: rebuilds and restarts the app only
docker compose restart app
docker compose logs --tail 200 app
docker compose down              # stop; data is kept
docker compose exec app python set_technician_categories.py   # run any one-off script
```

(The test suite is not in the image; run it from the source folder as before.)

Automatic daily backups keep working (Admin -> Database Backup). They are written to the `backups` volume,
so also copy them off the server occasionally (Download button, or `docker compose cp app:/app/backups/. ./backups-copy`).

## 4. Moving to another machine / server

Everything that makes the install yours - database, candidate documents, Truein cache and `.env.docker` -
goes into ONE file.

**On the old machine** (Docker stack running), in the project folder:
```bash
./docker/export.sh                  # -> rdc-migration-YYYYMMDD-HHMMSS.tar.gz  (~1 minute)
```

**On the new machine** (needs Docker with the Compose plugin, and `git`; nothing else - no Python, no MySQL):
```bash
git clone https://github.com/jarjishSiddibapa/rdc_onboarding.git
cd rdc_onboarding
# copy rdc-migration-XXXX.tar.gz into this folder (scp, encrypted USB, ...)
./docker/import.sh rdc-migration-XXXX.tar.gz
```
The import builds the image, restores the database, copies the documents and the Truein cache, and
re-creates `.env.docker` from the export (so the `SECRET_KEY` stays the same - the Approve/Reject links already
in people's inboxes keep working). It refuses to run over a database that already has data.

Then:
1. Open `http://<new-machine>:5000` and log in as before.
2. Admin -> Email Settings -> **App address**: set it to the new address (or edit `APP_BASE_URL`).
   The buttons in approval e-mails use it.
3. Switch the OLD install off (`docker compose down`) - two live copies would both push hires to Truein.
4. Delete the export file from both machines; it contains personal data and password hashes.

Windows target: use Docker Desktop and run the two commands in Git Bash. Linux/macOS: any shell.

Notes:
- The export file is a snapshot. Anything done on the old machine after it was made is not in it, so stop the
  old app (or accept the gap) before exporting for a real cut-over.
- Without internet access on the new machine, ship the image too: `docker save rdc-hiring:latest mysql:9.5 | gzip > images.tar.gz`
  on the old one and `docker load < images.tar.gz` before `import.sh` (or push the app image to a private registry).
- A manual alternative to the scripts: restore any `.sql` backup with
  `docker compose cp file.sql db:/tmp/r.sql` then `docker compose exec db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" < /tmp/r.sql'`.

## 5. Things worth knowing

- **One worker, on purpose.** The app keeps per-process caches and starts its own nightly-sync / backup /
  digest threads. Do not raise gunicorn's `--workers`; use `--threads` for more concurrency.
- **HTTPS.** Put a reverse proxy (Caddy, nginx, a cloud load balancer) in front, set `APP_BASE_URL=https://...`
  and `TRUSTED_PROXIES=1`. Until then the app is plain HTTP, like the current LAN setup.
- **Client IPs.** On Docker Desktop (Windows / Mac) every visitor can appear to come from the Docker gateway
  address, which makes the per-IP rate limit shared and the audit-log IP less useful. A Linux server keeps real
  addresses (and with a proxy, `TRUSTED_PROXIES` restores them).
- **Time.** The app works out Indian time itself (`app/timeutil.py`) and stores IST in the database, so the container's own clock zone does not matter and the 02:00 sync and 04:00 backup are unaffected. A database restored from a backup taken before 2026-10-14 holds UTC - run `python migrate_to_ist.py` once after restoring it.
- **Database port.** MySQL is not published on a host port. To inspect it:
  `docker compose exec db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" rdc_onboarding'`.
- **Using an existing MySQL server instead of the bundled one:** point `DATABASE_URL` at it, then delete the
  `db` service, its `depends_on`, and the `db_data` volume from `docker-compose.yml`. If MySQL runs on the Docker
  host itself, use host `host.docker.internal` (Docker Desktop) or add `extra_hosts: ["host.docker.internal:host-gateway"]`.
- **Wiping everything** (including the database!): `docker compose down -v`.
