from flask import Flask, render_template, request, redirect, url_for, jsonify, session
import sqlite3
import csv
import io
import requests  # <--- WE ARE USING THE RELIABLE LIBRARY NOW
from concurrent.futures import ThreadPoolExecutor
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from functools import wraps
import re
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
}

# --- APP CONFIG ---
USERNAME = os.environ.get("RADAR_USERNAME", "admin")
PASSWORD = os.environ.get("RADAR_PASSWORD", "admin")
DB_FILE = "radar.db"
SALES_TEAM = list(TEAM_EMAILS.keys())


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


# --- 🚀 ENGINE V6: THE "SIMPLE SCANNER" INTEGRATION ---
# This uses 'requests' instead of 'aiohttp'. It is slower but MUCH more reliable.
# It follows redirects automatically and handles cookies like a real browser.

def fetch_domain_sync(domain):
    domain = domain.strip().lower()
    if not domain: return None

    clean_domain = domain.replace("http://", "").replace("https://", "").split('/')[0]

    # We pretend to be a standard Windows Chrome browser
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/plain,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }

    urls_to_try = [
        f"https://{clean_domain}/ads.txt",
        f"http://{clean_domain}/ads.txt",
        f"https://www.{clean_domain}/ads.txt",
        f"http://www.{clean_domain}/ads.txt"
    ]

    final_status = "Connection Error"

    with requests.Session() as session:
        for url in urls_to_try:
            try:
                # 10 second timeout per try
                response = session.get(url, headers=headers, timeout=10, allow_redirects=True)

                if response.status_code == 200:
                    text = response.text
                    lower_text = text.lower()

                    # --- TRAP 1: HTML Content ---
                    # If it has <html> tags, it's a webpage, not a text file. Ignore it.
                    if "<!doctype html" in lower_text or "<html" in lower_text or "<body" in lower_text:
                        final_status = "Page Error (HTML)"
                        continue

                        # --- TRAP 2: Block Pages ---
                    block_keywords = ["verify you are human", "access denied", "security check", "cloudflare"]
                    if any(k in lower_text for k in block_keywords):
                        final_status = "Blocked (Soft 403)"
                        continue

                    # --- TRAP 3: Empty/Junk Files ---
                    if len(text) < 20 and "managerdomain" not in lower_text and "google.com" not in lower_text:
                        final_status = "No Ads.txt Found"
                        continue

                        # --- SUCCESS PARSING ---
                    pattern = r"managerdomain\s*=\s*([^#\s\r\n,]+)"
                    matches = re.findall(pattern, text, re.IGNORECASE)

                    if not matches: return "In-House"

                    found_managers = set()
                    for m in matches:
                        clean_mgr = m.strip().replace('"', '').replace("'", "").lower()
                        if clean_mgr: found_managers.add(clean_mgr)

                    if not found_managers: return "In-House"

                    return ", ".join(sorted(list(found_managers)))

                elif response.status_code == 404:
                    final_status = "No Ads.txt Found"
                elif response.status_code == 403:
                    final_status = "Blocked (403)"
                else:
                    final_status = f"Error {response.status_code}"

            except Exception as e:
                # Keep the last error but continue trying other URLs
                final_status = "Connection Error"
                continue

    return final_status


def run_batch_scan_threaded(domains):
    # Use 10 threads to speed up the slow synchronous requests
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(fetch_domain_sync, domains))
    return results


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
    for member in SALES_TEAM:
        badges[member] = \
        c.execute("SELECT COUNT(*) FROM history WHERE owner = ? AND date >= ?", (member, seven_days_ago)).fetchone()[0]

    params = [owner] if (owner and owner != "Global") else []
    where_clause = "WHERE owner = ?" if (owner and owner != "Global") else ""
    total = c.execute(f"SELECT COUNT(*) FROM domains {where_clause}", params).fetchone()[0]
    hist_where = "WHERE owner = ?" if (owner and owner != "Global") else ""
    changes_count = c.execute(f"SELECT COUNT(*) FROM history {hist_where}", params).fetchone()[0]

    # Clean Leaderboard
    raw_leaderboard = c.execute(f'''SELECT current_manager, COUNT(*) as count FROM domains {where_clause} 
                                {'AND' if where_clause else 'WHERE'} current_manager NOT IN ('Connection Error', 'New', 'No Ads.txt Found', 'Timeout', 'Page Error (HTML)', 'Blocked (403)', 'Blocked (Soft 403)') 
                                AND current_manager NOT LIKE 'Error%'
                                GROUP BY current_manager ORDER BY count DESC''', params).fetchall()

    chart_labels = [row[0] for row in raw_leaderboard]
    chart_data = [row[1] for row in raw_leaderboard]

    trends = c.execute(f'''SELECT new_manager, COUNT(*) as count FROM history {hist_where}
                      GROUP BY new_manager ORDER BY count DESC LIMIT 5''', params).fetchall()

    raw_activity = c.execute(f"SELECT * FROM history {hist_where} ORDER BY id DESC LIMIT 50", params).fetchall()

    formatted_activity = []
    for row in raw_activity:
        try:
            dt = datetime.strptime(row[4], "%Y-%m-%d %H:%M"); nice_date = dt.strftime("%b %d, %I:%M %p")
        except:
            nice_date = row[4]
        formatted_activity.append(
            {"id": row[0], "domain": row[1], "old": row[2], "new": row[3], "date": nice_date, "owner": row[5]})

    conn.close()
    return render_template('dashboard.html',
                           owner=owner, team=SALES_TEAM, badges=badges, total=total, changes_count=changes_count,
                           activity=formatted_activity, labels=chart_labels, data=chart_data,
                           trend_labels=[r[0] for r in trends], trend_data=[r[1] for r in trends])


@app.route('/add_manual', methods=['POST'])
@login_required
def add_manual():
    raw_text = request.form.get('domain_list', '')
    owner_name = request.form.get('owner_name')
    if not raw_text: return redirect(url_for('dashboard'))

    domains = [d.strip() for d in raw_text.split('\n') if d.strip()]
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    for domain in domains:
        clean_domain = domain.replace("https://", "").replace("http://", "").split('/')[0]
        c.execute(
            "INSERT OR IGNORE INTO domains (domain, current_manager, last_scanned, owner, status) VALUES (?, ?, ?, ?, ?)",
            (clean_domain, 'New', 'Never', owner_name, 'Pending'))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', owner=owner_name if owner_name != "Global" else None))


@app.route('/delete_activities', methods=['POST'])
@login_required
def delete_activities():
    data = request.json;
    ids = data.get('ids', [])
    if not ids: return jsonify({"status": "error", "message": "No items selected"})
    conn = sqlite3.connect(DB_FILE);
    c = conn.cursor()
    placeholders = ','.join(['?'] * len(ids))
    c.execute(f"DELETE FROM history WHERE id IN ({placeholders})", ids)
    conn.commit();
    conn.close()
    return jsonify({"status": "success"})


@app.route('/send_report', methods=['POST'])
@login_required
def send_report():
    data = request.json;
    recipient_names = data.get('recipients', []);
    activity_ids = data.get('ids', [])
    if not recipient_names or not activity_ids: return jsonify({"status": "error", "message": "Selection incomplete"})
    conn = sqlite3.connect(DB_FILE);
    c = conn.cursor()
    placeholders = ','.join(['?'] * len(activity_ids))
    rows = c.execute(f"SELECT domain, old_manager, new_manager, owner, date FROM history WHERE id IN ({placeholders})",
                     activity_ids).fetchall()
    conn.close()
    to_emails = [TEAM_EMAILS[name] for name in recipient_names if name in TEAM_EMAILS]
    if not to_emails: return jsonify({"status": "error", "message": "Invalid recipients"})
    html_rows = ""
    for r in rows:
        html_rows += f"""<tr><td style="padding:10px; border-bottom:1px solid #eee;"><a href="http://{r[0]}" style="color:#2563eb;">{r[0]}</a></td><td style="padding:10px; border-bottom:1px solid #eee;">{r[1]} &rarr; <b>{r[2]}</b></td><td style="padding:10px; border-bottom:1px solid #eee;">{r[3]}</td><td style="padding:10px; border-bottom:1px solid #eee;">{r[4]}</td></tr>"""
    html_content = f"""<html><body style="font-family:Arial;color:#333;"><h2 style="color:#5b45ff;">Competitor activity report</h2><table style="width:100%;border-collapse:collapse;text-align:left;"><thead><tr style="background:#f9fafb;color:#6b7280;font-size:12px;"><th>Domain</th><th>Change</th><th>Owner</th><th>Date</th></tr></thead><tbody>{html_rows}</tbody></table></body></html>"""
    try:
        msg = MIMEMultipart();
        msg['From'] = EMAIL_SENDER;
        msg['To'] = ", ".join(to_emails);
        msg['Subject'] = "Competitor activity alert"
        msg.attach(MIMEText(html_content, 'html'))
        server = smtplib.SMTP('smtp.gmail.com', 587);
        server.starttls();
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, to_emails, msg.as_string());
        server.quit()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route('/leads')
@login_required
def leads_view():
    search = request.args.get('search', '').strip()
    conn = sqlite3.connect(DB_FILE);
    c = conn.cursor()
    if search:
        rows = c.execute("SELECT * FROM domains WHERE domain LIKE ? OR owner LIKE ? LIMIT 500",
                         (f'%{search}%', f'%{search}%')).fetchall()
    else:
        rows = c.execute("SELECT * FROM domains ORDER BY rowid DESC LIMIT 100").fetchall()
    conn.close()
    return render_template('leads.html', rows=rows, search=search, team=SALES_TEAM)


@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'GET': return render_template('upload.html', team=SALES_TEAM)
    file = request.files['file']
    owner_name = request.form.get('owner_name')
    if file.filename == '': return redirect(url_for('dashboard'))
    stream = io.StringIO(file.stream.read().decode("UTF8", errors='ignore'), newline=None)
    rows = list(csv.reader(stream))
    if rows and "domain" in str(rows[0][0]).lower(): rows = rows[1:]
    conn = sqlite3.connect(DB_FILE);
    c = conn.cursor()
    for row in rows:
        if row and row[0].strip():
            c.execute(
                "INSERT OR IGNORE INTO domains (domain, current_manager, last_scanned, owner, status) VALUES (?, ?, ?, ?, ?)",
                (row[0].strip(), 'New', 'Never', owner_name, 'Pending'))
    conn.commit();
    conn.close()
    return redirect(url_for('dashboard', owner=owner_name if owner_name != "Global" else None))


@app.route('/scan_all')
@login_required
def scan_all():
    conn = sqlite3.connect(DB_FILE);
    c = conn.cursor()
    db_rows = c.execute("SELECT domain, current_manager, owner FROM domains").fetchall();
    conn.close()

    # USE THE NEW RELIABLE ENGINE
    results = run_batch_scan_threaded([r[0] for r in db_rows])

    conn = sqlite3.connect(DB_FILE);
    c = conn.cursor()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    for i, new_mgr in enumerate(results):
        domain, old_mgr, owner = db_rows[i]

        # --- FIXED LOGIC: Never overwrite valid data with bad data ---
        # We explicitly list all things that are NOT valid managers
        ignore_list = ["Error", "Timeout", "Connection", "Blocked", "Page Error"]
        is_error = any(x in new_mgr for x in ignore_list)

        if is_error:
            # We record that we scanned it, but we KEEP the old manager
            c.execute("UPDATE domains SET last_scanned = ? WHERE domain = ?",
                      (datetime.now().strftime("%Y-%m-%d"), domain))
        else:
            # Valid result (including "In-House" IF it passed the HTML trap check)
            c.execute("UPDATE domains SET current_manager = ?, last_scanned = ? WHERE domain = ?",
                      (new_mgr, datetime.now().strftime("%Y-%m-%d"), domain))

            if old_mgr != "New" and old_mgr != new_mgr:
                c.execute("INSERT INTO history (domain, old_manager, new_manager, date, owner) VALUES (?, ?, ?, ?, ?)",
                          (domain, old_mgr, new_mgr, timestamp, owner))

    conn.commit();
    conn.close()
    return redirect(url_for('dashboard'))


@app.route('/delete_domain/<domain>')
@login_required
def delete_domain(domain):
    source = request.args.get('source', 'dashboard')
    owner = request.args.get('owner')
    conn = sqlite3.connect(DB_FILE);
    c = conn.cursor()
    c.execute("DELETE FROM domains WHERE domain = ?", (domain,))
    c.execute("DELETE FROM history WHERE domain = ?", (domain,))
    conn.commit();
    conn.close()
    if source == 'leads': return redirect(url_for('leads_view'))
    return redirect(url_for('dashboard', owner=owner if owner and owner != "Global" else None))


@app.route('/delete_manager/<manager>')
@login_required
def delete_manager(manager):
    owner = request.args.get('owner')
    conn = sqlite3.connect(DB_FILE);
    c = conn.cursor()
    query = "DELETE FROM domains WHERE current_manager = ?";
    params = [manager]
    if owner and owner != "Global": query += " AND owner = ?"; params.append(owner)
    c.execute(query, params)

    # Clean history too
    hist_query = "DELETE FROM history WHERE new_manager = ?";
    hist_params = [manager]
    if owner and owner != "Global": hist_query += " AND owner = ?"; hist_params.append(owner)
    c.execute(hist_query, hist_params)

    conn.commit();
    conn.close()
    return redirect(url_for('dashboard', owner=owner if owner and owner != "Global" else None))


@app.route('/delete_list/<owner>')
@login_required
def delete_list(owner):
    if owner == "Global": return redirect(url_for('dashboard'))
    conn = sqlite3.connect(DB_FILE);
    c = conn.cursor()
    c.execute("DELETE FROM domains WHERE owner = ?", (owner,))
    conn.commit();
    conn.close()
    return redirect(url_for('dashboard', owner=owner))


@app.route('/delete_all')
@login_required
def delete_all():
    conn = sqlite3.connect(DB_FILE);
    c = conn.cursor()
    c.execute("DELETE FROM domains");
    c.execute("DELETE FROM history")
    conn.commit();
    conn.close()
    return redirect(url_for('dashboard', owner=owner))


@app.route('/api/domains/<manager>')
@login_required
def get_domains_by_manager(manager):
    owner = request.args.get('owner')
    conn = sqlite3.connect(DB_FILE);
    c = conn.cursor()
    query = "SELECT domain, last_scanned, owner FROM domains WHERE current_manager = ?"
    params = [manager]
    if owner and owner != "Global" and owner != "None": query += " AND owner = ?"; params.append(owner)
    domains = c.execute(query, params).fetchall();
    conn.close()
    return jsonify([{'domain': r[0], 'info': r[1], 'owner': r[2]} for r in domains])


@app.route('/api/trends/<manager>')
@login_required
def get_trend_domains(manager):
    owner = request.args.get('owner')
    conn = sqlite3.connect(DB_FILE);
    c = conn.cursor()
    query = "SELECT domain, date, owner FROM history WHERE new_manager = ?"
    params = [manager]
    if owner and owner != "Global" and owner != "None": query += " AND owner = ?"; params.append(owner)
    domains = c.execute(query, params).fetchall();
    conn.close()
    return jsonify([{'domain': r[0], 'info': r[1], 'owner': r[2]} for r in domains])


if __name__ == '__main__':
    app.run(debug=True, port=5000)