from flask import Flask, render_template, request, redirect, url_for, jsonify, session, Response, stream_with_context
import sqlite3
import csv
import io
import asyncio
from scrapling.fetchers import Fetcher, StealthyFetcher, StealthySession, AsyncStealthySession
from concurrent.futures import ThreadPoolExecutor, as_completed
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from functools import wraps
import re
import threading
import os

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")

# --- EMAIL (use environment variables in production) ---
EMAIL_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_SENDER = os.environ.get("SMTP_SENDER", "noreply@example.com")

# Example team roster — replace with your organization’s names and addresses
TEAM_EMAILS = {
    "Alice": "alice.sales@example.com",
    "Bob": "bob.sales@example.com",
    "Carol": "carol.sales@example.com",
    "Dana": "dana.sales@example.com",
    "Ellis": "ellis.sales@example.com",
    "Frank": "frank.sales@example.com",
    "Grace": "grace.sales@example.com",
    "Harper": "harper.sales@example.com",
    "Ivy": "ivy.sales@example.com",
    "Jordan": "jordan.sales@example.com",
    "Alerts": '"#notifications" <channel-placeholder@example.com>',
}

# --- APP CONFIG ---
USERNAME = os.environ.get("RADAR_USERNAME", "admin")
PASSWORD = os.environ.get("RADAR_PASSWORD", "admin")
# Use absolute path so the same DB is always used regardless of cwd (fixes "missing" data when run from another directory)
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(_APP_DIR, "radar.db")
SALES_TEAM = list(TEAM_EMAILS.keys())
LEAD_OWNER_TEAM = [name for name in SALES_TEAM if name != "Alerts"]

# --- SCAN PROGRESS STATE ---
scan_state = {
    "running": False,
    "total": 0,
    "current": 0,
    "domain": "",
    "redirect_to": "/dashboard",
    "lock": threading.Lock()
}


def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS domains 
                 (domain TEXT PRIMARY KEY, current_manager TEXT, last_scanned TEXT, owner TEXT, status TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS history 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, old_manager TEXT, new_manager TEXT, date TEXT, owner TEXT)''')
    conn.commit()
    conn.close()


init_db()


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# --- 🚀 ENGINE V11: TWO-PHASE FAST + STEALTH FALLBACK ---

def _parse_adstxt(text):
    """Parse ads.txt content and return manager domain(s) or status string."""
    lower_text = text.lower()
    block_keywords = ["verify you are human", "access denied", "security check",
                      "cloudflare", "attention required", "just a moment"]
    if any(k in lower_text for k in block_keywords) and "managerdomain" not in lower_text:
        return "Blocked by Firewall"
    if "<!doctype html" in lower_text or "<html" in lower_text or "<body" in lower_text:
        return "Page Error (HTML)"
    pattern = r"managerdomain\s*=\s*([^#\s\r\n,]+)"
    matches = re.findall(pattern, text, re.IGNORECASE)
    if matches:
        found_managers = set()
        for m in matches:
            clean_mgr = m.strip().replace('"', '').replace("'", "").lower()
            if clean_mgr:
                found_managers.add(clean_mgr)
        if found_managers:
            return ", ".join(sorted(list(found_managers)))
    is_valid_adstxt = "direct" in lower_text or "reseller" in lower_text or "google.com" in lower_text
    if is_valid_adstxt:
        return "In-House"
    return "Scan Error (Invalid Content)"


def fetch_domain_fast(domain):
    """Phase 1: Lightweight HTTP fetch — no browser, very fast. Returns (domain, result)."""
    domain_str = domain.strip().lower()
    clean_domain = domain_str.replace("http://", "").replace("https://", "").split('/')[0]
    urls_to_try = [
        f"https://{clean_domain}/ads.txt",
        f"https://www.{clean_domain}/ads.txt",
    ]
    with scan_state["lock"]:
        scan_state["domain"] = f"[fast] {clean_domain}"

    fetcher = Fetcher(auto_match=False)
    for url in urls_to_try:
        try:
            page = fetcher.get(url, timeout=10, stealthy_headers=True)
            if page is None:
                continue
            status_code = getattr(page, 'status', None)
            if status_code == 404:
                with scan_state["lock"]:
                    scan_state["current"] += 1
                return (domain_str, "No Ads.txt Found")
            raw = page.body if hasattr(page, 'body') else str(page)
            text = raw.decode('utf-8', errors='ignore') if isinstance(raw, bytes) else (raw or '')
            result = _parse_adstxt(text)
            if result not in ["Page Error (HTML)", "Scan Error (Invalid Content)", "Blocked by Firewall"]:
                with scan_state["lock"]:
                    scan_state["current"] += 1
                return (domain_str, result)
            if result == "Blocked by Firewall":
                return (domain_str, "Blocked by Firewall")  # escalate — don't increment yet
        except Exception:
            continue
    with scan_state["lock"]:
        scan_state["current"] += 1
    return (domain_str, "Connection Error")


async def fetch_domain_stealth_async(domain, session, sem, results):
    """Async Phase 2: Full stealth headless browser for Cloudflare-protected sites."""
    async with sem:
        domain_str = domain.strip().lower()
        clean_domain = domain_str.replace("http://", "").replace("https://", "").split('/')[0]
        urls_to_try = [
            f"https://{clean_domain}/ads.txt",
            f"https://www.{clean_domain}/ads.txt",
        ]
        
        final_result = "Connection Error"
        with scan_state["lock"]:
            scan_state["domain"] = f"[stealth] {clean_domain}"
        
        for url in urls_to_try:
            try:
                page = await session.fetch(url, network_idle=True, timeout=25000)
                if page is None:
                    continue
                
                status_code = getattr(page, 'status', None)
                if status_code == 404:
                    final_result = "No Ads.txt Found"
                    break
                elif status_code in [401, 403] and "cloudflare" not in getattr(page, 'body', b'').decode('utf-8', 'ignore').lower() and "just a moment" not in getattr(page, 'body', b'').decode('utf-8', 'ignore').lower():
                    pass

                raw = page.body if hasattr(page, 'body') else str(page)
                text = raw.decode('utf-8', errors='ignore') if isinstance(raw, bytes) else (raw or '')
                result = _parse_adstxt(text)

                if status_code == 404 and result == "Blocked by Firewall":
                    final_result = "No Ads.txt Found"
                    break

                if result not in ["Page Error (HTML)", "Scan Error (Invalid Content)"]:
                    final_result = result
                    break
            except Exception:
                continue
                
        results[domain_str] = final_result
        with scan_state["lock"]:
            scan_state["current"] += 1
            print(f"[Stealth Async] {domain_str} -> {final_result}")


def run_batch_scan_with_progress(db_rows, redirect_to="/dashboard"):
    """
    Two-phase scan:
      Phase 1: Fast plain-HTTP fetch for all domains (no browser — very fast).
      Phase 2: Full stealth headless browser ONLY for domains that were blocked by a firewall.
    """
    domains = [r[0] for r in db_rows]
    total = len(domains)
    results = {}

    with scan_state["lock"]:
        scan_state["running"] = True
        scan_state["total"] = total
        scan_state["current"] = 0
        scan_state["domain"] = ""
        scan_state["redirect_to"] = redirect_to

    try:
        # ── PHASE 1: Fast HTTP scan (no browser) ──────────────────────────────
        print(f"[Phase 1] Fast HTTP scan for {total} domains...")
        blocked_domains = []
        with ThreadPoolExecutor(max_workers=30) as executor:
            futures = {executor.submit(fetch_domain_fast, d): d for d in domains}
            for future in as_completed(futures):
                domain_str, result = future.result()
                results[domain_str] = result
                if result == "Blocked by Firewall":
                    blocked_domains.append(domain_str)
                else:
                    print(f"[Fast] {domain_str} -> {result}")

        # ── PHASE 2: Stealth browser only for blocked domains ─────────────────
        if blocked_domains:
            print(f"[Phase 2] Stealth scan for {len(blocked_domains)} blocked domain(s)...")
            async def _run_async_scan():
                sem = asyncio.Semaphore(10)
                async with AsyncStealthySession(headless=True, solve_cloudflare=True) as session:
                    chunk_size = 50
                    for i in range(0, len(blocked_domains), chunk_size):
                        chunk = blocked_domains[i:i + chunk_size]
                        tasks = [
                            asyncio.create_task(fetch_domain_stealth_async(d, session, sem, results))
                            for d in chunk
                        ]
                        await asyncio.gather(*tasks)
                        await asyncio.sleep(1)
            asyncio.run(_run_async_scan())
        else:
            print("[Phase 2] No blocked domains — skipping stealth browser entirely.")

        # Write results to DB
        print("Writing results to DB...")
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        timestamp_full = timestamp

        for row in db_rows:
            domain, old_mgr, owner = row[0], row[1], row[2]
            new_mgr = results.get(domain.strip().lower(), "Connection Error")
            if new_mgr is None:
                new_mgr = "Connection Error"
            
            c.execute("UPDATE domains SET current_manager = ?, last_scanned = ? WHERE domain = ?",
                      (new_mgr, timestamp_full, domain))
            
            if "New" not in old_mgr and old_mgr != new_mgr and "Error" not in new_mgr and "Blocked" not in new_mgr:
                c.execute("INSERT INTO history (domain, old_manager, new_manager, date, owner) VALUES (?, ?, ?, ?, ?)",
                          (domain, old_mgr, new_mgr, timestamp, owner))

        conn.commit()
        conn.close()
        print("Scan finished successfully.")

    except Exception as e:
        print(f"CRITICAL ERROR in stealth batch scan: {e}")
    finally:
        with scan_state["lock"]:
            scan_state["running"] = False
            scan_state["current"] = total
            scan_state["domain"] = "Scan Complete"


# --- ROUTES ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['username'] == USERNAME and request.form['password'] == PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    return redirect(url_for('dashboard'))


@app.route('/dashboard')
@app.route('/dashboard/<owner>')
@login_required
def dashboard(owner=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    badges = {}
    for member in LEAD_OWNER_TEAM:
        try:
            badges[member] = c.execute("SELECT COUNT(*) FROM history WHERE owner = ? AND date >= ?",
                                       (member, seven_days_ago)).fetchone()[0]
        except:
            badges[member] = 0

    params = [owner] if (owner and owner != "Global") else []
    where_clause = "WHERE owner = ?" if (owner and owner != "Global") else ""

    try:
        total = c.execute(f"SELECT COUNT(*) FROM domains {where_clause}", params).fetchone()[0]
        hist_where = "WHERE owner = ?" if (owner and owner != "Global") else ""
        changes_count = c.execute(f"SELECT COUNT(*) FROM history {hist_where}", params).fetchone()[0]

        raw_leaderboard = c.execute(f'''SELECT current_manager, COUNT(*) as count FROM domains {where_clause} 
                                    {'AND' if where_clause else 'WHERE'} current_manager NOT LIKE '%New%'
                                    AND current_manager != 'Pending'
                                    GROUP BY current_manager ORDER BY count DESC''', params).fetchall()

        chart_labels = [row[0] for row in raw_leaderboard]
        chart_data = [row[1] for row in raw_leaderboard]

        trends = c.execute(f'''SELECT new_manager, COUNT(*) as count FROM history {hist_where}
                        GROUP BY new_manager ORDER BY count DESC LIMIT 5''', params).fetchall()

        # Firewall Blocks List
        firewall_blocks = c.execute(f"SELECT domain, owner, last_scanned FROM domains {where_clause} {'AND' if where_clause else 'WHERE'} (current_manager LIKE '%Blocked%' OR current_manager LIKE '%Firewall%') ORDER BY last_scanned DESC LIMIT 20", params).fetchall()

        raw_activity = c.execute(f"SELECT * FROM history {hist_where} ORDER BY id DESC LIMIT 50", params).fetchall()

        formatted_activity = []
        for row in raw_activity:
            try:
                dt = datetime.strptime(row[4], "%Y-%m-%d %H:%M")
                nice_date = dt.strftime("%b %d, %I:%M %p")
            except Exception:
                nice_date = row[4]
            formatted_activity.append(
                {"id": row[0], "domain": row[1], "old": row[2], "new": row[3], "date": nice_date, "owner": row[5]})
    except Exception:
        total = 0
        changes_count = 0
        formatted_activity = []
        chart_labels = []
        chart_data = []
        trends = []
        firewall_blocks = []

    conn.close()

    return render_template('dashboard.html',
                           owner=owner, team=LEAD_OWNER_TEAM, report_recipients=SALES_TEAM, badges=badges,
                           total=total, changes_count=changes_count,
                           activity=formatted_activity, labels=chart_labels, data=chart_data,
                           trend_labels=[r[0] for r in trends], trend_data=[r[1] for r in trends],
                           firewall_blocks=firewall_blocks, firewall_count=len(firewall_blocks))


@app.route('/add_manual', methods=['POST'])
@login_required
def add_manual():
    raw_text = request.form.get('domains', '')
    owner_name = request.form.get('owner')
    if not raw_text: return redirect(url_for('dashboard'))

    domains = [d.strip() for d in raw_text.splitlines() if d.strip()]
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    for domain in domains:
        clean_domain = domain.replace("https://", "").replace("http://", "").split('/')[0]
        c.execute(
            "INSERT OR IGNORE INTO domains (domain, current_manager, last_scanned, owner, status) VALUES (?, ?, ?, ?, ?)",
            (clean_domain, 'New / Pending', 'Never', owner_name, 'Pending'))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', owner=owner_name if owner_name != "Global" else None))


@app.route('/api/domain_history/<path:domain>')
@login_required
def api_domain_history(domain):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Fetch all change history for this exact domain
    history = c.execute("SELECT old_manager, new_manager, date FROM history WHERE domain = ? ORDER BY id DESC", (domain,)).fetchall()
    conn.close()
    
    result = [{"old": h[0], "new": h[1], "date": h[2]} for h in history]
    return jsonify(result)


@app.route('/delete_activities', methods=['POST'])
@login_required
def delete_activities():
    data = request.json
    ids = data.get('ids', [])
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    placeholders = ','.join(['?'] * len(ids))
    c.execute(f"DELETE FROM history WHERE id IN ({placeholders})", ids)
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})


@app.route('/send_report', methods=['POST'])
@login_required
def send_report():
    data = request.json
    recipient_names = data.get('recipients', [])
    activity_ids = data.get('ids', [])

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    placeholders = ','.join(['?'] * len(activity_ids))
    rows = c.execute(f"SELECT domain, old_manager, new_manager, owner, date FROM history WHERE id IN ({placeholders})",
                     activity_ids).fetchall()
    conn.close()

    to_emails = [TEAM_EMAILS[name] for name in recipient_names if name in TEAM_EMAILS]
    if not to_emails: return jsonify({"status": "error", "message": "Invalid recipients"})

    html_rows = ""
    for r in rows:
        html_rows += f"""
        <tr>
            <td style="padding:12px 10px; border-bottom:1px solid #eee;">
                <a href="http://{r[0]}" style="color:#2563eb; text-decoration:underline;">{r[0]}</a>
            </td>
            <td style="padding:12px 10px; border-bottom:1px solid #eee;">
                {r[1]} &rarr; <b style="color:#2563eb;">{r[2]}</b>
            </td>
            <td style="padding:12px 10px; border-bottom:1px solid #eee;">{r[3]}</td>
            <td style="padding:12px 10px; border-bottom:1px solid #eee; color:#6b7280;">{r[4]}</td>
        </tr>
        """

    html_content = f"""
    <html>
    <body style="font-family:Arial, sans-serif; color:#333; margin:0; padding:20px;">
        <h2 style="color:#5b45ff; margin-bottom: 20px;">Competitor activity report</h2>
        <table style="width:100%; border-collapse:collapse; text-align:left; font-size:14px;">
            <thead>
                <tr style="background-color:#f9fafb; color:#6b7280; font-size:13px;">
                    <th style="padding:12px 10px; border-bottom: 2px solid #eee;">Domain</th>
                    <th style="padding:12px 10px; border-bottom: 2px solid #eee;">Change</th>
                    <th style="padding:12px 10px; border-bottom: 2px solid #eee;">Owner</th>
                    <th style="padding:12px 10px; border-bottom: 2px solid #eee;">Date</th>
                </tr>
            </thead>
            <tbody>
                {html_rows}
            </tbody>
        </table>
    </body>
    </html>
    """

    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_SENDER
        msg['To'] = ", ".join(to_emails)
        msg['Subject'] = "Competitor activity alert"
        msg.attach(MIMEText(html_content, 'html'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, to_emails, msg.as_string())
        server.quit()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# --- LEADS VIEW (paginated, selectable, CSV export) ---
LEADS_PER_PAGE = 50

@app.route('/leads')
@login_required
def leads_view():
    search = request.args.get('search', '').strip()
    view_filter = request.args.get('filter', '').strip()
    page = int(request.args.get('page', 1))
    offset = (page - 1) * LEADS_PER_PAGE

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    query_parts = []
    params = []
    
    if search:
        query_parts.append("(domain LIKE ? OR owner LIKE ?)")
        params.extend([f'%{search}%', f'%{search}%'])
    
    if view_filter == 'firewall':
        query_parts.append("(current_manager LIKE '%Blocked%' OR current_manager LIKE '%Firewall%')")
    
    where_clause = " WHERE " + " AND ".join(query_parts) if query_parts else ""
    
    total_count = c.execute(f"SELECT COUNT(*) FROM domains{where_clause}", params).fetchone()[0]
    rows = c.execute(f"SELECT * FROM domains{where_clause} ORDER BY rowid DESC LIMIT ? OFFSET ?",
                     params + [LEADS_PER_PAGE, offset]).fetchall()

    firewall_count = c.execute("SELECT COUNT(*) FROM domains WHERE current_manager LIKE '%Blocked%' OR current_manager LIKE '%Firewall%'").fetchone()[0]
    conn.close()

    total_pages = max(1, (total_count + LEADS_PER_PAGE - 1) // LEADS_PER_PAGE)
    return render_template('leads.html', rows=rows, search=search, filter=view_filter, team=LEAD_OWNER_TEAM,
                           page=page, total_pages=total_pages, total_count=total_count,
                           firewall_count=firewall_count)


@app.route('/leads/export_csv')
@login_required
def export_csv():
    search = request.args.get('search', '').strip()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if search:
        rows = c.execute("""SELECT d.domain, d.current_manager, 
                             (SELECT h.old_manager FROM history h WHERE h.domain = d.domain ORDER BY h.id DESC LIMIT 1) as prev_manager,
                             d.last_scanned, d.owner 
                             FROM domains d 
                             WHERE d.domain LIKE ? OR d.owner LIKE ?""",
                         (f'%{search}%', f'%{search}%')).fetchall()
    else:
        rows = c.execute("""SELECT d.domain, d.current_manager, 
                             (SELECT h.old_manager FROM history h WHERE h.domain = d.domain ORDER BY h.id DESC LIMIT 1) as prev_manager,
                             d.last_scanned, d.owner 
                             FROM domains d 
                             ORDER BY d.rowid DESC""").fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Domain", "Current Manager", "Previous Manager", "Last Scanned", "Owner"])
    writer.writerows(rows)
    output.seek(0)

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={"Content-Disposition": "attachment; filename=leads_export.csv"}
    )


@app.route('/scan_selected', methods=['POST'])
@login_required
def scan_selected():
    """Scan only the user-selected domains from the leads page."""
    if scan_state["running"]:
        return jsonify({"status": "error", "message": "A scan is already running."})

    domains = request.json.get('domains', [])
    if not domains:
        return jsonify({"status": "error", "message": "No domains selected."})

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    placeholders = ','.join(['?'] * len(domains))
    db_rows = c.execute(f"SELECT domain, current_manager, owner FROM domains WHERE domain IN ({placeholders})",
                        domains).fetchall()
    conn.close()

    redirect_to = "/leads"
    t = threading.Thread(target=run_batch_scan_with_progress, args=(db_rows, redirect_to), daemon=True)
    t.start()
    return jsonify({"status": "ok", "total": len(db_rows)})


@app.route('/scan_firewall', methods=['POST'])
@login_required
def scan_firewall():
    """Bulk rescan all domains that are blocked by firewalls."""
    if scan_state["running"]:
        return jsonify({"status": "error", "message": "A scan is already running."})

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    db_rows = c.execute("SELECT domain, current_manager, owner FROM domains WHERE current_manager LIKE '%Blocked%' OR current_manager LIKE '%Firewall%'").fetchall()
    conn.close()

    if not db_rows:
        return jsonify({"status": "error", "message": "No blocked domains to scan."})

    t = threading.Thread(target=run_batch_scan_with_progress, args=(db_rows, "/leads"), daemon=True)
    t.start()
    return jsonify({"status": "ok", "total": len(db_rows)})


@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'GET': return render_template('upload.html', team=LEAD_OWNER_TEAM)
    file = request.files['file']
    owner_name = request.form.get('owner_name')
    if file.filename == '': return redirect(url_for('dashboard'))
    stream = io.StringIO(file.stream.read().decode("UTF8", errors='ignore'), newline=None)
    rows = list(csv.reader(stream))
    if rows and "domain" in str(rows[0][0]).lower(): rows = rows[1:]
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    for row in rows:
        if row and row[0].strip():
            c.execute(
                "INSERT OR IGNORE INTO domains (domain, current_manager, last_scanned, owner, status) VALUES (?, ?, ?, ?, ?)",
                (row[0].strip(), 'New / Pending', 'Never', owner_name, 'Pending'))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', owner=owner_name if owner_name != "Global" else None))


# --- SCAN ROUTES (background + progress) ---

def _start_scan_background(db_rows, redirect_to):
    t = threading.Thread(target=run_batch_scan_with_progress, args=(db_rows, redirect_to), daemon=True)
    t.start()


@app.route('/scan_all', methods=['POST', 'GET'])
@login_required
def scan_all():
    if scan_state["running"]:
        if request.method == 'POST': return jsonify({"status": "error", "message": "A scan is already running."})
        return redirect(url_for('dashboard'))

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    db_rows = c.execute("SELECT domain, current_manager, owner FROM domains").fetchall()
    conn.close()

    _start_scan_background(db_rows, "/dashboard")
    
    if request.method == 'POST': return jsonify({"status": "ok"})
    return redirect(url_for('dashboard'))


@app.route('/scan_owner/<owner>', methods=['POST', 'GET'])
@login_required
def scan_owner(owner):
    if scan_state["running"]:
        if request.method == 'POST': return jsonify({"status": "error", "message": "A scan is already running."})
        return redirect(url_for('dashboard', owner=owner))

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    db_rows = c.execute(
        "SELECT domain, current_manager, owner FROM domains WHERE owner = ?", (owner,)
    ).fetchall()
    conn.close()

    if not db_rows:
        if request.method == 'POST': return jsonify({"status": "error", "message": "No domains found."})
        return redirect(url_for('dashboard', owner=owner))

    _start_scan_background(db_rows, f"/dashboard/{owner}")
    
    if request.method == 'POST': return jsonify({"status": "ok"})
    return redirect(url_for('dashboard', owner=owner))


@app.route('/scanning')
@login_required
def scanning_page():
    return render_template('scanning.html')


@app.route('/api/scan_status')
@login_required
def scan_status():
    with scan_state["lock"]:
        state = {
            "running": scan_state["running"],
            "total": scan_state["total"],
            "current": scan_state["current"],
            "domain": scan_state["domain"],
            "redirect_to": scan_state["redirect_to"],
            "percent": round((scan_state["current"] / scan_state["total"]) * 100)
                if scan_state["total"] > 0 else 0
        }
    return jsonify(state)


@app.route('/delete_selected_domains', methods=['POST'])
@login_required
def delete_selected_domains():
    """Bulk delete domains from the leads table."""
    data = request.json
    domains = data.get('domains', [])
    if not domains: return jsonify({"status": "error", "message": "No domains selected."})
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    placeholders = ','.join(['?'] * len(domains))
    c.execute(f"DELETE FROM domains WHERE domain IN ({placeholders})", domains)
    c.execute(f"DELETE FROM history WHERE domain IN ({placeholders})", domains)
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/change_owner', methods=['POST'])
@login_required
def change_owner():
    """Bulk change owner of selected domains."""
    data = request.json
    domains = data.get('domains', [])
    new_owner = data.get('new_owner', '').strip()
    if not domains or not new_owner: 
        return jsonify({"status": "error", "message": "Invalid data."})
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    placeholders = ','.join(['?'] * len(domains))
    c.execute(f"UPDATE domains SET owner = ? WHERE domain IN ({placeholders})", [new_owner] + domains)
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route('/delete_domain/<domain>')
@login_required
def delete_domain(domain):
    source = request.args.get('source', 'dashboard')
    owner = request.args.get('owner')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM domains WHERE domain = ?", (domain,))
    c.execute("DELETE FROM history WHERE domain = ?", (domain,))
    conn.commit()
    conn.close()
    if source == 'leads': return redirect(url_for('leads_view'))
    return redirect(url_for('dashboard', owner=owner if owner and owner != "Global" else None))


@app.route('/delete_manager/<manager>')
@login_required
def delete_manager(manager):
    owner = request.args.get('owner')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    query = "DELETE FROM domains WHERE current_manager = ?"
    params = [manager]
    if owner and owner != "Global": query += " AND owner = ?"; params.append(owner)
    c.execute(query, params)

    hist_query = "DELETE FROM history WHERE new_manager = ?"
    hist_params = [manager]
    if owner and owner != "Global": hist_query += " AND owner = ?"; hist_params.append(owner)
    c.execute(hist_query, hist_params)

    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', owner=owner if owner and owner != "Global" else None))


@app.route('/delete_list/<owner>')
@login_required
def delete_list(owner):
    if owner == "Global": return redirect(url_for('dashboard'))
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM domains WHERE owner = ?", (owner,))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', owner=owner))


@app.route('/delete_all')
@login_required
def delete_all():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM domains")
    c.execute("DELETE FROM history")
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))


@app.route('/api/domains/<manager>')
@login_required
def get_domains_by_manager(manager):
    owner = request.args.get('owner')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    query = "SELECT domain, last_scanned, owner FROM domains WHERE current_manager = ?"
    params = [manager]
    if owner and owner != "Global" and owner != "None": query += " AND owner = ?"; params.append(owner)
    domains = c.execute(query, params).fetchall()
    conn.close()
    return jsonify([{'domain': r[0], 'info': r[1], 'owner': r[2]} for r in domains])


@app.route('/api/trends/<manager>')
@login_required
def get_trend_domains(manager):
    owner = request.args.get('owner')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    query = "SELECT domain, date, owner FROM history WHERE new_manager = ?"
    params = [manager]
    if owner and owner != "Global" and owner != "None": query += " AND owner = ?"; params.append(owner)
    domains = c.execute(query, params).fetchall()
    conn.close()
    return jsonify([{'domain': r[0], 'info': r[1], 'owner': r[2]} for r in domains])


if __name__ == '__main__':
    # Restart triggered for Playwright update
    app.run(debug=True, port=5000)