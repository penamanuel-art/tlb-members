"""
The Sharp Team — Plataforma de miembros (v1).

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
import re
import secrets
import sqlite3
import urllib.request
from datetime import date, timedelta, datetime, timezone
from functools import wraps

import bcrypt
from flask import (
    Flask, Response, flash, g, jsonify, redirect, render_template, request,
    session, url_for,
)
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL")
USE_PG = bool(DATABASE_URL)

# Link de pago de Stripe para desbloquear Elite ($1 primera semana, luego $23/semana).
# Se cambia sin tocar código con la env var STRIPE_PLATINUM_URL en Render.
STRIPE_PLATINUM_URL = os.environ.get(
    "STRIPE_PLATINUM_URL", "https://buy.stripe.com/28E14p64MfDeeiybEQefC00"
)

# Web Push (notificaciones del +EV Board en el iPhone, estilo WGT).
# Claves VAPID como env vars en Render: VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY,
# VAPID_CLAIM_EMAIL y PUSH_TRIGGER_KEY
# (ver goals/programa-de-apuestas-deportivas/hidden_files/push-keys.env).
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_CLAIM_EMAIL = os.environ.get(
    "VAPID_CLAIM_EMAIL", "mailto:thelinebreaker680@gmail.com"
)
PUSH_TRIGGER_KEY = os.environ.get("PUSH_TRIGGER_KEY", "")

try:
    from pywebpush import webpush, WebPushException
    HAVE_WEBPUSH = True
except ImportError:  # pragma: no cover - sin pywebpush instalado
    webpush = None
    WebPushException = Exception
    HAVE_WEBPUSH = False

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
# Sesión persistente (2026-09-25): sin esto la cookie de sesión de Flask es
# temporal y iOS la borra al liberar memoria de Safari → logout constante.
# Con permanent=True en login(), la cookie dura 30 días.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

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
    stake_fijo_elite REAL,       -- monto fijo personal ELITE (NULL = fórmula 1%)
    stake_fijo_gold REAL,        -- monto fijo personal GOLD (NULL = fórmula 1%)
    platinum_unlocked INTEGER NOT NULL DEFAULT 0,  -- 1 = Elite desbloqueada
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
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id INTEGER NOT NULL REFERENCES users(id),
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_push_member ON push_subscriptions(member_id);
"""

DDL_PG = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    bankroll DOUBLE PRECISION,   -- bankroll del miembro (NULL = sin configurar)
    stake_fijo_elite DOUBLE PRECISION, -- monto fijo personal ELITE (NULL = fórmula 1%)
    stake_fijo_gold DOUBLE PRECISION,  -- monto fijo personal GOLD (NULL = fórmula 1%)
    platinum_unlocked INTEGER NOT NULL DEFAULT 0,  -- 1 = Elite desbloqueada
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
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id SERIAL PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES users(id),
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_push_member ON push_subscriptions(member_id);
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


def admin_required(view):
    """Solo administradores: requiere login + flag de admin."""
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not is_admin_for(current_user()):
            flash("Admin access required.", "error")
            return redirect(url_for("home"))
        return view(*args, **kwargs)
    return wrapper


def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()


def platinum_unlocked_for(user) -> bool:
    """True si el miembro desbloqueó la jugada Elite (default: bloqueada)."""
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
            msg["Subject"] = f"The Sharp Team: new member — {nombre}"
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
WELCOME_SUBJECT = "Welcome to The Sharp Team"

WELCOME_TEXT = """Hi {nombre},

Your account is ready. Welcome to The Sharp Team: a sports betting program built on numbers, discipline, and value. No hunches.

YOUR MEMBER DASHBOARD
Go straight here:
https://the-line-breaker-members.onrender.com/home

You'll see each morning's plays there (~11:00 AM, New York time).

HOW IT WORKS
- We publish a maximum of 3 plays per day, and only when the model detects real value. If there's no value, there's no play: discipline also means not betting.
- Each play shows its level: ELITE, the top play of the day (edge over 5%), or GOLD (edge between 3% and 5%), always with its odds, stake, and explanation.

ELITE
- The Elite play is exclusive to Elite members.
- It costs $1 the first week, then $23 per week. Activate it from your dashboard, under "Unlock Elite".

GOLDEN RULES
1. Bet exactly the amount indicated: no more, no less.
2. Never chase losses or wins.
3. Numbers and value only. No team favoritism.

See you tomorrow at 11:00 AM with the first plays.

- The Sharp Team

Bet responsibly - 21+ - Gambling problem? Call 1-800-GAMBLER (1-800-426-2537): free and confidential help, 24/7.
"""

WELCOME_HTML = """<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; max-width: 600px; margin: 0 auto; background: #ffffff;">
<div style="background: #1a1a2e; padding: 30px 20px; text-align: center; border-radius: 18px 18px 0 0;">
<img src="https://files.catbox.moe/g1ig30.jpg" alt="The Sharp Team" width="140" style="width: 140px; height: auto; display: block; margin: 0 auto; border: 0;">
<p style="margin: 12px 0 0; font-size: 16px; font-weight: 800; color: #c9a227; letter-spacing: 5px;">THE SHARP TEAM</p>
</div>
<div style="padding: 28px 26px; color: #1a1a2e;">
<p style="font-size: 20px; font-weight: 800; margin: 0 0 6px;">Hi {nombre},</p>
<p style="font-size: 15px; line-height: 1.7; margin: 0 0 18px; color: #333333;">Your account is ready. Welcome to <strong>The Sharp Team</strong>: a sports betting program built on numbers, discipline, and value. No hunches.</p>
<div style="text-align: center; margin: 22px 0;">
<a href="https://the-line-breaker-members.onrender.com/home" style="display: inline-block; background: #c9a227; color: #1a1a2e; font-size: 16px; font-weight: 800; padding: 14px 34px; border-radius: 12px; text-decoration: none;">Go to my dashboard &rarr;</a>
</div>
<p style="font-size: 14px; color: #666666; text-align: center; margin: 0 0 22px;">You&apos;ll see each morning&apos;s plays there (~11:00 AM, New York time).</p>
<p style="font-size: 16px; font-weight: 800; margin: 0 0 8px; color: #1a1a2e;">HOW IT WORKS</p>
<ul style="font-size: 14px; line-height: 1.8; color: #333333; margin: 0 0 18px; padding-left: 20px;">
<li>We publish a maximum of 3 plays per day, and only when the model detects real value. If there&apos;s no value, there&apos;s no play: discipline also means not betting.</li>
<li>Each play shows its level: <strong>ELITE</strong>, the top play of the day (edge over 5%), or <strong>GOLD</strong> (edge between 3% and 5%), always with its odds, stake, and explanation.</li>
</ul>
<p style="font-size: 16px; font-weight: 800; margin: 0 0 8px; color: #1a1a2e;">ELITE</p>
<ul style="font-size: 14px; line-height: 1.8; color: #333333; margin: 0 0 18px; padding-left: 20px;">
<li>The Elite play is exclusive to Elite members.</li>
<li>It costs <strong>$1 the first week</strong>, then <strong>$23 per week</strong>. Activate it from your dashboard, under &quot;Unlock Elite&quot;.</li>
</ul>
<p style="font-size: 16px; font-weight: 800; margin: 0 0 8px; color: #1a1a2e;">GOLDEN RULES</p>
<ol style="font-size: 14px; line-height: 1.8; color: #333333; margin: 0 0 18px; padding-left: 20px;">
<li>Bet exactly the amount indicated: no more, no less.</li>
<li>Never chase losses or wins.</li>
<li>Numbers and value only. No team favoritism.</li>
</ol>
<p style="font-size: 14px; color: #333333; line-height: 1.7; margin: 0;">See you tomorrow at 11:00 AM with the first plays.</p>
<p style="font-size: 14px; color: #333333; margin: 12px 0 0;">&mdash; <strong>The Sharp Team</strong></p>
</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin: 6px 0 0; background: #1a1a2e; border-radius: 0 0 18px 18px; border-top: 3px solid #c9a227;">
<tr><td style="padding: 22px 20px; text-align: center;">
<img src="https://files.catbox.moe/g1ig30.jpg" alt="The Sharp Team" width="110" style="width: 110px; height: auto; display: block; margin: 0 auto; border: 0;">
<p style="margin: 10px 0 4px; font-size: 14px; font-weight: 800; color: #c9a227; letter-spacing: 4px;">THE SHARP TEAM</p>
<p style="margin: 0; font-size: 12px; color: #dddddd;">250 Park Avenue, Suite 1800, New York, NY 10017</p>
<p style="margin: 4px 0 0; font-size: 12px; color: #dddddd;">(983) 819-4589</p>
<div style="width: 60%; height: 1px; background: #33334d; margin: 14px auto;">&nbsp;</div>
<p style="margin: 0; font-size: 11px; color: #aaaaaa; line-height: 1.6;">Bet responsibly &middot; 21+<br>Gambling problem? Call 1-800-GAMBLER (1-800-426-2537), free and confidential help, 24/7.</p>
<p style="margin: 10px 0 0; font-size: 10px; color: #888888; line-height: 1.6;">The Sharp Team picks are for informational and entertainment purposes. No pick guarantees winnings; sports betting involves risk of loss.</p>
<p style="margin: 8px 0 0; font-size: 10px; color: #666666;">&copy; 2026 The Sharp Team &middot; All rights reserved</p>
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
            msg["From"] = f"The Sharp Team <{user}>"
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


def stake_personalizado(play, user):
    """Monto a mostrar/trackear.

    Pedido por Alex 2026-09-26 (como la app de WGT): cada miembro ve en su
    dashboard su monto personal = 1% de SU bankroll x las unidades de la
    jugada (1u -> 1%, 0.75u -> 0.75%, 0.5u -> 0.5%). Si no tiene bankroll configurado,
    se usa el stake oficial del programa (stake_monto de plays.json).

    Montos fijos por nivel (pedido por Alex 2026-09-26): si el miembro tiene
    stake_fijo_elite / stake_fijo_gold configurado, ese monto fijo reemplaza
    la fórmula para ese nivel. Solo afecta a su dashboard, no al público.
    """
    nivel = (play.get("nivel") or "").upper()
    if user:
        try:
            if nivel == "ELITE" and user.get("stake_fijo_elite"):
                return round(float(user["stake_fijo_elite"]), 2)
            if nivel == "GOLD" and user.get("stake_fijo_gold"):
                return round(float(user["stake_fijo_gold"]), 2)
        except (TypeError, ValueError):
            pass
    try:
        units = float(play.get("stake_unidades") or 0)
    except (TypeError, ValueError):
        units = 0
    try:
        br = float((user or {}).get("bankroll") or 0)
    except (TypeError, ValueError):
        br = 0
    if br > 0 and units > 0:
        return round(br * 0.01 * units, 2)
    return play.get("stake_monto")


def personalizar_plays(plays, user):
    """Devuelve copias de las jugadas con stake_monto personalizado."""
    out = []
    for p in plays:
        p = dict(p)
        p["stake_monto"] = stake_personalizado(p, user)
        out.append(p)
    return out



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


def recientes_oficiales(n=6):
    """Jugadas liquidadas del archivo oficial (sin bloqueadas), más recientes primero."""
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
                    "comprobante": j.get("comprobante"),
                })
    return recientes[:n]


def program_stats():
    """Stats del programa calculadas EN VIVO desde data/archive.json.

    Reemplaza a data/results.json (quedó obsoleto el 2026-09-27): el
    dashboard debe reflejar los resultados oficiales inmediatamente
    después de cada liquidación, sin archivos intermedios.
    """
    ganadas = perdidas = 0
    profit = risked = 0.0
    for d in load_archive():
        for j in d.get("jugadas", []) or []:
            if j.get("resultado") not in ("WON", "LOST") or j.get("bloqueada"):
                continue
            if j["resultado"] == "WON":
                ganadas += 1
            else:
                perdidas += 1
            profit += float(j.get("profit") or 0.0)
            risked += float(j.get("stake_monto") or 0.0)
    settled = ganadas + perdidas
    return {
        "profit_all_time": round(profit, 2),
        "ganadas": ganadas,
        "perdidas": perdidas,
        "win_rate": round(ganadas / settled * 100, 1) if settled else 0.0,
        "total_arriesgado": round(risked, 2),
        "roi": round(profit / risked * 100, 1) if risked else 0.0,
    }


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


# Sharp Money board: el JSON vive en la rama `data-board`
# (igual que el +EV Board: actualizar datos NO redespliega Render).
SHARP_BOARD_URL = (
    "https://raw.githubusercontent.com/penamanuel-art/tlb-members"
    "/data-board/data/sharp_board.json"
)
_sharp_board_cache = {"at": 0.0, "data": {}}


def load_sharp_board():
    import time
    now = time.time()
    if now - _sharp_board_cache["at"] < 300 and _sharp_board_cache["data"]:
        return _sharp_board_cache["data"]
    data = {}
    try:
        req = urllib.request.Request(SHARP_BOARD_URL, headers={"User-Agent": "tlb-members"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except Exception:
        data = _sharp_board_cache["data"] or {}
    _sharp_board_cache["at"] = now
    _sharp_board_cache["data"] = data
    return data


# Player Props board: el JSON vive en la rama `data-board`
# (igual que el +EV Board: actualizar datos NO redespliega Render).
PROPS_BOARD_URL = (
    "https://raw.githubusercontent.com/penamanuel-art/tlb-members"
    "/data-board/data/props_board.json"
)
_props_board_cache = {"at": 0.0, "data": {}}


def load_props_board():
    import time
    now = time.time()
    if now - _props_board_cache["at"] < 300 and _props_board_cache["data"]:
        return _props_board_cache["data"]
    data = {}
    try:
        req = urllib.request.Request(PROPS_BOARD_URL, headers={"User-Agent": "tlb-members"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except Exception:
        data = _props_board_cache["data"] or {}
    _props_board_cache["at"] = now
    _props_board_cache["data"] = data
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


_EQUIPO_LOGO = {
    # nombre en el pick -> (liga ESPN, abreviatura ESPN en minusculas)
    "Braves": ("mlb", "atl"),
    "Marlins": ("mlb", "mia"),
    "Brewers": ("mlb", "mil"),
    "Phillies": ("mlb", "phi"),
    "Nationals": ("mlb", "wsh"),
    "Mets": ("mlb", "nym"),
    "Padres": ("mlb", "sd"),
    "Diamondbacks": ("mlb", "ari"),
    "Rays": ("mlb", "tb"),
    "Yankees": ("mlb", "nyy"),
    "Dodgers": ("mlb", "lad"),
    "Falcons": ("nfl", "atl"),
    "Packers": ("nfl", "gb"),
}


def equipo_logo_info(pick):
    """(ABBR, logo_url) del equipo elegido en el pick.

    El pick tiene forma '<Equipo> ML @/vs <rival>'. Si el equipo no esta
    en el mapa, devuelve ('', '') y la plantilla muestra el texto sin logo
    (nunca se inventa una abreviatura).
    """
    nombre = (pick or "").split(" ML ")[0].strip()
    info = _EQUIPO_LOGO.get(nombre)
    if not info:
        return "", ""
    liga, abbr = info
    return abbr.upper(), (
        f"https://a.espncdn.com/i/teamlogos/{liga}/500/{abbr}.png"
    )


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
        "risked": risked,
        "record": f"{wins}-{losses}",
        "net_units": net_units,
        "net_dollars": net_dollars,
        "roi": roi,
        "streak": streak,
    }



_RE_FECHA_PLAYID = re.compile(r"^(?:play|hist)-(\d{4}-\d{2}-\d{2})-")


def fecha_de_play(play):
    """Fecha de una jugada: campo fecha, o extraída del play_id.
    plays.json no trae fecha por jugada (solo raíz), así que el play_id
    es la fuente confiable: play-YYYY-MM-DD-<slug>."""
    f = (play.get("fecha") or "").strip()
    if f:
        return f
    m = _RE_FECHA_PLAYID.match(play.get("id") or "")
    return m.group(1) if m else ""


def auto_grado_tracked(db, user):
    """Liquida automáticamente las jugadas trackeadas pendientes usando los
    resultados oficiales del programa (data/archive.json).

    SOLO para el administrador (Alex): su tracker se actualiza solo en
    cuanto la liquidación nocturna publica el resultado oficial. Los
    miembros marcan sus resultados a mano, según sus propias jugadas.

    Se ejecuta en cada vista del tracker y del home. Solo toca jugadas
    con resultado pendiente (NULL); nunca reescribe un resultado ya
    marcado.
    """
    if not is_admin_for(user):
        return 0
    user_id = user["id"]
    pendientes = db.execute(
        "SELECT id, play_id, fecha, pick FROM tracked_plays "
        "WHERE user_id = ? AND resultado IS NULL",
        (user_id,),
    ).fetchall()
    if not pendientes:
        return 0
    oficiales = {}
    for d in load_archive():
        fecha = (d.get("fecha") or "").strip()
        for j in d.get("jugadas", []) or []:
            r = j.get("resultado")
            pick = (j.get("pick") or "").strip().lower()
            if r in ("WON", "LOST") and pick:
                oficiales[(fecha, pick)] = "W" if r == "WON" else "L"
    n = 0
    for t in pendientes:
        f = (t["fecha"] or "").strip() or (
            _RE_FECHA_PLAYID.match(t["play_id"] or "").group(1)
            if _RE_FECHA_PLAYID.match(t["play_id"] or "") else ""
        )
        key = (f, (t["pick"] or "").strip().lower())
        res = oficiales.get(key)
        if res:
            db.execute(
                "UPDATE tracked_plays SET resultado = ? WHERE id = ?",
                (res, t["id"]),
            )
            n += 1
    if n:
        db.commit()
    return n

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
        "ticker_days": compute_ticker_days(),
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
        if "stake_fijo_elite" not in cols:
            pending.append(f"ALTER TABLE users ADD COLUMN stake_fijo_elite {coltype}")
        if "stake_fijo_gold" not in cols:
            pending.append(f"ALTER TABLE users ADD COLUMN stake_fijo_gold {coltype}")
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


def compute_ticker_days():
    """Ticker de resultados (pedido por Alex 2026-09-26, refinado el mismo día):
    UN solo día: el último día con jugadas liquidadas (el día anterior).
    En vivo desde data/archive.json — se actualiza solo al liquidar.
    Sin inventos: solo jugadas WON/LOST reales."""
    ticker_days = []
    for d in load_archive():
        fecha = d.get("fecha", "")
        jugadas = [
            j for j in (d.get("jugadas", []) or [])
            if j.get("resultado") in ("WON", "LOST") and not j.get("bloqueada")
        ]
        if not jugadas:
            continue
        wins = sum(1 for j in jugadas if j.get("resultado") == "WON")
        losses = len(jugadas) - wins
        profit = sum(float(j.get("profit") or 0.0) for j in jugadas)
        risked = sum(float(j.get("stake_monto") or 0.0) for j in jugadas)
        roi = (profit / risked * 100.0) if risked else 0.0
        try:
            etiqueta = datetime.strptime(fecha, "%Y-%m-%d").strftime("%a %b %d").upper()
        except (ValueError, TypeError):
            etiqueta = (d.get("titulo") or fecha).upper()
        ticker_days.append({
            "etiqueta": etiqueta,
            "record": f"{wins}-{losses}",
            "profit": profit,
            "roi": roi,
        })
        if len(ticker_days) >= 1:
            break
    return ticker_days


@app.route("/home")
@login_required
def home():
    db = get_db()
    record_checkin(db, session["user_id"])
    auto_grado_tracked(db, current_user())
    program, plays = load_data()
    card_pendiente = not card_publicada_hoy()
    if card_pendiente:
        plays = []  # las de ayer no se muestran: la card de hoy aún no sale
    user = current_user()
    plays = personalizar_plays(plays, user)
    tracked_rows = db.execute(
        "SELECT * FROM tracked_plays WHERE user_id = ?",
        (session["user_id"],),
    ).fetchall()
    tracked_ids = {r["play_id"] for r in tracked_rows}
    tstats = compute_stats([dict(r) for r in tracked_rows])
    # Resultados recientes: jugadas liquidadas del archivo (sin bloqueadas).
    recientes = recientes_oficiales(6)
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
        res=program_stats(),
        leccion=load_masterclass(),
        archivo_mc=load_masterclass_archivo(),
        tstats=tstats,
        recientes=recientes,
        es_admin=is_admin_for(user),
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
        session.permanent = True  # 2026-09-25: cookie persistente 30 días (antes iOS la borraba → logout)
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


def resumen_rangos(tracked):
    """Resumen por rangos estilo app de referencia (pedido por Alex 2026-09-27):
    Today / Yesterday / This Week / This Month / This Year / All Time,
    cada uno con record W-L y profit — siempre con los numeros reales
    del miembro (o del admin) que esta viendo la pagina."""
    hoy = datetime.now(TZ).date()
    ayer = hoy - timedelta(days=1)
    inicio_semana = hoy - timedelta(days=(hoy.weekday() + 1) % 7)  # semana empieza domingo
    inicio_mes = date(hoy.year, hoy.month, 1)
    inicio_ano = date(hoy.year, 1, 1)

    def fecha_de(t):
        try:
            return date.fromisoformat((t["fecha"] or "").strip())
        except (ValueError, AttributeError):
            return None

    def agg(items):
        g = [t for t in items if t["resultado"] in ("W", "L")]
        w = sum(1 for t in g if t["resultado"] == "W")
        return {"record": f"{w}-{len(g) - w}-0",
                "profit": sum(play_profit_dollars(t) for t in g)}

    con_fecha = [(t, fecha_de(t)) for t in tracked]
    en = lambda d, ini: d is None or d >= ini  # sin fecha: cuenta como en rango (igual que las pildoras)
    return [
        ("Today", agg([t for t, d in con_fecha if d == hoy])),
        ("Yesterday", agg([t for t, d in con_fecha if d == ayer])),
        ("This Week", agg([t for t, d in con_fecha if en(d, inicio_semana)])),
        ("This Month", agg([t for t, d in con_fecha if en(d, inicio_mes)])),
        ("This Year", agg([t for t, d in con_fecha if en(d, inicio_ano)])),
        ("All Time", agg(tracked)),
    ]


@app.route("/tracker")
@login_required
def tracker():
    db = get_db()
    record_checkin(db, session["user_id"])
    auto_grado_tracked(db, current_user())
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
    # Filas enriquecidas para el tracker unico estilo WGT: marcador y casa
    # desde el archivo oficial (match por fecha+pick), logo ESPN del equipo.
    # Solo informativo: el resultado de cada fila lo decide el auto-grado
    # (admin) o el marcado manual del miembro; nunca se reescribe aqui.
    oficiales = {}
    for d in load_archive():
        fecha = (d.get("fecha") or "").strip()
        for j in d.get("jugadas", []) or []:
            pk = (j.get("pick") or "").strip().lower()
            if fecha and pk:
                oficiales[(fecha, pk)] = j
    rows = []
    for t in tracked:
        f = (t["fecha"] or "").strip()
        off = oficiales.get((f, (t["pick"] or "").strip().lower()), {})
        abbr, logo = equipo_logo_info(t["pick"])
        try:
            fecha_corta = datetime.strptime(f, "%Y-%m-%d").strftime("%d %b %Y").upper()
        except (ValueError, TypeError):
            fecha_corta = f
        rows.append({
            "id": t["id"],
            "fecha": f,
            "fecha_corta": fecha_corta,
            "pick": t["pick"],
            "cuota": t["cuota"],
            "cuota_txt": fmt_odds(t["cuota"]),
            "nivel": t["nivel"],
            "stake_unidades": t["stake_unidades"],
            "stake_monto": t["stake_monto"],
            "resultado": t["resultado"] or "",
            "profit": play_profit_dollars(t),
            "casa": (off.get("casa") or "Novig"),
            "marcador": (off.get("marcador") or ""),
            "comprobante": (off.get("comprobante") or ""),
            "abbr": abbr,
            "logo": logo,
        })
    return render_template(
        "dashboard.html",
        plays=[] if card_pendiente else load_plays(),
        card_pendiente=card_pendiente,
        rows=rows,
        tracked_ids=tracked_ids,
        stats=stats,
        rangos=resumen_rangos(tracked),
        nombre=session.get("nombre", ""),
        bankroll=user["bankroll"] if user else None,
        platinum_unlocked=platinum_unlocked_for(user),
        res=program_stats(),
        recientes=recientes_oficiales(),
        es_admin=is_admin_for(user),
    )


@app.route("/tracker/export")
def tracker_export():
    """Descarga CSV del tracker: semana / mes / año / rango de fechas a elegir.

    Params: range=7|30|365|mtd|all  o  from=YYYY-MM-DD&to=YYYY-MM-DD.
    Columnas: fecha, jugada, cuota, nivel, monto apostado, resultado,
    profit, marcador, casa + resumen (récord, win rate, ROI...).
    """
    if "user_id" not in session:
        return redirect(url_for("login"))
    rng = (request.args.get("range") or "").strip().lower()
    f_from = (request.args.get("from") or "").strip()
    f_to = (request.args.get("to") or "").strip()
    hoy = datetime.now(TZ).date()

    desde, hasta, nombre_rango = None, None, ""
    if f_from or f_to:
        try:
            desde = date.fromisoformat(f_from) if f_from else date(2020, 1, 1)
            hasta = date.fromisoformat(f_to) if f_to else hoy
        except ValueError:
            desde, hasta = None, None
        if desde and hasta and desde <= hasta:
            nombre_rango = f"{desde.isoformat()} to {hasta.isoformat()}"
    if not nombre_rango:
        if rng not in ("7", "30", "365", "mtd", "all"):
            rng = "7"
        nombre_rango = {"7": "Last 7 days", "30": "Last 30 days",
                        "365": "Last 12 months", "mtd": "Month to date",
                        "all": "All time"}[rng]
        if rng == "mtd":
            desde = date(hoy.year, hoy.month, 1)
            hasta = hoy
        elif rng != "all":
            desde = hoy - timedelta(days=int(rng) - 1)
            hasta = hoy

    def en_rango(fecha):
        if desde is None:
            return True
        f = (fecha or "").strip()
        if not f:
            return True
        try:
            d = date.fromisoformat(f)
        except ValueError:
            return True
        return desde <= d <= hasta

    db = get_db()
    tracked = db.execute(
        "SELECT * FROM tracked_plays WHERE user_id = ? ORDER BY fecha DESC, id DESC",
        (session["user_id"],),
    ).fetchall()
    tracked = [dict(t) for t in tracked]
    filtradas = [t for t in tracked if en_rango(t["fecha"])]
    stats = compute_stats(filtradas)

    oficiales = {}
    for d in load_archive():
        fecha = (d.get("fecha") or "").strip()
        for j in d.get("jugadas", []) or []:
            pk = (j.get("pick") or "").strip().lower()
            if fecha and pk:
                oficiales[(fecha, pk)] = j

    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["THE SHARP TEAM — Tracker export"])
    w.writerow(["Range", nombre_rango])
    w.writerow(["Generated", datetime.now(TZ).strftime("%Y-%m-%d %H:%M ET")])
    w.writerow([])
    w.writerow(["SUMMARY"])
    liquidadas = stats["wins"] + stats["losses"]
    w.writerow(["Settled plays", liquidadas])
    w.writerow(["Won", stats["wins"]])
    w.writerow(["Lost", stats["losses"]])
    wr = (stats["wins"] / liquidadas * 100) if liquidadas else 0
    w.writerow(["Win rate", f"{wr:.1f}%"])
    w.writerow(["Total staked", f"${stats['risked']:.2f}"])
    w.writerow(["Total profit", f"${stats['net_dollars']:.2f}"])
    w.writerow(["ROI", f"{stats['roi']:+.1f}%"])
    w.writerow([])
    w.writerow(["PLAYS"])
    w.writerow(["Date", "Pick", "Odds", "Level", "Staked", "Result",
                "Profit", "Score", "Book"])
    for t in filtradas:
        f = (t["fecha"] or "").strip()
        off = oficiales.get((f, (t["pick"] or "").strip().lower()), {})
        res = t["resultado"] or ""
        w.writerow([
            f or "—",
            t["pick"] or "",
            fmt_odds(t["cuota"]),
            t["nivel"] or "",
            f"${t['stake_monto']:.2f}",
            {"W": "Won", "L": "Lost"}.get(res, "Pending"),
            f"${play_profit_dollars(t):+.2f}" if res in ("W", "L") else "—",
            off.get("marcador") or "",
            off.get("casa") or "Novig",
        ])
    etiqueta = {"7": "7d", "30": "30d", "365": "12m", "mtd": "mtd", "all": "all"}.get(
        rng, f"{desde.isoformat()}_{hasta.isoformat()}" if desde else "all")
    fname = f"tracker-{etiqueta}-{hoy.isoformat()}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.route("/track/<play_id>", methods=["POST"])
@login_required
def track(play_id):
    play = next((p for p in load_plays() if p.get("id") == play_id), None)
    if not play:
        flash("Play not found.", "error")
        return redirect(url_for("home"))
    # La Elite bloqueada no se puede trackear: no revela nada.
    _cu = current_user()
    if play.get("nivel") in ("PLATINUM", "ELITE") and not platinum_unlocked_for(_cu):
        flash("The Elite play is locked. Unlock it to track it.", "warn")
        return redirect(url_for("desbloquear_platinum"))
    db = get_db()
    try:
        db.execute(
            """INSERT INTO tracked_plays
               (user_id, play_id, fecha, nivel, pick, cuota, stake_unidades,
                stake_monto, edge, resultado, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            (
                session["user_id"], play["id"], fecha_de_play(play),
                play.get("nivel", ""), play.get("pick", ""), int(play.get("cuota", 0)),
                float(play.get("stake_unidades", 0)),
                float(stake_personalizado(play, _cu)),
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
        res=program_stats(),
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
    """Activa o quita el acceso Elite de un miembro (solo Alex).

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
            "Elite access activated." if nuevo else "Elite access deactivated.",
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


@app.route("/admin/miembros/stake-fijo", methods=["POST"])
@login_required
def admin_stake_fijo():
    """Monto fijo personal por nivel para un miembro (solo Alex).

    Pedido por Alex 2026-09-26: p.ej. su cuenta con Elite $25 / Gold $15.
    Solo afecta al dashboard de ese miembro; el resto sigue con la fórmula 1%.
    Vacío = volver a la fórmula.
    """
    db = get_db()
    user = current_user()
    if not is_admin_for(user):
        flash("You don't have permission.", "error")
        return redirect(url_for("home"))
    uid = request.form.get("user_id")

    def parse(v):
        v = (v or "").strip()
        return float(v) if v else None

    try:
        elite = parse(request.form.get("stake_elite"))
        gold = parse(request.form.get("stake_gold"))
        if (elite is not None and elite <= 0) or (gold is not None and gold <= 0):
            raise ValueError
    except (TypeError, ValueError):
        flash("Enter valid amounts greater than zero (or leave empty).", "error")
        return redirect(url_for("admin_miembros"))
    db.execute(
        "UPDATE users SET stake_fijo_elite = ?, stake_fijo_gold = ? WHERE id = ?",
        (elite, gold, uid),
    )
    db.commit()
    flash("Fixed stakes saved.", "ok")
    return redirect(url_for("admin_miembros"))



@app.route("/admin/diag-tracker")
@admin_required
def diag_tracker():
    """Diagnóstico: muestra las jugadas trackeadas del admin en JSON plano."""
    from flask import Response
    db = get_db()
    rows = db.execute(
        "SELECT id, play_id, fecha, pick, cuota, resultado, created_at "
        "FROM tracked_plays WHERE user_id = ? ORDER BY id",
        (session["user_id"],),
    ).fetchall()
    data = [dict(r) for r in rows]
    return Response(json.dumps(data, indent=1, ensure_ascii=False),
                    mimetype="application/json")

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
        "SELECT id, nombre, email, created_at, platinum_unlocked, is_admin, "
        "stake_fijo_elite, stake_fijo_gold "
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
    return render_template("resultados.html", res=program_stats(), dias=load_archive())


@app.route("/ev-board")
@login_required
def ev_board():
    db = get_db()
    record_checkin(db, session["user_id"])
    return render_template("ev_board.html", board=load_ev_board())


@app.route("/sharp")
@login_required
def sharp():
    db = get_db()
    record_checkin(db, session["user_id"])
    return render_template("sharp.html", board=load_sharp_board())


@app.route("/props")
@login_required
def props():
    db = get_db()
    record_checkin(db, session["user_id"])
    return render_template("props.html", board=load_props_board())


# ------------------------------------------------------- Web Push ----
@app.route("/push/vapid-public-key")
def push_vapid_public_key():
    """Clave pública VAPID (pública, sin login): la usa el navegador para suscribirse."""
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})


@app.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    """Guarda la suscripción push del navegador del miembro."""
    data = request.get_json(force=True, silent=True) or {}
    endpoint = (data.get("endpoint") or "").strip()
    keys = data.get("keys") or {}
    p256dh = keys.get("p256dh", "")
    auth = keys.get("auth", "")
    if not endpoint or not p256dh or not auth:
        return jsonify({"ok": False, "error": "bad subscription"}), 400
    db = get_db()
    existing = db.execute(
        "SELECT id FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE push_subscriptions SET member_id = ?, p256dh = ?, auth = ? WHERE endpoint = ?",
            (session["user_id"], p256dh, auth, endpoint),
        )
    else:
        db.execute(
            "INSERT INTO push_subscriptions (member_id, endpoint, p256dh, auth, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (session["user_id"], endpoint, p256dh, auth, now_iso()),
        )
    db.commit()
    return jsonify({"ok": True})


@app.route("/push/unsubscribe", methods=["POST"])
@login_required
def push_unsubscribe():
    data = request.get_json(force=True, silent=True) or {}
    endpoint = (data.get("endpoint") or "").strip()
    if endpoint:
        db = get_db()
        db.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ? AND member_id = ?",
            (endpoint, session["user_id"]),
        )
        db.commit()
    return jsonify({"ok": True})


@app.route("/sw.js")
def service_worker():
    """Service Worker en la raíz (scope /): requerido para Web Push."""
    path = os.path.join(app.static_folder, "sw.js")
    with open(path, "rb") as f:
        body = f.read()
    return Response(body, mimetype="application/javascript")


@app.route("/manifest.json")
def manifest():
    path = os.path.join(app.static_folder, "manifest.json")
    with open(path, "rb") as f:
        body = f.read()
    return Response(body, mimetype="application/manifest+json")


@app.route("/api/push-edge", methods=["POST"])
def api_push_edge():
    """Envía un push a todos los miembros suscritos.

    Lo llama el cron de actualización del +EV Board cuando aparece un edge
    nuevo. Protegido con header X-Push-Key == PUSH_TRIGGER_KEY.
    JSON: {"title": "...", "body": "...", "url": "/ev-board"}.
    """
    if not PUSH_TRIGGER_KEY or not secrets.compare_digest(
        request.headers.get("X-Push-Key", ""), PUSH_TRIGGER_KEY
    ):
        return jsonify({"error": "forbidden"}), 403
    if not HAVE_WEBPUSH or not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        return jsonify({"error": "push not configured"}), 503
    data = request.get_json(force=True, silent=True) or {}
    title = data.get("title") or "The Sharp Team"
    body = data.get("body") or "New +EV edge on the board."
    url = data.get("url") or "/ev-board"
    payload = json.dumps({"title": title, "body": body, "url": url})
    db = get_db()
    subs = db.execute(
        "SELECT id, endpoint, p256dh, auth FROM push_subscriptions"
    ).fetchall()
    sent, failed = 0, 0
    for s in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": s["endpoint"],
                    "keys": {"p256dh": s["p256dh"], "auth": s["auth"]},
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIM_EMAIL},
            )
            sent += 1
        except WebPushException as ex:
            code = getattr(getattr(ex, "response", None), "status_code", None)
            if code in (404, 410):
                db.execute(
                    "DELETE FROM push_subscriptions WHERE id = ?", (s["id"],)
                )
            failed += 1
        except Exception:
            failed += 1
    db.commit()
    return jsonify({"sent": sent, "failed": failed})


@app.route("/api/untracked")
def api_untracked():
    """Jugadas de hoy que el admin (Alex) aún no trackeó.

    Protegido igual que /api/push-edge: header X-Push-Key == PUSH_TRIGGER_KEY
    (o ?key= como alternativa para GET simples). Lo usa el cron
    recordatorio-track: si Alex olvidó darle Track a alguna jugada, le avisa
    en el chat de notificaciones. (Pedido por Alex 2026-09-26.)
    """
    key = request.headers.get("X-Push-Key", "") or request.args.get("key", "")
    if not PUSH_TRIGGER_KEY or not secrets.compare_digest(key, PUSH_TRIGGER_KEY):
        return jsonify({"error": "forbidden"}), 403
    hoy = datetime.now(TZ).strftime("%Y-%m-%d")
    plays = [p for p in load_plays() if p.get("fecha") == hoy]
    db = get_db()
    admin = db.execute(
        "SELECT id FROM users WHERE is_admin = 1 LIMIT 1"
    ).fetchone()
    tracked = set()
    if admin:
        rows = db.execute(
            "SELECT play_id FROM tracked_plays WHERE user_id = ? AND fecha = ?",
            (admin["id"], hoy),
        ).fetchall()
        tracked = {r["play_id"] for r in rows}
    faltan = [
        {"id": p.get("id"), "nivel": p.get("nivel"), "pick": p.get("pick")}
        for p in plays
        if p.get("id") not in tracked
    ]
    return jsonify({"fecha": hoy, "total": len(plays), "faltan": faltan})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
