import os
import io
import csv
import base64
import traceback
import qrcode
import psycopg2
import psycopg2.extras
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, Response
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'jtdi_secure_master_2026')
app.permanent_session_lifetime = timedelta(hours=8)

DATABASE_URL = os.environ.get('DATABASE_URL')


def get_db_connection():
    url = DATABASE_URL
    if url and url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    return psycopg2.connect(url)


def is_admin():
    return session.get('role') == 'Admin'


def log_activity(user_label, action, asset_serial=None):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO activity_logs (user_email, action, asset_serial) VALUES (%s,%s,%s)",
            (user_label, action, asset_serial)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("ACTIVITY LOG ERROR:", e)


def log_access(email, action):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO access_logs (user_email, action) VALUES (%s,%s)",
            (email, action)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("ACCESS LOG ERROR:", e)


def ensure_bootstrap_admin():
    email = os.environ.get('BOOTSTRAP_ADMIN_EMAIL', 'admin@jtdi.gov.my').strip().lower()
    password = os.environ.get('BOOTSTRAP_ADMIN_PASSWORD', 'admin123')
    username = os.environ.get('BOOTSTRAP_ADMIN_USERNAME', 'admin')
    full_name = os.environ.get('BOOTSTRAP_ADMIN_NAME', 'System Administrator')
    hashed = generate_password_hash(password)

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(
        "SELECT id FROM users WHERE username = %s OR email = %s",
        (username, email)
    )
    row = cur.fetchone()
    if row:
        cur.execute("""
            UPDATE users
            SET full_name = %s, email = %s, password = %s, role = 'Admin'
            WHERE id = %s
        """, (full_name, email, hashed, row['id']))
    else:
        cur.execute("""
            INSERT INTO users (full_name, username, email, password, role)
            VALUES (%s, %s, %s, %s, 'Admin')
        """, (full_name, username, email, hashed))
    conn.commit()
    cur.close()
    conn.close()


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('''CREATE TABLE IF NOT EXISTS assets (
            id SERIAL PRIMARY KEY,
            asset_type TEXT,
            tracking_number TEXT,
            cpu_name TEXT,
            serial_number TEXT UNIQUE,
            ram_size TEXT,
            storage_type TEXT,
            location TEXT,
            status TEXT,
            description TEXT,
            is_deleted BOOLEAN DEFAULT FALSE,
            scan_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );''')

        cur.execute("ALTER TABLE assets ADD COLUMN IF NOT EXISTS description TEXT;")
        cur.execute("ALTER TABLE assets ADD COLUMN IF NOT EXISTS scan_count INTEGER DEFAULT 0;")
        cur.execute("ALTER TABLE assets ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;")

        cur.execute('''CREATE TABLE IF NOT EXISTS maintenance_logs (
            id SERIAL PRIMARY KEY,
            asset_id INTEGER REFERENCES assets(id),
            action_type TEXT,
            comment TEXT,
            updated_by TEXT,
            log_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );''')

        cur.execute('''CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            full_name TEXT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'User'
        );''')
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT;")

        cur.execute('''CREATE TABLE IF NOT EXISTS login_logs (
            id SERIAL PRIMARY KEY,
            full_name TEXT,
            email TEXT,
            login_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );''')

        cur.execute('''CREATE TABLE IF NOT EXISTS activity_logs (
            id SERIAL PRIMARY KEY,
            user_email TEXT,
            action TEXT,
            asset_serial TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );''')

        cur.execute('''CREATE TABLE IF NOT EXISTS access_logs (
            id SERIAL PRIMARY KEY,
            user_email TEXT,
            action TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );''')

        conn.commit()
    except Exception as e:
        conn.rollback()
        print("INIT DB ERROR:", e)
        raise
    finally:
        cur.close()
        conn.close()


def safe_startup():
    if not DATABASE_URL:
        print("WARNING: DATABASE_URL is not set. DB init and bootstrap skipped.")
        return
    try:
        init_db()
        ensure_bootstrap_admin()
        print("Startup OK. Bootstrap admin:", os.environ.get('BOOTSTRAP_ADMIN_EMAIL', 'admin@jtdi.gov.my'))
    except Exception as e:
        print("STARTUP ERROR:", e)
        traceback.print_exc()


safe_startup()


@app.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('login'))

    s = request.args.get('search', '').strip()
    c = request.args.get('category', '').strip()

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    query = "SELECT * FROM assets WHERE 1=1"
    params = []
    if session.get('role') != 'Admin':
        query += " AND is_deleted = FALSE"
    if s:
        query += (
            " AND (serial_number ILIKE %s OR tracking_number ILIKE %s "
            "OR cpu_name ILIKE %s OR location ILIKE %s)"
        )
        p = f'%{s}%'
        params.extend([p, p, p, p])
    if c:
        query += " AND asset_type = %s"
        params.append(c)

    cur.execute(query + " ORDER BY id DESC", tuple(params))
    data = cur.fetchall()

    stats = {
        'total': len(data),
        'working': len([r for r in data if r['status'] == 'Working']),
        'maint': len([r for r in data if r['status'] == 'Maintenance']),
        'faulty': len([r for r in data if r['status'] == 'Faulty'])
    }
    cur.close()
    conn.close()

    return render_template('assets.html', data=data, **stats, s_query=s, c_filter=c)


@app.route('/edit/<int:id>', methods=['GET', 'POST'])
def edit_asset(id):
    if 'user' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    if request.method == 'POST':
        cur.execute("""
            UPDATE assets SET
                asset_type=%s,
                tracking_number=%s,
                cpu_name=%s,
                ram_size=%s,
                storage_type=%s,
                location=%s,
                status=%s,
                description=%s
            WHERE id=%s
        """, (
            request.form.get('asset_type'),
            request.form.get('tracking_number'),
            request.form.get('cpu_name'),
            request.form.get('ram_size'),
            request.form.get('storage_type'),
            request.form.get('location'),
            request.form.get('status'),
            request.form.get('description'),
            id
        ))

        comment = request.form.get('comment', '').strip()
        if comment:
            cur.execute("""
                INSERT INTO maintenance_logs (asset_id, action_type, comment, updated_by)
                VALUES (%s, %s, %s, %s)
            """, (
                id,
                request.form.get('action_type'),
                comment,
                session.get('full_name')
            ))

        cur.execute("SELECT serial_number FROM assets WHERE id = %s", (id,))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()

        log_activity(
            session.get('email') or session.get('full_name'),
            "ASSET UPDATED",
            row['serial_number'] if row else None
        )
        flash("Update Saved!")
        return redirect(url_for('index'))

    cur.execute("SELECT * FROM assets WHERE id = %s", (id,))
    asset = cur.fetchone()
    cur.close()
    conn.close()

    if not asset:
        flash("Asset not found.")
        return redirect(url_for('index'))

    return render_template('edit.html', asset=asset)


@app.route('/view/<int:id>')
def view_asset(id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM assets WHERE id = %s", (id,))
    asset = cur.fetchone()
    if not asset:
        cur.close()
        conn.close()
        return "Not Found", 404

    cur.execute(
        "UPDATE assets SET scan_count = COALESCE(scan_count, 0) + 1 WHERE id = %s",
        (id,)
    )
    cur.execute(
        "SELECT * FROM maintenance_logs WHERE asset_id = %s ORDER BY log_date DESC",
        (id,)
    )
    logs = cur.fetchall()
    conn.commit()
    cur.close()
    conn.close()

    return render_template('view.html', asset=asset, logs=logs)


@app.route('/asset/<int:id>')
def legacy_asset_view(id):
    return redirect(url_for('view_asset', id=id))


@app.route('/qr/<int:id>')
def qr_code(id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM assets WHERE id = %s", (id,))
    asset = cur.fetchone()
    cur.close()
    conn.close()

    if not asset:
        flash("Asset not found.")
        return redirect(url_for('index'))

    qr_url = url_for('view_asset', id=id, _external=True)
    img = qrcode.make(qr_url)
    buf = io.BytesIO()
    img.save(buf)
    qr_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    return render_template('qr_display.html', qr_code=qr_b64, asset=asset)


@app.route('/delete/<int:id>', methods=['POST'])
def delete_asset(id):
    if 'user' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT serial_number FROM assets WHERE id = %s", (id,))
    row = cur.fetchone()
    cur.execute("UPDATE assets SET is_deleted = TRUE WHERE id = %s", (id,))
    conn.commit()
    cur.close()
    conn.close()

    if row:
        log_activity(
            session.get('email') or session.get('full_name'),
            "ASSET ARCHIVED",
            row['serial_number']
        )
    flash("Asset archived.")
    return redirect(url_for('index'))


@app.route('/add', methods=['GET', 'POST'])
def add_asset():
    if 'user' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            tn = (request.form.get('tracking_number') or '').strip()
            if not tn:
                tn = f"JTDI-{datetime.now().strftime('%y%m%H%M%S')}"

            cur.execute("""
                INSERT INTO assets (
                    asset_type, tracking_number, cpu_name, serial_number,
                    ram_size, storage_type, status, location, description, is_deleted
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, FALSE)
            """, (
                request.form.get('asset_type'),
                tn,
                request.form.get('cpu_name'),
                request.form.get('serial_number'),
                request.form.get('ram_size'),
                request.form.get('storage_type'),
                request.form.get('status'),
                request.form.get('location'),
                request.form.get('description')
            ))
            conn.commit()
            cur.close()
            conn.close()

            log_activity(
                session.get('email') or session.get('full_name'),
                "ASSET REGISTERED",
                request.form.get('serial_number')
            )
            return redirect(url_for('index'))
        except Exception:
            flash("Error: Serial number may already exist.")

    return render_template('add.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        session.clear()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cur.fetchone()

        if user and check_password_hash(user['password'], password):
            session.permanent = True
            session.update({
                'user': user['username'],
                'role': user['role'],
                'full_name': user['full_name'] or user['username'],
                'email': user['email']
            })
            cur.execute("""
                INSERT INTO login_logs (full_name, email) VALUES (%s, %s)
                """,
                (user['full_name'] or user['username'], user['email'])
            )
            conn.commit()
            cur.close()
            conn.close()
            log_access(user['email'], "LOGIN")
            return redirect(url_for('index'))

        cur.close()
        conn.close()
        flash("Invalid email or password.")

    return render_template('login.html')


@app.route('/logout')
def logout():
    if session.get('email'):
        log_access(session['email'], "LOGOUT")
    session.clear()
    return redirect(url_for('login'))


@app.route('/activity')
def activity():
    if 'user' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM activity_logs ORDER BY created_at DESC")
    logs = cur.fetchall()
    cur.close()
    conn.close()

    return render_template('activity.html', logs=logs)


@app.route('/export')
def export_csv():
    if 'user' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    cur = conn.cursor()
    query = "SELECT * FROM assets WHERE 1=1"
    if session.get('role') != 'Admin':
        query += " AND is_deleted = FALSE"
    query += " ORDER BY id DESC"
    cur.execute(query)
    rows = cur.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([d[0] for d in cur.description])
    writer.writerows(rows)
    output.seek(0)

    cur.close()
    conn.close()

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=assets.csv"}
    )


@app.route('/export/excel')
def export_excel():
    if 'user' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    cur = conn.cursor()
    query = """
        SELECT asset_type, tracking_number, cpu_name, serial_number,
               ram_size, storage_type, location, status, description,
               is_deleted, scan_count, created_at
        FROM assets WHERE 1=1
    """
    if session.get('role') != 'Admin':
        query += " AND is_deleted = FALSE"
    query += " ORDER BY id DESC"
    
    cur.execute(query)
    rows = cur.fetchall()
    column_names = [desc[0] for desc in cur.description]
    
    cur.close()
    conn.close()

    df = pd.DataFrame(rows, columns=column_names)
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Assets')
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f"Assets_{datetime.now().strftime('%Y%m%d')}.xlsx"
    )


@app.route('/admin')
def admin_dashboard():
    if not is_admin():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM users ORDER BY id DESC")
    users = cur.fetchall()
    cur.execute("SELECT * FROM access_logs ORDER BY created_at DESC LIMIT 25")
    access_logs = cur.fetchall()
    cur.close()
    conn.close()

    return render_template('admin.html', users=users, access_logs=access_logs)


@app.route('/admin/users', methods=['GET', 'POST'])
def manage_users():
    if not is_admin():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    if request.method == 'POST':
        role = request.form.get('role', 'User')
        if role not in ('User', 'Admin'):
            role = 'User'
        try:
            cur.execute("""
                INSERT INTO users (full_name, username, email, password, role)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                request.form.get('full_name') or request.form.get('username'),
                request.form.get('username'),
                request.form.get('email', '').strip().lower(),
                generate_password_hash(request.form.get('password')),
                role
            ))
            conn.commit()
            flash("User created successfully.")
        except Exception:
            conn.rollback()
            flash("Error: Username or email already exists.")

    cur.execute("SELECT * FROM users ORDER BY id DESC")
    users = cur.fetchall()
    cur.close()
    conn.close()

    return render_template('manage_user.html', users=users)


@app.route('/admin/edit_user/<int:id>', methods=['GET', 'POST'])
def edit_user(id):
    if not is_admin():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    if request.method == 'POST':
        role = request.form.get('role', 'User')
        if role not in ('User', 'Admin'):
            role = 'User'
        new_password = request.form.get('password', '').strip()

        if new_password:
            cur.execute("""
                UPDATE users SET full_name=%s, email=%s, role=%s, password=%s
                WHERE id=%s
            """, (
                request.form.get('full_name'),
                request.form.get('email', '').strip().lower(),
                role,
                generate_password_hash(new_password),
                id
            ))
        else:
            cur.execute("""
                UPDATE users SET full_name=%s, email=%s, role=%s WHERE id=%s
            """, (
                request.form.get('full_name'),
                request.form.get('email', '').strip().lower(),
                role,
                id
            ))

        try:
            conn.commit()
            flash("User updated.")
        except Exception:
            conn.rollback()
            flash("Update failed: email may already be in use.")

        cur.close()
        conn.close()
        return redirect(url_for('manage_users'))

    cur.execute("SELECT * FROM users WHERE id = %s", (id,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    if not user:
        flash("User not found.")
        return redirect(url_for('manage_users'))

    return render_template('edit_user.html', user=user)


@app.route('/admin/delete_user/<int:id>', methods=['POST'])
def delete_user(id):
    if not is_admin():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT username FROM users WHERE id = %s", (id,))
    user = cur.fetchone()

    if user and user['username'] == 'admin':
        flash("Cannot delete the main administrator account.")
    else:
        cur.execute("DELETE FROM users WHERE id = %s", (id,))
        conn.commit()
        flash("User deleted.")

    cur.close()
    conn.close()
    return redirect(url_for('manage_users'))


@app.route('/admin/logs')
def admin_logs():
    if not is_admin():
        return redirect(url_for('login'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM login_logs ORDER BY login_time DESC LIMIT 100")
    logs = cur.fetchall()
    cur.close()
    conn.close()

    return render_template('login_logs.html', logs=logs)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
