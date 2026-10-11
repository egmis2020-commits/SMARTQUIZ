from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, make_response, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3, json, csv, io, re, os, secrets, string, socket, time, hashlib, threading, unicodedata
from pathlib import Path
from datetime import datetime, timedelta
import qrcode
try:
    import duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    duckdb = None
    DUCKDB_AVAILABLE = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, 'quiz.db')
ANALYTICS_DB = os.path.join(BASE_DIR, 'analytics.duckdb')
ANALYTICS_LOCK = threading.Lock()
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'), static_folder=os.path.join(BASE_DIR, 'static'))
app.secret_key = os.environ.get('QUIZ_SECRET', 'tle-css-quiz-v4-change-this')
# LAN/offline-safe session settings. Students access the system over plain HTTP
# on the local network, so the session cookie must NOT require HTTPS.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=(os.environ.get('QUIZ_ACCESS_MODE', 'lan').strip().lower() == 'public'),
    SESSION_COOKIE_PATH='/',
    SESSION_REFRESH_EACH_REQUEST=True,
)
app.config['JSON_SORT_KEYS'] = False
PASS_MARK = 75
DEFAULT_DURATION = 60
ALLOWED_IMAGE_EXTENSIONS = {'png','jpg','jpeg','gif','webp','bmp'}
QUESTION_IMAGE_DIR = os.path.join(BASE_DIR, 'static', 'uploads', 'questions')
os.makedirs(QUESTION_IMAGE_DIR, exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 24 * 1024 * 1024

@app.route('/maintenance.html')
@app.route('/maintenance')
def maintenance_page():
    """Serve the public maintenance page at a real Flask route (templates are not static files)."""
    return render_template('maintenance.html')


# SQLite allows many readers but only one writer at a time. Flask can process
# multiple requests concurrently, so serialize write requests inside this
# single local quiz server. This prevents students' simultaneous submissions,
# heartbeats, registrations, and admin edits from fighting for the SQLite lock.
DB_WRITE_LOCK = threading.Lock()


def analytics_connection():
    if not DUCKDB_AVAILABLE:
        return None
    try:
        conn = duckdb.connect(ANALYTICS_DB)
        conn.execute("PRAGMA threads=2")
        return conn
    except Exception:
        return None


def init_analytics_db():
    conn = analytics_connection()
    if conn is None:
        return False
    try:
        with ANALYTICS_LOCK:
            conn.execute("""CREATE TABLE IF NOT EXISTS attempt_analytics (
                attempt_id BIGINT PRIMARY KEY, user_id BIGINT, username VARCHAR, full_name VARCHAR,
                gender VARCHAR, grade_level INTEGER, quiz_id BIGINT, quiz_title VARCHAR,
                quiz_grade_level INTEGER, created_by BIGINT, attempt_number INTEGER, started_at VARCHAR,
                completed_at VARCHAR, score INTEGER, percentage DOUBLE, correct INTEGER, incorrect INTEGER,
                skipped INTEGER, timed_out INTEGER, average_time DOUBLE, passed BOOLEAN, hidden BOOLEAN,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS response_analytics (
                attempt_id BIGINT, question_id BIGINT, item_number INTEGER, selected_answer VARCHAR,
                correct_answer VARCHAR, is_correct BOOLEAN, time_spent DOUBLE, completed_at VARCHAR,
                quiz_id BIGINT, user_id BIGINT, PRIMARY KEY (attempt_id, question_id))""")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_aa_quiz ON attempt_analytics(quiz_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_aa_user ON attempt_analytics(user_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_ra_quiz ON response_analytics(quiz_id)')
        conn.close(); return True
    except Exception:
        try: conn.close()
        except Exception: pass
        return False


def sync_attempt_to_analytics(attempt_id):
    if not init_analytics_db():
        return False
    ac = None
    try:
        conn=db()
        row=conn.execute("""SELECT a.*,u.username,COALESCE(u.full_name,u.username) AS full_name,u.gender,
            u.grade_level AS user_grade,q.title AS quiz_title,q.grade_level AS quiz_grade_level,q.created_by
            FROM attempts a JOIN users u ON u.id=a.user_id JOIN quizzes q ON q.id=a.quiz_id WHERE a.id=?""",(attempt_id,)).fetchone()
        questions=conn.execute('SELECT id,answer FROM questions WHERE quiz_id=? ORDER BY id',(row['quiz_id'],)).fetchall() if row else []
        conn.close()
        if not row: return False
        payload=json.loads(row['answers'] or '{}'); answers=payload.get('answers') or []; times=payload.get('times') or []; order=json.loads(row['question_order'] or '[]')
        qmap={int(q['id']):q for q in questions}
        ac=analytics_connection()
        if ac is None: return False
        with ANALYTICS_LOCK:
            ac.execute("""INSERT OR REPLACE INTO attempt_analytics
                (attempt_id,user_id,username,full_name,gender,grade_level,quiz_id,quiz_title,quiz_grade_level,created_by,
                 attempt_number,started_at,completed_at,score,percentage,correct,incorrect,skipped,timed_out,average_time,passed,hidden,synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (row['id'],row['user_id'],row['username'],row['full_name'],row['gender'],row['user_grade'],row['quiz_id'],row['quiz_title'],row['quiz_grade_level'],row['created_by'],row['attempt_number'],row['started_at'],row['completed_at'],row['score'],row['percentage'],row['correct'],row['incorrect'],row['skipped'],row['timed_out'],row['average_time'],bool(row['passed']),bool(row['hidden'])))
            ac.execute('DELETE FROM response_analytics WHERE attempt_id=?',(row['id'],))
            for i,raw_qid in enumerate(order):
                try: qid=int(raw_qid)
                except (TypeError,ValueError): continue
                q=qmap.get(qid)
                if not q: continue
                selected=answers[i] if i<len(answers) else 'timed'; t=float(times[i]) if i<len(times) else 0.0
                ac.execute('INSERT INTO response_analytics VALUES (?,?,?,?,?,?,?,?,?,?)',(row['id'],qid,i+1,str(selected),str(q['answer']),str(selected)==str(q['answer']),t,row['completed_at'],row['quiz_id'],row['user_id']))
        ac.close(); return True
    except Exception:
        try:
            if ac: ac.close()
        except Exception: pass
        return False


def sync_all_analytics():
    if not init_analytics_db(): return 0
    try:
        conn=db(); ids=[r['id'] for r in conn.execute("SELECT id FROM attempts WHERE status='completed' ORDER BY id").fetchall()]; conn.close()
        return sum(1 for attempt_id in ids if sync_attempt_to_analytics(attempt_id))
    except Exception: return 0


def analytics_status():
    if not DUCKDB_AVAILABLE:
        return {'available':False,'message':'DuckDB package is not installed. SQLite-only fallback is active.','attempts':0,'responses':0}
    conn=analytics_connection()
    if conn is None:
        return {'available':False,'message':'DuckDB is unavailable. SQLite remains the source of truth.','attempts':0,'responses':0}
    try:
        a=conn.execute('SELECT COUNT(*) FROM attempt_analytics').fetchone()[0]; r=conn.execute('SELECT COUNT(*) FROM response_analytics').fetchone()[0]; conn.close()
        return {'available':True,'message':'DuckDB analytics is healthy.','attempts':int(a),'responses':int(r)}
    except Exception:
        try: conn.close()
        except Exception: pass
        return {'available':False,'message':'DuckDB analytics database could not be read. SQLite remains the source of truth.','attempts':0,'responses':0}


def delete_quiz_analytics(quiz_id):
    conn=analytics_connection()
    if conn is None: return
    try:
        with ANALYTICS_LOCK:
            conn.execute('DELETE FROM response_analytics WHERE quiz_id=?',(quiz_id,)); conn.execute('DELETE FROM attempt_analytics WHERE quiz_id=?',(quiz_id,))
        conn.close()
    except Exception:
        try: conn.close()
        except Exception: pass


def refresh_analytics_attempt_hidden(attempt_id, hidden):
    conn=analytics_connection()
    if conn is None: return
    try:
        with ANALYTICS_LOCK: conn.execute('UPDATE attempt_analytics SET hidden=?,synced_at=CURRENT_TIMESTAMP WHERE attempt_id=?',(bool(hidden),attempt_id))
        conn.close()
    except Exception:
        try: conn.close()
        except Exception: pass


def get_lan_ip():
    """Best-effort local IPv4 address used by other devices on the LAN."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.168.1.1", 80))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    finally:
        sock.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return "127.0.0.1"

def access_mode():
    """Return the configured access mode. LAN is the safe default."""
    return os.environ.get('QUIZ_ACCESS_MODE', 'lan').strip().lower()

def public_access_url():
    """Return the public tunnel URL only when public mode is explicitly enabled."""
    env_url = os.environ.get('QUIZ_PUBLIC_URL', '').strip()
    if env_url:
        return env_url.rstrip('/')
    marker = os.path.join(BASE_DIR, 'public_url.txt')
    try:
        if os.path.exists(marker):
            value = Path(marker).read_text(encoding='utf-8').strip()
            if value:
                return value.rstrip('/')
    except OSError:
        pass
    return f"http://{get_lan_ip()}:8000"


def student_access_url():
    # LAN/offline is the normal classroom mode. Public internet access is opt-in.
    if access_mode() != 'public':
        return f"http://{get_lan_ip()}:8000"
    return public_access_url()


def db():
    """Open the explicitly selected primary database.

    Local mode retains the original SQLite file for LAN/offline use. Hosted
    Turso mode never falls back to a per-instance SQLite file on failure.
    """
    mode = os.environ.get('QUIZ_DB_MODE', 'local').strip().lower()
    if mode == 'turso':
        url = os.environ.get('TURSO_DATABASE_URL', '').strip()
        token = os.environ.get('TURSO_AUTH_TOKEN', '').strip()
        if not url or not token:
            raise RuntimeError('QUIZ_DB_MODE=turso requires TURSO_DATABASE_URL and TURSO_AUTH_TOKEN.')
        from turso_compat import TursoConnection
        return TursoConnection(url, token)
    if mode != 'local':
        raise RuntimeError('Invalid QUIZ_DB_MODE. Use local or turso.')
    conn = sqlite3.connect(DB, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 30000')
    conn.execute('PRAGMA wal_autocheckpoint = 1000')
    conn.execute('PRAGMA temp_store = MEMORY')
    return conn


def now():
    return datetime.now().isoformat(timespec='seconds')


def parse_schedule_value(value):
    """Normalize an HTML datetime-local value to local ISO minute precision."""
    value = str(value or '').strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(second=0, microsecond=0).isoformat(timespec='minutes')
    except ValueError:
        return None


def quiz_availability(quiz_row, current=None):
    """Return the server-side availability state for a quiz.
    No schedule means the quiz remains available as in previous versions.
    """
    current = current or datetime.now()
    raw_start = quiz_row['available_from'] if 'available_from' in quiz_row.keys() else None
    raw_end = quiz_row['available_until'] if 'available_until' in quiz_row.keys() else None
    try:
        start = datetime.fromisoformat(raw_start) if raw_start else None
    except (TypeError, ValueError):
        start = None
    try:
        end = datetime.fromisoformat(raw_end) if raw_end else None
    except (TypeError, ValueError):
        end = None
    if start and current < start:
        return {'state':'scheduled','label':'Scheduled','available':False,'start':start,'end':end}
    if end and current >= end:
        return {'state':'closed','label':'Closed','available':False,'start':start,'end':end}
    return {'state':'available','label':'Available','available':True,'start':start,'end':end}


def format_schedule(value):
    if not value:
        return ''
    try:
        return datetime.fromisoformat(value).strftime('%b %d, %Y %I:%M %p')
    except (TypeError, ValueError):
        return str(value)


@app.before_request
def add_request_context():
    request._request_id = secrets.token_hex(4)


@app.after_request
def no_cache_authenticated_pages(response):
    # Prevent older mobile browsers/proxies from replaying stale login/dashboard pages.
    if request.path in ('/login', '/', '/student', '/teacher', '/admin', '/session-check') or session.get('uid'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    response.headers['X-Quiz-Request-ID'] = getattr(request, '_request_id', '')
    return response


# Do not lock an entire POST request: password hashing, rendering, and client
# delays would queue every login/heartbeat/registration behind the prior request.
# Use DB_WRITE_LOCK only around short SQLite write transactions.


def audit_log(action, actor=None, target_type='', target_id=None, details=''):
    """Write a security/activity audit event without exposing passwords."""
    try:
        actor_id = actor['id'] if actor is not None and 'id' in actor.keys() else None
        actor_username = actor['username'] if actor is not None and 'username' in actor.keys() else None
        actor_role = actor['role'] if actor is not None and 'role' in actor.keys() else None
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or '')
        if ',' in ip:
            ip = ip.split(',', 1)[0].strip()
        ua = (request.headers.get('User-Agent') or '')[:500]
        acquired = False
        if not getattr(request, '_db_write_lock_held', False):
            DB_WRITE_LOCK.acquire()
            acquired = True
        conn = db()
        conn.execute("""INSERT INTO audit_logs
            (actor_id,actor_username,actor_role,action,target_type,target_id,details,ip_address,user_agent,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (actor_id,actor_username,actor_role,action,target_type,target_id,str(details)[:2000],ip,ua,now()))
        conn.commit(); conn.close()
        if acquired:
            DB_WRITE_LOCK.release()
    except Exception:
        try:
            if 'acquired' in locals() and acquired:
                DB_WRITE_LOCK.release()
        except Exception:
            pass
        # Audit logging must never break the LMS or a quiz submission.
        try: conn.close()
        except Exception: pass


def init_db():
    conn = db()
    # WAL lets many students read while another request writes. The busy timeout
    # prevents short read/write collisions from immediately becoming 'database is locked'.
    # WAL + a longer busy timeout is important when many student devices are connected.
    if os.environ.get('QUIZ_DB_MODE', 'local').strip().lower() == 'local':
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
        except sqlite3.OperationalError:
            pass
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'student',
        grade_level INTEGER,
        full_name TEXT,
        gender TEXT,
        password_temp INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS quizzes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL DEFAULT 60,
        max_attempts INTEGER NOT NULL DEFAULT 1,
        grade_level INTEGER NOT NULL DEFAULT 10,
        created_at TEXT NOT NULL,
        created_by INTEGER,
        review_enabled INTEGER NOT NULL DEFAULT 0,
        available_from TEXT,
        available_until TEXT
    );
    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quiz_id INTEGER NOT NULL,
        question TEXT NOT NULL,
        a TEXT NOT NULL,
        b TEXT NOT NULL,
        c TEXT NOT NULL,
        d TEXT NOT NULL,
        answer TEXT NOT NULL,
        image TEXT,
        FOREIGN KEY (quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        quiz_id INTEGER NOT NULL,
        attempt_number INTEGER NOT NULL,
        started_at TEXT,
        completed_at TEXT NOT NULL,
        score INTEGER NOT NULL,
        percentage REAL NOT NULL,
        correct INTEGER NOT NULL,
        incorrect INTEGER NOT NULL,
        skipped INTEGER NOT NULL,
        timed_out INTEGER NOT NULL,
        average_time REAL NOT NULL,
        passed INTEGER NOT NULL,
        question_order TEXT NOT NULL,
        choice_order TEXT NOT NULL,
        answers TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'completed',
        hidden INTEGER NOT NULL DEFAULT 0,
        submission_token TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_id INTEGER,
        actor_username TEXT,
        actor_role TEXT,
        action TEXT NOT NULL,
        target_type TEXT,
        target_id INTEGER,
        details TEXT,
        ip_address TEXT,
        user_agent TEXT,
        created_at TEXT NOT NULL
    );
    ''')
    # Upgrade existing databases without destroying data.
    question_cols = {r['name'] for r in conn.execute('PRAGMA table_info(questions)').fetchall()}
    if 'image' not in question_cols:
        conn.execute('ALTER TABLE questions ADD COLUMN image TEXT')
    cols = {r['name'] for r in conn.execute('PRAGMA table_info(quizzes)').fetchall()}
    if 'created_by' not in cols:
        conn.execute('ALTER TABLE quizzes ADD COLUMN created_by INTEGER')
    if 'review_enabled' not in cols:
        conn.execute('ALTER TABLE quizzes ADD COLUMN review_enabled INTEGER NOT NULL DEFAULT 0')
    if 'available_from' not in cols:
        conn.execute('ALTER TABLE quizzes ADD COLUMN available_from TEXT')
    if 'available_until' not in cols:
        conn.execute('ALTER TABLE quizzes ADD COLUMN available_until TEXT')
    if 'duration_minutes' not in cols:
        conn.execute('ALTER TABLE quizzes ADD COLUMN duration_minutes INTEGER NOT NULL DEFAULT 60')
    if 'max_attempts' not in cols:
        conn.execute('ALTER TABLE quizzes ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 1')
    if 'grade_level' not in cols:
        conn.execute('ALTER TABLE quizzes ADD COLUMN grade_level INTEGER NOT NULL DEFAULT 10')
    conn.execute('UPDATE quizzes SET duration_minutes=60 WHERE duration_minutes IS NULL OR duration_minutes < 1')
    conn.execute('UPDATE quizzes SET max_attempts=1 WHERE max_attempts IS NULL OR max_attempts < 1')
    conn.execute('UPDATE quizzes SET grade_level=10 WHERE grade_level IS NULL OR grade_level NOT BETWEEN 7 AND 12')
    attempt_cols = {r['name'] for r in conn.execute('PRAGMA table_info(attempts)').fetchall()}
    if 'hidden' not in attempt_cols:
        conn.execute('ALTER TABLE attempts ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0')
    if 'submission_token' not in attempt_cols:
        conn.execute('ALTER TABLE attempts ADD COLUMN submission_token TEXT')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_attempt_submission_token ON attempts(submission_token) WHERE submission_token IS NOT NULL')
    user_cols = {r['name'] for r in conn.execute('PRAGMA table_info(users)').fetchall()}
    if 'grade_level' not in user_cols:
        conn.execute('ALTER TABLE users ADD COLUMN grade_level INTEGER')
    if 'last_login' not in user_cols:
        conn.execute('ALTER TABLE users ADD COLUMN last_login TEXT')
    if 'last_seen' not in user_cols:
        conn.execute('ALTER TABLE users ADD COLUMN last_seen TEXT')
    if 'full_name' not in user_cols:
        conn.execute('ALTER TABLE users ADD COLUMN full_name TEXT')
    if 'gender' not in user_cols:
        conn.execute('ALTER TABLE users ADD COLUMN gender TEXT')
    if 'password_temp' not in user_cols:
        conn.execute('ALTER TABLE users ADD COLUMN password_temp INTEGER NOT NULL DEFAULT 0')
    conn.execute("UPDATE users SET grade_level=10 WHERE role='student' AND grade_level IS NULL")
    conn.execute("UPDATE users SET full_name=username WHERE role='student' AND (full_name IS NULL OR TRIM(full_name)='')")
    conn.execute("UPDATE users SET gender=NULL WHERE role='student' AND gender IS NOT NULL AND gender NOT IN ('Male','Female')")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_logs(created_at DESC)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_logs(actor_id)')

    if not conn.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
        conn.execute('INSERT INTO users(username,password_hash,role) VALUES(?,?,?)',
                     ('admin', generate_password_hash('admin123'), 'admin'))
    if not conn.execute('SELECT 1 FROM quizzes').fetchone():
        qid = conn.execute('INSERT INTO quizzes(title,duration_minutes,max_attempts,grade_level,created_at) VALUES(?,?,?,?,?)',
                           ('TLE-ICT-CSS Grade 10 Quiz', DEFAULT_DURATION, 1, 10, now())).lastrowid
        sample = [
            ('Which component is commonly called the brain of the computer?', ['RAM','CPU','SSD','Power supply'], 'B'),
            ('Which tool protects components from electrostatic discharge?', ['Anti-static wrist strap','Paint brush','Hammer','Flashlight'], 'A'),
            ('Which device provides temporary working memory?', ['RAM','HDD','Monitor','Keyboard'], 'A'),
            ('Which software manages hardware resources?', ['Operating system','Web browser','Word processor','Media player'], 'A')
        ]
        for question, choices, answer in sample:
            conn.execute('INSERT INTO questions(quiz_id,question,a,b,c,d,answer) VALUES(?,?,?,?,?,?,?)',
                         (qid, question, *choices, answer))
    conn.commit()
    conn.close()
    if DUCKDB_AVAILABLE:
        threading.Thread(target=sync_all_analytics, daemon=True, name='duckdb-sync-startup').start()


def current_user():
    uid = session.get('uid')
    if not uid:
        return None
    conn = db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    conn.close()
    return user


def require_user():
    return current_user()


def teacher_can_access_quiz(user, quiz_id):
    if not user:
        return False
    if user['role'] == 'admin':
        return True
    if user['role'] != 'teacher':
        return False
    conn = db()
    row = conn.execute('SELECT created_by FROM quizzes WHERE id=?', (quiz_id,)).fetchone()
    conn.close()
    return bool(row and row['created_by'] == user['id'])


def teacher_or_admin(user):
    return bool(user and user['role'] in ('admin','teacher'))


def teacher_dashboard_url(user):
    return 'teacher_dashboard' if user and user['role'] == 'teacher' else 'admin'


def parse_questions(raw):
    blocks = re.split(r'\n\s*\n', raw.strip()) if raw.strip() else []
    parsed, errors = [], []
    for index, block in enumerate(blocks, 1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        qline = next((line for line in lines if re.match(r'^(?:question\s*\d*|q\s*\d*|\d+)[\s:.)-]+', line, re.I)), None)
        if not qline:
            errors.append(f'Block {index}: missing question line.')
            continue
        question = re.sub(r'^(?:question\s*\d*|q\s*\d*|\d+)[\s:.)-]+', '', qline, flags=re.I).strip()
        choices, answer, image_ref = {}, None, None
        for line in lines:
            m = re.match(r'^([ABCD])[\s.)\-:]+(.+)$', line, re.I)
            if m:
                choices[m.group(1).upper()] = m.group(2).strip()
                continue
            m = re.match(r'^answer\s*[:\-]?\s*([ABCD])\s*$', line, re.I)
            if m:
                answer = m.group(1).upper()
                continue
            m = re.match(r'^image\s*[:\-]?\s*(.+)$', line, re.I)
            if m:
                image_ref = m.group(1).strip()
        missing = [letter for letter in 'ABCD' if not choices.get(letter)]
        if not question:
            errors.append(f'Block {index}: question text is empty.')
        elif missing:
            errors.append(f'Block {index}: missing choice(s): {", ".join(missing)}.')
        elif answer not in choices:
            errors.append(f'Block {index}: answer must be A, B, C, or D.')
        else:
            parsed.append({'question':question,'a':choices['A'],'b':choices['B'],'c':choices['C'],'d':choices['D'],'answer':answer,'image_ref':image_ref})
    return parsed, errors

def save_question_images(files):
    mapping = {}
    for f in files:
        if not f or not f.filename:
            continue
        original = f.filename
        safe = secure_filename(original)
        ext = safe.rsplit('.',1)[-1].lower() if '.' in safe else ''
        if not safe or ext not in ALLOWED_IMAGE_EXTENSIONS:
            continue
        stored = f"{secrets.token_hex(8)}_{safe}"
        path = os.path.join(QUESTION_IMAGE_DIR, stored)
        f.save(path)
        mapping[original.lower()] = '/static/uploads/questions/' + stored
        mapping[safe.lower()] = '/static/uploads/questions/' + stored
    return mapping

def resolve_question_image(ref, uploaded_map, existing_image=None):
    if not ref:
        ref = existing_image or ''
    ref = str(ref).strip()
    if not ref:
        return None
    if ref.startswith('data:image/'):
        return ref
    if ref.startswith('/static/uploads/questions/'):
        name = os.path.basename(ref)
        return '/question-image/' + name if name else existing_image
    if ref.startswith('/question-image/'):
        return '/question-image/' + os.path.basename(ref)
    return uploaded_map.get(ref.lower(), existing_image)


def get_quizzes(owner_id=None):
    conn = db()
    if owner_id is None:
        rows = conn.execute('''
            SELECT q.*, COUNT(x.id) AS n, COALESCE(u.full_name,u.username,'Administrator') AS creator_name
            FROM quizzes q LEFT JOIN questions x ON x.quiz_id=q.id
            LEFT JOIN users u ON u.id=q.created_by
            GROUP BY q.id ORDER BY q.id DESC
        ''').fetchall()
    else:
        rows = conn.execute('''
            SELECT q.*, COUNT(x.id) AS n, COALESCE(u.full_name,u.username) AS creator_name
            FROM quizzes q LEFT JOIN questions x ON x.quiz_id=q.id
            LEFT JOIN users u ON u.id=q.created_by
            WHERE q.created_by=? GROUP BY q.id ORDER BY q.id DESC
        ''', (owner_id,)).fetchall()
    conn.close()
    return rows


def make_temp_password(length=8):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


@app.route('/qr.png')
def qr_code():
    """Return a QR code for the current LAN or public tunnel URL."""
    img = qrcode.make(student_access_url())
    out = io.BytesIO()
    img.save(out, format='PNG')
    out.seek(0)
    return send_file(out, mimetype='image/png', max_age=0)

@app.route('/question-image/<path:filename>')
def question_image(filename):
    # Explicitly serve local question images. Use send_file rather than relying
    # on static-file routing so this remains reliable when the project is moved.
    safe_name = os.path.basename(str(filename).replace('\\','/'))
    if not safe_name or safe_name != str(filename).replace('\\','/').split('/')[-1]:
        return ('', 404)
    path = os.path.join(QUESTION_IMAGE_DIR, safe_name)
    if not os.path.isfile(path):
        return ('', 404)
    import mimetypes
    mimetype = mimetypes.guess_type(safe_name)[0] or 'application/octet-stream'
    return send_file(path, mimetype=mimetype, conditional=True, max_age=0)

@app.route('/')
def home():
    user = current_user()
    if not user:
        return redirect(url_for('login'))
    return redirect(url_for('admin' if user['role'] == 'admin' else ('teacher_dashboard' if user['role']=='teacher' else 'student')))


@app.route('/login', methods=['GET', 'POST'])
def login():
    msg = ''
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            msg = 'Please enter both username and password.'
            return render_template('login.html', msg=msg), 400
        conn = db()
        user = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            # Keep the signed LAN session very small and refresh it cleanly on login.
            session.clear()
            session.permanent = True
            session['uid'] = int(user['id'])
            session['login_at'] = now()
            session.modified = True

            with DB_WRITE_LOCK:
                conn = db()
                try:
                    stamp = now()
                    conn.execute('UPDATE users SET last_login=?, last_seen=? WHERE id=?',
                                 (stamp, stamp, user['id']))
                    conn.commit()
                finally:
                    conn.close()
            audit_log('LOGIN_SUCCESS', user, 'user', user['id'], 'Successful login')

            if int(user['password_temp'] or 0) == 1:
                return redirect(url_for('change_temporary_password'))
            return redirect(url_for('home'))
        audit_log('LOGIN_FAILED', None, 'user', None,
                  f'Failed login attempt for username={username[:120]}')
        msg = 'Invalid username or password.'
    return render_template('login.html', msg=msg)


@app.route('/session-check')
def session_check():
    """Small LAN diagnostic endpoint used to confirm that the browser retained login state."""
    user = current_user()
    if not user:
        return jsonify({'ok': False, 'logged_in': False, 'message': 'No active login session.'}), 401
    return jsonify({'ok': True, 'logged_in': True, 'role': user['role'], 'user_id': user['id']})


@app.route('/register', methods=['GET', 'POST'])
def register_disabled():
    """Self-registration is disabled; student accounts are provisioned by administrators."""
    return redirect(url_for('login'))


@app.route('/logout')
def logout():
    user=require_user()
    if user: audit_log('LOGOUT', user, 'user', user['id'], 'User logged out')
    session.clear(); return redirect(url_for('login'))


@app.route('/student')
def student():
    user = require_user()
    if not user: return redirect(url_for('login'))
    conn = db()
    quizzes = conn.execute('''
        SELECT q.*, COUNT(x.id) AS n,
               COUNT(DISTINCT a.id) AS attempts,
               COALESCE(MAX(a.percentage),0) AS best_score,
               COALESCE(AVG(a.percentage),0) AS avg_score,
               MAX(a.completed_at) AS last_attempt
        FROM quizzes q
        LEFT JOIN questions x ON x.quiz_id=q.id
        LEFT JOIN attempts a ON a.quiz_id=q.id AND a.user_id=?
        WHERE q.grade_level = ?
        GROUP BY q.id ORDER BY q.id DESC
    ''', (user['id'], user['grade_level'] or 0)).fetchall()
    conn.close()
    quiz_cards=[]
    for q in quizzes:
        item=dict(q)
        item['availability']=quiz_availability(q)
        item['available_from_display']=format_schedule(q['available_from'])
        item['available_until_display']=format_schedule(q['available_until'])
        quiz_cards.append(item)
    return render_template('student.html', user=user, quizzes=quiz_cards)


@app.route('/quiz/<int:qid>')
def quiz(qid):
    user = require_user()
    if not user: return redirect(url_for('login'))
    conn = db()
    quiz_row = conn.execute('SELECT * FROM quizzes WHERE id=?', (qid,)).fetchone()
    rows = conn.execute('SELECT * FROM questions WHERE quiz_id=? ORDER BY id', (qid,)).fetchall()
    conn.close()
    if not quiz_row or not rows or int(quiz_row['grade_level']) != int(user['grade_level'] or 0): return redirect(url_for('student'))
    availability = quiz_availability(quiz_row)
    if not availability['available']:
        return redirect(url_for('student'))
    conn2=db(); used=conn2.execute('SELECT COUNT(*) FROM attempts WHERE user_id=? AND quiz_id=?',(user['id'],qid)).fetchone()[0]; conn2.close()
    if used >= int(quiz_row['max_attempts']): return redirect(url_for('student'))
    questions = [{'id': r['id'], 'q': r['question'], 'choices': [r['a'],r['b'],r['c'],r['d']], 'answer': r['answer'], 'image': r['image']} for r in rows]
    return render_template('quiz.html', quiz=quiz_row, questions=questions, user=user)


@app.route('/submit', methods=['POST'])
def submit():
    user = require_user()
    if not user: return jsonify(ok=False, error='You are not logged in.'), 401
    data = request.get_json(silent=True) or {}
    conn = None
    try:
        quiz_id = int(data.get('quiz_id'))
        question_order = data.get('question_order') or []
        answers = data.get('answers') or []
        times = data.get('times') or []
        choice_order = data.get('choice_order') or []
        submission_token = str(data.get('submission_token') or '').strip()
        if not submission_token or len(submission_token) > 100:
            submission_token = secrets.token_urlsafe(24)
        if not question_order: raise ValueError('No questions were submitted.')
        if len(answers) != len(question_order) or len(times) != len(question_order):
            raise ValueError('Answer data is incomplete. Please restart the quiz.')
        conn = db()
        existing_token = conn.execute('SELECT id,score,percentage FROM attempts WHERE submission_token=?',(submission_token,)).fetchone()
        if existing_token:
            conn.close()
            audit_log('QUIZ_DUPLICATE_SUBMISSION', user, 'attempt', existing_token['id'], 'Duplicate submission token safely ignored')
            return jsonify(ok=True, score=existing_token['score'], percentage=round(existing_token['percentage'],1), duplicate=True)
        quiz_row = conn.execute('SELECT * FROM quizzes WHERE id=?', (quiz_id,)).fetchone()
        if not quiz_row: raise ValueError('Quiz no longer exists.')
        if int(quiz_row['grade_level']) != int(user['grade_level'] or 0): raise ValueError('This quiz is not assigned to your grade level.')
        availability = quiz_availability(quiz_row)
        if availability['state'] == 'scheduled':
            raise ValueError('This quiz is not available yet.')
        if availability['state'] == 'closed':
            # A student may finish a quiz that was legitimately started before its
            # availability window closed, but cannot start a new attempt afterward.
            submitted_start = str(data.get('started_at') or '').strip()
            try:
                started_dt = datetime.fromisoformat(submitted_start) if submitted_start else None
            except ValueError:
                started_dt = None
            if not started_dt or (availability['end'] and started_dt >= availability['end']):
                raise ValueError('This quiz is no longer available.')
        existing_attempts = conn.execute('SELECT COUNT(*) FROM attempts WHERE user_id=? AND quiz_id=?',(user['id'],quiz_id)).fetchone()[0]
        if existing_attempts >= int(quiz_row['max_attempts']): raise ValueError('You have used all allowed attempts for this quiz.')
        rows = conn.execute('SELECT * FROM questions WHERE quiz_id=?', (quiz_id,)).fetchall()
        question_map = {int(r['id']): r for r in rows}
        if set(map(int, question_order)) != set(question_map.keys()):
            raise ValueError('The quiz question order is invalid. Please restart the quiz.')
        max_seconds = max(60, int(quiz_row['duration_minutes']) * 60)
        correct = incorrect = skipped = timed_out = 0
        safe_times, safe_answers = [], []
        for raw_id, raw_answer, raw_time in zip(question_order, answers, times):
            qid = int(raw_id); row = question_map[qid]
            try: t = max(0.0, min(float(max_seconds), float(raw_time)))
            except (TypeError, ValueError): t = 0.0
            safe_times.append(round(t,2))
            if raw_answer == 'timed': timed_out += 1; safe_answers.append('timed')
            elif raw_answer in (None, 'skip'):
                raise ValueError('Please answer all questions before submitting. Skipped questions must be answered before the quiz can be submitted.')
            elif isinstance(raw_answer, int) and 0 <= raw_answer <= 3:
                letter = 'ABCD'[raw_answer]; safe_answers.append(letter)
                if letter == row['answer']: correct += 1
                else: incorrect += 1
            elif isinstance(raw_answer, str) and raw_answer.upper() in 'ABCD':
                letter = raw_answer.upper(); safe_answers.append(letter)
                if letter == row['answer']: correct += 1
                else: incorrect += 1
            else:
                raise ValueError('One or more questions do not have a valid answer. Please review the quiz before submitting.')
        total = len(question_order)
        percentage = (correct / total) * 100 if total else 0
        average_time = sum(safe_times) / total if total else 0
        attempt_number = conn.execute('SELECT COALESCE(MAX(attempt_number),0)+1 FROM attempts WHERE user_id=? AND quiz_id=?', (user['id'],quiz_id)).fetchone()[0]
        attempt_cursor = conn.execute('''INSERT INTO attempts(user_id,quiz_id,attempt_number,started_at,completed_at,score,percentage,correct,incorrect,skipped,timed_out,average_time,passed,question_order,choice_order,answers,status,submission_token)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                     (user['id'],quiz_id,attempt_number,data.get('started_at'),now(),correct,percentage,correct,incorrect,skipped,timed_out,average_time,int(percentage>=PASS_MARK),json.dumps(question_order),json.dumps(choice_order),json.dumps({'answers':safe_answers,'times':safe_times}), 'completed', submission_token))
        attempt_id=attempt_cursor.lastrowid
        conn.commit(); conn.close()
        audit_log('QUIZ_SUBMITTED', user, 'attempt', attempt_id, f'Quiz {quiz_id}; score={correct}/{total}; percentage={percentage:.1f}; attempt={attempt_number}')
        threading.Thread(target=sync_attempt_to_analytics,args=(attempt_id,),daemon=True,name='duckdb-sync-attempt').start()
        return jsonify(ok=True, score=correct, percentage=round(percentage,1))
    except (ValueError,TypeError,sqlite3.Error) as exc:
        if conn:
            try: conn.close()
            except Exception: pass
        return jsonify(ok=False,error=str(exc)),400


@app.route('/history')
def history():
    user = require_user()
    if not user: return redirect(url_for('login'))
    conn = db()
    rows = conn.execute('''SELECT a.*,q.title,q.duration_minutes FROM attempts a JOIN quizzes q ON q.id=a.quiz_id WHERE a.user_id=? ORDER BY a.id DESC''',(user['id'],)).fetchall()
    conn.close()
    return render_template('history.html', rows=rows, user=user)


@app.route('/student/reviews')
def student_reviews():
    user=require_user()
    if not user or user['role']!='student': return redirect(url_for('login'))
    conn=db()
    rows=conn.execute('''SELECT a.id,a.attempt_number,a.completed_at,a.percentage,a.correct,a.incorrect,a.timed_out,
                                q.id AS quiz_id,q.title,q.grade_level,q.review_enabled
                         FROM attempts a JOIN quizzes q ON q.id=a.quiz_id
                         WHERE a.user_id=? AND q.review_enabled=1 AND q.grade_level=?
                         ORDER BY a.id DESC''',(user['id'],user['grade_level'] or 0)).fetchall()
    conn.close()
    return render_template('student_reviews.html',rows=rows,user=user)

@app.route('/student/review/<int:attempt_id>')
def student_review(attempt_id):
    user=require_user()
    if not user or user['role']!='student': return redirect(url_for('login'))
    conn=db()
    attempt=conn.execute('''SELECT a.*,q.title,q.grade_level,q.review_enabled
                            FROM attempts a JOIN quizzes q ON q.id=a.quiz_id
                            WHERE a.id=? AND a.user_id=?''',(attempt_id,user['id'])).fetchone()
    if not attempt or not attempt['review_enabled'] or int(attempt['grade_level']) != int(user['grade_level'] or 0):
        conn.close(); return redirect(url_for('student_reviews'))
    try:
        order=json.loads(attempt['question_order'] or '[]')
        saved=json.loads(attempt['answers'] or '{}')
        answer_list=saved.get('answers',[]) if isinstance(saved,dict) else []
        time_list=saved.get('times',[]) if isinstance(saved,dict) else []
    except (TypeError,ValueError,json.JSONDecodeError):
        conn.close(); return redirect(url_for('student_reviews'))
    qmap={int(r['id']):r for r in conn.execute('SELECT * FROM questions WHERE quiz_id=?',(attempt['quiz_id'],)).fetchall()}
    review=[]
    for i,raw_qid in enumerate(order):
        try: qid=int(raw_qid)
        except (TypeError,ValueError): continue
        q=qmap.get(qid)
        if not q: continue
        selected=answer_list[i] if i < len(answer_list) else 'timed'
        correct=q['answer']
        if selected=='timed': status='Timed Out'
        elif selected==correct: status='Correct'
        else: status='Incorrect'
        review.append({'number':i+1,'question':q['question'],'choices':[q['a'],q['b'],q['c'],q['d']],
                       'selected':selected,'correct':correct,'status':status,
                       'time':time_list[i] if i < len(time_list) else 0})
    conn.close()
    return render_template('student_review.html',attempt=attempt,review=review,user=user)

@app.route('/progress')
def progress():
    user = require_user()
    if not user: return redirect(url_for('login'))
    conn = db()
    rows = conn.execute('''
        SELECT q.id,q.title,q.duration_minutes,q.grade_level,COUNT(a.id) attempts,
               COALESCE(MAX(a.percentage),0) best_score,
               COALESCE(AVG(a.percentage),0) avg_score,
               COALESCE(MAX(a.correct),0) best_correct,
               MAX(a.completed_at) last_attempt
        FROM quizzes q LEFT JOIN attempts a ON a.quiz_id=q.id AND a.user_id=?
        WHERE q.grade_level=?
        GROUP BY q.id ORDER BY q.id DESC
    ''',(user['id'], user['grade_level'] or 0)).fetchall()
    totals = conn.execute('''SELECT COUNT(*) attempts, COALESCE(AVG(percentage),0) avg_score, COALESCE(MAX(percentage),0) best_score, SUM(passed) passed FROM attempts WHERE user_id=?''',(user['id'],)).fetchone()
    conn.close()
    return render_template('progress.html', rows=rows, totals=totals, user=user)


@app.route('/change-temporary-password', methods=['GET','POST'])
def change_temporary_password():
    user=require_user()
    if not user: return redirect(url_for('login'))
    if int(user['password_temp'] or 0) != 1:
        return redirect(url_for('home'))
    error=None
    if request.method=='POST':
        new=request.form.get('new_password','')
        confirm=request.form.get('confirm_password','')
        if len(new) < 6:
            error='New password must be at least 6 characters.'
        elif new != confirm:
            error='New passwords do not match.'
        else:
            conn=db(); conn.execute('UPDATE users SET password_hash=?, password_temp=0 WHERE id=?',(generate_password_hash(new),user['id'])); conn.commit(); conn.close()
            audit_log('TEMP_PASSWORD_CHANGED', user, 'user', user['id'], 'Temporary password replaced by user')
            if user['role']=='teacher': return redirect(url_for('teacher_dashboard'))
            if user['role']=='student': return redirect(url_for('student'))
            return redirect(url_for('home'))
    return render_template('change_temporary_password.html', user=user, error=error)


@app.route('/student/change-password', methods=['POST'])
def student_change_password():
    user = require_user()
    if not user: return redirect(url_for('login'))
    current = request.form.get('current_password','')
    new = request.form.get('new_password','')
    confirm = request.form.get('confirm_password','')
    if not check_password_hash(user['password_hash'], current):
        return render_template('progress.html', rows=[], totals={'attempts':0,'avg_score':0,'best_score':0,'passed':0}, user=user, password_error='Current password is incorrect.')
    if len(new) < 4 or new != confirm:
        return redirect(url_for('progress', password_error='New passwords must match and contain at least 4 characters.'))
    conn=db(); conn.execute('UPDATE users SET password_hash=? WHERE id=?',(generate_password_hash(new),user['id'])); conn.commit(); conn.close()
    audit_log('PASSWORD_CHANGED', user, 'user', user['id'], 'User changed password')
    return redirect(url_for('progress', password_changed=1))


@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    user=require_user()
    if not user or user['role']!='student': return jsonify(error='forbidden'),403
    conn=None
    try:
        stamp=now()
        # Avoid turning every 10-second heartbeat from every device into a write.
        # A write is only needed when the previous heartbeat is older than 12 seconds.
        cutoff=(datetime.now()-timedelta(seconds=20)).isoformat(timespec='seconds')
        with DB_WRITE_LOCK:
            conn=db()
            try:
                cur=conn.execute("UPDATE users SET last_seen=? WHERE id=? AND (last_seen IS NULL OR last_seen < ?)",(stamp,user['id'],cutoff))
                if cur.rowcount:
                    conn.commit()
                else:
                    conn.rollback()
            finally:
                conn.close()
        return jsonify(ok=True)
    except sqlite3.OperationalError as exc:
        if conn:
            try: conn.rollback(); conn.close()
            except Exception: pass
        return jsonify(ok=False,error='Server is busy. Your online status will update automatically.'),503

@app.route('/api/online-students')
def online_students():
    user=require_user()
    if not teacher_or_admin(user): return jsonify(error='forbidden'),403
    cutoff=(datetime.now()-timedelta(seconds=30)).isoformat(timespec='seconds')
    try:
        conn=db()
        rows=conn.execute("SELECT id,username,COALESCE(full_name,username) AS full_name,gender,grade_level,last_seen FROM users WHERE role='student' ORDER BY full_name,username").fetchall()
        conn.close()
    except sqlite3.OperationalError:
        if 'conn' in locals():
            try: conn.close()
            except Exception: pass
        return jsonify(error='Database is busy. Please retry shortly.'),503
    students=[]
    for r in rows:
        d=dict(r)
        d['online']=bool(d.get('last_seen') and d['last_seen'] >= cutoff)
        students.append(d)
    return jsonify(count=sum(1 for x in students if x['online']), students=students)



def build_item_analysis(quiz_id, include_hidden=False):
    """Use DuckDB analytics first; retain the V20 SQLite algorithm as a safe fallback."""
    conn=db(); quiz_row=conn.execute('SELECT * FROM quizzes WHERE id=?',(quiz_id,)).fetchone(); questions=conn.execute('SELECT * FROM questions WHERE quiz_id=? ORDER BY id',(quiz_id,)).fetchall(); conn.close()
    if not quiz_row: return None,[]
    ac=analytics_connection()
    if ac is not None:
        try:
            hidden_clause='' if include_hidden else 'AND aa.hidden=FALSE'
            attempts=ac.execute(f'SELECT attempt_id,percentage FROM attempt_analytics aa WHERE quiz_id=? {"" if include_hidden else "AND hidden=FALSE"} ORDER BY percentage DESC,attempt_id DESC',(quiz_id,)).fetchall()
            n=len(attempts); gs=max(1,int(round(n*.27))) if n else 0; upper=[int(x[0]) for x in attempts[:gs]]; lower=[int(x[0]) for x in attempts[-gs:]] if n else []
            results=[]
            for q in questions:
                qid=int(q['id']); key=str(q['answer']).upper(); base=f'FROM response_analytics ra JOIN attempt_analytics aa ON aa.attempt_id=ra.attempt_id WHERE ra.quiz_id=? AND ra.question_id=? {hidden_clause}'
                total=int(ac.execute('SELECT COUNT(*) '+base,(quiz_id,qid)).fetchone()[0]); correct=int(ac.execute('SELECT COUNT(*) '+base+' AND ra.is_correct=TRUE',(quiz_id,qid)).fetchone()[0]); difficulty=correct/total*100 if total else 0
                if upper:
                    uc=int(ac.execute('SELECT COUNT(*) '+base+' AND ra.is_correct=TRUE AND ra.attempt_id IN (SELECT UNNEST(?))',(quiz_id,qid,upper)).fetchone()[0]); lc=int(ac.execute('SELECT COUNT(*) '+base+' AND ra.is_correct=TRUE AND ra.attempt_id IN (SELECT UNNEST(?))',(quiz_id,qid,lower)).fetchone()[0]); disc=uc/gs-lc/gs
                else: uc=lc=0; disc=0
                dl='No responses' if not total else ('Very Difficult' if difficulty<30 else 'Difficult' if difficulty<50 else 'Moderate' if difficulty<75 else 'Easy' if difficulty<90 else 'Very Easy'); il='Insufficient data' if not gs else ('Excellent' if disc>=.40 else 'Good' if disc>=.30 else 'Fair / Review' if disc>=.20 else 'Needs Review')
                results.append({'number':len(results)+1,'id':qid,'question':q['question'],'answer':key,'total':total,'correct':correct,'incorrect':max(0,total-correct),'difficulty':difficulty,'difficulty_label':dl,'upper_correct':uc,'lower_correct':lc,'discrimination':disc*100,'discrimination_label':il})
            ac.close(); return quiz_row,results
        except Exception:
            try: ac.close()
            except Exception: pass
    return build_item_analysis_sqlite_fallback(quiz_id,include_hidden)


def build_item_analysis_sqlite_fallback(quiz_id, include_hidden=False):
    conn=db(); quiz_row=conn.execute('SELECT * FROM quizzes WHERE id=?',(quiz_id,)).fetchone(); questions=conn.execute('SELECT * FROM questions WHERE quiz_id=? ORDER BY id',(quiz_id,)).fetchall(); where='' if include_hidden else ' AND a.hidden=0'; attempts=conn.execute(f"SELECT a.id,a.percentage,a.answers,a.question_order FROM attempts a WHERE a.quiz_id=? AND a.status='completed'{where} ORDER BY a.percentage DESC,a.id DESC",(quiz_id,)).fetchall(); conn.close()
    if not quiz_row:return None,[]
    parsed=[]
    for a in attempts:
        try:
            payload=json.loads(a['answers'] or '{}'); ans=payload.get('answers') or []; order=json.loads(a['question_order'] or '[]')
            if len(ans)!=len(order):continue
            parsed.append({'percentage':float(a['percentage'] or 0),'answers':{int(q):v for q,v in zip(order,ans)}})
        except Exception:continue
    n=len(parsed); gs=max(1,int(round(n*.27))) if n else 0; upper=parsed[:gs]; lower=parsed[-gs:] if n else []; results=[]
    for q in questions:
        key=str(q['answer']).upper(); vals=[a['answers'].get(int(q['id'])) for a in parsed]; valid=[v for v in vals if v in ('A','B','C','D')]; correct=sum(v==key for v in valid); total=len(valid); diff=correct/total*100 if total else 0; uc=sum(a['answers'].get(int(q['id']))==key for a in upper); lc=sum(a['answers'].get(int(q['id']))==key for a in lower); disc=(uc/gs-lc/gs) if gs else 0; dl='No responses' if not total else ('Very Difficult' if diff<30 else 'Difficult' if diff<50 else 'Moderate' if diff<75 else 'Easy' if diff<90 else 'Very Easy'); il='Insufficient data' if not gs else ('Excellent' if disc>=.40 else 'Good' if disc>=.30 else 'Fair / Review' if disc>=.20 else 'Needs Review')
        results.append({'number':len(results)+1,'id':int(q['id']),'question':q['question'],'answer':key,'total':total,'correct':correct,'incorrect':max(0,total-correct),'difficulty':diff,'difficulty_label':dl,'upper_correct':uc,'lower_correct':lc,'discrimination':disc*100,'discrimination_label':il})
    return quiz_row,results

@app.route('/admin/item-analysis/<int:qid>')
def item_analysis(qid):
    user=require_user()
    if not teacher_or_admin(user) or not teacher_can_access_quiz(user,qid): return redirect(url_for('login'))
    include_hidden=request.args.get('include_hidden')=='1'
    quiz_row, items=build_item_analysis(qid, include_hidden)
    if not quiz_row: return redirect(url_for('admin'))
    return render_template('item_analysis.html', quiz=quiz_row, items=items, include_hidden=include_hidden, is_teacher=(user['role']=='teacher'))

@app.route('/admin/item-analysis/<int:qid>.csv')
def item_analysis_csv(qid):
    user=require_user()
    if not teacher_or_admin(user) or not teacher_can_access_quiz(user,qid): return 'Forbidden',403
    include_hidden=request.args.get('include_hidden')=='1'
    quiz_row, items=build_item_analysis(qid, include_hidden)
    if not quiz_row: return 'Quiz not found',404
    out=io.StringIO(); writer=csv.writer(out)
    writer.writerow(['Quiz','Item','Question','Correct Answer','Responses','Correct','Incorrect','Difficulty %','Difficulty Interpretation','Upper Group Correct','Lower Group Correct','Discrimination %','Discrimination Interpretation'])
    for x in items:
        writer.writerow([quiz_row['title'],x['number'],x['question'],x['answer'],x['total'],x['correct'],x['incorrect'],f"{x['difficulty']:.1f}",x['difficulty_label'],x['upper_correct'],x['lower_correct'],f"{x['discrimination']:.1f}",x['discrimination_label']])
    resp=make_response(out.getvalue())
    resp.headers['Content-Type']='text/csv; charset=utf-8'
    resp.headers['Content-Disposition']=f'attachment; filename="item_analysis_quiz_{qid}.csv"'
    return resp

@app.route('/admin/analytics')
def admin_analytics():
    user=require_user()
    if not user or user['role']!='admin': return redirect(url_for('login'))
    status=analytics_status(); data={'quiz_count':0,'attempt_count':0,'response_count':0,'average_percentage':0,'passing_rate':0,'top_quizzes':[]}; ac=analytics_connection()
    if ac is not None:
        try:
            data['quiz_count']=int(ac.execute('SELECT COUNT(DISTINCT quiz_id) FROM attempt_analytics').fetchone()[0]); data['attempt_count']=int(ac.execute('SELECT COUNT(*) FROM attempt_analytics').fetchone()[0]); data['response_count']=int(ac.execute('SELECT COUNT(*) FROM response_analytics').fetchone()[0]); data['average_percentage']=float(ac.execute('SELECT COALESCE(AVG(percentage),0) FROM attempt_analytics WHERE hidden=FALSE').fetchone()[0]); data['passing_rate']=float(ac.execute('SELECT COALESCE(AVG(CASE WHEN passed THEN 100.0 ELSE 0.0 END),0) FROM attempt_analytics WHERE hidden=FALSE').fetchone()[0]); data['top_quizzes']=[dict(zip(['quiz_id','quiz_title','attempts','mps'],r)) for r in ac.execute('SELECT quiz_id,quiz_title,COUNT(*),AVG(percentage) FROM attempt_analytics WHERE hidden=FALSE GROUP BY quiz_id,quiz_title ORDER BY COUNT(*) DESC LIMIT 10').fetchall()]; ac.close()
        except Exception:
            try: ac.close()
            except Exception: pass
    return render_template('analytics_dashboard.html',user=user,status=status,data=data,scope='Administrator')


@app.route('/teacher/analytics')
def teacher_analytics():
    user=require_user()
    if not user or user['role']!='teacher': return redirect(url_for('login'))
    status=analytics_status(); data={'quiz_count':0,'attempt_count':0,'response_count':0,'average_percentage':0,'passing_rate':0,'top_quizzes':[]}; ac=analytics_connection()
    if ac is not None:
        try:
            owner=user['id']; data['quiz_count']=int(ac.execute('SELECT COUNT(DISTINCT quiz_id) FROM attempt_analytics WHERE created_by=?',(owner,)).fetchone()[0]); data['attempt_count']=int(ac.execute('SELECT COUNT(*) FROM attempt_analytics WHERE created_by=?',(owner,)).fetchone()[0]); data['response_count']=int(ac.execute('SELECT COUNT(*) FROM response_analytics ra JOIN attempt_analytics aa ON aa.attempt_id=ra.attempt_id WHERE aa.created_by=?',(owner,)).fetchone()[0]); data['average_percentage']=float(ac.execute('SELECT COALESCE(AVG(percentage),0) FROM attempt_analytics WHERE created_by=? AND hidden=FALSE',(owner,)).fetchone()[0]); data['passing_rate']=float(ac.execute('SELECT COALESCE(AVG(CASE WHEN passed THEN 100.0 ELSE 0.0 END),0) FROM attempt_analytics WHERE created_by=? AND hidden=FALSE',(owner,)).fetchone()[0]); data['top_quizzes']=[dict(zip(['quiz_id','quiz_title','attempts','mps'],r)) for r in ac.execute('SELECT quiz_id,quiz_title,COUNT(*),AVG(percentage) FROM attempt_analytics WHERE created_by=? AND hidden=FALSE GROUP BY quiz_id,quiz_title ORDER BY COUNT(*) DESC LIMIT 10',(owner,)).fetchall()]; ac.close()
        except Exception:
            try: ac.close()
            except Exception: pass
    return render_template('analytics_dashboard.html',user=user,status=status,data=data,scope='Teacher')


@app.route('/admin/analytics/sync', methods=['POST'])
def analytics_sync():
    user=require_user()
    if not user or user['role']!='admin': return jsonify(ok=False,error='forbidden'),403
    count=sync_all_analytics(); audit_log('ANALYTICS_SYNC', user, 'system', None, f'Manual DuckDB synchronization completed; synced={count}'); return jsonify(ok=True,synced=count,status=analytics_status())


@app.route('/admin/audit-log')
def admin_audit_log():
    user=require_user()
    if not user or user['role']!='admin': return redirect(url_for('login'))
    search=request.args.get('search','').strip()
    action=request.args.get('action','').strip()
    role=request.args.get('role','').strip()
    try: limit=max(25,min(500,int(request.args.get('limit','200'))))
    except ValueError: limit=200
    conn=db()
    clauses=[]; params=[]
    if search:
        term=f'%{search}%'
        clauses.append('(COALESCE(actor_username,\'\') LIKE ? OR COALESCE(details,\'\') LIKE ? OR COALESCE(target_type,\'\') LIKE ? OR CAST(COALESCE(target_id,\'\') AS TEXT) LIKE ?)')
        params.extend([term,term,term,term])
    if action:
        clauses.append('action=?'); params.append(action)
    if role:
        clauses.append('COALESCE(actor_role,\'\')=?'); params.append(role)
    sql='SELECT * FROM audit_logs'
    if clauses: sql+=' WHERE '+' AND '.join(clauses)
    sql+=' ORDER BY id DESC LIMIT ?'; params.append(limit)
    rows=conn.execute(sql,params).fetchall()
    actions=[r['action'] for r in conn.execute('SELECT DISTINCT action FROM audit_logs ORDER BY action').fetchall()]
    conn.close()
    audit_log('VIEW_AUDIT_LOG', user, 'system', None, f'Viewed audit log; filters search={search[:60]}, action={action}, role={role}')
    return render_template('audit_log.html',user=user,rows=rows,actions=actions,search=search,selected_action=action,selected_role=role,limit=limit)

@app.route('/admin/audit-log.csv')
def admin_audit_csv():
    user=require_user()
    if not user or user['role']!='admin': return 'Forbidden',403
    search=request.args.get('search','').strip(); action=request.args.get('action','').strip(); role=request.args.get('role','').strip()
    conn=db(); clauses=[]; params=[]
    if search:
        term=f'%{search}%'; clauses.append('(COALESCE(actor_username,\'\') LIKE ? OR COALESCE(details,\'\') LIKE ? OR COALESCE(target_type,\'\') LIKE ? OR CAST(COALESCE(target_id,\'\') AS TEXT) LIKE ?)'); params.extend([term,term,term,term])
    if action: clauses.append('action=?'); params.append(action)
    if role: clauses.append('COALESCE(actor_role,\'\')=?'); params.append(role)
    sql='SELECT * FROM audit_logs'
    if clauses: sql+=' WHERE '+' AND '.join(clauses)
    sql+=' ORDER BY id DESC'
    rows=conn.execute(sql,params).fetchall(); conn.close()
    out=io.StringIO(); writer=csv.writer(out)
    writer.writerow(['ID','Date/Time','Actor','Role','Action','Target','Target ID','Details','IP Address','User Agent'])
    for r in rows:
        writer.writerow([r['id'],r['created_at'],r['actor_username'] or 'System/Guest',r['actor_role'] or '',r['action'],r['target_type'] or '',r['target_id'] or '',r['details'] or '',r['ip_address'] or '',r['user_agent'] or ''])
    audit_log('EXPORT_AUDIT_LOG', user, 'system', None, f'Exported audit log CSV; rows={len(rows)}')
    return send_file(io.BytesIO(out.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name='audit_log.csv')

@app.route('/admin/maintenance', methods=['GET'])
def admin_maintenance():
    """Render the administrator maintenance page.

    The Windows EXE update workflow is not available on Render; this route
    keeps the admin navigation from returning a 404 and explains the runtime.
    """
    user = require_user()
    if not user or user['role'] != 'admin':
        return redirect(url_for('login'))

    is_render = bool(os.environ.get('RENDER') or os.environ.get('RENDER_SERVICE_ID'))
    return render_template(
        'admin_maintenance.html',
        user=user,
        app_version=os.environ.get('APP_VERSION', '24.6.2'),
        data_dir=BASE_DIR,
        restore_pending=False,
        success=None,
        error=(
            'Windows EXE update and local restore actions are not supported on Render. '
            'Use your Render deployment and Turso database tools for hosted maintenance.'
            if is_render else None
        ),
        update_validated=None,
    )


@app.route('/admin')
def admin():
    user=require_user()
    if not user or user['role']!='admin': return redirect(url_for('login'))
    return render_template('admin.html',quizzes=get_quizzes(),user=user,access_url=student_access_url())


@app.route('/admin/teachers')
def admin_teachers():
    user=require_user()
    if not user or user['role']!='admin': return redirect(url_for('login'))
    conn=db(); teachers=conn.execute("SELECT id,username,full_name,last_login FROM users WHERE role='teacher' ORDER BY full_name,username").fetchall(); conn.close()
    return render_template('teachers.html', user=user, teachers=teachers)


@app.route('/admin/create-teacher', methods=['POST'])
def create_teacher():
    user=require_user()
    if not user or user['role']!='admin': return 'Forbidden',403
    full_name=request.form.get('full_name','').strip()
    username=re.sub(r'[^a-z0-9]', '', full_name.lower())
    if len(full_name)<2 or len(username)<2:
        return redirect(url_for('admin_teachers', error='Please enter a valid teacher name.'))
    password=username+'123'
    conn=db()
    if conn.execute('SELECT 1 FROM users WHERE username=?',(username,)).fetchone():
        conn.close(); return redirect(url_for('admin_teachers', error=f'Username {username} already exists. Use a different name.'))
    new_id=conn.execute('INSERT INTO users(username,password_hash,role,full_name,password_temp) VALUES(?,?,?,?,?)',(username,generate_password_hash(password),'teacher',full_name,1)).lastrowid
    conn.commit(); conn.close()
    audit_log('CREATE_TEACHER', user, 'user', new_id, f'Created teacher account username={username}, name={full_name}')
    return redirect(url_for('admin_teachers', created_username=username, created_password=password))


@app.route('/admin/edit-teacher/<int:uid>', methods=['POST'])
def edit_teacher(uid):
    user=require_user()
    if not user or user['role']!='admin': return 'Forbidden',403
    full_name=request.form.get('full_name','').strip()
    if len(full_name) < 2:
        return redirect(url_for('admin_teachers', error='Please enter a valid teacher name.'))
    conn=db()
    target=conn.execute("SELECT id FROM users WHERE id=? AND role='teacher'",(uid,)).fetchone()
    if not target:
        conn.close(); return redirect(url_for('admin_teachers', error='Teacher account not found.'))
    conn.execute('UPDATE users SET full_name=? WHERE id=?',(full_name,uid))
    conn.commit(); conn.close()
    audit_log('EDIT_TEACHER', user, 'user', uid, f'Updated teacher name to {full_name}')
    return redirect(url_for('admin_teachers', updated=1))


@app.route('/admin/reset-teacher-password/<int:uid>', methods=['POST'])
def reset_teacher_password(uid):
    user=require_user()
    if not user or user['role']!='admin': return 'Forbidden',403
    temp=make_temp_password()
    conn=db(); target=conn.execute("SELECT id,username,full_name FROM users WHERE id=? AND role='teacher'",(uid,)).fetchone()
    if not target:
        conn.close(); return redirect(url_for('admin_teachers', error='Teacher account not found.'))
    conn.execute('UPDATE users SET password_hash=?, password_temp=1 WHERE id=?',(generate_password_hash(temp),uid))
    conn.commit(); conn.close()
    audit_log('RESET_TEACHER_PASSWORD', user, 'user', uid, f'Reset temporary password for teacher username={target["username"]}')
    return render_template('teacher_reset.html',user=user,teacher=target,temp_password=temp)


@app.route('/teacher')
def teacher_dashboard():
    user=require_user()
    if not user or user['role']!='teacher': return redirect(url_for('login'))
    return render_template('teacher.html',quizzes=get_quizzes(user['id']),user=user,access_url=student_access_url())


@app.route('/teacher/security', methods=['GET','POST'])
def teacher_security():
    user=require_user()
    if not user or user['role']!='teacher': return redirect(url_for('login'))
    message=error=None
    if request.method=='POST':
        current=request.form.get('current_password','')
        new=request.form.get('new_password','')
        confirm=request.form.get('confirm_password','')
        if not check_password_hash(user['password_hash'], current): error='Current password is incorrect.'
        elif len(new)<6: error='New password must be at least 6 characters.'
        elif new!=confirm: error='New passwords do not match.'
        else:
            conn=db(); conn.execute('UPDATE users SET password_hash=?, password_temp=0 WHERE id=?',(generate_password_hash(new),user['id'])); conn.commit(); conn.close(); audit_log('PASSWORD_CHANGED', user, 'user', user['id'], 'Teacher changed password'); message='Password changed successfully.'
    return render_template('teacher_security.html',user=user,message=message,error=error)



@app.route('/admin/bulk-register', methods=['POST'])
def admin_bulk_register():
    user = require_user()
    if not user or user['role'] != 'admin':
        return redirect(url_for('login'))

    # Each row uses: Complete Name, Gender, Grade Level. Commas or tabs separate fields.
    names = []
    seen_names = set()
    invalid_lines = []
    for line_no, raw_line in enumerate(request.form.get('student_names', '').splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if '\t' in line:
            fields = [part.strip() for part in line.split('\t')]
        else:
            fields = [part.strip() for part in line.rsplit(',', 2)]
        if len(fields) != 3:
            invalid_lines.append(f'Line {line_no}: use Complete Name, Gender, Grade Level (example: Juan Dela Cruz, Male, 7).')
            continue
        full_name, gender, grade_text = fields
        if len(full_name) < 2:
            invalid_lines.append(f'Line {line_no}: enter a complete student name.')
            continue
        if gender.casefold() not in ('male', 'female'):
            invalid_lines.append(f'Line {line_no}: gender must be Male or Female.')
            continue
        gender = 'Male' if gender.casefold() == 'male' else 'Female'
        try:
            line_grade = int(grade_text)
        except (TypeError, ValueError):
            line_grade = 0
        if line_grade not in range(7, 13):
            invalid_lines.append(f'Line {line_no}: grade level must be 7 to 12.')
            continue
        full_name = ' '.join(full_name.split())
        key = (full_name.casefold(), gender, line_grade)
        if key not in seen_names:
            names.append({'full_name': full_name, 'gender': gender, 'grade_level': line_grade})
            seen_names.add(key)

    if invalid_lines:
        return render_template('bulk_student_credentials.html', records=[], errors=invalid_lines, grade_level=10, user=user), 400
    if not names:
        return render_template('bulk_student_credentials.html', records=[], errors=['Paste at least one student row in Name, Gender, Grade Level format.'], grade_level=10, user=user), 400
    if len(names) > 200:
        return render_template('bulk_student_credentials.html', records=[], errors=['For reliability, create up to 200 accounts per batch. Split the list into smaller batches.'], grade_level=10, user=user), 400

    def username_base(full_name):
        first = full_name.split()[0].lower()
        first = unicodedata.normalize('NFKD', first).encode('ascii', 'ignore').decode('ascii')
        first = re.sub(r'[^a-z0-9]', '', first) or 'student'
        return first[:30]

    conn = db()
    try:
        existing = {str(r['username']).casefold() for r in conn.execute('SELECT username FROM users').fetchall()}
    finally:
        conn.close()
    used = set(existing)
    records = []
    serial_base = secrets.randbelow(700000) + 100000
    for index, student in enumerate(names):
        full_name = student['full_name']
        base_name = username_base(full_name)
        username = base_name
        suffix = 2
        while username.casefold() in used:
            username = f'{base_name}{suffix}'
            suffix += 1
        used.add(username.casefold())
        student_number = serial_base + index
        password = f'{base_name}.{student_number}'
        records.append({'full_name': full_name, 'username': username, 'password': password,
                        'student_number': str(student_number), 'gender': student['gender'],
                        'grade_level': student['grade_level'],
                        'password_hash': generate_password_hash(password)})

    # A single short transaction prevents partially-created batches.
    try:
        with DB_WRITE_LOCK:
            conn = db()
            try:
                now_used = {str(r['username']).casefold() for r in conn.execute('SELECT username FROM users').fetchall()}
                collision = [r['username'] for r in records if r['username'].casefold() in now_used]
                if collision:
                    return render_template('bulk_student_credentials.html', records=[], errors=['A username was created at the same time. No accounts from this batch were added; submit the batch again.'], grade_level=10, user=user), 409
                conn.executemany("""INSERT INTO users(username,password_hash,role,grade_level,last_login,full_name,gender,password_temp)
                    VALUES(?,?,'student',?,NULL,?,?,1)""",
                    [(r['username'], r['password_hash'], r['grade_level'], r['full_name'], r['gender']) for r in records])
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
    except sqlite3.IntegrityError:
        return render_template('bulk_student_credentials.html', records=[], errors=['A username conflict occurred. Please retry this batch.'], grade_level=10, user=user), 409
    except sqlite3.OperationalError as exc:
        app.logger.exception('Bulk student registration database error')
        return render_template('bulk_student_credentials.html', records=[], errors=[f'Database is busy or unavailable ({exc}). Please retry after a few seconds.'], grade_level=10, user=user), 503

    audit_log('BULK_REGISTER_STUDENTS', user, 'user_batch', None, f'Created {len(records)} student accounts using per-student name/gender/grade fields')
    for r in records:
        r.pop('password_hash', None)
    return render_template('bulk_student_credentials.html', records=records, errors=[], grade_level=10, user=user)


@app.route('/admin/students')
def admin_students():
    user=require_user()
    if not user or user['role']!='admin': return redirect(url_for('login'))
    inactive_cutoff=(datetime.now()-timedelta(days=30)).isoformat(timespec='seconds')
    return render_template('students.html',students=get_students(),user=user,inactive_cutoff=inactive_cutoff)


@app.route('/teacher/results')
def teacher_results():
    user=require_user()
    if not user or user['role']!='teacher': return redirect(url_for('login'))
    return render_template('teacher_results.html',quizzes=get_quizzes(user['id']),user=user)


@app.route('/admin/toggle-review/<int:qid>', methods=['POST'])
def toggle_review(qid):
    user=require_user()
    if not teacher_or_admin(user) or not teacher_can_access_quiz(user,qid): return jsonify(error='forbidden'),403
    data=request.get_json(silent=True) or {}
    enabled=1 if request.form.get('enabled')=='1' or data.get('enabled') in (1,True,'1') else 0
    conn=db()
    row=conn.execute('SELECT id FROM quizzes WHERE id=?',(qid,)).fetchone()
    if not row:
        conn.close(); return jsonify(error='Quiz not found'),404
    conn.execute('UPDATE quizzes SET review_enabled=? WHERE id=?',(enabled,qid)); conn.commit(); conn.close()
    audit_log('TOGGLE_REVIEW', user, 'quiz', qid, f'Admin review permission set to {bool(enabled)}')
    return jsonify(ok=True,review_enabled=bool(enabled))


@app.route('/teacher/toggle-review/<int:qid>', methods=['POST'])
def teacher_toggle_review(qid):
    user=require_user()
    if not user or user['role']!='teacher' or not teacher_can_access_quiz(user,qid): return jsonify(error='forbidden'),403
    data=request.get_json(silent=True) or {}
    enabled=1 if request.form.get('enabled')=='1' or data.get('enabled') in (1,True,'1') else 0
    conn=db()
    conn.execute('UPDATE quizzes SET review_enabled=? WHERE id=?',(enabled,qid)); conn.commit(); conn.close()
    audit_log('TOGGLE_REVIEW', user, 'quiz', qid, f'Teacher review permission set to {bool(enabled)}')
    return jsonify(ok=True,review_enabled=bool(enabled))

@app.route('/admin/results')
def admin_results():
    user=require_user()
    if not user or user['role']!='admin': return redirect(url_for('login'))
    return render_template('results.html',quizzes=get_quizzes(),user=user)


@app.route('/admin/add-quiz', methods=['POST'])
def add_quiz():
    user=require_user()
    if not teacher_or_admin(user): return 'Forbidden',403
    title=request.form.get('title','').strip(); raw=request.form.get('questions','')
    review_enabled=1 if request.form.get('review_enabled')=='1' else 0
    available_from_raw=request.form.get('available_from','').strip()
    available_until_raw=request.form.get('available_until','').strip()
    available_from=parse_schedule_value(available_from_raw)
    available_until=parse_schedule_value(available_until_raw)
    try: duration=max(1,min(480,int(request.form.get('duration_minutes','60'))))
    except ValueError: duration=DEFAULT_DURATION
    try: max_attempts=max(1,min(100,int(request.form.get('max_attempts','1'))))
    except ValueError: max_attempts=1
    try: grade_level=int(request.form.get('grade_level','10'))
    except ValueError: grade_level=10
    parsed,errors=parse_questions(raw)
    if available_from_raw and not available_from: errors.append('Start date/time is invalid.')
    if available_until_raw and not available_until: errors.append('End date/time is invalid.')
    if available_from and available_until and available_until <= available_from: errors.append('End date/time must be later than the start date/time.')
    if grade_level not in range(7,13): errors.append('Grade level must be 7 to 12.')
    uploaded_images = save_question_images(request.files.getlist('question_images')) if not errors else {}
    for item in parsed:
        item['image'] = resolve_question_image(item.get('image_ref'), uploaded_images)
    if not title or not parsed or errors:
        return render_template('teacher.html' if user['role']=='teacher' else 'admin.html',quizzes=get_quizzes(user['id']) if user['role']=='teacher' else get_quizzes(),user=user,access_url=student_access_url(),add_error='Please fix the following: '+' '.join(errors or ['Quiz title and at least one valid question are required.']))
    conn=db(); qid=conn.execute('INSERT INTO quizzes(title,duration_minutes,max_attempts,grade_level,created_at,created_by,review_enabled,available_from,available_until) VALUES(?,?,?,?,?,?,?,?,?)',(title,duration,max_attempts,grade_level,now(),user['id'],review_enabled,available_from,available_until)).lastrowid
    for item in parsed: conn.execute('INSERT INTO questions(quiz_id,question,a,b,c,d,answer,image) VALUES(?,?,?,?,?,?,?,?)',(qid,item['question'],item['a'],item['b'],item['c'],item['d'],item['answer'],item.get('image')))
    conn.commit(); conn.close(); audit_log('CREATE_QUIZ', user, 'quiz', qid, f'Created quiz title={title}; grade={grade_level}; questions={len(parsed)}'); return redirect(url_for('teacher_dashboard' if user['role']=='teacher' else 'admin'))


def get_students():
    conn=db(); rows=conn.execute("SELECT u.id,u.username,u.full_name,u.gender,u.grade_level,u.last_login,MAX(a.completed_at) AS last_attempt FROM users u LEFT JOIN attempts a ON a.user_id=u.id WHERE u.role='student' GROUP BY u.id ORDER BY u.username").fetchall(); conn.close(); return rows


@app.route('/admin/edit-student/<int:uid>', methods=['POST'])
def edit_student(uid):
    user = require_user()
    if not user or user['role'] != 'admin':
        return redirect(url_for('login'))
    full_name = ' '.join(request.form.get('full_name', '').split())
    gender = request.form.get('gender', '').strip()
    try:
        grade_level = int(request.form.get('grade_level', '0'))
    except (TypeError, ValueError):
        grade_level = 0
    if len(full_name) < 2:
        return redirect(url_for('admin_students', student_error='Please enter a valid complete name.'))
    if gender not in ('Male', 'Female'):
        return redirect(url_for('admin_students', student_error='Choose Male or Female.'))
    if grade_level not in range(7, 13):
        return redirect(url_for('admin_students', student_error='Grade level must be 7 to 12.'))
    with DB_WRITE_LOCK:
        conn = db()
        student = conn.execute("SELECT id FROM users WHERE id=? AND role='student'", (uid,)).fetchone()
        if not student:
            conn.close()
            return redirect(url_for('admin_students', student_error='Student account not found.'))
        conn.execute('UPDATE users SET full_name=?,gender=?,grade_level=? WHERE id=?', (full_name, gender, grade_level, uid))
        conn.commit()
        conn.close()
    audit_log('EDIT_STUDENT_PROFILE', user, 'user', uid, f'Updated student profile: name={full_name}; gender={gender}; grade={grade_level}')
    return redirect(url_for('admin_students', student_updated=1))


@app.route('/admin/edit-quiz/<int:qid>', methods=['GET','POST'])
def edit_quiz(qid):
    user=require_user()
    if not teacher_or_admin(user) or not teacher_can_access_quiz(user,qid): return 'Forbidden',403
    conn=db(); q=conn.execute('SELECT * FROM quizzes WHERE id=?',(qid,)).fetchone()
    questions=conn.execute('SELECT * FROM questions WHERE quiz_id=? ORDER BY id',(qid,)).fetchall() if q else []
    conn.close()
    if not q: return redirect(url_for('admin'))
    if request.method=='GET':
        raw='\n\n'.join([f"Question {i}: {r['question']}\nA. {r['a']}\nB. {r['b']}\nC. {r['c']}\nD. {r['d']}\nAnswer: {r['answer']}" + (f"\nImage: {r['image']}" if r['image'] else '') for i,r in enumerate(questions,1)])
        return render_template('edit_quiz.html',quiz=q,raw=raw,user=user)
    title=request.form.get('title','').strip(); raw=request.form.get('questions','')
    review_enabled=1 if request.form.get('review_enabled')=='1' else 0
    available_from_raw=request.form.get('available_from','').strip()
    available_until_raw=request.form.get('available_until','').strip()
    available_from=parse_schedule_value(available_from_raw)
    available_until=parse_schedule_value(available_until_raw)
    try: duration=max(1,min(480,int(request.form.get('duration_minutes','60'))))
    except ValueError: duration=60
    try: max_attempts=max(1,min(100,int(request.form.get('max_attempts','1'))))
    except ValueError: max_attempts=1
    try: grade_level=int(request.form.get('grade_level','10'))
    except ValueError: grade_level=10
    parsed,errors=parse_questions(raw)
    if available_from_raw and not available_from: errors.append('Start date/time is invalid.')
    if available_until_raw and not available_until: errors.append('End date/time is invalid.')
    if available_from and available_until and available_until <= available_from: errors.append('End date/time must be later than the start date/time.')
    if grade_level not in range(7,13): errors.append('Grade level must be 7 to 12.')
    uploaded_images = save_question_images(request.files.getlist('question_images')) if not errors else {}
    for i,item in enumerate(parsed):
        existing = questions[i]['image'] if i < len(questions) else None
        item['image'] = resolve_question_image(item.get('image_ref'), uploaded_images, existing)
    if not title or not parsed or errors:
        return render_template('edit_quiz.html',quiz=q,raw=raw,user=user,edit_error='Please fix: '+' '.join(errors or ['Quiz title and at least one valid question are required.']))
    conn=db()
    conn.execute('UPDATE quizzes SET title=?,duration_minutes=?,max_attempts=?,grade_level=?,review_enabled=?,available_from=?,available_until=? WHERE id=?',(title,duration,max_attempts,grade_level,review_enabled,available_from,available_until,qid))
    conn.execute('DELETE FROM questions WHERE quiz_id=?',(qid,))
    for item in parsed:
        conn.execute('INSERT INTO questions(quiz_id,question,a,b,c,d,answer,image) VALUES(?,?,?,?,?,?,?,?)',(qid,item['question'],item['a'],item['b'],item['c'],item['d'],item['answer'],item.get('image')))
    conn.commit(); conn.close()
    audit_log('EDIT_QUIZ', user, 'quiz', qid, f'Edited quiz title={title}; grade={grade_level}; questions={len(parsed)}')
    return redirect(url_for('teacher_dashboard' if user['role']=='teacher' else 'admin'))

@app.route('/admin/reset-password/<int:uid>', methods=['POST'])
def reset_password(uid):
    user=require_user()
    if not user or user['role']!='admin': return 'Forbidden',403
    temp=make_temp_password()
    conn=db(); target=conn.execute("SELECT username FROM users WHERE id=? AND role='student'",(uid,)).fetchone()
    if not target: conn.close(); return redirect(url_for('admin_students'))
    conn.execute('UPDATE users SET password_hash=?, password_temp=1 WHERE id=?',(generate_password_hash(temp),uid)); conn.commit(); conn.close()
    audit_log('RESET_STUDENT_PASSWORD', user, 'user', uid, f'Reset temporary password for student username={target["username"]}')
    return render_template('admin_reset.html',user=user,student=target,temp_password=temp)


@app.route('/admin/delete-student/<int:uid>', methods=['POST'])
def delete_student(uid):
    user=require_user()
    if not user or user['role']!='admin': return 'Forbidden',403
    conn=db(); target=conn.execute("SELECT id,username,full_name,last_login FROM users WHERE id=? AND role='student'",(uid,)).fetchone()
    if not target: conn.close(); return redirect(url_for('admin_students'))
    # Inactive means no login in the last 30 days, or never logged in.
    inactive=True
    if target['last_login']:
        try: inactive=(datetime.fromisoformat(target['last_login'])).timestamp() < (datetime.now().timestamp()-30*86400)
        except ValueError: inactive=False
    if not inactive:
        conn.close(); return redirect(url_for('admin_students'))
    conn.execute('DELETE FROM attempts WHERE user_id=?',(uid,)); conn.execute('DELETE FROM users WHERE id=?',(uid,)); conn.commit(); conn.close(); audit_log('DELETE_STUDENT', user, 'user', uid, f'Deleted inactive student username={target["username"]}'); return redirect(url_for('admin_students'))

@app.route('/admin/delete/<int:qid>', methods=['POST'])
def delete(qid):
    user=require_user()
    if not teacher_or_admin(user) or not teacher_can_access_quiz(user,qid): return 'Forbidden',403
    conn=db(); target=conn.execute('SELECT title FROM quizzes WHERE id=?',(qid,)).fetchone(); conn.execute('DELETE FROM attempts WHERE quiz_id=?',(qid,)); conn.execute('DELETE FROM questions WHERE quiz_id=?',(qid,)); conn.execute('DELETE FROM quizzes WHERE id=?',(qid,)); conn.commit(); conn.close()
    delete_quiz_analytics(qid)
    audit_log('DELETE_QUIZ', user, 'quiz', qid, f'Deleted quiz title={target["title"] if target else qid}')
    return redirect(url_for('teacher_dashboard' if user['role']=='teacher' else 'admin'))


def result_rows(selected='all', gender='all', sort='rank', include_hidden=False, all_attempts=False, student_search='', owner_id=None):
    conn=db()
    sql='''SELECT a.id,a.user_id,u.username,COALESCE(u.full_name,u.username) AS full_name,COALESCE(u.gender,'Not set') AS gender,
                  a.hidden, q.id AS quiz_id,q.title,a.attempt_number,a.completed_at,a.percentage,a.correct,a.incorrect,a.skipped,a.timed_out,a.average_time,a.passed
           FROM attempts a JOIN users u ON u.id=a.user_id JOIN quizzes q ON q.id=a.quiz_id'''
    clauses=[]; params=[]
    if owner_id is not None:
        clauses.append('q.created_by=?'); params.append(owner_id)
    if not include_hidden:
        clauses.append('COALESCE(a.hidden,0)=0')
    if selected!='all':
        try: clauses.append('a.quiz_id=?'); params.append(int(selected))
        except ValueError: pass
    if gender!='all':
        clauses.append("COALESCE(u.gender,'Not specified')=?"); params.append(gender)
    if student_search.strip():
        term=f"%{student_search.strip()}%"
        clauses.append("(COALESCE(u.full_name,u.username) LIKE ? OR u.username LIKE ?)")
        params.extend([term, term])
    if clauses: sql += ' WHERE ' + ' AND '.join(clauses)
    rows=[dict(r) for r in conn.execute(sql,params).fetchall()]
    conn.close()
    # By default show only the latest result for each student + quiz. This prevents
    # repeated rows from multiple attempts while preserving the full attempt history.
    if not all_attempts:
        latest={}
        for r in rows:
            key=(r['user_id'],r['quiz_id'])
            if key not in latest or (r['completed_at'] or '', r['id']) > (latest[key]['completed_at'] or '', latest[key]['id']):
                latest[key]=r
        rows=list(latest.values())
    # Rank and MPS are calculated within each quiz using the currently selected gender filter.
    groups={}
    for r in rows: groups.setdefault(r['quiz_id'],[]).append(r)
    for group in groups.values():
        scores=sorted([float(r['percentage']) for r in group], reverse=True)
        mps=(sum(scores)/len(scores)) if scores else 0
        for r in group:
            r['rank']=scores.index(float(r['percentage']))+1
            r['mps']=mps
    key=sort or 'rank'
    if key=='name': rows.sort(key=lambda r:(r['full_name'].lower(), r['quiz_id'], -float(r['percentage']), r['attempt_number']))
    elif key=='gender': rows.sort(key=lambda r:(r['gender'].lower(), r['full_name'].lower(), r['quiz_id']))
    elif key=='score_desc': rows.sort(key=lambda r:(-float(r['percentage']), r['full_name'].lower()))
    elif key=='score_asc': rows.sort(key=lambda r:(float(r['percentage']), r['full_name'].lower()))
    elif key=='date': rows.sort(key=lambda r:r['completed_at'] or '', reverse=True)
    elif key=='rank': rows.sort(key=lambda r:(r['quiz_id'], r['rank'], r['full_name'].lower()))
    else: rows.sort(key=lambda r:r['id'], reverse=True)
    return rows

@app.route('/api/live')
def live():
    user=require_user()
    if not teacher_or_admin(user): return jsonify(error='forbidden'),403
    selected=request.args.get('quiz_id','all'); gender=request.args.get('gender','all'); sort=request.args.get('sort','rank')
    include_hidden=request.args.get('include_hidden','0')=='1'
    all_attempts=request.args.get('all_attempts','0')=='1'
    student_search=request.args.get('student_search','').strip()
    try:
        return jsonify(result_rows(selected, gender, sort, include_hidden, all_attempts, student_search, user['id'] if user['role']=='teacher' else None))
    except sqlite3.OperationalError:
        return jsonify(error='Database is busy. Please wait a moment and retry.'),503

@app.route('/admin/hide-result/<int:attempt_id>', methods=['POST'])
def hide_result(attempt_id):
    user=require_user()
    if not teacher_or_admin(user): return 'Forbidden',403
    conn=db(); allowed=conn.execute("SELECT a.id FROM attempts a JOIN quizzes q ON q.id=a.quiz_id WHERE a.id=? AND (?='admin' OR q.created_by=?)",(attempt_id,user['role'],user['id'])).fetchone();
    if not allowed: conn.close(); return 'Forbidden',403
    conn.execute('UPDATE attempts SET hidden=1 WHERE id=?',(attempt_id,)); conn.commit(); conn.close()
    refresh_analytics_attempt_hidden(attempt_id, True)
    audit_log('HIDE_RESULT', user, 'attempt', attempt_id, 'Result hidden from normal results views')
    return ('',204)

@app.route('/admin/restore-result/<int:attempt_id>', methods=['POST'])
def restore_result(attempt_id):
    user=require_user()
    if not teacher_or_admin(user): return 'Forbidden',403
    conn=db(); allowed=conn.execute("SELECT a.id FROM attempts a JOIN quizzes q ON q.id=a.quiz_id WHERE a.id=? AND (?='admin' OR q.created_by=?)",(attempt_id,user['role'],user['id'])).fetchone();
    if not allowed: conn.close(); return 'Forbidden',403
    conn.execute('UPDATE attempts SET hidden=0 WHERE id=?',(attempt_id,)); conn.commit(); conn.close()
    refresh_analytics_attempt_hidden(attempt_id, False)
    audit_log('RESTORE_RESULT', user, 'attempt', attempt_id, 'Result restored to normal results views')
    return ('',204)

@app.route('/admin/download.csv')
def csvout():
    user=require_user()
    if not teacher_or_admin(user): return 'Forbidden',403
    selected=request.args.get('quiz_id','all'); gender=request.args.get('gender','all'); sort=request.args.get('sort','rank')
    all_attempts=request.args.get('all_attempts','0')=='1'
    student_search=request.args.get('student_search','').strip()
    rows=result_rows(selected, gender, sort, False, all_attempts, student_search, user['id'] if user['role']=='teacher' else None)
    suffix='all_quizzes'
    conn=db()
    if selected!='all':
        try:
            q=conn.execute('SELECT title FROM quizzes WHERE id=?',(int(selected),)).fetchone()
            suffix=re.sub(r'[^A-Za-z0-9_-]+','_',q['title']).strip('_').lower() if q else f'quiz_{selected}'
        except ValueError: pass
    conn.close()
    if gender!='all': suffix += '_' + re.sub(r'[^A-Za-z0-9_-]+','_',gender).strip('_').lower()
    if student_search: suffix += '_student_' + re.sub(r'[^A-Za-z0-9_-]+','_',student_search).strip('_').lower()[:40]
    output=io.StringIO(); writer=csv.writer(output)
    writer.writerow(['Rank','Complete Name','Username','Gender','Quiz','Attempt','Date/Time','Percentage','MPS','Correct','Incorrect','Skipped','Timed Out','Average Time','Result'])
    for row in rows:
        writer.writerow([row['rank'],row['full_name'],row['username'],row['gender'],row['title'],row['attempt_number'],row['completed_at'],f"{row['percentage']:.1f}%",f"{row['mps']:.1f}%",row['correct'],row['incorrect'],row['skipped'],row['timed_out'],f"{row['average_time']:.1f}s",'PASS' if row['passed'] else 'FAIL'])
    return send_file(io.BytesIO(output.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name=f'quiz_results_{suffix}.csv')


@app.errorhandler(sqlite3.OperationalError)
def handle_sqlite_operational_error(exc):
    if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
        return ('<h2>Server is busy</h2><p>The quiz server is handling many requests at once. Please wait a few seconds and try again.</p>',503)
    return ('<h2>Database error</h2><p>The application could not complete the request. Please try again.</p>',500)

if __name__=='__main__':
    init_db()
    lan_ip = get_lan_ip()
    print('\n' + '='*52)
    print('       TLE-ICT CSS QUIZ SERVER')
    print('='*52)
    print('Teacher access:  http://127.0.0.1:8000')
    print(f'LAN access:      http://{lan_ip}:8000')
    print('Mode:             LAN / OFFLINE (default)')
    print('Internet:         NOT required')
    print('Public access:    optional — start_public.py')
    print('QR code:          LAN URL only in default mode')
    print('Primary DB mode:   ' + os.environ.get('QUIZ_DB_MODE', 'local').strip().lower())
    print('Analytics:        DuckDB secondary store (non-critical)')
    print('='*52 + '\n')
    app.run(host='0.0.0.0', port=8000, debug=False)
