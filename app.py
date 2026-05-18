import os, io, qrcode, base64, psycopg2, psycopg2.extras, pandas as pd
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = 'jtdi_secure_master_2026'
app.permanent_session_lifetime = timedelta(hours=8)

DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# --- DATABASE INITIALIZATION ---
def init_db():
    conn = get_db_connection(); cur = conn.cursor()
    # Assets Table
    cur.execute('''CREATE TABLE IF NOT EXISTS assets (
        id SERIAL PRIMARY KEY, asset_type TEXT, tracking_number TEXT, cpu_name TEXT, 
        serial_number TEXT UNIQUE, ram_size TEXT, storage_type TEXT, location TEXT, 
        status TEXT, is_deleted BOOLEAN DEFAULT FALSE);''')
    
    # Maintenance Logs Table (FIX: This allows UNLIMITED history entries)
    cur.execute('''CREATE TABLE IF NOT EXISTS maintenance_logs (
        id SERIAL PRIMARY KEY, asset_id INTEGER REFERENCES assets(id), 
        action_type TEXT, comment TEXT, updated_by TEXT, log_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
    
    cur.execute('''CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY, full_name TEXT, username TEXT UNIQUE NOT NULL, 
        email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'User');''')
    
    cur.execute('''CREATE TABLE IF NOT EXISTS login_logs (
        id SERIAL PRIMARY KEY, full_name TEXT, email TEXT, login_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
    
    conn.commit(); cur.close(); conn.close()

init_db()

# --- 1. DASHBOARD & SEARCH ---
@app.route('/')
def index():
    if 'user' not in session: return redirect(url_for('login'))
    s, c = request.args.get('search', '').strip(), request.args.get('category', '').strip()
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    query = "SELECT * FROM assets WHERE 1=1"
    params = []
    if session.get('role') != 'Admin': query += " AND is_deleted = FALSE"
    if s:
        query += " AND (serial_number ILIKE %s OR tracking_number ILIKE %s OR cpu_name ILIKE %s)"
        params.extend([f'%{s}%', f'%{s}%', f'%{s}%'])
    if c:
        query += " AND asset_type = %s"; params.append(c)
    cur.execute(query + " ORDER BY id DESC", tuple(params))
    data = cur.fetchall()
    stats = {'total': len(data), 'working': len([r for r in data if r['status'] == 'Working']), 'maint': len([r for r in data if r['status'] == 'Maintenance']), 'faulty': len([r for r in data if r['status'] == 'Faulty'])}
    cur.close(); conn.close()
    return render_template('assets.html', data=data, **stats, s_query=s, c_filter=c)

# --- 2. EDIT ASSET (FIX: Inserts NEW log entry every time) ---
@app.route('/edit/<int:id>', methods=['GET', 'POST'])
def edit_asset(id):
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    if request.method == 'POST':
        # Update Asset Stats
        cur.execute("""UPDATE assets SET asset_type=%s, cpu_name=%s, ram_size=%s, 
                       storage_type=%s, location=%s, status=%s WHERE id=%s""", 
                    (request.form.get('asset_type'), request.form.get('cpu_name'), 
                     request.form.get('ram_size'), request.form.get('storage_type'), 
                     request.form.get('location'), request.form.get('status'), id))
        
        # INSERT NEW LOG (Problem Report)
        comment = request.form.get('comment', '').strip()
        if comment:
            cur.execute("""INSERT INTO maintenance_logs (asset_id, action_type, comment, updated_by) 
                           VALUES (%s, %s, %s, %s)""", 
                        (id, request.form.get('action_type'), comment, session.get('full_name')))
        
        conn.commit(); cur.close(); conn.close()
        flash("Update Saved!"); return redirect(url_for('index'))
    
    cur.execute("SELECT * FROM assets WHERE id = %s", (id,)); asset = cur.fetchone(); cur.close(); conn.close()
    return render_template('edit.html', asset=asset)

# --- 3. VIEW ASSET (FIX: Retrieves ALL history entries) ---
@app.route('/view/<int:id>')
def view_asset(id):
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM assets WHERE id = %s", (id,)); asset = cur.fetchone()
    if not asset: return "Not Found", 404
    # Fetch full history sorted by latest date
    cur.execute("SELECT * FROM maintenance_logs WHERE asset_id = %s ORDER BY log_date DESC", (id,))
    logs = cur.fetchall(); cur.close(); conn.close()
    return render_template('view.html', asset=asset, logs=logs)

# --- 4. QR, DELETE, ADD, AUTH (Remaining functions) ---
@app.route('/qr/<int:id>')
def qr_code(id):
    qr_url = url_for('view_asset', id=id, _external=True)
    img = qrcode.make(qr_url); buf = io.BytesIO(); img.save(buf); qr_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    return render_template('qr_display.html', qr_code=qr_b64)

@app.route('/delete/<int:id>', methods=['POST'])
def delete_asset(id):
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection(); cur = conn.cursor(); cur.execute("UPDATE assets SET is_deleted = TRUE WHERE id = %s", (id,)); conn.commit(); cur.close(); conn.close()
    return redirect(url_for('index'))

@app.route('/add', methods=['GET', 'POST'])
def add_asset():
    if 'user' not in session: return redirect(url_for('login'))
    if request.method == 'POST':
        conn = get_db_connection(); cur = conn.cursor()
        t = f"JTDI-{datetime.now().strftime('%y%m%H%M%S')}"
        cur.execute("INSERT INTO assets (asset_type, tracking_number, cpu_name, serial_number, ram_size, storage_type, status, location, is_deleted) VALUES (%s,%s,%s,%s,%s,%s,%s,%s, FALSE)", (request.form.get('asset_type'), t, request.form.get('cpu_name'), request.form.get('serial_number'), request.form.get('ram_size'), request.form.get('storage_type'), request.form.get('status'), request.form.get('location')))
        conn.commit(); cur.close(); conn.close(); return redirect(url_for('index'))
    return render_template('add.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        session.clear()
        email = request.form.get('email', '').strip().lower()
        conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor); cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cur.fetchone()
        if user and check_password_hash(user['password'], request.form.get('password')):
            session.update({'user': user['username'], 'role': user['role'], 'full_name': user['full_name']}); conn.commit(); cur.close(); conn.close(); return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))
