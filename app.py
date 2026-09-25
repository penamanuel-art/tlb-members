"""
The Line Breaker — Plataforma de miembros (v1).

Cuentas gratuitas, sin pagos. Flask + SQLite en local / PostgreSQL en Render
(cuando existe la env var DATABASE_URL), hashing bcrypt, sesiones firmadas
por cookie (las de Flask por defecto: el filesystem de Render es efímero).

Uso local:
    ./venv/bin/python app.py
    # o: ./venv/bin/flask --app app run

En Render (ver render.yaml):
    gunicorn app:app --bind 0.0.0.0:$PORT
"""
import json
import os
import secrets
import sqlite3
import urllib.request
from datetime import timedelta, datetime, timezone
from functools import wraps

import bcrypt
from flask import (
    Flask, flash, g, redirect, render_template, request, session, url_for,
)
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL")
USE_PG = bool(DATABASE_URL)

# Link de pago de Stripe para desbloquear Platinum ($1 primera semana, luego $23/semana).
# Se cambia sin tocar código con la env var STRIPE_PLATINUM_URL en Render.
STRIPE_PLATINUM_URL = os.environ.get(
    "STRIPE_PLATINUM_URL", "https://buy.stripe.com/28E14p64MfDeeiybEQefC00"
)

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg import errors as pg_errors
    HAVE_PSYCOPG = True
except ImportError:  # pragma: no cover - entorno local sin psycopg
    psycopg = None
    pg_errors = None
    HAVE_PSYCOPG = False

if USE_PG and not HAVE_PSYCOPG:
    raise RuntimeError(
        "DATABASE_URL is set but psycopg is not installed. "
        "Agrega psycopg[binary] a requirements.txt"
    )

# Excepciones de integridad en ambos backends (ej. email duplicado).
INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
if HAVE_PSYCOPG:
    INTEGRITY_ERRORS = INTEGRITY_ERRORS + (pg_errors.UniqueViolation,)

app = Flask(__name__, instance_path=INSTANCE_DIR)

_secret = os.environ.get("SECRET_KEY")
if not _secret:
    if USE_PG:
        raise RuntimeError(
            "SECRET_KEY is required in production: set the env var "
            "SECRET_KEY en el Web Service de Render antes de desplegar."
        )
    _secret = secrets.token_hex(32)  # solo desarrollo local
app.config["SECRET_KEY"] = _secret
# Sesiones firmadas por cookie (Flask por defecto). Flask-Session con
# filesystem no sirve en Render (disco efímero). Solo guardamos user_id y
# nombre en la sesión, caben sin problema en la cookie.

DB_PATH = os.path.join(INSTANCE_DIR, "members.db")
PLAYS_PATH = os.path.join(BASE_DIR, "data", "plays.json")
MASTERCLASS_PATH = os.path.join(BASE_DIR, "data", "masterclass.json")
RESULTS_PATH = os.path.join(BASE_DIR, "data", "results.json")
ARCHIVE_PATH = os.path.join(BASE_DIR, "data", "archive.json")
MASTERCLASS_ARCHIVO_PATH = os.path.join(BASE_DIR, "data", "masterclass-archivo.json")


# ---------------------------------------------------------------- DB ----
class DBConnection:
    """Capa mínima dual SQLite/PostgreSQL.

    - Convierte placeholders `?` → `%s` cuando habla con Postgres.
    - Devuelve filas como dict en ambos backends
      (sqlite3.Row en SQLite, dict_row en psycopg).
    """

    def __init__(self, conn, use_pg: bool):
        self._conn = conn
        self.use_pg = use_pg

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.use_pg else sql

    def execute(self, sql, params=()):
        return self._conn.execute(self._sql(sql), params)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _raw_connect():
    if USE_PG:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_db() -> DBConnection:
    if "db" not in g:
        g.db = DBConnection(_raw_connect(), USE_PG)
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    bankroll REAL,               -- bankroll del miembro (NULL = sin configurar)
    platinum_unlocked INTEGER NOT NULL DEFAULT 0,  -- 1 = Platinum desbloqueada
    is_admin INTEGER NOT NULL DEFAULT 0,           -- 1 = administrador
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracked_plays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    play_id TEXT NOT NULL,
    fecha TEXT NOT NULL,
    nivel TEXT NOT NULL,
    pick TEXT NOT NULL,
    cuota INTEGER NOT NULL,
    stake_unidades REAL NOT NULL,
    stake_monto REAL NOT NULL,
    edge REAL,
    resultado TEXT,          -- NULL = pendiente, 'W' = ganada, 'L' = perdida
    created_at TEXT NOT NULL,
    UNIQUE(user_id, play_id)
);
CREATE INDEX IF NOT EXISTS idx_tracked_user ON tracked_plays(user_id);
CREATE TABLE IF NOT EXISTS checkins (
    user_id INTEGER NOT NULL REFERENCES users(id),
    fecha TEXT NOT NULL,
    PRIMARY KEY (user_id, fecha)
);
"""

DDL_PG = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    bankroll DOUBLE PRECISION,   -- bankroll del miembro (NULL = sin configurar)
    platinum_unlocked INTEGER NOT NULL DEFAULT 0,  -- 1 = Platinum desbloqueada
    is_admin INTEGER NOT NULL DEFAULT 0,           -- 1 = administrador
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracked_plays (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    play_id TEXT NOT NULL,
    fecha TEXT NOT NULL,
    nivel TEXT NOT NULL,
    pick TEXT NOT NULL,
    cuota INTEGER NOT NULL,
    stake_unidades DOUBLE PRECISION NOT NULL,
    stake_monto DOUBLE PRECISION NOT NULL,
    edge DOUBLE PRECISION,
    resultado TEXT,          -- NULL = pendiente, 'W' = ganada, 'L' = perdida
    created_at TEXT NOT NULL,
    UNIQUE(user_id, play_id)
);
CREATE INDEX IF NOT EXISTS idx_tracked_user ON tracked_plays(user_id);
CREATE TABLE IF NOT EXISTS checkins (
    user_id INTEGER NOT NULL REFERENCES users(id),
    fecha TEXT NOT NULL,
    PRIMARY KEY (user_id, fecha)
);
"""


def _run_statements(conn, script: str):
    # Ni sqlite3.executescript ni psycopg aceptan multi-statement igual;
    # partimos por ';' y ejecutamos una a una.
    for stmt in (s.strip() for s in script.split(";")):
        if stmt:
            conn.execute(stmt)


def init_db():
    """Idempotente: crea las tablas si no existen (SQLite o PostgreSQL)."""
    conn = _raw_connect()
    try:
        _run_statements(conn, DDL_PG if USE_PG else DDL_SQLITE)
        conn.commit()
    finally:
        conn.close()


def insert_returning_id(db: DBConnection, sql: str, params):
    """INSERT que devuelve el id autogenerado, en ambos backends."""
    if db.use_pg:
        row = db.execute(sql + " RETURNING id", params).fetchone()
        return row["id"]
    return db.execute(sql, params).lastrowid


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_iso() -> str:
    return datetime.now(TZ).date().isoformat()


DAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]


def fecha_larga() -> str:
    d = datetime.now(TZ).date()
    return f"{DAYS_EN[d.weekday()]}, {MONTHS_EN[d.month - 1]} {d.day}, {d.year}"


def primer_nombre(nombre: str) -> str:
    return (nombre or "").strip().split(" ")[0] if nombre else ""


def saludo_hoy() -> str:
    """Saludo según la hora en America/New_York."""
    h = datetime.now(TZ).hour
    if 5 <= h < 12:
        return "Good morning"
    if 12 <= h < 19:
        return "Good afternoon"
    return "Good evening"


def record_checkin(db, user_id: int):
    if db.use_pg:
        db.execute(
            "INSERT INTO checkins (user_id, fecha) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (user_id, today_iso()),
        )
    else:
        db.execute(
            "INSERT OR IGNORE INTO checkins (user_id, fecha) VALUES (?, ?)",
            (user_id, today_iso()),
        )
    db.commit()


def checkin_streak(db, user_id: int) -> int:
    rows = db.execute(
        "SELECT fecha FROM checkins WHERE user_id = ? ORDER BY fecha DESC",
        (user_id,),
    ).fetchall()
    fechas = {r["fecha"] for r in rows}
    racha = 0
    d = datetime.now(TZ).date()
    # Si hoy aún no hay check-in (no debería pasar), se cuenta desde ayer.
    if d.isoformat() not in fechas:
        d = d.fromordinal(d.toordinal() - 1)
    while d.isoformat() in fechas:
        racha += 1
        d = d.fromordinal(d.toordinal() - 1)
    return racha


# -------------------------------------------------------------- Auth ----
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Log in to continue.", "warn")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()


def platinum_unlocked_for(user) -> bool:
    """True si el miembro desbloqueó la jugada Platinum (default: bloqueada)."""
    try:
        return bool(user and user["platinum_unlocked"])
    except (KeyError, IndexError, TypeError):
        return False


def is_admin_for(user) -> bool:
    """True si el usuario es administrador."""
    try:
        return bool(user and user["is_admin"])
    except (KeyError, IndexError, TypeError):
        return False


def admin_email() -> str:
    """Email del administrador (env ADMIN_EMAIL), en minúsculas."""
    return (os.environ.get("ADMIN_EMAIL") or "").strip().lower()


def notify_new_member(nombre: str, email: str):
    """Avisa a Alex por email cuando alguien se registra.

    SMTP Gmail con EMAIL_USER / EMAIL_PASS / ADMIN_EMAIL.
    Si las variables no están configuradas o el envío falla, no hace
    nada: el registro sigue funcionando sin errores para el usuario.
    Se ejecuta en un hilo aparte para no retrasar la respuesta.
    """
    user = (os.environ.get("EMAIL_USER") or "").strip()
    pwd = os.environ.get("EMAIL_PASS") or ""
    dest = admin_email()
    if not (user and pwd and dest):
        return

    def _send():
        try:
            import smtplib
            from email.message import EmailMessage

            msg = EmailMessage()
            msg["Subject"] = f"The Line Breaker: new member — {nombre}"
            msg["From"] = user
            msg["To"] = dest
            msg.set_content(
                f"A new member registered:\n\n"
                f"Name: {nombre}\n"
                f"Email: {email}\n"
                f"Date: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M %Z')}\n"
            )
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
                s.starttls()
                s.login(user, pwd)
                s.send_message(msg)
        except Exception:
            pass  # silencioso: nunca rompe el registro

    import threading
    threading.Thread(target=_send, daemon=True).start()


# ------------------------------------------------------------- Plays ----
WELCOME_SUBJECT = "Welcome to The Line Breaker"

WELCOME_TEXT = """Hi {nombre},

Your account is ready. Welcome to The Line Breaker: a sports betting program built on numbers, discipline, and value. No hunches.

YOUR MEMBER DASHBOARD
Go straight here:
https://the-line-breaker-members.onrender.com/home

You'll see each morning's plays there (~11:00 AM, New York time).

HOW IT WORKS
- We publish a maximum of 3 plays per day, and only when the model detects real value. If there's no value, there's no play: discipline also means not betting.
- Each play shows its level: PLATINUM, the top play of the day (edge over 5%), or GOLD (edge between 3% and 5%), always with its odds, stake, and explanation.

PLATINUM
- The Platinum play is exclusive to Platinum members.
- It costs $1 the first week, then $23 per week. Activate it from your dashboard, under "Unlock Platinum".

GOLDEN RULES
1. Bet exactly the amount indicated: no more, no less.
2. Never chase losses or wins.
3. Numbers and value only. No team favoritism.

See you tomorrow at 11:00 AM with the first plays.

- The Line Breaker

Bet responsibly - 21+ - Gambling problem? Call 1-800-GAMBLER (1-800-426-2537): free and confidential help, 24/7.
"""

WELCOME_HTML = """<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; max-width: 600px; margin: 0 auto; background: #ffffff;">
<div style="background: #1a1a2e; padding: 30px 20px; text-align: center; border-radius: 18px 18px 0 0;">
<img src="https://files.catbox.moe/g1ig30.jpg" alt="The Line Breaker" width="140" style="width: 140px; height: auto; display: block; margin: 0 auto; border: 0;">
<p style="margin: 12px 0 0; font-size: 16px; font-weight: 800; color: #c9a227; letter-spacing: 5px;">THE LINE BREAKER</p>
</div>
<div style="padding: 28px 26px; color: #1a1a2e;">
<p style="font-size: 20px; font-weight: 800; margin: 0 0 6px;">Hi {nombre},</p>
<p style="font-size: 15px; line-height: 1.7; margin: 0 0 18px; color: #333333;">Your account is ready. Welcome to <strong>The Line Breaker</strong>: a sports betting program built on numbers, discipline, and value. No hunches.</p>
<div style="text-align: center; margin: 22px 0;">
<a href="https://the-line-breaker-members.onrender.com/home" style="display: inline-block; background: #c9a227; color: #1a1a2e; font-size: 16px; font-weight: 800; padding: 14px 34px; border-radius: 12px; text-decoration: none;">Go to my dashboard &rarr;</a>
</div>
<p style="font-size: 14px; color: #666666; text-align: center; margin: 0 0 22px;">You&apos;ll see each morning&apos;s plays there (~11:00 AM, New York time).</p>
<p style="font-size: 16px; font-weight: 800; margin: 0 0 8px; color: #1a1a2e;">HOW IT WORKS</p>
<ul style="font-size: 14px; line-height: 1.8; color: #333333; margin: 0 0 18px; padding-left: 20px;">
<li>We publish a maximum of 3 plays per day, and only when the model detects real value. If there&apos;s no value, there&apos;s no play: discipline also means not betting.</li>
<li>Each play shows its level: <strong>PLATINUM</strong>, the top play of the day (edge over 5%), or <strong>GOLD</strong> (edge between 3% and 5%), always with its odds, stake, and explanation.</li>
</ul>
<p style="font-size: 16px; font-weight: 800; margin: 0 0 8px; color: #1a1a2e;">PLATINUM</p>
<ul style="font-size: 14px; line-height: 1.8; color: #333333; margin: 0 0 18px; padding-left: 20px;">
<li>The Platinum play is exclusive to Platinum members.</li>
<li>It costs <strong>$1 the first week</strong>, then <strong>$23 per week</strong>. Activate it from your dashboard, under &quot;Unlock Platinum&quot;.</li>
</ul>
<p style="font-size: 16px; font-weight: 800; margin: 0 0 8px; color: #1a1a2e;">GOLDEN RULES</p>
<ol style="font-size: 14px; line-height: 1.8; color: #333333; margin: 0 0 18px; padding-left: 20px;">
<li>Bet exactly the amount indicated: no more, no less.</li>
<li>Never chase losses or wins.</li>
<li>Numbers and value only. No team favoritism.</li>
</ol>
<p style="font-size: 14px; color: #333333; line-height: 1.7; margin: 0;">See you tomorrow at 11:00 AM with the first plays.</p>
<p style="font-size: 14px; color: #333333; margin: 12px 0 0;">&mdash; <strong>The Line Breaker</strong></p>
</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin: 6px 0 0; background: #1a1a2e; border-radius: 0 0 18px 18px; border-top: 3px solid #c9a227;">
<tr><td style="padding: 22px 20px; text-align: center;">
<img src="https://files.catbox.moe/g1ig30.jpg" alt="The Line Breaker" width="110" style="width: 110px; height: auto; display: block; margin: 0 auto; border: 0;">
<p style="margin: 10px 0 4px; font-size: 14px; font-weight: 800; color: #c9a227; letter-spacing: 4px;">THE LINE BREAKER</p>
<p style="margin: 0; font-size: 12px; color: #dddddd;">250 Park Avenue, Suite 1800, New York, NY 10017</p>
<p style="margin: 4px 0 0; font-size: 12px; color: #dddddd;">(983) 819-4589</p>
<div style="width: 60%; height: 1px; background: #33334d; margin: 14px auto;">&nbsp;</div>
<p style="margin: 0; font-size: 11px; color: #aaaaaa; line-height: 1.6;">Bet responsibly &middot; 21+<br>Gambling problem? Call 1-800-GAMBLER (1-800-426-2537), free and confidential help, 24/7.</p>
<p style="margin: 10px 0 0; font-size: 10px; color: #888888; line-height: 1.6;">The Line Breaker picks are for informational and entertainment purposes. No pick guarantees winnings; sports betting involves risk of loss.</p>
<p style="margin: 8px 0 0; font-size: 10px; color: #666666;">&copy; 2026 The Line Breaker &middot; All rights reserved</p>
</td></tr>
</table>
</div>
"""


def send_welcome_email(nombre: str, email: str):
    """Email de bienvenida al nuevo miembro (HTML profesional + texto plano).

    Usa el mismo SMTP Gmail (EMAIL_USER / EMAIL_PASS). Si no está
    configurado o el envío falla, no hace nada: el registro sigue
    funcionando y el miembro ve la página de bienvenida en pantalla.
    Se ejecuta en un hilo aparte para no retrasar la respuesta.
    """
    user = (os.environ.get("EMAIL_USER") or "").strip()
    pwd = os.environ.get("EMAIL_PASS") or ""
    if not (user and pwd):
        return

    def _send():
        try:
            import smtplib
            from email.message import EmailMessage

            msg = EmailMessage()
            msg["Subject"] = WELCOME_SUBJECT
            msg["From"] = f"The Line Breaker <{user}>"
            msg["To"] = email
            msg.set_content(WELCOME_TEXT.format(nombre=nombre))
            msg.add_alternative(WELCOME_HTML.format(nombre=nombre), subtype="html")
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
                s.starttls()
                s.login(user, pwd)
                s.send_message(msg)
        except Exception:
            pass  # silencioso: nunca rompe el registro

    import threading
    threading.Thread(target=_send, daemon=True).start()


def load_data():
    """Devuelve (program, plays). Soporta plays.json como lista (viejo) o dict (nuevo)."""
    try:
        with open(PLAYS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = []
    if isinstance(data, dict):
        program = data.get("program", {}) or {}
        plays = data.get("plays", []) or []
    else:
        program = {}
        plays = data if isinstance(data, list) else []
    plays = [p for p in plays if isinstance(p, dict) and p.get("id")]
    return program, plays


def load_plays():
    return load_data()[1]


def load_program():
    return load_data()[0]


def card_publicada_hoy() -> bool:
    """True si data/plays.json trae la card de hoy (America/New_York).

    Antes de la publicación diaria (11:05 AM ET) el archivo aún tiene la
    card de ayer: en ese caso el dashboard muestra 0 jugadas y el aviso
    de 'aún no publicada', nunca las jugadas de ayer.
    """
    hoy = datetime.now(TZ).strftime("%Y-%m-%d")
    data = load_json_file(PLAYS_PATH)
    if isinstance(data, dict):
        fecha = data.get("fecha")
        if fecha:
            return fecha == hoy
        plays = data.get("plays") or []
    elif isinstance(data, list):
        plays = data
    else:
        return False
    plays = [p for p in plays if isinstance(p, dict) and p.get("id")]
    return bool(plays) and all(p.get("fecha") == hoy for p in plays)


def load_json_file(path):
    """Lee un JSON de datos; {} si no existe o está corrupto."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def load_masterclass():
    return load_json_file(MASTERCLASS_PATH)


def load_masterclass_archivo():
    """Las 30 lecciones del curso (archivo estático)."""
    return load_json_file(MASTERCLASS_ARCHIVO_PATH).get("lecciones", [])


def load_results():
    return load_json_file(RESULTS_PATH)


def load_archive():
    """Archivo de jugadas publicadas por fecha."""
    return load_json_file(ARCHIVE_PATH).get("dias", [])


# +EV Board (versión filtrada): el JSON vive en la rama `data-board`
# (rama de datos: actualizarla NO redespliega Render). La app lo lee en
# vivo con caché corto en memoria.
EV_BOARD_URL = (
    "https://raw.githubusercontent.com/penamanuel-art/tlb-members"
    "/data-board/data/ev_board.json"
)
_ev_board_cache = {"at": 0.0, "data": {}}


def load_ev_board():
    import time
    now = time.time()
    if now - _ev_board_cache["at"] < 300 and _ev_board_cache["data"]:
        return _ev_board_cache["data"]
    data = {}
    try:
        req = urllib.request.Request(EV_BOARD_URL, headers={"User-Agent": "tlb-members"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except Exception:
        data = _ev_board_cache["data"] or {}
    _ev_board_cache["at"] = now
    _ev_board_cache["data"] = data
    return data


def american_profit_ratio(odds) -> float:
    """Cuánto se gana por cada 1 apostado en cuota americana."""
    o = int(odds)
    if o == 0:
        return 0.0
    return o / 100.0 if o > 0 else 100.0 / abs(o)


def play_profit_units(tp) -> float:
    ratio = american_profit_ratio(tp["cuota"])
    if tp["resultado"] == "W":
        return tp["stake_unidades"] * ratio
    if tp["resultado"] == "L":
        return -tp["stake_unidades"]
    return 0.0


def play_profit_dollars(tp) -> float:
    ratio = american_profit_ratio(tp["cuota"])
    if tp["resultado"] == "W":
        return tp["stake_monto"] * ratio
    if tp["resultado"] == "L":
        return -tp["stake_monto"]
    return 0.0


def compute_stats(tracked):
    graded = [t for t in tracked if t["resultado"] in ("W", "L")]
    wins = sum(1 for t in graded if t["resultado"] == "W")
    losses = sum(1 for t in graded if t["resultado"] == "L")
    net_units = sum(play_profit_units(t) for t in graded)
    net_dollars = sum(play_profit_dollars(t) for t in graded)
    risked = sum(t["stake_monto"] for t in graded)
    roi = (net_dollars / risked * 100.0) if risked else 0.0

    # Racha: orden cronológico, racha actual desde la más reciente.
    ordered = sorted(graded, key=lambda t: (t["fecha"], t["id"]))
    streak = "—"
    if ordered:
        last = ordered[-1]["resultado"]
        n = 0
        for t in reversed(ordered):
            if t["resultado"] == last:
                n += 1
            else:
                break
        streak = f"W{n}" if last == "W" else f"L{n}"

    return {
        "tracked": len(tracked),
        "pending": len(tracked) - len(graded),
        "wins": wins,
        "losses": losses,
        "record": f"{wins}-{losses}",
        "net_units": net_units,
        "net_dollars": net_dollars,
        "roi": roi,
        "streak": streak,
    }


def fmt_money(v: float) -> str:
    sign = "+" if v > 0 else ("-" if v < 0 else "")
    return f"{sign}${abs(v):,.2f}"


def fmt_units(v: float) -> str:
    sign = "+" if v > 0 else ("-" if v < 0 else "")
    return f"{sign}{abs(v):.2f}u"


def fmt_odds(o) -> str:
    o = int(o)
    return f"+{o}" if o > 0 else str(o)


app.jinja_env.globals.update(fmt_money=fmt_money, fmt_units=fmt_units, fmt_odds=fmt_odds)


@app.context_processor
def inject_user():
    nombre = session.get("nombre", "")
    corto = primer_nombre(nombre)
    return {
        "nombre_corto": corto,
        "inicial": corto[:1].upper(),
        "es_admin": bool(session.get("is_admin")),
        "miembro_platinum": platinum_unlocked_for(current_user()),
    }


def back(fallback="home"):
    """Vuelve a la página de origen (misma pestaña), o al fallback."""
    ref = request.referrer
    if ref and ref.startswith(request.host_url):
        return redirect(ref)
    return redirect(url_for(fallback))

init_db()  # idempotente: crea las tablas si no existen


def migrate_db():
    """Migración segura: agrega columnas si faltan (sin borrar datos)."""
    conn = _raw_connect()
    try:
        if USE_PG:
            rows = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'users'"
            ).fetchall()
            cols = {r["column_name"] for r in rows}
            coltype = "DOUBLE PRECISION"
        else:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            coltype = "REAL"
        pending = []
        if "bankroll" not in cols:
            pending.append(f"ALTER TABLE users ADD COLUMN bankroll {coltype}")
        if "platinum_unlocked" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN platinum_unlocked INTEGER NOT NULL DEFAULT 0")
        if "is_admin" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        for stmt in pending:
            conn.execute(stmt)
        if pending:
            conn.commit()
    finally:
        conn.close()


migrate_db()  # no borra datos: solo agrega la columna si falta


# ------------------------------------------------------------ Routes ----
@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("home"))
    return render_template("index.html")


@app.route("/home")
@login_required
def home():
    db = get_db()
    record_checkin(db, session["user_id"])
    program, plays = load_data()
    card_pendiente = not card_publicada_hoy()
    if card_pendiente:
        plays = []  # las de ayer no se muestran: la card de hoy aún no sale
    user = current_user()
    tracked_rows = db.execute(
        "SELECT * FROM tracked_plays WHERE user_id = ?",
        (session["user_id"],),
    ).fetchall()
    tracked_ids = {r["play_id"] for r in tracked_rows}
    tstats = compute_stats([dict(r) for r in tracked_rows])
    # Resultados recientes: jugadas liquidadas del archivo (sin bloqueadas).
    recientes = []
    for d in load_archive():
        for j in d.get("jugadas", []) or []:
            if (
                j.get("resultado") in ("WON", "LOST")
                and not j.get("bloqueada")
                and j.get("pick")
            ):
                recientes.append({
                    "fecha": d.get("titulo") or d.get("fecha", ""),
                    "nivel": j.get("nivel", ""),
                    "pick": j.get("pick", ""),
                    "cuota": j.get("cuota"),
                    "resultado": j.get("resultado"),
                    "profit": j.get("profit") if j.get("profit") is not None else 0.0,
                })
    recientes = recientes[:6]
    return render_template(
        "home.html",
        plays=plays,
        card_pendiente=card_pendiente,
        tracked_ids=tracked_ids,
        program=program,
        fecha=fecha_larga(),
        saludo=saludo_hoy(),
        racha=checkin_streak(db, session["user_id"]),
        bankroll=user["bankroll"] if user else None,
        platinum_unlocked=platinum_unlocked_for(user),
        res=load_results(),
        leccion=load_masterclass(),
        tstats=tstats,
        recientes=recientes,
    )


@app.route("/registro", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("home"))
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not nombre or not email or len(password) < 6:
            flash("Enter your name, a valid email and a password of at least 6 characters.", "error")
            return render_template("register.html"), 400
        db = get_db()
        exists = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if exists:
            flash("That email is already registered. Log in.", "error")
            return render_template("register.html"), 400
        new_id = insert_returning_id(
            db,
            "INSERT INTO users (nombre, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (nombre, email, hash_password(password), now_iso()),
        )
        db.commit()
        # El email del admin (ADMIN_EMAIL) queda marcado automáticamente.
        es_admin = bool(admin_email()) and email == admin_email()
        if es_admin:
            db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (new_id,))
            db.commit()
        session["user_id"] = new_id
        session["nombre"] = nombre
        session["is_admin"] = es_admin
        notify_new_member(nombre, email)
        send_welcome_email(nombre, email)
        return redirect(url_for("bienvenida"))
    return render_template("register.html")


@app.route("/bienvenida")
@login_required
def bienvenida():
    """Página de bienvenida tras el registro: explica el programa."""
    return render_template("bienvenida.html", nombre=session.get("nombre", ""))


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("home"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user or not check_password(password, user["password_hash"]):
            flash("Incorrect email or password.", "error")
            return render_template("login.html"), 401
        # Marca admin automáticamente si el email coincide con ADMIN_EMAIL
        # (cubre cuentas creadas antes de configurar la variable).
        es_admin = is_admin_for(user)
        if not es_admin and admin_email() and email == admin_email():
            db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user["id"],))
            db.commit()
            es_admin = True
        # Arranque: si todavía no existe ningún admin, el primero en entrar lo es.
        if not es_admin:
            fila = db.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin = 1").fetchone()
            n_admin = fila["n"] if isinstance(fila, dict) else fila[0]
            if n_admin == 0:
                db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user["id"],))
                db.commit()
                es_admin = True
        session["user_id"] = user["id"]
        session["nombre"] = user["nombre"]
        session["is_admin"] = es_admin
        flash(f"Welcome back, {user['nombre']}!", "ok")
        next_url = request.args.get("next") or url_for("home")
        return redirect(next_url)
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "ok")
    return redirect(url_for("index"))


@app.route("/cuenta", methods=["GET", "POST"])
@login_required
def cuenta():
    """Mi cuenta: configurar/actualizar el bankroll del miembro (persistencia en servidor)."""
    db = get_db()
    user = current_user()
    if request.method == "POST":
        raw = request.form.get("bankroll", "").strip()
        # Acepta formatos como "$10,000", "10,000", "10000", "10000.50".
        raw = raw.replace("$", "").replace(",", "").replace(" ", "")
        try:
            val = float(raw)
        except (ValueError, TypeError):
            val = 0.0
        if val <= 0:
            flash("Enter a valid bankroll greater than zero.", "error")
            return render_template("cuenta.html", bankroll=user["bankroll"] if user else None), 400
        db.execute("UPDATE users SET bankroll = ? WHERE id = ?", (val, session["user_id"]))
        db.commit()
        flash(f"Bankroll saved: ${val:,.2f}.", "ok")
        return redirect(url_for("cuenta"))
    return render_template("cuenta.html", bankroll=user["bankroll"] if user else None)


@app.route("/tracker")
@login_required
def tracker():
    db = get_db()
    record_checkin(db, session["user_id"])
    tracked = db.execute(
        "SELECT * FROM tracked_plays WHERE user_id = ? ORDER BY fecha DESC, id DESC",
        (session["user_id"],),
    ).fetchall()
    tracked = [dict(t) for t in tracked]
    tracked_ids = {t["play_id"] for t in tracked}
    stats = compute_stats(tracked)
    # CLV promedio: se toma del CLV publicado de cada jugada (plays.json).
    play_map = {p["id"]: p for p in load_plays()}
    clvs = [
        play_map[t["play_id"]].get("clv")
        for t in tracked
        if t["resultado"] in ("W", "L")
        and t["play_id"] in play_map
        and play_map[t["play_id"]].get("clv") is not None
    ]
    stats["clv_avg"] = (sum(clvs) / len(clvs)) if clvs else None
    user = current_user()
    card_pendiente = not card_publicada_hoy()
    return render_template(
        "dashboard.html",
        plays=[] if card_pendiente else load_plays(),
        card_pendiente=card_pendiente,
        tracked=tracked,
        tracked_ids=tracked_ids,
        stats=stats,
        nombre=session.get("nombre", ""),
        bankroll=user["bankroll"] if user else None,
        platinum_unlocked=platinum_unlocked_for(user),
    )


@app.route("/track/<play_id>", methods=["POST"])
@login_required
def track(play_id):
    play = next((p for p in load_plays() if p.get("id") == play_id), None)
    if not play:
        flash("Play not found.", "error")
        return redirect(url_for("home"))
    # La Platinum bloqueada no se puede trackear: no revela nada.
    if play.get("nivel") == "PLATINUM" and not platinum_unlocked_for(current_user()):
        flash("The Platinum play is locked. Unlock it to track it.", "warn")
        return redirect(url_for("desbloquear_platinum"))
    db = get_db()
    try:
        db.execute(
            """INSERT INTO tracked_plays
               (user_id, play_id, fecha, nivel, pick, cuota, stake_unidades,
                stake_monto, edge, resultado, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            (
                session["user_id"], play["id"], play.get("fecha", ""),
                play.get("nivel", ""), play.get("pick", ""), int(play.get("cuota", 0)),
                float(play.get("stake_unidades", 0)), float(play.get("stake_monto", 0)),
                play.get("edge"), now_iso(),
            ),
        )
        db.commit()
        flash("Play added to your tracker.", "ok")
    except INTEGRITY_ERRORS:
        flash("That play is already in your tracker.", "warn")
    return back("home")


@app.route("/resultado/<int:tracked_id>", methods=["POST"])
@login_required
def set_result(tracked_id):
    resultado = request.form.get("resultado")
    if resultado not in ("W", "L", ""):
        flash("Invalid result.", "error")
        return redirect(url_for("home"))
    db = get_db()
    row = db.execute(
        "SELECT id FROM tracked_plays WHERE id = ? AND user_id = ?",
        (tracked_id, session["user_id"]),
    ).fetchone()
    if not row:
        flash("Play not found.", "error")
        return redirect(url_for("home"))
    db.execute(
        "UPDATE tracked_plays SET resultado = ? WHERE id = ?",
        (resultado if resultado else None, tracked_id),
    )
    db.commit()
    flash("Result updated.", "ok")
    return back("tracker")


@app.route("/quitar/<int:tracked_id>", methods=["POST"])
@login_required
def untrack(tracked_id):
    db = get_db()
    db.execute(
        "DELETE FROM tracked_plays WHERE id = ? AND user_id = ?",
        (tracked_id, session["user_id"]),
    )
    db.commit()
    flash("Play removed from your tracker.", "ok")
    return back("tracker")


@app.route("/masterclass")
@login_required
def masterclass():
    db = get_db()
    record_checkin(db, session["user_id"])
    return render_template(
        "masterclass.html",
        leccion=load_masterclass(),
        archivo=load_masterclass_archivo(),
    )


@app.route("/programa")
@login_required
def programa():
    db = get_db()
    record_checkin(db, session["user_id"])
    return render_template("programa.html")


@app.route("/archivo")
@login_required
def archivo():
    db = get_db()
    record_checkin(db, session["user_id"])
    return render_template(
        "archivo.html",
        dias=load_archive(),
        res=load_results(),
    )


@app.route("/desbloquear-platinum")
@login_required
def desbloquear_platinum():
    db = get_db()
    record_checkin(db, session["user_id"])
    user = current_user()
    return render_template(
        "desbloquear.html",
        ya_desbloqueada=platinum_unlocked_for(user),
        stripe_url=STRIPE_PLATINUM_URL,
    )


@app.route("/admin/miembros/platinum", methods=["POST"])
@login_required
def admin_toggle_platinum():
    """Activa o quita el acceso Platinum de un miembro (solo Alex).

    Stripe (payment link) no avisa solo a la app, así que cuando llega
    la notificación de pago Alex activa el acceso aquí con un toque.
    """
    db = get_db()
    if not is_admin_for(current_user()):
        flash("You don't have permission.", "error")
        return redirect(url_for("home"))
    user_id = request.form.get("user_id")
    row = db.execute("SELECT platinum_unlocked FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        flash("Member not found.", "error")
    else:
        nuevo = 0 if row["platinum_unlocked"] else 1
        db.execute("UPDATE users SET platinum_unlocked = ? WHERE id = ?", (nuevo, user_id))
        db.commit()
        flash(
            "Platinum access activated." if nuevo else "Platinum access deactivated.",
            "ok",
        )
    return redirect(url_for("admin_miembros"))


@app.route("/admin/miembros/eliminar", methods=["POST"])
@login_required
def admin_eliminar_miembro():
    """Elimina una cuenta de miembro (solo admin). No permite borrarse a sí mismo ni a otro admin."""
    me = current_user()
    if not is_admin_for(me):
        flash("You don't have permission.", "error")
        return redirect(url_for("home"))
    user_id = request.form.get("user_id", type=int)
    if not user_id or (me and user_id == me["id"]):
        flash("You can't delete that account.", "error")
        return redirect(url_for("admin_miembros"))
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        flash("That account no longer exists.", "error")
        return redirect(url_for("admin_miembros"))
    if is_admin_for(target):
        flash("You can't delete another administrator.", "error")
        return redirect(url_for("admin_miembros"))
    db.execute("DELETE FROM tracked_plays WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash(f"Account {target['email']} deleted.", "ok")
    return redirect(url_for("admin_miembros"))


@app.route("/admin/seed-tracker")
@login_required
def admin_seed_tracker():
    """Importa una sola vez las jugadas liquidadas del programa al tracker personal del admin."""
    db = get_db()
    user = current_user()
    if not is_admin_for(user):
        flash("You don't have permission to view this page.", "error")
        return redirect(url_for("home"))
    added = 0
    for d in load_archive():
        fecha = d.get("fecha", "")
        for j in d.get("jugadas", []) or []:
            if j.get("resultado") not in ("WON", "LOST") or not j.get("pick"):
                continue
            slug = "".join(c if c.isalnum() else "-" for c in j["pick"].lower())
            slug = "-".join(s for s in slug.split("-") if s)
            play_id = f"hist-{fecha}-{slug}"
            try:
                db.execute(
                    """INSERT INTO tracked_plays
                       (user_id, play_id, fecha, nivel, pick, cuota, stake_unidades,
                        stake_monto, edge, resultado, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session["user_id"], play_id, fecha,
                        j.get("nivel", ""), j.get("pick", ""), int(j.get("cuota", 0)),
                        float(j.get("stake_unidades", 0)), float(j.get("stake_monto", 0)),
                        j.get("edge"), "W" if j.get("resultado") == "WON" else "L",
                        now_iso(),
                    ),
                )
                added += 1
            except INTEGRITY_ERRORS:
                pass
    db.commit()
    flash(f"Tracker seeded: {added} settled plays imported.", "ok")
    return redirect(url_for("tracker"))


@app.route("/admin/miembros")
@login_required
def admin_miembros():
    """Lista privada de miembros — solo para Alex (is_admin=1)."""
    db = get_db()
    user = current_user()
    if not is_admin_for(user):
        flash("You don't have permission to view this page.", "error")
        return redirect(url_for("home"))
    miembros = db.execute(
        "SELECT id, nombre, email, created_at, platinum_unlocked, is_admin "
        "FROM users ORDER BY created_at DESC"
    ).fetchall()
    ahora = datetime.now(timezone.utc)
    lista = []
    for m in miembros:
        d = dict(m)
        try:
            creado = datetime.fromisoformat(d["created_at"])
            if creado.tzinfo is None:
                creado = creado.replace(tzinfo=timezone.utc)
            d["es_nuevo"] = (ahora - creado) < timedelta(hours=48)
        except Exception:
            d["es_nuevo"] = False
        lista.append(d)
    return render_template("admin_miembros.html", miembros=lista)


@app.route("/resultados")
@login_required
def resultados():
    db = get_db()
    record_checkin(db, session["user_id"])
    return render_template("resultados.html", res=load_results())


@app.route("/ev-board")
@login_required
def ev_board():
    db = get_db()
    record_checkin(db, session["user_id"])
    return render_template("ev_board.html", board=load_ev_board())


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
