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
import random
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

def _asset_version():
    """Short git hash so every deploy busts the static-asset cache."""
    try:
        import subprocess
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M")

ASSET_V = _asset_version()

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

# Tracker "Free Plays" (solo Alex): stake fijo por jugada para validar el
# sistema en 200 jugadas antes del lanzamiento público. Se cambia con la
# env var FREE_STAKE en Render (default $50).
FREE_STAKE = float(os.environ.get("FREE_STAKE", "50"))
# Meta de jugadas para la validación (default 200).
FREE_PLAYS_GOAL = int(os.environ.get("FREE_PLAYS_GOAL", "200"))

# Stripe API (automatización total): con STRIPE_SECRET_KEY + STRIPE_WEBHOOK_SECRET
# como env vars en Render, los webhooks de Stripe activan/desactivan Elite solos:
#  - checkout.session.completed  -> activa Elite al miembro (match por email)
#  - customer.subscription.deleted -> desactiva Elite
#  - invoice.payment_failed       -> queda en auditoría para que Alex lo vea
# Sin estas env vars, el endpoint /stripe/webhook responde 400 y nada cambia
# (el flujo manual de Alex sigue funcionando igual).
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")


def stripe_configurado():
    return bool(STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET)

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

# Secreto del webhook de Telegram (ruta /telegram/webhook/<secreto>).
# Sin él, Render no acepta updates del bot. Se configura en Render como
# env var TELEGRAM_WEBHOOK_SECRET.
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "thesharpteam_bot")

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
    stake_mode TEXT NOT NULL DEFAULT 'units', -- modo de sizing: units | kelly
    platinum_unlocked INTEGER NOT NULL DEFAULT 0,  -- 1 = Elite desbloqueada
    is_admin INTEGER NOT NULL DEFAULT 0,           -- 1 = administrador
    cancel_requested_at TEXT,                      -- fecha ISO en que pidió cancelar (NULL = activa)
    stripe_customer_id TEXT,                     -- cliente de Stripe (NULL = sin vincular)
    stripe_subscription_id TEXT,                  -- suscripción activa de Stripe
    foto TEXT,                                     -- foto de perfil (data URI JPEG, NULL = inicial)
    telegram_user_id TEXT,                         -- id de Telegram vinculado (NULL = sin vincular)
    telegram_ban_pending INTEGER NOT NULL DEFAULT 0,   -- 1 = banear de Sharp Club (lo procesa el cron)
    telegram_unban_pending INTEGER NOT NULL DEFAULT 0, -- 1 = desbanear de Sharp Club (lo procesa el cron)
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
-- Tracker paralelo "Free Plays" (solo Alex): las jugadas recomendadas se
-- registran automáticamente con su stake configurado para validar el
-- sistema en 200 jugadas antes del lanzamiento público. Tabla aislada:
-- se puede borrar sin afectar tracked_plays ni el récord oficial.
CREATE TABLE IF NOT EXISTS free_plays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    play_id TEXT NOT NULL UNIQUE,
    fecha TEXT NOT NULL,
    nivel TEXT NOT NULL,
    pick TEXT NOT NULL,
    cuota INTEGER NOT NULL,
    stake_unidades REAL NOT NULL DEFAULT 1.0,
    stake_monto REAL NOT NULL,
    edge REAL,
    resultado TEXT,          -- NULL = pendiente, 'W' = ganada, 'L' = perdida
    created_at TEXT NOT NULL
);
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
CREATE TABLE IF NOT EXISTS stripe_events (   -- auditoría de webhooks de Stripe (deduplicada por event_id)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    tipo TEXT NOT NULL,
    email TEXT,
    detalle TEXT,
    created_at TEXT NOT NULL
);
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
    stake_mode TEXT NOT NULL DEFAULT 'units', -- modo de sizing: units | kelly
    platinum_unlocked INTEGER NOT NULL DEFAULT 0,  -- 1 = Elite desbloqueada
    is_admin INTEGER NOT NULL DEFAULT 0,           -- 1 = administrador
    cancel_requested_at TEXT,                      -- fecha ISO en que pidió cancelar (NULL = activa)
    stripe_customer_id TEXT,                     -- cliente de Stripe (NULL = sin vincular)
    stripe_subscription_id TEXT,                  -- suscripción activa de Stripe
    foto TEXT,                                     -- foto de perfil (data URI JPEG, NULL = inicial)
    telegram_user_id TEXT,                         -- id de Telegram vinculado (NULL = sin vincular)
    telegram_ban_pending INTEGER NOT NULL DEFAULT 0,   -- 1 = banear de Sharp Club (lo procesa el cron)
    telegram_unban_pending INTEGER NOT NULL DEFAULT 0, -- 1 = desbanear de Sharp Club (lo procesa el cron)
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
-- Tracker paralelo "Free Plays" (solo Alex): las jugadas recomendadas se
-- registran automáticamente con su stake configurado para validar el
-- sistema en 200 jugadas antes del lanzamiento público. Tabla aislada:
-- se puede borrar sin afectar tracked_plays ni el récord oficial.
CREATE TABLE IF NOT EXISTS free_plays (
    id SERIAL PRIMARY KEY,
    play_id TEXT NOT NULL UNIQUE,
    fecha TEXT NOT NULL,
    nivel TEXT NOT NULL,
    pick TEXT NOT NULL,
    cuota INTEGER NOT NULL,
    stake_unidades DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    stake_monto DOUBLE PRECISION NOT NULL,
    edge DOUBLE PRECISION,
    resultado TEXT,          -- NULL = pendiente, 'W' = ganada, 'L' = perdida
    created_at TEXT NOT NULL
);
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
CREATE TABLE IF NOT EXISTS stripe_events (   -- auditoría de webhooks de Stripe (deduplicada por event_id)
    id SERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    tipo TEXT NOT NULL,
    email TEXT,
    detalle TEXT,
    created_at TEXT NOT NULL
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


def _resend_send(to: str, subject: str, text_body: str, html_body=None) -> tuple:
    """Envía un email vía Resend HTTP API (HTTPS/443, funciona en Render free
    donde el SMTP está bloqueado). Devuelve (ok, detalle)."""
    import urllib.request
    key = (os.environ.get("RESEND_API_KEY") or "").strip()
    if not key:
        return False, "falta RESEND_API_KEY"
    from_addr = (os.environ.get("RESEND_FROM")
                 or "The Sharp Team <onboarding@resend.dev>").strip()
    reply_to = (os.environ.get("EMAIL_USER") or "thesharpteam8@gmail.com").strip()
    payload = {"from": from_addr, "to": [to], "subject": subject,
               "reply_to": reply_to, "text": text_body}
    if html_body:
        payload["html"] = html_body
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read(2000).decode("utf-8", "replace")
            ok = 200 <= r.status < 300
            return ok, f"resend HTTP {r.status}: {body[:200]}"
    except Exception as e:
        return False, f"resend falló: {type(e).__name__}: {e}"


def _smtp_send(to: str, subject: str, text_body: str, html_body=None,
               from_header=None) -> tuple:
    """Envío clásico por Gmail SMTP (fallback para desarrollo local; en
    Render free está bloqueado). Devuelve (ok, detalle)."""
    import smtplib
    from email.message import EmailMessage
    user = (os.environ.get("EMAIL_USER") or "").strip()
    pwd = os.environ.get("EMAIL_PASS") or ""
    if not (user and pwd):
        return False, "faltan EMAIL_USER/EMAIL_PASS"
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_header or f"The Sharp Team <{user}>"
        msg["To"] = to
        msg.set_content(text_body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        return True, f"smtp enviado a {to}"
    except Exception as e:
        return False, f"smtp falló: {type(e).__name__}: {e}"


def _dispatch_email(to: str, subject: str, text_body: str, html_body=None,
                    from_header=None) -> tuple:
    """Despacha un email: Resend si hay API key (producción en Render),
    si no SMTP Gmail (desarrollo local). Devuelve (ok, detalle)."""
    if (os.environ.get("RESEND_API_KEY") or "").strip():
        return _resend_send(to, subject, text_body, html_body)
    return _smtp_send(to, subject, text_body, html_body, from_header)


def send_email_sync(to: str, subject: str, text_body: str,
                    html_body=None) -> tuple:
    """Versión síncrona para pruebas/admin: devuelve (ok, detalle) real."""
    return _dispatch_email(to, subject, text_body, html_body)


def notify_new_member(nombre: str, email: str):
    """Avisa a Alex por email cuando alguien se registra.

    Resend (RESEND_API_KEY) en producción; SMTP Gmail (EMAIL_USER /
    EMAIL_PASS) como fallback local. Usa ADMIN_EMAIL como destino.
    Si las variables no están configuradas o el envío falla, no hace
    nada: el registro sigue funcionando sin errores para el usuario.
    Se ejecuta en un hilo aparte para no retrasar la respuesta.
    """
    dest = admin_email()
    resend_ok = bool((os.environ.get("RESEND_API_KEY") or "").strip())
    user = (os.environ.get("EMAIL_USER") or "").strip()
    pwd = os.environ.get("EMAIL_PASS") or ""
    if not dest or not (resend_ok or (user and pwd)):
        return

    subject = f"The Sharp Team: new member — {nombre}"
    text = (f"A new member registered:\n\n"
            f"Name: {nombre}\n"
            f"Email: {email}\n"
            f"Date: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M %Z')}\n")

    def _send():
        try:
            _dispatch_email(dest, subject, text, from_header=user or None)
        except Exception:
            pass  # silencioso: nunca rompe el registro

    import threading
    threading.Thread(target=_send, daemon=True).start()


# ------------------------------------------------------------- Plays ----
WELCOME_SUBJECT = "Welcome to The Sharp Team \U0001F988"

WELCOME_TEXT = """Hi __NOMBRE__,

Welcome to The Sharp Team! Your dashboard is ready.

EVERYTHING YOUR PLAN UNLOCKS
1. THE DAILY CARD - Every morning at 11:00 AM ET: the day's official plays (Free Plays plus the one VIP), each with its original bet ticket. Minimum 3-point edge. No filler, ever.
2. YOUR PERSONAL TRACKER - One-tap tracking: mark each play, log won or lost. Verified history, real record, net units, ROI and streak. No spreadsheets.
3. LIVE +EV BOARD - Value edges updated around the clock, measured against Pinnacle's no-vig fair price, with sharp-confirmed badges.
4. PUSH ALERTS - Alerts the moment the card drops and when new edges appear. Turn "Alerts on" in your dashboard.
5. TELEGRAM CHANNELS - Free Plays in the free channel, VIP plays in the private VIP channel, each with the original ticket.
6. DAILY MASTERCLASS - One sharp betting lesson every morning: discipline, bankroll management, reading lines and edges.

"Every play is posted before game time - then locked and graded against the final score."

Enter your dashboard:
https://the-line-breaker-members.onrender.com/home

Your first week is $1 - then $23/week. Cancel anytime. No questions asked.

- The Sharp Team

Bet responsibly - 21+ - Gambling problem? Call 1-800-GAMBLER (1-800-426-2537): free and confidential help, 24/7.
"""

WELCOME_HTML = """<div style="max-width:460px;margin:0 auto;background:#f2efe7;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;">
<div style="background:#0a0a0f;text-align:center;padding:28px 30px 26px;border-top:3px solid #d4af37;border-radius:18px 18px 0 0;">
<div style="font-size:13px;letter-spacing:4px;color:#d4af37;margin-bottom:16px;">MEMBERSHIP&nbsp;CONFIRMED</div>
<img src="https://files.catbox.moe/74qot5.png" alt="The Sharp Team" style="width:110px;height:auto;display:block;margin:0 auto 10px;border-radius:50%;border:1px solid #3a2f14;">
<div style="color:#d4af37;font-size:16px;font-weight:700;letter-spacing:5px;margin-bottom:14px;">THE SHARP TEAM</div>
<div style="width:130px;height:1px;margin:0 auto 18px;background:linear-gradient(90deg,transparent,#d4af37,transparent);"></div>
<h1 style="font-family:Georgia,'Times New Roman',serif;font-size:42px;font-weight:400;color:#ffffff;margin:0 0 10px;line-height:1.25;">Welcome to<br><em style="color:#ffd257;">the team.</em></h1>
<p style="color:#a9a9b8;font-size:17px;line-height:1.6;margin:0;">Hi __NOMBRE__ &mdash; your dashboard is ready.<br>Here is everything waiting for you inside:</p>
</div>
<div style="padding:28px 24px 18px;text-align:center;">
<div style="font-family:Georgia,'Times New Roman',serif;font-size:32px;color:#14141d;line-height:1.3;">Everything Your<br><em style="color:#8a6a1c;">Plan Unlocks</em></div>
<div style="width:64px;height:2px;background:#d4af37;margin:14px auto 0;"></div>
</div>
<div style="background:#ffffff;border:1px solid #e8e0cb;border-radius:16px;margin:0 24px 14px;box-shadow:0 2px 10px rgba(20,20,29,0.05);overflow:hidden;">
<div style="height:3px;background:linear-gradient(90deg,#b8912b,#f5e3a8,#b8912b);"></div>
<table width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="64" style="vertical-align:top;padding:20px 0 20px 22px;"><div style="font-family:Georgia,'Times New Roman',serif;font-size:36px;color:#d4af37;line-height:1;">01</div><div style="width:28px;height:2px;background:#d4af37;margin-top:8px;"></div></td>
<td style="vertical-align:top;padding:20px 22px 20px 6px;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"><div style="font-size:16px;font-weight:800;color:#14141d;letter-spacing:1.5px;margin-bottom:6px;">THE DAILY CARD</div><div style="font-size:15px;color:#5a5a68;line-height:1.65;">Every morning at 11:00 AM ET: the day's official plays &mdash; Free Plays plus the one &starf; VIP &mdash; each with its <em>original bet ticket</em>. Minimum 3-point edge. No filler, ever.</div></td>
</tr></table></div><div style="background:#ffffff;border:1px solid #e8e0cb;border-radius:16px;margin:0 24px 14px;box-shadow:0 2px 10px rgba(20,20,29,0.05);overflow:hidden;">
<div style="height:3px;background:linear-gradient(90deg,#b8912b,#f5e3a8,#b8912b);"></div>
<table width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="64" style="vertical-align:top;padding:20px 0 20px 22px;"><div style="font-family:Georgia,'Times New Roman',serif;font-size:36px;color:#d4af37;line-height:1;">02</div><div style="width:28px;height:2px;background:#d4af37;margin-top:8px;"></div></td>
<td style="vertical-align:top;padding:20px 22px 20px 6px;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"><div style="font-size:16px;font-weight:800;color:#14141d;letter-spacing:1.5px;margin-bottom:6px;">YOUR PERSONAL TRACKER</div><div style="font-size:15px;color:#5a5a68;line-height:1.65;">One-tap tracking: mark each play, log won or lost. Verified history, real record, net units, ROI and streak &mdash; no spreadsheets.</div></td>
</tr></table></div><div style="background:#ffffff;border:1px solid #e8e0cb;border-radius:16px;margin:0 24px 14px;box-shadow:0 2px 10px rgba(20,20,29,0.05);overflow:hidden;">
<div style="height:3px;background:linear-gradient(90deg,#b8912b,#f5e3a8,#b8912b);"></div>
<table width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="64" style="vertical-align:top;padding:20px 0 20px 22px;"><div style="font-family:Georgia,'Times New Roman',serif;font-size:36px;color:#d4af37;line-height:1;">03</div><div style="width:28px;height:2px;background:#d4af37;margin-top:8px;"></div></td>
<td style="vertical-align:top;padding:20px 22px 20px 6px;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"><div style="font-size:16px;font-weight:800;color:#14141d;letter-spacing:1.5px;margin-bottom:6px;">LIVE +EV BOARD</div><div style="font-size:15px;color:#5a5a68;line-height:1.65;">Value edges updated around the clock, measured against Pinnacle's no-vig fair price, with sharp-confirmed badges.</div></td>
</tr></table></div><div style="background:#ffffff;border:1px solid #e8e0cb;border-radius:16px;margin:0 24px 14px;box-shadow:0 2px 10px rgba(20,20,29,0.05);overflow:hidden;">
<div style="height:3px;background:linear-gradient(90deg,#b8912b,#f5e3a8,#b8912b);"></div>
<table width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="64" style="vertical-align:top;padding:20px 0 20px 22px;"><div style="font-family:Georgia,'Times New Roman',serif;font-size:36px;color:#d4af37;line-height:1;">04</div><div style="width:28px;height:2px;background:#d4af37;margin-top:8px;"></div></td>
<td style="vertical-align:top;padding:20px 22px 20px 6px;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"><div style="font-size:16px;font-weight:800;color:#14141d;letter-spacing:1.5px;margin-bottom:6px;">PUSH ALERTS</div><div style="font-size:15px;color:#5a5a68;line-height:1.65;">Alerts the moment the card drops and when new edges appear. Turn &ldquo;Alerts on&rdquo; in your dashboard.</div></td>
</tr></table></div><div style="background:#ffffff;border:1px solid #e8e0cb;border-radius:16px;margin:0 24px 14px;box-shadow:0 2px 10px rgba(20,20,29,0.05);overflow:hidden;">
<div style="height:3px;background:linear-gradient(90deg,#b8912b,#f5e3a8,#b8912b);"></div>
<table width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="64" style="vertical-align:top;padding:20px 0 20px 22px;"><div style="font-family:Georgia,'Times New Roman',serif;font-size:36px;color:#d4af37;line-height:1;">05</div><div style="width:28px;height:2px;background:#d4af37;margin-top:8px;"></div></td>
<td style="vertical-align:top;padding:20px 22px 20px 6px;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"><div style="font-size:16px;font-weight:800;color:#14141d;letter-spacing:1.5px;margin-bottom:6px;">TELEGRAM CHANNELS</div><div style="font-size:15px;color:#5a5a68;line-height:1.65;">Free Plays in the free channel, VIP plays in the private VIP channel &mdash; each with the original ticket.</div></td>
</tr></table></div>
<div style="background:#ffffff;border:1px solid #e8e0cb;border-radius:16px;margin:0 24px 14px;box-shadow:0 2px 10px rgba(20,20,29,0.05);overflow:hidden;">
<div style="height:3px;background:linear-gradient(90deg,#b8912b,#f5e3a8,#b8912b);"></div>
<table width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="64" style="vertical-align:top;padding:20px 0 20px 22px;"><div style="font-family:Georgia,'Times New Roman',serif;font-size:36px;color:#d4af37;line-height:1;">06</div><div style="width:28px;height:2px;background:#d4af37;margin-top:8px;"></div></td>
<td style="vertical-align:top;padding:20px 22px 20px 6px;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;"><div style="font-size:16px;font-weight:800;color:#14141d;letter-spacing:1.5px;margin-bottom:6px;">DAILY MASTERCLASS</div><div style="font-size:15px;color:#5a5a68;line-height:1.65;">One sharp betting lesson every morning &mdash; discipline, bankroll management, reading lines and edges. The 30-day course that turns bettors into investors.</div></td>
</tr></table></div>
<div style="margin:12px 24px 0;padding:18px 20px;border-left:3px solid #d4af37;background:#0a0a0f;border-radius:0 12px 12px 0;">
<p style="font-family:Georgia,'Times New Roman',serif;font-size:18px;font-style:italic;line-height:1.7;color:#e6ddc4;margin:0;">&ldquo;Every play is posted before game time &mdash; then locked and graded against the final score.&rdquo;</p>
</div>
<div style="text-align:center;padding:26px 30px 8px;">
<a href="https://the-line-breaker-members.onrender.com/home" style="display:inline-block;background:linear-gradient(180deg,#ffd257,#c9971f);color:#0a0a0f;font-size:18px;font-weight:800;letter-spacing:1px;padding:15px 40px;border-radius:30px;text-decoration:none;">ENTER THE DASHBOARD</a>
<p style="color:#5a5a68;font-size:16px;line-height:1.7;margin:16px 0 4px;">Your first week is <strong style="color:#8a6a1c;">$1</strong> &mdash; then $23/week.<br>Cancel anytime. No questions asked.</p>
</div>
<div style="margin-top:22px;background:#0a0a0f;text-align:center;padding:24px 30px;border-radius:0 0 18px 18px;border-top:3px solid #d4af37;">
<img src="https://files.catbox.moe/74qot5.png" alt="The Sharp Team" style="width:72px;height:auto;display:block;margin:0 auto 10px;border-radius:50%;">
<div style="color:#d4af37;font-size:14px;font-weight:700;letter-spacing:3px;margin-bottom:10px;">THE SHARP TEAM</div>
<div style="color:#77778a;font-size:14px;line-height:1.8;">250 Park Avenue, Suite 1800, New York, NY 10017<br>(983) 819-4589<br><br>Juega responsablemente &middot; 21+ &middot; 1-800-GAMBLER<br><span style="font-size:13px;">Informational purposes only. Betting involves risk &mdash; never wager more than you can afford to lose.</span></div>
</div>
</div>"""


def send_welcome_email(nombre: str, email: str):
    """Email de bienvenida al nuevo miembro (HTML profesional + texto plano).

    Resend (RESEND_API_KEY) en producción; SMTP Gmail como fallback local.
    Si no está configurado o el envío falla, no hace nada: el registro sigue
    funcionando y el miembro ve la página de bienvenida en pantalla.
    Se ejecuta en un hilo aparte para no retrasar la respuesta.
    """
    resend_ok = bool((os.environ.get("RESEND_API_KEY") or "").strip())
    user = (os.environ.get("EMAIL_USER") or "").strip()
    pwd = os.environ.get("EMAIL_PASS") or ""
    if not (resend_ok or (user and pwd)):
        return

    subject = WELCOME_SUBJECT
    text = WELCOME_TEXT.replace("__NOMBRE__", nombre)
    html = WELCOME_HTML.replace("__NOMBRE__", nombre)

    def _send():
        try:
            _dispatch_email(email, subject, text, html)
        except Exception:
            pass  # silencioso: nunca rompe el registro

    import threading
    threading.Thread(target=_send, daemon=True).start()


# --------------------------------- Dunning (pago fallido) ----
DUNNING_SUBJECT = "Today's card is set. Your card isn't."

DUNNING_TEXT = """Hi __NOMBRE__,

Today's card is set. Your card isn't.

Your last membership payment did not go through, so your VIP plays are paused.

Nothing is canceled and nothing was missed on your end - cards just fail sometimes. Today's card is posted and waiting. Fix the card and everything switches back on instantly, usually within a couple of minutes.

Fix your card in one tap (takes about 30 seconds):
__PAY_URL__

Your Free Plays stay on while the card is sorted - that is part of every membership. Your VIP plays unlock the moment the payment lands. If anything looks off, just reply to this email and a real person will sort it.

- The Sharp Team

Service notice about your membership billing.

Bet responsibly - 21+ - Gambling problem? Call 1-800-GAMBLER (1-800-426-2537): free and confidential help, 24/7.
"""

DUNNING_HTML = """<div style="background:#f4f4f5;padding:28px 12px;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<div style="max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #e7e7ea;border-radius:12px;overflow:hidden;">
<!-- BLACK BANNER -->
<div style="background:#0a0a0a;padding:14px 24px 6px;text-align:center;">
<div style="font-size:11px;letter-spacing:3px;color:#d4af37;font-weight:700;">EVERY PLAY TRACKED &middot; WIN OR LOSS, ON THE RECORD</div>
</div>
<div style="background:#0a0a0a;text-align:center;padding:10px 24px 20px;">
<img src="https://files.catbox.moe/74qot5.png" alt="The Sharp Team" width="64" style="width:64px;height:64px;border-radius:50%;display:block;margin:0 auto 8px;">
<div style="font-size:13px;letter-spacing:5px;color:#ffffff;font-weight:700;">THE SHARP TEAM</div>
</div>
<!-- BODY -->
<div style="padding:22px 28px 26px;">
<div style="font-size:12px;letter-spacing:3px;color:#b8860b;font-weight:700;margin-bottom:8px;">BILLING NOTICE</div>
<div style="font-size:26px;font-weight:800;color:#111;line-height:1.25;margin:0 0 10px;text-align:center;">Today&#39;s card <span style="color:#b8860b;">is set.</span><br>Your card isn&#39;t.</div>
<p style="font-size:16px;color:#555;line-height:1.6;margin:0 0 12px;">Hi __NOMBRE__,</p>
<div style="border:2px solid #d4af37;border-radius:10px;background:#fffdf5;padding:16px 20px;margin:0 0 16px;">
<p style="font-size:17px;color:#7a5c00;font-weight:800;line-height:1.6;margin:0 0 10px;">Your last membership payment did not go through, so your VIP plays are paused.</p>
<p style="font-size:15px;color:#6b5a2e;line-height:1.7;margin:0;">Nothing is canceled and nothing was missed on your end &mdash; cards just fail sometimes. Fix your card and everything switches back on instantly, usually within a couple of minutes.</p>
</div>
<div style="text-align:center;margin:0 0 18px;">
<a href="__PAY_URL__" style="display:inline-block;background:#d4af37;color:#111;font-size:17px;font-weight:800;padding:15px 44px;border-radius:8px;text-decoration:none;">Fix my card &rarr;</a>
</div>
<div style="font-size:14px;color:#777;line-height:2;margin:0 0 6px;"><b style="color:#111;">What happens next:</b></div>
<div style="font-size:14px;color:#777;line-height:2;margin:0;">1 &mdash; Update your card on the secure Stripe page.<br>2 &mdash; We get notified automatically.<br>3 &mdash; Your VIP plays turn back on. <b style="color:#111;">Free Plays never stopped.</b></div>
</div>
<!-- FOOTER -->
<div style="background:#fafafa;border-top:1px solid #eee;padding:16px 28px;text-align:center;">
<img src="https://files.catbox.moe/74qot5.png" alt="The Sharp Team" width="110" style="width:110px;height:auto;display:block;margin:0 auto 10px;">
<div style="font-size:12px;letter-spacing:2px;color:#111;font-weight:700;">THE SHARP TEAM</div>
<div style="font-size:12px;color:#888;margin-top:8px;">250 Park Avenue, Suite 1800, New York, NY 10017<br>(551) 326-3312</div>
<div style="font-size:11px;color:#aaa;margin-top:10px;">Juega responsablemente &middot; 21+ &middot; 1-800-GAMBLER</div>
</div>
</div>
</div>"""


def send_payment_failed_email(nombre: str, email: str, pay_url: str) -> bool:
    """Dunning estilo WGT: al fallar un cobro (invoice.payment_failed).

    Email al miembro con el botón "Fix my card in one tap" que apunta a la
    hosted_invoice_url de Stripe (paga esa factura / actualiza la tarjeta).
    Se envía UNA vez por factura (el webhook deduplica por invoice id, porque
    Stripe reintenta el cobro y manda payment_failed en cada intento).

    Usa Resend (RESEND_API_KEY) en producción; SMTP Gmail como fallback
    local. Si no está configurado o el envío falla, no hace nada y devuelve
    False (el webhook lo audita).
    Se ejecuta en un hilo aparte para no retrasar la respuesta al webhook.
    """
    resend_ok = bool((os.environ.get("RESEND_API_KEY") or "").strip())
    user = (os.environ.get("EMAIL_USER") or "").strip()
    pwd = os.environ.get("EMAIL_PASS") or ""
    if not (email and pay_url and (resend_ok or (user and pwd))):
        return False

    fecha = datetime.now(TZ).strftime("%b %-d, %-I:%M:%S %p %Z")
    html = (DUNNING_HTML
            .replace("__NOMBRE__", nombre or "there")
            .replace("__PAY_URL__", pay_url)
            .replace("__FECHA__", fecha))
    text = (DUNNING_TEXT
            .replace("__NOMBRE__", nombre or "there")
            .replace("__PAY_URL__", pay_url))

    def _send():
        try:
            _dispatch_email(email, DUNNING_SUBJECT, text, html)
        except Exception:
            pass  # silencioso: nunca rompe el webhook

    import threading
    threading.Thread(target=_send, daemon=True).start()
    return True



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


def _uget(user, key, default=None):
    """Lee un campo del usuario sirva dict (Postgres) o sqlite3.Row (dev local)."""
    if not user:
        return default
    try:
        v = user[key]
        return default if v is None else v
    except (KeyError, IndexError, TypeError):
        return default


def stake_kelly(play, bankroll):
    """Quarter Kelly sobre el edge del modelo (modo Kelly $ del miembro).

    f* = p - (1-p)/b, con b = cuota decimal - 1 y p = prob. implícita + edge.
    Se apuesta el cuarto de Kelly: 0.25 * f* * bankroll. Si f* <= 0, $0.
    """
    try:
        edge = float(play.get("edge") or 0) / 100.0
        cuota = str(play.get("cuota") or "").strip().replace("−", "-")
        if cuota.startswith("+"):
            v = float(cuota[1:])
            b = v / 100.0
            implied = 100.0 / (v + 100.0)
        elif cuota.startswith("-"):
            v = float(cuota[1:])
            if v <= 0:
                return 0.0
            b = 100.0 / v
            implied = v / (v + 100.0)
        else:
            return 0.0
        p = implied + edge
        if not (0.0 < p < 1.0):
            return 0.0
        f = p - (1.0 - p) / b
        if f <= 0:
            return 0.0
        return round(0.25 * f * float(bankroll), 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def stake_personalizado(play, user):
    """Monto a mostrar/trackear.

    Pedido por Alex 2026-09-26 (como la app de WGT): cada miembro ve en su
    dashboard su monto personal = 1% de SU bankroll x las unidades de la
    jugada (1u -> 1%, 0.75u -> 0.75%, 0.5u -> 0.5%). Si no tiene bankroll configurado,
    se usa el stake oficial del programa (stake_monto de plays.json).

    Montos fijos por nivel (pedido por Alex 2026-09-26): si el miembro tiene
    stake_fijo_elite / stake_fijo_gold configurado, ese monto fijo reemplaza
    la fórmula para ese nivel. Solo afecta a su dashboard, no al público.

    Modo Kelly $ (pedido por Alex 2026-09-28): si el miembro eligió el modo
    kelly, el monto es el cuarto de Kelly sobre el edge del modelo
    (stake_kelly). Los montos fijos siguen teniendo prioridad.
    """
    nivel = (play.get("nivel") or "").upper()
    if user:
        try:
            if nivel == "ELITE" and _uget(user, "stake_fijo_elite"):
                return round(float(_uget(user, "stake_fijo_elite")), 2)
            if nivel == "GOLD" and _uget(user, "stake_fijo_gold"):
                return round(float(_uget(user, "stake_fijo_gold")), 2)
        except (TypeError, ValueError):
            pass
    try:
        br = float(_uget(user, "bankroll") or 0)
    except (TypeError, ValueError):
        br = 0
    if br > 0 and (_uget(user, "stake_mode") or "units").lower() == "kelly":
        return stake_kelly(play, br)
    try:
        units = float(play.get("stake_unidades") or 0)
    except (TypeError, ValueError):
        units = 0
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
    niveles = {"GOLD": {"ganadas": 0, "perdidas": 0, "profit": 0.0},
               "ELITE": {"ganadas": 0, "perdidas": 0, "profit": 0.0}}
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
            _nv = (j.get("nivel") or "").upper()
            if _nv in niveles:
                _s = niveles[_nv]
                if j["resultado"] == "WON":
                    _s["ganadas"] += 1
                else:
                    _s["perdidas"] += 1
                _s["profit"] += float(j.get("profit") or 0.0)
    settled = ganadas + perdidas
    for _s in niveles.values():
        _n = int(round(_s["profit"]))
        _s["net_display"] = ("+$" if _n > 0 else ("-$" if _n < 0 else "$")) + f"{abs(_n):,}"
        _s["net_cls"] = "pos" if _n > 0 else ("neg" if _n < 0 else "")
        _s["record"] = f"{_s['ganadas']}–{_s['perdidas']}"
        _st = _s["ganadas"] + _s["perdidas"]
        _s["win_rate"] = round(_s["ganadas"] / _st * 100, 1) if _st else 0.0
    _pn = int(round(profit))
    return {
        "profit_all_time": round(profit, 2),
        "profit_display": ("+$" if _pn > 0 else ("-$" if _pn < 0 else "$")) + f"{abs(_pn):,}",
        "profit_cls": "pos" if _pn > 0 else ("neg" if _pn < 0 else ""),
        "ganadas": ganadas,
        "perdidas": perdidas,
        "win_rate": round(ganadas / settled * 100, 1) if settled else 0.0,
        "total_arriesgado": round(risked, 2),
        "roi": round(profit / risked * 100, 1) if risked else 0.0,
        "niveles": niveles,
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


# Public free tools: Sharp Line Moves — el JSON vive en la rama `data-board`
# (igual que el +EV Board: actualizar datos NO redespliega Render).
# La página /tools/line-moves es pública (sin login) y lo lee en vivo.
LINE_MOVES_URL = (
    "https://raw.githubusercontent.com/penamanuel-art/tlb-members"
    "/data-board/data/line_moves.json"
)
_line_moves_cache = {"at": 0.0, "data": {}}


def load_line_moves():
    import time
    now = time.time()
    if now - _line_moves_cache["at"] < 300 and _line_moves_cache["data"]:
        return _line_moves_cache["data"]
    data = {}
    try:
        req = urllib.request.Request(LINE_MOVES_URL, headers={"User-Agent": "tlb-members"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except Exception:
        data = _line_moves_cache["data"] or {}
    _line_moves_cache["at"] = now
    _line_moves_cache["data"] = data
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


def profit_curve_svg(values, w=300, h=84, pad=6):
    """Paths SVG (línea + área) del profit acumulado del miembro."""
    if not values:
        return None
    n = len(values)
    xs = [pad + i * (w - 2 * pad) / max(n - 1, 1) for i in range(n)]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    ys = [pad + (1 - (v - lo) / span) * (h - 2 * pad) for v in values]
    line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area = f"{line} L{xs[-1]:.1f},{h} L{xs[0]:.1f},{h} Z"
    return {"line": line, "area": area, "w": w, "h": h,
            "up": values[-1] >= 0}


def stats_por_nivel(tracked):
    """Ganadas/perdidas/profit por nivel (GOLD/ELITE) para el bloque estilo WGT."""
    out = {}
    for t in tracked:
        lvl = (t.get("nivel") or "").upper()
        if lvl not in ("GOLD", "ELITE"):
            continue
        s = out.setdefault(lvl, {"wins": 0, "losses": 0, "net": 0.0})
        if t.get("resultado") == "W":
            s["wins"] += 1
            s["net"] += play_profit_dollars(t)
        elif t.get("resultado") == "L":
            s["losses"] += 1
            s["net"] += play_profit_dollars(t)
    for s in out.values():
        _n = int(round(s["net"]))
        s["net_display"] = ("+$" if _n > 0 else ("-$" if _n < 0 else "$")) + f"{abs(_n):,}"
        s["net_cls"] = "pos" if _n > 0 else ("neg" if _n < 0 else "")
        s["record"] = f"{s['wins']}–{s['losses']}"
    for _lvl in ("GOLD", "ELITE"):
        out.setdefault(_lvl, {"wins": 0, "losses": 0, "net": 0.0,
                              "net_display": "$0", "net_cls": "", "record": "0–0"})
    return out


def fmt_big_dollars(amount):
    """+$1,234 estilo WGT (dólares enteros con separador de miles)."""
    n = int(round(amount or 0))
    return ("+$" if n > 0 else ("-$" if n < 0 else "$")) + f"{abs(n):,}"



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


def _norm_free_pick(pick):
    """Normaliza el nombre de la jugada para deduplicar: quita la cuota entre
    paréntesis y el rival. Así 'White Sox ML (+134)', 'White Sox ML @ Astros'
    y 'White Sox ML' generan el mismo ID (2026-10-01, pedido por Alex: las
    repetidas se eliminan, queda una sola fila por juego)."""
    p = (pick or "").strip()
    p = re.sub(r"\s*\([+-]?\d+\)\s*", " ", p)  # quita (+134), (-140)
    p = re.sub(r"\s+(@|vs\.?|at)\s+[A-Za-z0-9 .'\-]+$", "", p, flags=re.IGNORECASE)  # quita @ Astros / vs Cubs
    return re.sub(r"\s+", " ", p).strip()


def _free_play_id(fecha, pick):
    """ID determinista para jugadas del archivo histórico (sin id propio)."""
    slug = re.sub(r"[^a-z0-9]+", "-", _norm_free_pick(pick).lower()).strip("-")
    return f"free-{fecha}-{slug}"


def _dedup_free_plays(db):
    """Migración única (2026-10-01, pedido por Alex): elimina filas duplicadas
    del tracker Free Plays — la misma jugada registrada con dos nombres
    ('White Sox ML (+134)' vs 'White Sox ML @ Astros'). Conserva la fila
    liquidada (con resultado) o, si ninguna lo está, la más antigua.
    Idempotente: sin duplicados no hace nada."""
    rows = db.execute(
        "SELECT id, fecha, pick, resultado FROM free_plays"
    ).fetchall()

    def _v(r, key, idx):
        try:
            return r[key]
        except (KeyError, IndexError, TypeError):
            return r[idx]

    groups = {}
    for r in rows:
        rid = _v(r, "id", 0)
        fecha = _v(r, "fecha", 1) or ""
        pick = _v(r, "pick", 2) or ""
        res = _v(r, "resultado", 3)
        slug = re.sub(r"[^a-z0-9]+", "-", _norm_free_pick(pick).lower()).strip("-")
        groups.setdefault((fecha, slug), []).append((rid, res))
    doomed = []
    for items in groups.values():
        if len(items) < 2:
            continue
        # primero las liquidadas, luego la de id menor (más antigua)
        items.sort(key=lambda x: (0 if x[1] not in (None, "") else 1, x[0]))
        doomed.extend(rid for rid, _ in items[1:])
    for rid in doomed:
        db.execute("DELETE FROM free_plays WHERE id = ?", (rid,))
    if doomed:
        db.commit()
    return len(doomed)


def sync_free_plays(db):
    """Registra automáticamente en el tracker "Free Plays" las jugadas de la
    card publicada (data/plays.json) y del histórico (data/archive.json)
    que aún no estén registradas.

    Cada jugada entra con el stake fijo FREE_STAKE. Idempotente: las jugadas
    ya registradas (por play_id) se saltan. Se llama al publicar la card y
    al abrir /free-plays.
    """
    _dedup_free_plays(db)  # 2026-10-01: limpia duplicados existentes (una fila por juego)
    now = datetime.now(timezone.utc).isoformat()
    n = 0

    def _insert(pid, fecha, nivel, pick, cuota, edge):
        nonlocal n
        if not pid:
            return
        exists = db.execute(
            "SELECT 1 FROM free_plays WHERE play_id = ?", (pid,)
        ).fetchone()
        if exists:
            return
        try:
            cuota = int(cuota)
        except (TypeError, ValueError):
            return
        db.execute(
            "INSERT INTO free_plays "
            "(play_id, fecha, nivel, pick, cuota, stake_unidades, stake_monto, edge, resultado, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (pid, fecha or "", nivel or "GOLD", pick or "", cuota,
             1.0, FREE_STAKE, edge, now),
        )
        n += 1

    # 1) Card actual (plays.json)
    for p in load_plays():
        _insert(
            p.get("id"), p.get("fecha"), p.get("nivel"),
            p.get("pick"), p.get("cuota"), p.get("edge"),
        )

    # 2) Histórico (archive.json) — backfill de jugadas ya publicadas
    for d in load_archive():
        fecha = (d.get("fecha") or "").strip()
        for j in d.get("jugadas", []) or []:
            pick = j.get("pick")
            if not pick:
                continue
            _insert(
                _free_play_id(fecha, pick), fecha, j.get("nivel"),
                pick, j.get("cuota"), j.get("edge"),
            )

    if n:
        db.commit()
    return n


def auto_grado_free(db):
    """Liquida las jugadas pendientes del tracker "Free Plays" usando los
    resultados oficiales del programa (data/archive.json).

    Misma lógica que auto_grado_tracked pero sobre la tabla free_plays.
    Solo toca jugadas con resultado pendiente (NULL).
    """
    pendientes = db.execute(
        "SELECT id, play_id, fecha, pick FROM free_plays WHERE resultado IS NULL"
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
                "UPDATE free_plays SET resultado = ? WHERE id = ?",
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

# Marcas de agua rotativas (2026-09-30, pedido por Alex): cada sesión ve una
# distinta — fútbol americano o béisbol — siempre tenue.
WATERMARKS = [
    "img/watermark-football.jpg",
    "img/watermark-baseball.jpg",
]


@app.context_processor
def inject_user():
    wm = session.get("watermark")
    if wm not in WATERMARKS:
        wm = random.choice(WATERMARKS)
        session["watermark"] = wm
    nombre = session.get("nombre", "")
    corto = primer_nombre(nombre)
    cu = current_user()
    try:
        foto = cu["foto"] if cu else None
    except (KeyError, IndexError, TypeError):
        foto = None
    try:
        creado = datetime.fromisoformat(cu["created_at"]) if cu else None
        dias_miembro = max(1, (datetime.now() - creado).days + 1) if creado else 1
    except (ValueError, TypeError, KeyError, IndexError):
        dias_miembro = 1
    etiqueta = "VIP Member" if platinum_unlocked_for(cu) else "Member"
    return {
        "nombre_corto": corto,
        "nombre_completo": nombre,
        "inicial": corto[:1].upper(),
        "foto_perfil": foto,
        "dias_miembro": dias_miembro,
        "etiqueta_miembro": etiqueta,
        "es_admin": bool(session.get("is_admin")),
        "miembro_platinum": platinum_unlocked_for(cu),
        "ticker_days": compute_ticker_days(),
        "asset_v": ASSET_V,
        "watermark_img": session.get("watermark", WATERMARKS[0]),
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
        if "stake_mode" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN stake_mode TEXT NOT NULL DEFAULT 'units'")
        if "platinum_unlocked" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN platinum_unlocked INTEGER NOT NULL DEFAULT 0")
        if "is_admin" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        if "cancel_requested_at" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN cancel_requested_at TEXT")
        if "stripe_customer_id" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
        if "stripe_subscription_id" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT")
        if "foto" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN foto TEXT")
        if "telegram_user_id" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN telegram_user_id TEXT")
        if "telegram_ban_pending" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN telegram_ban_pending INTEGER NOT NULL DEFAULT 0")
        if "telegram_unban_pending" not in cols:
            pending.append("ALTER TABLE users ADD COLUMN telegram_unban_pending INTEGER NOT NULL DEFAULT 0")
        for stmt in pending:
            conn.execute(stmt)
        if pending:
            conn.commit()
        # Columna stake_unidades en free_plays (la tabla se creó sin ella).
        if USE_PG:
            fcols = {r["column_name"] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'free_plays'"
            ).fetchall()}
        else:
            fcols = {r[1] for r in conn.execute("PRAGMA table_info(free_plays)").fetchall()}
        if fcols and "stake_unidades" not in fcols:
            conn.execute(
                f"ALTER TABLE free_plays ADD COLUMN stake_unidades {coltype} NOT NULL DEFAULT 1.0"
            )
            conn.commit()
        # Códigos de vinculación Telegram (un solo uso, los crea /cuenta).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS telegram_link_codes (
                code TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL
            )"""
        )
        # Pool de links de invitación a Sharp Club (un solo uso, los genera
        # el cron de la VM con el token del bot; /cuenta los reparte).
        _id_col = "id SERIAL PRIMARY KEY" if USE_PG else "id INTEGER PRIMARY KEY AUTOINCREMENT"
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS telegram_invite_links (
                {_id_col},
                invite_link TEXT NOT NULL UNIQUE,
                used INTEGER NOT NULL DEFAULT 0,
                used_by_user_id INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL
            )"""
        )
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


@app.route("/ticket/<play_id>")
def ticket_view(play_id):
    """Visor del ticket de una jugada — diseño propio de The Sharp Team
    (2026-09-29, pedido por Alex: 'otro diseño para no copiar el de Wise').
    Lo abre el botón 🎫 View ticket de Telegram.

    Sin pago no hay acceso (2026-09-30, pedido por Alex): si la jugada es
    ELITE, exige sesión y Elite activa; si no, va a la pantalla de
    desbloqueo. Las GOLD siguen públicas.
    """
    play = next((p for p in load_plays() if p.get("id") == play_id), None)
    if play is None:
        try:
            arch = json.load(open(ARCHIVE_PATH, "r", encoding="utf-8"))
            for dia in (arch.get("dias") or []):
                play = next(
                    (j for j in (dia.get("jugadas") or [])
                     if isinstance(j, dict) and j.get("id") == play_id),
                    None,
                )
                if play:
                    break
        except (OSError, json.JSONDecodeError):
            pass
    if play is None:
        return render_template("404.html"), 404
    # Sin pago no hay acceso: la jugada ELITE exige sesión y Elite activa.
    if str(play.get("nivel") or "").upper() in ("ELITE", "PLATINUM"):
        cu = current_user()
        if not platinum_unlocked_for(cu):
            if cu is None:
                return redirect(url_for("login", next=url_for("ticket_view", play_id=play_id)))
            return redirect(url_for("desbloquear_platinum"))
    return render_template("ticket.html", p=play)


def last_elite_win():
    """Última jugada ELITE ganada en el archivo (prueba social de la tarjeta bloqueada)."""
    try:
        with open(ARCHIVE_PATH, encoding="utf-8") as f:
            arch = json.load(f)
    except Exception:
        return None
    for d in sorted(arch.get("dias", []), key=lambda x: x.get("fecha", ""), reverse=True):
        for j in d.get("jugadas", []):
            if j.get("nivel") == "ELITE" and j.get("resultado") == "WON" and j.get("profit"):
                return int(round(j["profit"]))
    return None


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
    tlevels = stats_por_nivel([dict(r) for r in tracked_rows])
    tstats["net_display"] = fmt_big_dollars(tstats["net_dollars"])
    # Curva de profit acumulado para el gráfico del tracker personal.
    _graded = sorted(
        (dict(r) for r in tracked_rows if r["resultado"] in ("W", "L")),
        key=lambda t: (t["fecha"], t["id"]),
    )
    _cum, _vals = 0.0, []
    for _t in _graded:
        _cum += play_profit_dollars(_t)
        _vals.append(round(_cum, 2))
    tcurve = profit_curve_svg(_vals)
    # Resultados recientes: jugadas liquidadas del archivo (sin bloqueadas).
    recientes = recientes_oficiales(6)
    # Vista previa admin (pedido Alex 2026-09-30): ?preview=locked muestra el
    # home como lo ve un miembro sin Elite (teaser bloqueado + pancarta $1).
    preview_locked = is_admin_for(user) and request.args.get("preview") == "locked"
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
        stake_mode=user["stake_mode"] if user else "units",
        platinum_unlocked=(platinum_unlocked_for(user) and not preview_locked),
        res=program_stats(),
        leccion=load_masterclass(),
        archivo_mc=load_masterclass_archivo(),
        tstats=tstats,
        tlevels=tlevels,
        tcurve=tcurve,
        recientes=recientes,
        last_elite_win=last_elite_win(),
        es_admin=is_admin_for(user),
        skip_splash=request.args.get("splash") == "0",
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


@app.route("/cuenta/stake-mode", methods=["POST"])
@login_required
def stake_mode():
    """Cambia el modo de sizing del miembro: units (TST Units) o kelly (Kelly $).

    Pedido por Alex 2026-09-28: tarjeta de bankroll en el Dashboard con los dos
    modos, como la configuración de WGT pero con diseño propio.
    """
    mode = (request.form.get("mode") or "").strip().lower()
    if mode not in ("units", "kelly"):
        flash("Invalid sizing mode.", "error")
        return redirect(url_for("cuenta"))
    db = get_db()
    db.execute("UPDATE users SET stake_mode = ? WHERE id = ?", (mode, session["user_id"]))
    db.commit()
    nxt = (request.form.get("next") or "").strip()
    if nxt == "home":
        return redirect(url_for("home"))
    return redirect(url_for("cuenta"))


@app.route("/cuenta", methods=["GET", "POST"])
@login_required
def cuenta():
    """Mi cuenta: configurar/actualizar el bankroll del miembro (persistencia en servidor)."""
    db = get_db()
    user = current_user()
    uid = session.get("user_id")
    # Vinculación Telegram: código pendiente (un solo uso) y estado.
    db.execute(
        "DELETE FROM telegram_link_codes WHERE created_at < ?",
        ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),),
    )
    link_row = db.execute(
        "SELECT code FROM telegram_link_codes WHERE user_id = ?"
        " ORDER BY created_at DESC LIMIT 1",
        (uid,),
    ).fetchone()
    link_code = link_row["code"] if isinstance(link_row, dict) else (link_row[0] if link_row else None)
    tg_link = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start={link_code}" if link_code else None
    tg_linked = bool(user and user["telegram_user_id"])
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
            return render_template("cuenta.html", bankroll=user["bankroll"] if user else None,
                                   cancel_at=user["cancel_requested_at"] if user else None,
                                   platinum=platinum_unlocked_for(user),
                                   stake_mode=user["stake_mode"] if user else "units",
                                   tg_link=tg_link, tg_linked=tg_linked), 400
        db.execute("UPDATE users SET bankroll = ? WHERE id = ?", (val, session["user_id"]))
        db.commit()
        flash(f"Bankroll saved: ${val:,.2f}.", "ok")
        return redirect(url_for("cuenta"))
    return render_template("cuenta.html", bankroll=user["bankroll"] if user else None,
                           cancel_at=user["cancel_requested_at"] if user else None,
                           platinum=platinum_unlocked_for(user),
                           stake_mode=user["stake_mode"] if user else "units",
                           tg_link=tg_link, tg_linked=tg_linked)


@app.route("/cuenta/telegram-link", methods=["POST"])
@login_required
def cuenta_telegram_link():
    """Genera un código de un solo uso para vincular el Telegram del miembro.

    El miembro abre el link, pulsa START en el bot y la app vincula su
    telegram_user_id. Sirve para sacarlo del canal Sharp Club si cancela.
    """
    db = get_db()
    uid = session.get("user_id")
    db.execute("DELETE FROM telegram_link_codes WHERE user_id = ?", (uid,))
    code = secrets.token_urlsafe(16)
    db.execute(
        "INSERT INTO telegram_link_codes (code, user_id, created_at) VALUES (?, ?, ?)",
        (code, uid, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()
    return redirect(url_for("cuenta"))


@app.route("/cuenta/telegram-unlink", methods=["POST"])
@login_required
def cuenta_telegram_unlink():
    """Desvincula el Telegram del miembro."""
    db = get_db()
    db.execute(
        "UPDATE users SET telegram_user_id = NULL WHERE id = ?",
        (session.get("user_id"),),
    )
    db.commit()
    flash("Telegram disconnected.", "ok")
    return redirect(url_for("cuenta"))


@app.route("/cuenta/cancelar", methods=["GET", "POST"])
@login_required
def cuenta_cancelar():
    """Cancelación de membresía en dos pasos: GET muestra la confirmación, POST la ejecuta."""
    db = get_db()
    user = current_user()
    if request.method == "POST":
        if user and not user["cancel_requested_at"]:
            db.execute("UPDATE users SET cancel_requested_at = ? WHERE id = ?",
                       (now_iso(), session["user_id"]))
            db.commit()
            flash("Your membership has been cancelled. No further charges.", "ok")
        return redirect(url_for("cuenta"))
    if user and user["cancel_requested_at"]:
        return redirect(url_for("cuenta"))
    return render_template("cancelar.html")


@app.route("/cuenta/reactivar", methods=["POST"])
@login_required
def cuenta_reactivar():
    """Deshace una cancelación solicitada por el propio miembro."""
    db = get_db()
    db.execute("UPDATE users SET cancel_requested_at = NULL WHERE id = ?", (session["user_id"],))
    db.commit()
    flash("Welcome back — your membership is active again.", "ok")
    return redirect(url_for("cuenta"))


@app.route("/cuenta/foto", methods=["POST"])
@login_required
def cuenta_foto():
    """Subir o quitar la foto de perfil. Se guarda como data URI JPEG (256px)
    en la columna users.foto: sobrevive a los redeploys sin disco persistente."""
    db = get_db()
    if request.form.get("quitar"):
        db.execute("UPDATE users SET foto = NULL WHERE id = ?", (session["user_id"],))
        db.commit()
        flash("Profile photo removed.", "ok")
        return redirect(url_for("cuenta"))
    f = request.files.get("foto")
    if not f or not f.filename:
        flash("Choose a photo first.", "error")
        return redirect(url_for("cuenta"))
    if request.content_length and request.content_length > 8 * 1024 * 1024:
        flash("Photo too large (max 8 MB).", "error")
        return redirect(url_for("cuenta"))
    try:
        import base64
        import io
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(f.read()))
        img.load()
        img = ImageOps.exif_transpose(img)  # enderezar según EXIF (fotos de iPhone)
        img = img.convert("RGB")
        img.thumbnail((256, 256), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        flash("That file is not a valid image.", "error")
        return redirect(url_for("cuenta"))
    db.execute("UPDATE users SET foto = ? WHERE id = ?", (uri, session["user_id"]))
    db.commit()
    flash("Profile photo updated.", "ok")
    return redirect(url_for("cuenta"))


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
        nombre=session.get("nombre", ""),
        bankroll=user["bankroll"] if user else None,
        stake_mode=user["stake_mode"] if user else "units",
        platinum_unlocked=platinum_unlocked_for(user),
        res=program_stats(),
        recientes=recientes_oficiales(),
        es_admin=is_admin_for(user),
    )


@app.route("/free-plays")
@login_required
def free_plays():
    """Tracker paralelo "Free Plays" — SOLO admin (Alex).

    Las jugadas recomendadas se registran automáticamente con el stake fijo
    FREE_STAKE para validar el sistema en FREE_PLAYS_GOAL jugadas antes del
    lanzamiento público. Tabla aislada: no toca tracked_plays ni el récord.
    """
    user = current_user()
    if not is_admin_for(user):
        return redirect(url_for("home"))
    db = get_db()
    record_checkin(db, session["user_id"])
    sync_free_plays(db)
    auto_grado_free(db)
    plays = [
        dict(t)
        for t in db.execute(
            "SELECT * FROM free_plays ORDER BY fecha DESC, id DESC"
        ).fetchall()
    ]
    stats = compute_stats(plays)
    stats["goal"] = FREE_PLAYS_GOAL
    stats["stake"] = FREE_STAKE
    return render_template(
        "free_plays.html",
        plays=plays,
        stats=stats,
        es_admin=True,
    )


@app.route("/admin/free-plays/clear", methods=["POST"])
@login_required
def admin_free_plays_clear():
    """Borra el tracker "Free Plays" (solo admin). No afecta tracked_plays
    ni el récord oficial del programa."""
    user = current_user()
    if not is_admin_for(user):
        return redirect(url_for("home"))
    db = get_db()
    db.execute("DELETE FROM free_plays")
    db.commit()
    return redirect(url_for("free_plays"))


@app.route("/api/free-plays/add", methods=["POST"])
def api_free_plays_add():
    """Registra jugadas en el tracker "Free Plays". Protegido con header
    X-Push-Key == PUSH_TRIGGER_KEY (lo usa el asistente al entregar la card).

    JSON: {"plays": [{"pick": "...", "cuota": 128, "nivel": "ELITE",
           "fecha": "2026-09-30", "edge": 4.9}, ...]}.
    Cada jugada entra con stake FREE_STAKE, resultado pendiente.
    """
    if not PUSH_TRIGGER_KEY or not secrets.compare_digest(
        request.headers.get("X-Push-Key", ""), PUSH_TRIGGER_KEY
    ):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(force=True, silent=True) or {}
    plays = data.get("plays") or []
    if not isinstance(plays, list):
        return jsonify({"error": "plays must be a list"}), 400
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    added, skipped = 0, 0
    for p in plays:
        pick = (p.get("pick") or "").strip()
        fecha = (p.get("fecha") or "").strip()
        if not pick or not fecha:
            skipped += 1
            continue
        pid = _free_play_id(fecha, pick)
        exists = db.execute(
            "SELECT 1 FROM free_plays WHERE play_id = ?", (pid,)
        ).fetchone()
        if exists:
            skipped += 1
            continue
        try:
            cuota = int(p.get("cuota"))
        except (TypeError, ValueError):
            skipped += 1
            continue
        db.execute(
            "INSERT INTO free_plays "
            "(play_id, fecha, nivel, pick, cuota, stake_unidades, stake_monto, edge, resultado, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (pid, fecha, p.get("nivel") or "GOLD", pick, cuota,
             1.0, FREE_STAKE, p.get("edge"), now),
        )
        added += 1
    if added:
        db.commit()
    return jsonify({"added": added, "skipped": skipped})


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


@app.route("/track/<play_id>", methods=["GET", "POST"])
@login_required
def track(play_id):
    play = next((p for p in load_plays() if p.get("id") == play_id), None)
    if not play:
        flash("Play not found.", "error")
        return redirect(url_for("home"))
    # La Elite bloqueada no se puede trackear: no revela nada.
    _cu = current_user()
    if play.get("nivel") in ("PLATINUM", "ELITE") and not platinum_unlocked_for(_cu):
        flash("The VIP play is locked. Unlock it to track it.", "warn")
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
        if nuevo:
            db.execute(
                "UPDATE users SET platinum_unlocked = 1, telegram_unban_pending = 1, "
                "telegram_ban_pending = 0 WHERE id = ?",
                (user_id,),
            )
        else:
            db.execute(
                "UPDATE users SET platinum_unlocked = 0, telegram_ban_pending = 1, "
                "telegram_unban_pending = 0 WHERE id = ?",
                (user_id,),
            )
        db.commit()
        flash(
            "VIP access activated." if nuevo else "VIP access deactivated.",
            "ok",
        )
    return redirect(url_for("admin_miembros"))


@app.route("/admin/test-simulate-cancel", methods=["POST"])
@login_required
def admin_test_simulate_cancel():
    """TEMPORAL para pruebas de Alex: simula lo que hace el webhook de Stripe
    al cancelar (customer.subscription.deleted). Solo admin."""
    me = current_user()
    if not is_admin_for(me):
        flash("You don't have permission.", "error")
        return redirect(url_for("home"))
    db = get_db()
    email = request.form.get("email", "").strip().lower()
    row = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if not row:
        flash("Member not found.", "error")
    else:
        db.execute(
            "UPDATE users SET platinum_unlocked = 0, telegram_ban_pending = 1, "
            "telegram_unban_pending = 0 WHERE id = ?",
            (row["id"],),
        )
        db.commit()
        flash(f"Simulated cancellation for {email}: VIP off, ban pending.", "ok")
    return redirect(url_for("admin_miembros"))


@app.route("/admin/test-simulate-reactivate", methods=["POST"])
@login_required
def admin_test_simulate_reactivate():
    """TEMPORAL para pruebas de Alex: simula lo que hace el webhook de Stripe
    al reactivar/pagar (invoice.paid). Solo admin."""
    me = current_user()
    if not is_admin_for(me):
        flash("You don't have permission.", "error")
        return redirect(url_for("home"))
    db = get_db()
    email = request.form.get("email", "").strip().lower()
    row = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if not row:
        flash("Member not found.", "error")
    else:
        db.execute(
            "UPDATE users SET platinum_unlocked = 1, telegram_unban_pending = 1, "
            "telegram_ban_pending = 0 WHERE id = ?",
            (row["id"],),
        )
        db.commit()
        flash(f"Simulated reactivation for {email}: VIP on, unban pending.", "ok")
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
    db.execute("DELETE FROM checkins WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM push_subscriptions WHERE member_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash(f"Account {target['email']} deleted.", "ok")
    return redirect(url_for("admin_miembros"))


@app.route("/admin/miembros/email", methods=["POST"])
@login_required
def admin_cambiar_email():
    """Cambia el email de una cuenta de miembro (solo admin). Valida unicidad."""
    me = current_user()
    if not is_admin_for(me):
        flash("You don't have permission.", "error")
        return redirect(url_for("home"))
    user_id = request.form.get("user_id", type=int)
    nuevo = (request.form.get("nuevo_email") or "").strip().lower()
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone() if user_id else None
    if not target:
        flash("Account not found.", "error")
        return redirect(url_for("admin_miembros"))
    if not nuevo or "@" not in nuevo or "." not in nuevo.split("@")[-1]:
        flash("Invalid email address.", "error")
        return redirect(url_for("admin_miembros"))
    otro = db.execute("SELECT id FROM users WHERE email = ? AND id != ?", (nuevo, user_id)).fetchone()
    if otro:
        flash(f"Email {nuevo} is already used by another account.", "error")
        return redirect(url_for("admin_miembros"))
    viejo = target["email"]
    db.execute("UPDATE users SET email = ? WHERE id = ?", (nuevo, user_id))
    db.commit()
    flash(f"Email changed: {viejo} → {nuevo}.", "ok")
    return redirect(url_for("admin_miembros"))


@app.route("/admin/test-welcome", methods=["POST"])
def admin_test_welcome():
    """ONE-TIME (prueba Resend 2026-10-01): envía el email de bienvenida REAL
    a un email dado, como si el miembro se acabara de suscribir. Protegido
    con X-Push-Key. Síncrono: reporta el resultado real del envío.
    Se elimina después de la prueba."""
    if not PUSH_TRIGGER_KEY or not secrets.compare_digest(
        request.headers.get("X-Push-Key", ""), PUSH_TRIGGER_KEY
    ):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip()
    nombre = (data.get("nombre") or "").strip() or "Alex"
    if "@" not in email:
        return jsonify({"ok": False, "detail": "email inválido"}), 400
    try:
        ok, detail = send_email_sync(
            email, WELCOME_SUBJECT,
            WELCOME_TEXT.replace("__NOMBRE__", nombre),
            WELCOME_HTML.replace("__NOMBRE__", nombre))
        return jsonify({"ok": ok, "detail": detail})
    except Exception as e:
        return jsonify({"ok": False,
                        "detail": f"falló: {type(e).__name__}: {e}"}), 500


@app.route("/admin/migrar-track", methods=["GET", "POST"])
@login_required
def admin_migrar_track():
    """Migra el historial de trackeo (jugadas + checkins + bankroll) de una
    cuenta a otra. Solo admin. No toca is_admin, platinum_unlocked ni datos de Stripe."""
    me = current_user()
    if not is_admin_for(me):
        flash("You don't have permission.", "error")
        return redirect(url_for("home"))
    db = get_db()
    if request.method == "POST":
        src = request.form.get("origen", type=int)
        dst = request.form.get("destino", type=int)
        if not src or not dst or src == dst:
            flash("Pick two different accounts.", "error")
            return redirect(url_for("admin_migrar_track"))
        srow = db.execute("SELECT * FROM users WHERE id = ?", (src,)).fetchone()
        drow = db.execute("SELECT * FROM users WHERE id = ?", (dst,)).fetchone()
        if not srow or not drow:
            flash("Account not found.", "error")
            return redirect(url_for("admin_migrar_track"))
        movidas, dups = 0, 0
        for r in db.execute("SELECT * FROM tracked_plays WHERE user_id = ?", (src,)).fetchall():
            ex = db.execute(
                "SELECT id, resultado FROM tracked_plays WHERE user_id = ? AND play_id = ?",
                (dst, r["play_id"])).fetchone()
            if ex:
                # conflicto: conservar la fila con resultado; si ninguna, la del destino
                if r["resultado"] and not ex["resultado"]:
                    db.execute("DELETE FROM tracked_plays WHERE id = ?", (ex["id"],))
                    db.execute("UPDATE tracked_plays SET user_id = ? WHERE id = ?", (dst, r["id"]))
                else:
                    db.execute("DELETE FROM tracked_plays WHERE id = ?", (r["id"],))
                dups += 1
            else:
                db.execute("UPDATE tracked_plays SET user_id = ? WHERE id = ?", (dst, r["id"]))
                movidas += 1
        for r in db.execute("SELECT fecha FROM checkins WHERE user_id = ?", (src,)).fetchall():
            ex = db.execute("SELECT 1 FROM checkins WHERE user_id = ? AND fecha = ?",
                            (dst, r["fecha"])).fetchone()
            if ex:
                db.execute("DELETE FROM checkins WHERE user_id = ? AND fecha = ?",
                           (src, r["fecha"]))
            else:
                db.execute("UPDATE checkins SET user_id = ? WHERE user_id = ? AND fecha = ?",
                           (dst, src, r["fecha"]))
        campos = []
        for col in ("bankroll", "stake_fijo_elite", "stake_fijo_gold", "stake_mode"):
            if srow[col] is not None:
                campos.append(col)
        if campos:
            sets = ", ".join(f"{c} = ?" for c in campos)
            db.execute(f"UPDATE users SET {sets} WHERE id = ?",
                       tuple(srow[c] for c in campos) + (dst,))
        db.commit()
        flash(f"Moved {movidas} tracked plays ({dups} duplicates merged) from "
              f"{srow['email']} to {drow['email']}.", "ok")
        return redirect(url_for("admin_migrar_track"))
    users = db.execute(
        "SELECT id, nombre, email, bankroll FROM users ORDER BY id").fetchall()
    resumen = []
    for u in users:
        tp = db.execute("SELECT COUNT(*) AS c FROM tracked_plays WHERE user_id = ?",
                        (u["id"],)).fetchone()["c"]
        ck = db.execute("SELECT COUNT(*) AS c FROM checkins WHERE user_id = ?",
                        (u["id"],)).fetchone()["c"]
        resumen.append({"id": u["id"], "nombre": u["nombre"], "email": u["email"],
                        "tracked": tp, "checkins": ck, "bankroll": u["bankroll"]})
    sugerido = next((u["id"] for u in resumen if "pena.manuel" in (u["email"] or "")), None)
    return render_template("admin_migrar_track.html", resumen=resumen, sugerido=sugerido)


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
        "stake_fijo_elite, stake_fijo_gold, cancel_requested_at, stripe_customer_id "
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
    try:
        eventos = db.execute(
            "SELECT tipo, email, detalle, created_at FROM stripe_events "
            "ORDER BY created_at DESC LIMIT 15"
        ).fetchall()
    except Exception:
        eventos = []
    return render_template("admin_miembros.html", miembros=lista,
                           eventos=[dict(e) for e in eventos])


@app.route("/resultados")
@login_required
def resultados():
    db = get_db()
    record_checkin(db, session["user_id"])
    # Jugadas pendientes de hoy (pedido Alex 2026-09-28): la card del día vive
    # en plays.json; el archivo solo tiene liquidadas. Se muestran marcadas
    # como pendientes, sin tocar las stats (que siguen siendo solo settled).
    pendientes = []
    fecha_hoy = ""
    if card_publicada_hoy():
        try:
            with open(PLAYS_PATH, "r", encoding="utf-8") as f:
                _dj = json.load(f)
            fecha_hoy = _dj.get("fecha", "") if isinstance(_dj, dict) else ""
        except (OSError, json.JSONDecodeError):
            pass
        pendientes = [
            p for p in load_plays()
            if p.get("pick") and not p.get("bloqueada")
            and p.get("resultado") not in ("WON", "LOST")
        ]
    return render_template("resultados.html", res=program_stats(), dias=load_archive(),
                           pendientes=pendientes, fecha_hoy=fecha_hoy)


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


# ----------------------------- Free public tools (no login) ----------------
@app.route("/tools/parlay-calculator")
def tools_parlay_calculator():
    """Calculadora de parlays 100% del lado del cliente. Pública, sin login."""
    return render_template("tools_parlay_calculator.html")


@app.route("/tools/line-moves")
def tools_line_moves():
    """Movimientos sharp recientes. Pública, sin login; datos de la rama data-board."""
    board = load_line_moves()
    return render_template("tools_line_moves.html",
                           moves=board.get("moves", []),
                           updated_at=board.get("updated_at", ""))


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
    image = data.get("image") or ""
    payload = json.dumps({"title": title, "body": body, "url": url, "image": image})
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


@app.route("/api/push-status")
def api_push_status():
    """Estado de suscripciones push. Protegido con X-Push-Key.

    Devuelve el total de suscripciones y si el admin (Alex) tiene al
    menos una suscripción activa, para verificar antes de cada envío
    que él también recibe las pushes de miembro.
    """
    if not PUSH_TRIGGER_KEY or not secrets.compare_digest(
        request.headers.get("X-Push-Key", ""), PUSH_TRIGGER_KEY
    ):
        return jsonify({"error": "forbidden"}), 403
    db = get_db()
    total = db.execute("SELECT COUNT(*) AS n FROM push_subscriptions").fetchone()
    total = total["n"] if isinstance(total, dict) else total[0]
    admin_subs = db.execute(
        "SELECT COUNT(*) AS n FROM push_subscriptions WHERE member_id IN"
        " (SELECT id FROM users WHERE is_admin = 1)"
    ).fetchone()
    admin_subs = admin_subs["n"] if isinstance(admin_subs, dict) else admin_subs[0]
    return jsonify(
        {"total": total, "admin_subscriptions": admin_subs, "admin_active": admin_subs > 0}
    )


@app.route("/telegram/webhook/<secret>", methods=["POST"])
def telegram_webhook(secret):
    """Recibe updates del bot @thesharpteam_bot (solo vinculación /start).

    Render no tiene el token del bot, así que la app solo RECIBE updates
    aquí (nunca llama a la API de Telegram). El baneo/desbaneo del canal
    Sharp Club lo hace el cron de la VM con el conector de Telegram,
    leyendo /api/telegram-pending.
    """
    if not TELEGRAM_WEBHOOK_SECRET or not secrets.compare_digest(
        secret, TELEGRAM_WEBHOOK_SECRET
    ):
        return jsonify({"error": "forbidden"}), 403
    update = request.get_json(force=True, silent=True) or {}
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    frm = msg.get("from") or {}
    tg_id = frm.get("id")
    if text.startswith("/start") and tg_id:
        parts = text.split(None, 1)
        code = parts[1].strip() if len(parts) > 1 else ""
        if code:
            db = get_db()
            row = db.execute(
                "SELECT user_id FROM telegram_link_codes WHERE code = ?", (code,)
            ).fetchone()
            if row:
                uid = row["user_id"] if isinstance(row, dict) else row[0]
                db.execute(
                    "UPDATE users SET telegram_user_id = ? WHERE id = ?",
                    (str(tg_id), uid),
                )
                db.execute(
                    "DELETE FROM telegram_link_codes WHERE code = ?", (code,)
                )
                db.commit()
    return jsonify({"ok": True})


@app.route("/api/telegram-pending", methods=["GET"])
def api_telegram_pending():
    """Baneos/desbaneos pendientes del canal Sharp Club.

    Lo lee el cron de la VM cada 10 min (Render no tiene el token del bot).
    Protegido con header X-Push-Key == PUSH_TRIGGER_KEY.
    """
    if not PUSH_TRIGGER_KEY or not secrets.compare_digest(
        request.headers.get("X-Push-Key", ""), PUSH_TRIGGER_KEY
    ):
        return jsonify({"error": "forbidden"}), 403
    db = get_db()
    rows = db.execute(
        "SELECT id, telegram_user_id, telegram_ban_pending, telegram_unban_pending"
        " FROM users WHERE telegram_user_id IS NOT NULL"
        " AND (telegram_ban_pending = 1 OR telegram_unban_pending = 1)"
    ).fetchall()
    pending = []
    for r in rows:
        d = dict(r) if not isinstance(r, dict) else r
        action = "ban" if d["telegram_ban_pending"] else "unban"
        pending.append(
            {
                "user_id": d["id"],
                "telegram_user_id": d["telegram_user_id"],
                "action": action,
            }
        )
    return jsonify({"pending": pending})


@app.route("/api/telegram-pending/ack", methods=["POST"])
def api_telegram_pending_ack():
    """Confirma que el cron procesó un baneo/desbaneo. JSON: {user_id, action}."""
    if not PUSH_TRIGGER_KEY or not secrets.compare_digest(
        request.headers.get("X-Push-Key", ""), PUSH_TRIGGER_KEY
    ):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(force=True, silent=True) or {}
    uid = data.get("user_id")
    action = data.get("action")
    if not uid or action not in ("ban", "unban"):
        return jsonify({"error": "bad request"}), 400
    col = "telegram_ban_pending" if action == "ban" else "telegram_unban_pending"
    db = get_db()
    db.execute(f"UPDATE users SET {col} = 0 WHERE id = ?", (uid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/new-members", methods=["GET"])
def api_new_members():
    """Miembros registrados. ?since=ISO (created_at) filtra desde esa marca.

    Lo lee el cron de la VM cada 10 min para avisar a Alex en el chat
    cada vez que alguien crea su cuenta. Protegido con
    header X-Push-Key == PUSH_TRIGGER_KEY.
    """
    if not PUSH_TRIGGER_KEY or not secrets.compare_digest(
        request.headers.get("X-Push-Key", ""), PUSH_TRIGGER_KEY
    ):
        return jsonify({"error": "forbidden"}), 403
    since = (request.args.get("since") or "").strip()
    db = get_db()
    if since:
        rows = db.execute(
            "SELECT id, nombre, email, created_at, platinum_unlocked FROM users"
            " WHERE created_at >= ? ORDER BY created_at ASC, id ASC",
            (since,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, nombre, email, created_at, platinum_unlocked FROM users"
            " ORDER BY created_at DESC, id DESC LIMIT 20"
        ).fetchall()
    members = []
    for r in rows:
        d = dict(r) if not isinstance(r, dict) else r
        members.append(
            {
                "id": d["id"],
                "nombre": d.get("nombre"),
                "email": d.get("email"),
                "created_at": d.get("created_at"),
                "elite": bool(d.get("platinum_unlocked")),
            }
        )
    return jsonify({"members": members})


@app.route("/api/telegram-invite-pool/add", methods=["POST"])
def api_telegram_invite_pool_add():
    """El cron de la VM deposita links de invitación a Sharp Club.

    JSON: {links: ["https://t.me/+..."]}. Protegido con X-Push-Key.
    """
    if not PUSH_TRIGGER_KEY or not secrets.compare_digest(
        request.headers.get("X-Push-Key", ""), PUSH_TRIGGER_KEY
    ):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(force=True, silent=True) or {}
    links = data.get("links") or []
    db = get_db()
    added = 0
    for link in links:
        if not link:
            continue
        try:
            db.execute(
                "INSERT INTO telegram_invite_links (invite_link, created_at)"
                " VALUES (?, ?)",
                (link, now_iso()),
            )
            added += 1
        except INTEGRITY_ERRORS:
            pass
    db.commit()
    return jsonify({"ok": True, "added": added})


@app.route("/api/telegram-invite-pool/status", methods=["GET"])
def api_telegram_invite_pool_status():
    """Cuántos links sin usar hay en el pool. Lo lee el cron de la VM."""
    if not PUSH_TRIGGER_KEY or not secrets.compare_digest(
        request.headers.get("X-Push-Key", ""), PUSH_TRIGGER_KEY
    ):
        return jsonify({"error": "forbidden"}), 403
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) AS n FROM telegram_invite_links WHERE used = 0"
    ).fetchone()
    n = row["n"] if row else 0
    return jsonify({"unused": n})


@app.route("/api/telegram-invite", methods=["GET"])
def api_telegram_invite():
    """Entrega al miembro Elite un link personal de invitación a Sharp Club.

    Un solo uso por link (member_limit=1 en Telegram). Requiere sesión y
    Elite activa. Si el pool está vacío, el cron lo rellena en ~10 min.
    """
    user = current_user()
    if user is None:
        return jsonify({"error": "login_required"}), 401
    if not platinum_unlocked_for(user):
        return jsonify({"error": "elite_required"}), 403
    db = get_db()
    row = db.execute(
        "SELECT id, invite_link FROM telegram_invite_links"
        " WHERE used = 0 ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return jsonify({"error": "empty_pool", "retry_in": "10 min"}), 503
    d = dict(row) if not isinstance(row, dict) else row
    db.execute(
        "UPDATE telegram_invite_links SET used = 1, used_by_user_id = ?"
        " WHERE id = ?",
        (user["id"], d["id"]),
    )
    db.commit()
    return jsonify({"ok": True, "invite_link": d["invite_link"]})


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Webhook de Stripe: automatiza la membresía sin que Alex mueva un dedo.

    Eventos que procesa (configurar en el Dashboard de Stripe):
      - checkout.session.completed   -> activa Elite al miembro (match por email)
      - invoice.paid                 -> activa Elite (pago inicial o renovación semanal)
      - customer.subscription.updated -> actualiza estado / detecta cancelación al fin del período
      - customer.subscription.deleted -> desactiva Elite
      - invoice.payment_failed        -> dunning al miembro (email "Fix my card", 1x por factura)

    Seguridad: verifica la firma con STRIPE_WEBHOOK_SECRET. Deduplica por
    event_id (Stripe reintenta eventos). Nunca devuelve 500: ante cualquier
    error interno responde 200 tras registrar, para no provocar reintentos
    infinitos; el detalle queda en stripe_events.
    """
    if not stripe_configurado():
        return jsonify({"error": "stripe webhook not configured"}), 400
    import stripe
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return jsonify({"error": "invalid signature"}), 400

    db = get_db()
    eid = event.get("id", "")
    etype = event.get("type", "")
    data = (event.get("data") or {}).get("object") or {}

    # La activación es idempotente (UPDATE a platinum_unlocked=1): reprocesar
    # un evento de activación es seguro aunque Stripe lo reenvíe.
    es_activacion = etype in ("checkout.session.completed", "invoice.paid")
    try:
        db.execute(
            "INSERT INTO stripe_events (event_id, tipo, created_at) VALUES (?, ?, ?)",
            (eid, etype, now_iso()),
        )
        db.commit()
        duplicado = False
    except INTEGRITY_ERRORS:
        duplicado = True
        if not es_activacion:
            return jsonify({"ok": True, "deduped": True}), 200

    def _auditar(email="", detalle=""):
        try:
            db.execute(
                "UPDATE stripe_events SET email = ?, detalle = ? WHERE event_id = ?",
                (email or None, detalle or None, eid),
            )
            db.commit()
        except Exception:
            pass

    def _activar_elite(email, customer_id, sub_id, origen):
        """Activa Elite al miembro con ese email. Idempotente."""
        email = (email or "").strip().lower()
        if not email:
            _auditar("", f"{origen} sin email")
            return
        row = db.execute("SELECT id, nombre, platinum_unlocked, cancel_requested_at, telegram_user_id FROM users WHERE email = ?",
                         (email,)).fetchone()
        if row:
            uid = row["id"]
            era_nuevo = not bool(row["platinum_unlocked"])
            estaba_cancelado = bool(row["cancel_requested_at"])
            tiene_tg = bool(row["telegram_user_id"])
            db.execute(
                "UPDATE users SET platinum_unlocked = 1, stripe_customer_id = ?, "
                "stripe_subscription_id = ?, cancel_requested_at = NULL WHERE id = ?",
                (customer_id or None, sub_id or None, uid),
            )
            # Si vuelve después de cancelar y tiene Telegram vinculado, el cron
            # lo desbanea del canal Sharp Club para que pueda reingresar.
            if estaba_cancelado and tiene_tg:
                db.execute(
                    "UPDATE users SET telegram_unban_pending = 1, telegram_ban_pending = 0"
                    " WHERE id = ?", (uid,),
                )
            db.commit()
            _auditar(email, f"Elite activada ({origen}, sub {(sub_id or 'n/a')[:20]})"
                     + (" [reproceso]" if duplicado else ""))
            # Pedido Alex 2026-09-30: al pagar le llega el email bonito de
            # bienvenida (WELCOME_HTML: todo lo incluido en la membresía +
            # botones directos al Dashboard). Solo en activaciones nuevas.
            if era_nuevo:
                try:
                    send_welcome_email(row["nombre"] or "", email)
                except Exception:
                    pass
        else:
            _auditar(email, "pago sin cuenta registrada: activar manual")

    try:
        if etype == "checkout.session.completed":
            # Pago completado: activar Elite al miembro con ese email.
            _activar_elite(
                ((data.get("customer_details") or {}).get("email") or ""),
                data.get("customer") or "",
                data.get("subscription") or "",
                "checkout",
            )
        elif etype == "invoice.paid":
            # Factura de suscripción pagada (inicial o renovación): activar Elite.
            _activar_elite(
                data.get("customer_email") or "",
                data.get("customer") or "",
                data.get("subscription") or "",
                "invoice.paid",
            )
        elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
            # Cancelación o cambio de estado: localizar por suscripción o cliente.
            sub_id = data.get("id") or ""
            customer_id = data.get("customer") or ""
            status = data.get("status") or ""
            cancel_at_end = bool(data.get("cancel_at_period_end"))
            row = None
            if sub_id:
                row = db.execute("SELECT id, email FROM users WHERE stripe_subscription_id = ?",
                                 (sub_id,)).fetchone()
            if row is None and customer_id:
                row = db.execute("SELECT id, email FROM users WHERE stripe_customer_id = ?",
                                 (customer_id,)).fetchone()
            if row:
                email = row["email"]
                if etype == "customer.subscription.deleted" or status in (
                        "canceled", "unpaid", "incomplete_expired"):
                    db.execute(
                        "UPDATE users SET platinum_unlocked = 0, stripe_subscription_id = NULL, "
                        "cancel_requested_at = ? WHERE id = ?", (now_iso(), row["id"]))
                    # Sin pago no hay acceso: si tiene Telegram vinculado, el cron
                    # de la VM lo banea del canal privado Sharp Club.
                    db.execute(
                        "UPDATE users SET telegram_ban_pending = 1, telegram_unban_pending = 0"
                        " WHERE id = ? AND telegram_user_id IS NOT NULL", (row["id"],))
                    db.commit()
                    _auditar(email, f"Elite desactivada ({etype}, status={status})")
                else:
                    db.execute(
                        "UPDATE users SET stripe_subscription_id = ? WHERE id = ?",
                        (sub_id or None, row["id"]))
                    db.commit()
                    nota = "cancelación al fin del período" if cancel_at_end else f"status={status}"
                    _auditar(email, f"suscripción actualizada: {nota}")
            else:
                _auditar("", f"{etype} sin miembro vinculado (sub {sub_id[:20] if sub_id else 'n/a'})")
        elif etype == "invoice.payment_failed":
            # Cobro fallido: dunning estilo WGT al miembro (una vez por factura,
            # porque Stripe reintenta y manda payment_failed en cada intento).
            email = (data.get("customer_email") or "").strip().lower()
            invoice_id = data.get("id") or ""
            pay_url = (data.get("hosted_invoice_url") or
                       "https://the-line-breaker-members.onrender.com/cuenta")
            nombre = ""
            if email:
                r = db.execute("SELECT nombre FROM users WHERE email = ?",
                               (email,)).fetchone()
                if r:
                    nombre = r["nombre"] or ""
            ya_notificado = False
            if invoice_id:
                ya_notificado = bool(db.execute(
                    "SELECT 1 FROM stripe_events WHERE tipo = 'invoice.payment_failed'"
                    " AND detalle LIKE ? LIMIT 1",
                    (f"%{invoice_id}%",)).fetchone())
            if ya_notificado:
                _auditar(email, f"pago fallido invoice {invoice_id}: ya notificado antes")
            else:
                ok = send_payment_failed_email(nombre, email, pay_url)
                _auditar(email, f"pago fallido invoice {invoice_id}: "
                                + ("dunning enviado" if ok
                                   else "dunning NO enviado (falta EMAIL_USER/EMAIL_PASS en Render)"))
        else:
            _auditar("", f"evento no procesado: {etype}")
    except Exception as exc:  # nunca 500: Stripe reintentaría sin parar
        _auditar("", f"error interno: {type(exc).__name__}")
    return jsonify({"ok": True}), 200


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
