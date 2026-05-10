# Competitor Dashboard (Lead Radar)

Flask application for tracking **publisher domains** and inferring which **ads.txt manager** (competitive signal) is associated with each site. It fetches each domain’s `ads.txt`, parses `managerdomain=` entries, stores results in SQLite, and surfaces trends and change history for your sales team.

## What it does

1. **Imports leads** — Paste domains manually, upload a CSV, or combine both; each row is tagged with a **lead owner** from the configured team list.
2. **Scans `ads.txt`** — For every domain, the app requests `https://{domain}/ads.txt` and `https://www.{domain}/ads.txt`, then parses `managerdomain=` lines. Outcomes include resolved manager domain(s), **In-House** (valid ads.txt but no manager line), **No Ads.txt Found**, connection errors, or firewall/HTML interference.
3. **Two-phase scan engine** (main `app.py`):
   - **Phase 1 — Fast HTTP:** Uses [scrapling](https://github.com/) `Fetcher` with stealth-oriented headers (high concurrency, no browser).
   - **Phase 2 — Stealth browser:** Domains that appear **blocked by a firewall** (e.g. Cloudflare) are retried with `AsyncStealthySession` (headless Playwright, Cloudflare solving enabled), in bounded chunks.
4. **Dashboard** — Charts for manager distribution and recent trends, activity feed of manager changes, optional **per-owner** views (`/dashboard/<owner>`), and a short list of recent firewall-blocked domains.
5. **Leads workspace** — Paginated table with search, firewall filter, bulk scan / rescan firewall-only / delete / reassign owner, and CSV export.
6. **Email alerts** — Selected history rows can be emailed as HTML via SMTP. Configure sender and password with environment variables (see below).

## Configuration (environment variables)

| Variable | Purpose |
|----------|---------|
| `FLASK_SECRET_KEY` | Flask session signing (required for production) |
| `RADAR_USERNAME` | Login username (default `admin`) |
| `RADAR_PASSWORD` | Login password (default `admin`) |
| `SMTP_SENDER` | From address for email reports (default `noreply@example.com`) |
| `SMTP_PASSWORD` | SMTP / app password for sending mail |

Team names and addresses are defined in `TEAM_EMAILS` in `app.py` (and the smaller example set in `simple_scanner.py`); replace the placeholder `example.com` roster with your own.

Template with empty values: **`env.example`**. Copy the variables into your shell, a local `.env` (gitignored—see `.gitignore`), or your deployment settings.

## Tech stack

| Layer | Choice |
|--------|--------|
| Web | Flask 3, Jinja2 templates, Chart.js (CDN), Feather icons |
| Data | SQLite file `radar.db` next to `app.py` |
| Fetching | scrapling (`Fetcher`, `AsyncStealthySession`) |
| Browser automation | Playwright / patchright (for stealth phase) |
| Legacy alternate | `simple_scanner.py` — older variant using `requests` only; **use `app.py` as the main entrypoint** |

## Project layout

```
competitor-dashboard/
├── app.py              # Main application
├── simple_scanner.py   # Legacy scanner variant (requests-based)
├── trick_db.py         # Dev helper script for local DB experiments
├── env.example         # Environment variable names (no secrets)
├── requirements.txt
├── radar.db            # SQLite DB (created on first run if missing)
└── templates/
    ├── login.html
    ├── dashboard.html
    ├── leads.html
    ├── upload.html
    └── scanning.html
```

## Data model

SQLite tables (created in `init_db()`):

- **`domains`** — `domain` (PK), `current_manager`, `last_scanned`, `owner`, `status`
- **`history`** — Append-only change log: `domain`, `old_manager`, `new_manager`, `date`, `owner`

When a scan finds a **real** manager change (excluding noisy error states), a row is inserted into `history`.

## Running locally

```bash
cd competitor-dashboard
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Install browser binaries used by scrapling/Playwright if the installer does not do it automatically (follow scrapling/Playwright docs for your OS).

```bash
export FLASK_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
python app.py
```

Default server: **http://127.0.0.1:5000** (Flask `debug=True` in `__main__` — turn off for production).

Default login when env vars are unset: **admin** / **admin** — override with `RADAR_USERNAME` and `RADAR_PASSWORD`.

## Main HTTP routes (overview)

| Area | Routes |
|------|--------|
| Auth | `/login`, `/logout` |
| Dashboard | `/`, `/dashboard`, `/dashboard/<owner>` |
| Leads | `/leads`, `/leads/export_csv` |
| Ingest | `/upload`, `/add_manual` |
| Scanning | `/scan_all`, `/scan_owner/<owner>`, `/scan_selected`, `/scan_firewall`, `/scanning`, `/api/scan_status` |
| APIs | `/api/domain_history/<domain>`, `/api/domains/<manager>`, `/api/trends/<manager>` |
| Maintenance | `/delete_*`, `/change_owner`, `/send_report`, … |

Scans run **in a background thread**; the UI polls `/api/scan_status` for progress.

## Security notes

- Do not commit real passwords or API keys; use the environment variables above and keep `.env` out of git (see `.gitignore`).
- Disable Flask debug mode and use a production WSGI server when exposing the app beyond localhost.
- The SQLite file contains your lead data — exclude it from public repos (see `.gitignore`).
- **Historical commits:** If this project ever contained real credentials, rotate them in your provider (e.g. Gmail app passwords) even after scrubbing the tree — Git history may still contain old blobs until rewritten.

## Related file

- **`simple_scanner.py`** — Self-contained Flask app using synchronous `requests`. It does not include the two-phase stealth pipeline or several routes present in `app.py`. Both entrypoints use the filename `radar.db` (with `simple_scanner`, the DB is created relative to the current working directory).
