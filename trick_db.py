import sqlite3

# 1. SETUP
DB_FILE = "radar.db"
target_domain = "example-publisher.com"  # <--- CHANGE THIS to a domain present in your dashboard DB
fake_manager = "Fake Competitor LLC"

# 2. CONNECT
conn = sqlite3.connect(DB_FILE)
c = conn.cursor()

# 3. CHECK IF DOMAIN EXISTS
row = c.execute("SELECT current_manager FROM domains WHERE domain = ?", (target_domain,)).fetchone()

if row:
    print(f"🕵️  Found {target_domain}. Current status: {row[0]}")

    # 4. INJECT FALSE MEMORY
    c.execute("UPDATE domains SET current_manager = ? WHERE domain = ?", (fake_manager, target_domain))
    conn.commit()

    print(f"✅ SUCCESS: Database manipulated.")
    print(f"👉 The tool now thinks {target_domain} is managed by '{fake_manager}'.")
    print("👉 Go to your Dashboard and click 'Start New Scan' to trigger the alert!")
else:
    print(f"❌ ERROR: {target_domain} is not in your database.")
    print("   Please open the script and change 'target_domain' to one of your uploaded leads.")

conn.close()